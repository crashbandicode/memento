"""Ingest-owned, per-document projection for dashboard refreshes."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..db.models import (
    ConversationReadModel,
    DashboardDocumentProjection,
    DashboardProjectionState,
    Document,
)
from .conversation_hierarchy import (
    conversation_root_thread_id,
    current_thread_id,
    is_conversation_subagent,
)
from .document_delivery import document_delivery_state, document_metadata
from .subagent_lifecycle import (
    SUBAGENT_LIFECYCLE_AT_KEY,
    SUBAGENT_LIFECYCLE_EVIDENCE_KEY,
    SUBAGENT_LIFECYCLE_SOURCE_KEY,
    SUBAGENT_LIFECYCLE_STATUS_KEY,
)

DASHBOARD_PROJECTION_VERSION = 1
DASHBOARD_PROJECTION_STATE_ID = 1
DASHBOARD_BACKFILL_BATCH_SIZE = 250

_HIERARCHY_METADATA_KEYS = {
    "agent_depth",
    "agent_id",
    "agent_launch_description",
    "agent_nickname",
    "agent_path",
    "agent_tool_use_id",
    "is_subagent",
    "parent_thread_id",
    "root_session_id",
    "session_id",
    "thread_id",
    "thread_source",
}


def _bounded(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _hierarchy_metadata(
    document: Document,
    read_model: ConversationReadModel | None,
) -> dict:
    metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
    result = {
        key: value
        for key, value in metadata.items()
        if key in _HIERARCHY_METADATA_KEYS
    }
    if read_model is not None:
        if read_model.thread_id:
            result["session_id"] = read_model.thread_id
            result["thread_id"] = read_model.thread_id
        if read_model.root_thread_id:
            result["root_session_id"] = read_model.root_thread_id
        if read_model.parent_thread_id:
            result["parent_thread_id"] = read_model.parent_thread_id
        if read_model.agent_id:
            result["agent_id"] = read_model.agent_id
        if read_model.agent_tool_use_id:
            result["agent_tool_use_id"] = read_model.agent_tool_use_id
        result["agent_depth"] = int(read_model.agent_depth or 0)
        result["is_subagent"] = bool(read_model.is_subagent)
        if read_model.is_subagent and document.tool_id == "codex":
            result["thread_source"] = "subagent"
        if read_model.runtime:
            result.update({
                "model": read_model.runtime.get("model"),
                "model_family": read_model.runtime.get("model_family"),
                "reasoning_effort": read_model.runtime.get("reasoning_effort"),
            })
        if read_model.lifecycle:
            lifecycle = read_model.lifecycle
            result[SUBAGENT_LIFECYCLE_STATUS_KEY] = lifecycle.get("status")
            result[SUBAGENT_LIFECYCLE_SOURCE_KEY] = lifecycle.get("source")
            result[SUBAGENT_LIFECYCLE_AT_KEY] = lifecycle.get("timestamp")
            result[SUBAGENT_LIFECYCLE_EVIDENCE_KEY] = lifecycle.get("evidence")
    return {
        key: value
        for key, value in result.items()
        if value is not None
    }


def dashboard_projection_values(
    document: Document,
    read_model: ConversationReadModel | None,
) -> dict:
    """Return the deterministic replacement value for one document."""
    metadata = document_metadata(document)
    delivery = document_delivery_state(document)
    is_conversation = document.category == "conversation"
    thread_id = (
        read_model.thread_id
        if read_model is not None
        else current_thread_id(metadata)
    ) if is_conversation else current_thread_id(metadata)
    root_thread_id = (
        read_model.root_thread_id
        if read_model is not None
        else conversation_root_thread_id(
            document.tool_id,
            document.relative_path,
            metadata,
        )
    ) if is_conversation else thread_id
    is_subagent = (
        bool(read_model.is_subagent)
        if read_model is not None
        else is_conversation_subagent(
            document.tool_id,
            document.relative_path,
            metadata,
        )
    ) if is_conversation else False
    pending_count = _non_negative_int(metadata.get("pending_question_count"))
    if (
        is_conversation
        and "pending_question_count" not in metadata
        and read_model is not None
    ):
        pending_count = len(read_model.pending_interactions or [])

    return {
        "machine_id": document.machine_id,
        "project_id": document.project_id,
        "tool_id": document.tool_id,
        "category": document.category,
        "visibility": document.visibility or "private",
        "title": document.title,
        "relative_path": document.relative_path,
        "file_size_bytes": int(
            (
                delivery.file_size_bytes
                if delivery is not None
                else document.file_size_bytes
            )
            or 0
        ),
        "synced_at": (
            delivery.synced_at if delivery is not None else document.synced_at
        ),
        "source_modified_at": (
            delivery.source_modified_at
            if delivery is not None
            else document.source_modified_at
        ),
        "activity_at": (
            delivery.activity_at if delivery is not None else document.activity_at
        ),
        "session_id": _bounded(thread_id, 512) or None,
        "root_thread_id": _bounded(root_thread_id, 512) or None,
        "parent_thread_id": (
            _bounded(
                read_model.parent_thread_id
                if read_model is not None
                else metadata.get("parent_thread_id"),
                512,
            )
            or None
        ),
        "is_subagent": is_subagent,
        "hierarchy_metadata": _hierarchy_metadata(document, read_model),
        "message_count": (
            int(read_model.message_count or 0) if read_model is not None else 0
        ),
        "user_message_count": (
            int(read_model.user_message_count or 0)
            if read_model is not None
            else 0
        ),
        "assistant_message_count": (
            int(read_model.assistant_message_count or 0)
            if read_model is not None
            else 0
        ),
        "human_character_count": (
            int(read_model.human_character_count or 0)
            if read_model is not None
            else 0
        ),
        "pending_question_count": pending_count,
        "agent_mode": _bounded(metadata.get("_assistant_agent_mode"), 64),
        "projection_version": DASHBOARD_PROJECTION_VERSION,
    }


async def refresh_dashboard_document_projection(
    db: AsyncSession,
    document: Document,
) -> tuple[DashboardDocumentProjection, bool]:
    """Idempotently replace the projection row for one current document."""
    read_model = (
        await db.get(ConversationReadModel, document.id)
        if document.category == "conversation"
        else None
    )
    values = dashboard_projection_values(document, read_model)
    projection = await db.get(DashboardDocumentProjection, document.id)
    changed = projection is None or any(
        getattr(projection, key) != value
        for key, value in values.items()
    )
    if projection is None:
        projection = DashboardDocumentProjection(
            document_id=document.id,
            **values,
        )
        db.add(projection)
    elif changed:
        for key, value in values.items():
            setattr(projection, key, value)
    return projection, changed


async def dashboard_projection_backfill_complete(db: AsyncSession) -> bool:
    state = await db.get(DashboardProjectionState, DASHBOARD_PROJECTION_STATE_ID)
    return bool(
        state is not None
        and state.backfill_complete
        and state.projection_version == DASHBOARD_PROJECTION_VERSION
    )


async def _set_backfill_complete(db: AsyncSession) -> None:
    state = await db.get(DashboardProjectionState, DASHBOARD_PROJECTION_STATE_ID)
    if state is None:
        db.add(DashboardProjectionState(
            id=DASHBOARD_PROJECTION_STATE_ID,
            projection_version=DASHBOARD_PROJECTION_VERSION,
            backfill_complete=True,
        ))
    else:
        state.projection_version = DASHBOARD_PROJECTION_VERSION
        state.backfill_complete = True
    await db.flush()


def dashboard_backfill_documents_statement(
    *,
    document_ids: Iterable[object] = (),
    after_id: object | None = None,
):
    """Select one keyset batch without loading raw document bodies."""
    statement = (
        select(Document)
        .options(load_only(
            Document.id,
            Document.machine_id,
            Document.project_id,
            Document.tool_id,
            Document.relative_path,
            Document.category,
            Document.title,
            Document.file_size_bytes,
            Document.metadata_,
            Document.visibility,
            Document.source_modified_at,
            Document.activity_at,
            Document.synced_at,
        ))
        .order_by(Document.id)
        .limit(DASHBOARD_BACKFILL_BATCH_SIZE)
    )
    ids = list(document_ids)
    if ids:
        statement = statement.where(Document.id.in_(ids))
    if after_id is not None:
        statement = statement.where(Document.id > after_id)
    return statement


async def backfill_dashboard_document_projections(
    db: AsyncSession,
    document_ids: Iterable[object] | None = None,
) -> dict[str, int]:
    """Build exact replacement rows for legacy documents in bounded batches."""
    requested_ids = list(document_ids or [])
    last_id = None
    visited = 0
    changed = 0
    while True:
        documents = (
            await db.execute(
                dashboard_backfill_documents_statement(
                    document_ids=requested_ids,
                    after_id=last_id,
                )
            )
        ).scalars().all()
        if not documents:
            break
        for document in documents:
            if document.category == "conversation":
                from .conversation_read_model import (
                    READ_MODEL_VERSION,
                    refresh_conversation_read_model_in_batches,
                )

                read_model = await db.get(ConversationReadModel, document.id)
                if (
                    read_model is None
                    or read_model.projection_version != READ_MODEL_VERSION
                ):
                    await refresh_conversation_read_model_in_batches(
                        db,
                        document,
                    )
            _, projection_changed = await refresh_dashboard_document_projection(
                db,
                document,
            )
            visited += 1
            changed += int(projection_changed)
        last_id = documents[-1].id
        await db.flush()
        if requested_ids:
            break

    if not requested_ids:
        await _set_backfill_complete(db)
    return {"documents": visited, "created_or_updated": changed}
