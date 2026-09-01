"""Indexed conversation-usage aggregation for API and MCP consumers."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ConversationMessage,
    ConversationUsageEvent,
    Document,
    DocumentDeliveryState,
)
from .conversation_hierarchy import (
    conversation_display_title,
    is_conversation_subagent,
)
from .conversation_identity import conversation_native_id
from .conversation_usage import (
    LAST_ACTIVITY_AT_METADATA_KEY,
    STARTED_AT_METADATA_KEY,
    add_token_usage,
    normalize_token_usage,
)
from .document_delivery import delivery_metadata_expression

_USAGE_COLUMNS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

_TOOL_IDS = {
    "claude": "claude_code",
    "claude_code": "claude_code",
    "codex": "codex",
    "cursor": "cursor",
}


def normalize_usage_tool(tool: str) -> str | None:
    value = str(tool or "all").strip().lower()
    if value == "all":
        return None
    if value not in _TOOL_IDS:
        raise ValueError("tool must be claude, codex, cursor, or all")
    return _TOOL_IDS[value]


def _summed_columns():
    return [
        func.coalesce(func.sum(getattr(ConversationUsageEvent, key)), 0).label(key)
        for key in _USAGE_COLUMNS
    ]


def _usage_from_row(row) -> dict[str, object]:
    return normalize_token_usage(
        {key: int(getattr(row, key, 0) or 0) for key in _USAGE_COLUMNS}
    )


def _metadata_text(
    metadata: dict[str, object],
    key: str,
    *,
    max_length: int = 512,
) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_length] or None


async def conversation_usage_models(
    db: AsyncSession,
    document_id: uuid.UUID,
) -> list[dict[str, object]]:
    """Return lifetime usage grouped by exact model/effort selection."""
    rows = (
        await db.execute(
            select(
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
                *_summed_columns(),
            )
            .where(
                ConversationUsageEvent.document_id == document_id,
                ConversationUsageEvent.attribution_status == "attributed",
            )
            .group_by(
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
            )
            .order_by(
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
            )
        )
    ).all()
    return [
        {
            "model": row.model,
            "reasoning_effort": row.reasoning_effort,
            "token_usage": _usage_from_row(row),
        }
        for row in rows
        if _usage_from_row(row)
    ]


async def aggregate_usage_cycle(
    db: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tool: str = "all",
    machine_ids: Collection[uuid.UUID] | None = None,
    include_threads: bool = False,
) -> dict[str, object]:
    """Aggregate exact timestamped usage in the half-open ``[since, until)`` range."""
    tool_id = normalize_usage_tool(tool)
    document_filters = [Document.category == "conversation"]
    event_filters = [
        ConversationUsageEvent.occurred_at >= since,
        ConversationUsageEvent.occurred_at < until,
    ]
    if tool_id:
        document_filters.append(Document.tool_id == tool_id)
        event_filters.append(ConversationUsageEvent.tool_id == tool_id)
    if machine_ids is not None:
        mids = list(machine_ids)
        document_filters.append(Document.machine_id.in_(mids))
        event_filters.append(ConversationUsageEvent.machine_id.in_(mids))

    active_rows = (
        await db.execute(
            select(Document.id, Document.tool_id)
            .join(
                ConversationMessage,
                ConversationMessage.document_id == Document.id,
            )
            .where(
                *document_filters,
                ConversationMessage.timestamp >= since,
                ConversationMessage.timestamp < until,
            )
            .distinct()
        )
    ).all()
    active_tools = {row.id: row.tool_id for row in active_rows}

    event_rows = (
        await db.execute(
            select(
                ConversationUsageEvent.document_id,
                ConversationUsageEvent.attribution_status,
            )
            .where(*event_filters)
            .distinct()
        )
    ).all()
    event_document_ids = {row.document_id for row in event_rows}
    attributed_docs = {
        row.document_id for row in event_rows if row.attribution_status == "attributed"
    }
    missing_model_docs = {
        row.document_id
        for row in event_rows
        if row.attribution_status == "missing_model"
    }
    counter_reset_docs = {
        row.document_id
        for row in event_rows
        if row.attribution_status == "counter_reset"
    }

    missing_timestamp_query = select(ConversationUsageEvent.document_id).where(
        ConversationUsageEvent.occurred_at.is_(None),
        ConversationUsageEvent.attribution_status == "missing_timestamp",
    )
    if tool_id:
        missing_timestamp_query = missing_timestamp_query.where(
            ConversationUsageEvent.tool_id == tool_id
        )
    if machine_ids is not None:
        missing_timestamp_query = missing_timestamp_query.where(
            ConversationUsageEvent.machine_id.in_(list(machine_ids))
        )
    missing_timestamp_docs = set(
        (await db.execute(missing_timestamp_query.distinct())).scalars().all()
    ) & set(active_tools)

    cursor_null_docs = {
        document_id
        for document_id, active_tool in active_tools.items()
        if active_tool == "cursor"
    }
    event_docs = attributed_docs | missing_model_docs | counter_reset_docs
    missing_usage_docs = {
        document_id
        for document_id, active_tool in active_tools.items()
        if active_tool != "cursor"
        and document_id not in event_docs
        and document_id not in missing_timestamp_docs
    }

    model_rows = (
        await db.execute(
            select(
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
                func.count(func.distinct(ConversationUsageEvent.document_id)).label(
                    "conversation_count"
                ),
                *_summed_columns(),
            )
            .where(
                *event_filters,
                ConversationUsageEvent.attribution_status == "attributed",
            )
            .group_by(
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
            )
            .order_by(
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
            )
        )
    ).all()
    models = [
        {
            "model": row.model,
            "reasoning_effort": row.reasoning_effort,
            "conversation_count": int(row.conversation_count or 0),
            "token_usage": _usage_from_row(row),
        }
        for row in model_rows
        if _usage_from_row(row)
    ]
    total_usage: dict[str, object] = {}
    for model in models:
        total_usage = add_token_usage(total_usage, model["token_usage"])

    all_conversation_ids = set(active_tools) | event_document_ids
    unattributed_ids = (
        cursor_null_docs
        | missing_timestamp_docs
        | missing_model_docs
        | counter_reset_docs
        | missing_usage_docs
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "tool": "claude" if tool_id == "claude_code" else (tool_id or "all"),
        "conversation_count": len(all_conversation_ids),
        "attributed_conversation_count": len(attributed_docs),
        "token_usage": total_usage or None,
        "models": models,
        "unattributed": {
            "conversation_count": len(unattributed_ids),
            "cursor_null": len(cursor_null_docs),
            "missing_timestamps": len(missing_timestamp_docs),
            "missing_model": len(missing_model_docs),
            "counter_reset": len(counter_reset_docs),
            "missing_usage": len(missing_usage_docs),
        },
    }
    if not include_threads:
        return result

    thread_ids = all_conversation_ids
    if not thread_ids:
        result["threads"] = []
        return result

    document_rows = (
        await db.execute(
            select(
                Document.id,
                Document.tool_id,
                Document.title,
                Document.relative_path,
                delivery_metadata_expression(joined=True).label("metadata"),
                Document.activity_at,
                DocumentDeliveryState.activity_at.label("delivery_activity_at"),
            )
            .select_from(Document)
            .outerjoin(
                DocumentDeliveryState,
                DocumentDeliveryState.document_id == Document.id,
            )
            .where(Document.id.in_(thread_ids))
        )
    ).all()

    thread_model_rows = (
        await db.execute(
            select(
                ConversationUsageEvent.document_id,
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
                *_summed_columns(),
            )
            .where(
                *event_filters,
                ConversationUsageEvent.attribution_status == "attributed",
                ConversationUsageEvent.document_id.in_(thread_ids),
            )
            .group_by(
                ConversationUsageEvent.document_id,
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
            )
        )
    ).all()
    models_by_document: dict[uuid.UUID, list[dict[str, object]]] = defaultdict(list)
    usage_by_document: dict[uuid.UUID, dict[str, object]] = {}
    for row in thread_model_rows:
        usage = _usage_from_row(row)
        models_by_document[row.document_id].append(
            {
                "model": row.model,
                "reasoning_effort": row.reasoning_effort,
                "token_usage": usage,
            }
        )
        usage_by_document[row.document_id] = add_token_usage(
            usage_by_document.get(row.document_id),
            usage,
        )

    threads = []
    for row in document_rows:
        metadata = row.metadata or {}
        persisted_last_activity = str(metadata.get(LAST_ACTIVITY_AT_METADATA_KEY) or "")
        fallback_last_candidates = (row.delivery_activity_at, row.activity_at)
        fallback_last_activity = max(
            (value for value in fallback_last_candidates if value is not None),
            default=None,
        )
        skipped_reasons = []
        for reason, ids in (
            ("cursor_null", cursor_null_docs),
            ("missing_timestamp", missing_timestamp_docs),
            ("missing_model", missing_model_docs),
            ("counter_reset", counter_reset_docs),
            ("missing_usage", missing_usage_docs),
        ):
            if row.id in ids:
                skipped_reasons.append(reason)
        threads.append(
            {
                "document_id": str(row.id),
                "native_id": conversation_native_id(
                    row.tool_id,
                    "conversation",
                    metadata,
                ),
                "tool": "claude" if row.tool_id == "claude_code" else row.tool_id,
                "title": conversation_display_title(
                    row.tool_id,
                    row.relative_path,
                    metadata,
                    row.title,
                ),
                "orchestration": _metadata_text(
                    metadata,
                    "orchestration",
                    max_length=64,
                ),
                "orchestration_parent_document_id": _metadata_text(
                    metadata,
                    "orchestration_parent_document_id",
                ),
                "is_subagent": is_conversation_subagent(
                    row.tool_id,
                    row.relative_path,
                    metadata,
                ),
                "thread_source": _metadata_text(
                    metadata,
                    "thread_source",
                    max_length=64,
                ),
                "parent_thread_id": _metadata_text(
                    metadata,
                    "parent_thread_id",
                ),
                "started_at": (
                    str(metadata.get(STARTED_AT_METADATA_KEY) or "") or None
                ),
                "last_activity_at": (
                    persisted_last_activity
                    or (
                        fallback_last_activity.isoformat()
                        if fallback_last_activity
                        else None
                    )
                ),
                "token_usage": usage_by_document.get(row.id) or None,
                "models": models_by_document.get(row.id, []),
                "skipped_reasons": skipped_reasons,
            }
        )
    threads.sort(key=lambda item: item["last_activity_at"] or "", reverse=True)
    result["threads"] = threads
    return result
