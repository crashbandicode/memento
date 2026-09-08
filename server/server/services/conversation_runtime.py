"""Ordered storage and lookup for hook-observed native agent sessions."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationRuntime, Document
from .conversation_identity import conversation_native_id
from .thread_metadata_service import ThreadTitleUpdateResult

_TOOLS = frozenset({"claude_code", "codex", "cursor"})
_STATES = frozenset({"running", "ended"})
_PRIVILEGES = frozenset({"standard", "administrator", "root", "unknown"})
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,512}$")
_FUTURE_CLOCK_SKEW = timedelta(minutes=5)


def parse_runtime_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def apply_conversation_runtime_update(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    tool_id: str,
    session_id: str,
    runtime_state: str,
    runtime_privilege: str,
    timestamp: str,
) -> ThreadTitleUpdateResult:
    """Apply a monotonic runtime observation scoped to one collector machine."""

    clean_session_id = str(session_id or "").strip()
    state = str(runtime_state or "").strip().casefold()
    privilege = str(runtime_privilege or "").strip().casefold()
    observed_at = parse_runtime_timestamp(timestamp)
    if (
        tool_id not in _TOOLS
        or not _SAFE_SESSION_ID.fullmatch(clean_session_id)
        or state not in _STATES
        or privilege not in _PRIVILEGES
        or observed_at is None
        or observed_at > datetime.now(timezone.utc) + _FUTURE_CLOCK_SKEW
    ):
        return ThreadTitleUpdateResult(0, 0, 1, valid=False)

    runtime = (
        await db.execute(
            select(ConversationRuntime)
            .where(
                ConversationRuntime.machine_id == machine_id,
                ConversationRuntime.tool_id == tool_id,
                ConversationRuntime.session_id == clean_session_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if runtime is not None and observed_at <= runtime.observed_at:
        return ThreadTitleUpdateResult(1, 0, 1)

    if runtime is None:
        runtime = ConversationRuntime(
            machine_id=machine_id,
            tool_id=tool_id,
            session_id=clean_session_id,
            state=state,
            privilege=privilege,
            started_at=observed_at if state == "running" else None,
            observed_at=observed_at,
            ended_at=observed_at if state == "ended" else None,
        )
        db.add(runtime)
    else:
        reopening = runtime.state == "ended" and state == "running"
        runtime.state = state
        if privilege != "unknown" or runtime.privilege == "unknown":
            runtime.privilege = privilege
        runtime.observed_at = observed_at
        if reopening or (state == "running" and runtime.started_at is None):
            runtime.started_at = observed_at
        runtime.ended_at = observed_at if state == "ended" else None
    await db.flush()
    return ThreadTitleUpdateResult(1, 1, 0)


async def conversation_runtime_for_document(
    db: AsyncSession,
    document: Document,
) -> ConversationRuntime | None:
    """Return the runtime row matching this exact machine/tool/native session."""

    if document.machine_id is None:
        return None
    session_id = conversation_native_id(
        document.tool_id,
        "conversation",
        document.metadata_,
    )
    if not session_id:
        return None
    return (
        await db.execute(
            select(ConversationRuntime).where(
                ConversationRuntime.machine_id == document.machine_id,
                ConversationRuntime.tool_id == document.tool_id,
                ConversationRuntime.session_id == session_id,
            )
        )
    ).scalar_one_or_none()
