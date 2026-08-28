"""Durably reconcile collector metadata that arrives before conversation content."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ConversationMetadataInbox,
    Document,
    Machine,
)
from .conversation_identity import conversation_session_id
from .document_delivery import delivery_metadata_expression
from .thread_metadata_service import (
    apply_codex_thread_title_update,
    apply_conversation_activity_update,
    apply_conversation_interaction_update,
)

_UUID_TOKEN = re.compile(
    r"(?<![0-9a-f])"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)
_MAX_REPLAY_ROWS = 256


def normalized_metadata_session_id(
    tool_id: str,
    relative_path: str,
    explicit_session_id: object = None,
) -> str | None:
    """Return a verified stable route for metadata from relocatable sources."""
    if tool_id != "cursor":
        return None
    try:
        return str(uuid.UUID(str(explicit_session_id)))
    except (ValueError, TypeError, AttributeError):
        pass
    normalized_path = str(relative_path or "").replace("\\", "/")
    filename = normalized_path.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    try:
        return str(uuid.UUID(stem))
    except (ValueError, AttributeError):
        pass
    matches = _UUID_TOKEN.findall(normalized_path)
    return str(uuid.UUID(matches[-1])) if matches else None


def metadata_signal_id(payload: dict) -> str:
    metadata_type = str(payload.get("metadata_type") or "")
    if metadata_type == "codex_thread_title":
        return str(payload.get("thread_id") or "")
    if metadata_type == "conversation_interaction":
        return str(payload.get("interaction_id") or "")
    if metadata_type == "conversation_activity":
        return str(payload.get("activity_id") or "")
    return ""


def _route_hash(
    relative_path: str,
    session_id: str | None,
    *,
    metadata_type: str,
    signal_id: str,
) -> str:
    route = (
        f"session:{session_id}"
        if session_id
        else (
            f"path:{relative_path.casefold()}"
            if relative_path
            else f"{metadata_type}:{signal_id}"
        )
    )
    return hashlib.sha256(route.encode("utf-8")).hexdigest()


def _expiry(metadata_type: str, now: datetime) -> datetime:
    if metadata_type == "conversation_activity":
        return now + timedelta(days=2)
    if metadata_type == "conversation_interaction":
        return now + timedelta(days=7)
    return now + timedelta(days=30)


async def resolve_metadata_relative_path(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_id: str,
    relative_path: str,
    session_id: str | None,
) -> str:
    """Resolve an alias path to the current canonical conversation location."""
    if not session_id:
        return relative_path
    effective_metadata = delivery_metadata_expression()
    document = (
        await db.execute(
            select(Document.relative_path)
            .where(
                Document.machine_id == machine_id,
                Document.machine_id.in_(
                    select(Machine.id).where(Machine.user_id == user_id)
                ),
                Document.tool_id == tool_id,
                Document.category == "conversation",
                effective_metadata["session_id"].astext == session_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(document or relative_path)


async def defer_conversation_metadata(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: dict,
) -> bool:
    """Upsert the latest state for a signal the server cannot apply yet."""
    tool_id = str(payload.get("tool") or "")
    metadata_type = str(payload.get("metadata_type") or "")
    relative_path = (
        str(payload.get("relative_path") or "")
        .replace("\\", "/")
        .lstrip("/")
    )
    signal_id = metadata_signal_id(payload)
    session_id = normalized_metadata_session_id(
        tool_id,
        relative_path,
        payload.get("session_id"),
    )
    if (
        tool_id not in {"claude_code", "codex", "cursor"}
        or not metadata_type
        or not signal_id
        or (not relative_path and metadata_type != "codex_thread_title")
    ):
        return False

    now = datetime.now(timezone.utc)
    values = {
        "machine_id": machine_id,
        "user_id": user_id,
        "tool_id": tool_id,
        "route_hash": _route_hash(
            relative_path,
            session_id,
            metadata_type=metadata_type,
            signal_id=signal_id,
        ),
        "relative_path": relative_path,
        "session_id": session_id,
        "metadata_type": metadata_type,
        "signal_id": signal_id[:512],
        "payload": payload,
        "source_timestamp": str(payload.get("timestamp") or "")[:128],
        "expires_at": _expiry(metadata_type, now),
        "updated_at": now,
    }
    statement = pg_insert(ConversationMetadataInbox).values(**values)
    await db.execute(
        statement.on_conflict_do_update(
            constraint="uq_conversation_metadata_inbox_signal",
            set_={
                "relative_path": statement.excluded.relative_path,
                "session_id": statement.excluded.session_id,
                "payload": statement.excluded.payload,
                "source_timestamp": statement.excluded.source_timestamp,
                "expires_at": statement.excluded.expires_at,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )
    return True


async def purge_expired_conversation_metadata(db: AsyncSession) -> int:
    """Remove signals whose source document never arrived within its TTL."""
    result = await db.execute(
        delete(ConversationMetadataInbox).where(
            ConversationMetadataInbox.expires_at <= datetime.now(timezone.utc)
        )
    )
    return int(result.rowcount or 0)


async def apply_deferred_conversation_metadata(
    db: AsyncSession,
    *,
    document: Document,
    user_id: uuid.UUID,
) -> int:
    """Apply and retire signals whose conversation has now been ingested."""
    if document.machine_id is None or document.category != "conversation":
        return 0
    session_id = conversation_session_id(
        document.tool_id,
        document.category,
        document.metadata_,
    )
    route_filter = or_(
        ConversationMetadataInbox.route_hash == _route_hash(
            document.relative_path,
            None,
            metadata_type="",
            signal_id="",
        ),
        ConversationMetadataInbox.relative_path == document.relative_path,
    )
    if session_id:
        route_filter = or_(
            route_filter,
            ConversationMetadataInbox.session_id == session_id,
        )
    thread_id = str((document.metadata_ or {}).get("thread_id") or "")
    if thread_id:
        route_filter = or_(
            route_filter,
            (
                (ConversationMetadataInbox.metadata_type == "codex_thread_title")
                & (ConversationMetadataInbox.signal_id == thread_id)
            ),
        )
    now = datetime.now(timezone.utc)
    rows = (
        (
            await db.execute(
                select(ConversationMetadataInbox)
                .where(
                    ConversationMetadataInbox.machine_id == document.machine_id,
                    ConversationMetadataInbox.user_id == user_id,
                    ConversationMetadataInbox.tool_id == document.tool_id,
                    ConversationMetadataInbox.expires_at > now,
                    route_filter,
                )
                .order_by(
                    ConversationMetadataInbox.created_at,
                    ConversationMetadataInbox.id,
                )
                .limit(_MAX_REPLAY_ROWS)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    applied_ids: list[uuid.UUID] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        metadata_type = str(payload.get("metadata_type") or "")
        result = None
        if metadata_type == "conversation_interaction":
            result = await apply_conversation_interaction_update(
                db,
                machine_id=document.machine_id,
                user_id=user_id,
                tool_id=document.tool_id,
                relative_path=document.relative_path,
                interaction_id=str(payload.get("interaction_id") or ""),
                interaction_status=str(payload.get("interaction_status") or ""),
                question_tool=str(payload.get("question_tool") or ""),
                interaction_input=payload.get("interaction_input") or {},
                interaction_origin=payload.get("interaction_origin"),
                timestamp=str(payload.get("timestamp") or ""),
            )
        elif metadata_type == "conversation_activity":
            result = await apply_conversation_activity_update(
                db,
                machine_id=document.machine_id,
                user_id=user_id,
                tool_id=document.tool_id,
                relative_path=document.relative_path,
                activity_id=str(payload.get("activity_id") or ""),
                activity_status=str(payload.get("activity_status") or ""),
                activity_tool=str(payload.get("activity_tool") or ""),
                command=payload.get("command") or "",
                is_background=payload.get("is_background") is True,
                timestamp=str(payload.get("timestamp") or ""),
            )
        elif metadata_type == "codex_thread_title" and document.tool_id == "codex":
            try:
                thread_id = uuid.UUID(str(payload.get("thread_id") or ""))
                revision = int(payload.get("revision") or 0)
            except (ValueError, TypeError, AttributeError):
                thread_id = None
                revision = 0
            if thread_id is not None and revision > 0:
                result = await apply_codex_thread_title_update(
                    db,
                    machine_id=document.machine_id,
                    thread_id=thread_id,
                    title=str(payload.get("title") or ""),
                    title_kind=str(payload.get("title_kind") or "unknown"),
                    revision=revision,
                    relative_path=document.relative_path,
                    user_id=user_id,
                )
        if result is not None and (result.matched > 0 or not result.valid):
            applied_ids.append(row.id)

    if applied_ids:
        await db.execute(
            delete(ConversationMetadataInbox).where(
                ConversationMetadataInbox.id.in_(applied_ids)
            )
        )
    return len(applied_ids)
