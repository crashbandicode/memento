"""Direct-database token usage queries for the MCP server.

Remote MCP installations use the authenticated HTTP API.  These helpers keep
the explicitly supported direct-PostgreSQL mode on the same JSON contract
without creating a second poller or materialization process.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select

from .db import (
    ConversationMessage,
    ConversationUsageEvent,
    Document,
    DocumentDeliveryState,
)

_COUNT_FIELDS = (
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
_STARTED_AT_METADATA_KEY = "_assistant_started_at"
_LAST_ACTIVITY_AT_METADATA_KEY = "_assistant_last_activity_at"


def parse_cycle_timestamp(value: str, field_name: str) -> datetime:
    candidate = str(value or "").strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def normalize_tool(tool: str) -> str | None:
    value = str(tool or "all").strip().lower()
    if value == "all":
        return None
    if value not in _TOOL_IDS:
        raise ValueError("tool must be claude, codex, cursor, or all")
    return _TOOL_IDS[value]


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    usage = {
        key: _count(value.get(key))
        for key in _COUNT_FIELDS
        if _count(value.get(key)) > 0
    }
    if not usage.get("input_tokens") and not usage.get("output_tokens"):
        return {}
    if not usage.get("total_tokens"):
        usage["total_tokens"] = (
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        )
    for public_name, stored_name in (
        ("input_uncached", "uncached_input_tokens"),
        ("cache_read_tokens", "cached_input_tokens"),
        ("cache_write_tokens", "cache_write_input_tokens"),
    ):
        if usage.get(stored_name):
            usage[public_name] = usage[stored_name]
    return usage


def _add_usage(current: object, addition: object) -> dict[str, int]:
    left = normalize_usage(current)
    right = normalize_usage(addition)
    return normalize_usage(
        {
            key: left.get(key, 0) + right.get(key, 0)
            for key in _COUNT_FIELDS
        }
    )


def _summed_columns():
    return [
        func.coalesce(func.sum(getattr(ConversationUsageEvent, key)), 0).label(key)
        for key in _COUNT_FIELDS
    ]


def _usage_from_row(row) -> dict[str, int]:
    return normalize_usage(
        {key: int(getattr(row, key, 0) or 0) for key in _COUNT_FIELDS}
    )


def _native_id(metadata: object) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = (
        metadata.get("root_session_id")
        or metadata.get("session_id")
        or metadata.get("thread_id")
        or metadata.get("cascade_id")
    )
    return str(value) if value else None


async def direct_conversation_details(
    db,
    document_id: uuid.UUID,
    *,
    metadata: object = None,
    fallback_last: datetime | None = None,
) -> dict:
    """Return lifetime bounds and per-model usage for one conversation."""
    delivery_row = (
        await db.execute(
            select(
                DocumentDeliveryState.activity_at,
                DocumentDeliveryState.delivery_metadata,
            ).where(
                DocumentDeliveryState.document_id == document_id
            )
        )
    ).one_or_none()
    delivery_last = delivery_row.activity_at if delivery_row is not None else None
    effective_metadata = (
        delivery_row.delivery_metadata
        if delivery_row is not None
        and isinstance(delivery_row.delivery_metadata, dict)
        else metadata
    )
    model_rows = (
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
    models = []
    for row in model_rows:
        usage = _usage_from_row(row)
        if usage:
            models.append(
                {
                    "model": row.model,
                    "reasoning_effort": row.reasoning_effort,
                    "token_usage": usage,
                }
            )
    return {
        "started_at": (
            str((effective_metadata or {}).get(_STARTED_AT_METADATA_KEY) or "")
            if isinstance(effective_metadata, dict)
            else None
        ) or None,
        "last_activity_at": (
            str((effective_metadata or {}).get(_LAST_ACTIVITY_AT_METADATA_KEY) or "")
            if isinstance(effective_metadata, dict)
            else ""
        )
        or (
            max(
                value for value in (fallback_last, delivery_last) if value is not None
            ).isoformat()
            if fallback_last is not None or delivery_last is not None
            else None
        ),
        "models": models,
    }


async def aggregate_usage_cycle(
    db,
    *,
    since: datetime,
    until: datetime,
    tool: str,
    include_threads: bool,
) -> dict[str, object]:
    """Direct-mode equivalent of ``GET /api/conversations/usage-cycle``."""
    tool_id = normalize_tool(tool)
    document_filters = [Document.category == "conversation"]
    event_filters = [
        ConversationUsageEvent.occurred_at >= since,
        ConversationUsageEvent.occurred_at < until,
    ]
    if tool_id:
        document_filters.append(Document.tool_id == tool_id)
        event_filters.append(ConversationUsageEvent.tool_id == tool_id)

    active_rows = (
        await db.execute(
            select(Document.id, Document.tool_id)
            .join(ConversationMessage, ConversationMessage.document_id == Document.id)
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
        row.document_id for row in event_rows if row.attribution_status == "missing_model"
    }
    counter_reset_docs = {
        row.document_id for row in event_rows if row.attribution_status == "counter_reset"
    }
    missing_timestamp_query = select(ConversationUsageEvent.document_id).where(
        ConversationUsageEvent.occurred_at.is_(None),
        ConversationUsageEvent.attribution_status == "missing_timestamp",
    )
    if tool_id:
        missing_timestamp_query = missing_timestamp_query.where(
            ConversationUsageEvent.tool_id == tool_id
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
    models: list[dict[str, object]] = []
    total_usage: dict[str, int] = {}
    for row in model_rows:
        usage = _usage_from_row(row)
        if not usage:
            continue
        models.append(
            {
                "model": row.model,
                "reasoning_effort": row.reasoning_effort,
                "conversation_count": int(row.conversation_count or 0),
                "token_usage": usage,
            }
        )
        total_usage = _add_usage(total_usage, usage)

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
    if not all_conversation_ids:
        result["threads"] = []
        return result

    document_rows = (
        await db.execute(
            select(
                Document.id,
                Document.tool_id,
                Document.title,
                Document.relative_path,
                func.coalesce(
                    DocumentDeliveryState.delivery_metadata,
                    Document.metadata_,
                ).label("metadata"),
                Document.activity_at,
                DocumentDeliveryState.activity_at.label("delivery_activity_at"),
            )
            .select_from(Document)
            .outerjoin(
                DocumentDeliveryState,
                DocumentDeliveryState.document_id == Document.id,
            )
            .where(Document.id.in_(all_conversation_ids))
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
                ConversationUsageEvent.document_id.in_(all_conversation_ids),
            )
            .group_by(
                ConversationUsageEvent.document_id,
                ConversationUsageEvent.model,
                ConversationUsageEvent.reasoning_effort,
            )
        )
    ).all()
    models_by_document: dict[uuid.UUID, list[dict[str, object]]] = defaultdict(list)
    usage_by_document: dict[uuid.UUID, dict[str, int]] = {}
    for row in thread_model_rows:
        usage = _usage_from_row(row)
        models_by_document[row.document_id].append(
            {
                "model": row.model,
                "reasoning_effort": row.reasoning_effort,
                "token_usage": usage,
            }
        )
        usage_by_document[row.document_id] = _add_usage(
            usage_by_document.get(row.document_id), usage
        )

    threads = []
    for row in document_rows:
        metadata = row.metadata or {}
        persisted_last_activity = str(
            metadata.get(_LAST_ACTIVITY_AT_METADATA_KEY) or ""
        )
        last_values = [
            value
            for value in (
                row.delivery_activity_at,
                row.activity_at,
            )
            if value is not None
        ]
        skipped_reasons = [
            reason
            for reason, ids in (
                ("cursor_null", cursor_null_docs),
                ("missing_timestamp", missing_timestamp_docs),
                ("missing_model", missing_model_docs),
                ("counter_reset", counter_reset_docs),
                ("missing_usage", missing_usage_docs),
            )
            if row.id in ids
        ]
        threads.append(
            {
                "document_id": str(row.id),
                "native_id": _native_id(metadata),
                "tool": "claude" if row.tool_id == "claude_code" else row.tool_id,
                "title": row.title or row.relative_path,
                "started_at": (
                    str(
                        metadata.get(_STARTED_AT_METADATA_KEY)
                        or ""
                    )
                    or None
                ),
                "last_activity_at": (
                    persisted_last_activity
                    or (max(last_values).isoformat() if last_values else None)
                ),
                "token_usage": usage_by_document.get(row.id) or None,
                "models": models_by_document.get(row.id, []),
                "skipped_reasons": skipped_reasons,
            }
        )
    threads.sort(key=lambda item: item["last_activity_at"] or "", reverse=True)
    result["threads"] = threads
    return result
