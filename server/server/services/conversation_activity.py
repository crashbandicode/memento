"""Shared conversation activity classification for list surfaces."""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, func, or_, select

from ..db.models import ConversationMessage, ConversationRuntime
from .conversation_identity import conversation_native_id
from .document_delivery import (
    advance_document_activity,
    document_delivery_state,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..db.models import Document


SHORT_EXCHANGE_CHARACTER_LIMIT = 120
REAL_ACTIVITY_ROLES = ("user", "assistant")
RUNNING_ACTIVITY_MAX_AGE = timedelta(hours=24)
TERMINAL_ACTIVITY_MAX_AGE = timedelta(hours=1)
ACTIVITY_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
CONVERSATION_ACTIVITY_STATUSES = {
    "running",
    "completed",
    "failed",
    "cancelled",
}


@dataclass(frozen=True, slots=True)
class ConversationActivitySummary:
    """Small shared activity projection for every conversation list surface."""

    message_count: int = 0
    user_count: int = 0
    assistant_count: int = 0
    human_character_count: int = 0

    @property
    def is_low_activity(self) -> bool:
        return is_low_activity_summary(
            self.user_count,
            self.assistant_count,
            self.human_character_count,
        )


def parse_conversation_activity_timestamp(value: object) -> datetime | None:
    """Parse a source event timestamp into aware UTC, or reject it."""
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def conversation_activity_is_fresh(
    activity: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a persisted activity card is safe to expose."""
    status = str(activity.get("status") or "").strip().casefold()
    if status not in CONVERSATION_ACTIVITY_STATUSES:
        return False
    event_at = parse_conversation_activity_timestamp(
        activity.get("updated_at")
        or activity.get("started_at")
        or activity.get("timestamp")
    )
    if event_at is None:
        return False
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    else:
        observed_at = observed_at.astimezone(timezone.utc)
    if event_at - observed_at > ACTIVITY_FUTURE_CLOCK_SKEW:
        return False
    max_age = (
        RUNNING_ACTIVITY_MAX_AGE
        if status == "running"
        else TERMINAL_ACTIVITY_MAX_AGE
    )
    return observed_at - event_at <= max_age


def background_activity_retired_by_session_end(
    activity: Mapping[str, object],
    session_ended_at: object,
) -> bool:
    """Return whether an ended owning session retires a background card.

    A launched-and-forgotten Claude ``run_in_background`` shell never emits a
    completion event, so its projected activity stays ``running`` until the
    freshness TTL (:data:`RUNNING_ACTIVITY_MAX_AGE`). When the owning session
    ends -- a deterministic lifecycle signal the collector already reports via
    the session-runtime path -- that background shell can no longer surface a
    completion in this session, so the card is retired at read time instead of
    lingering for a day.

    Only still-``running`` background activities are affected. Non-background
    activities, already-terminal activities, activities whose session is still
    open (``session_ended_at`` unset), and any activity that clearly began
    after the recorded session end are always kept. This is applied at read
    time -- like :func:`conversation_activity_is_fresh` -- so a full projection
    rebuild never resurrects a card and the stored transcript is never mutated.
    """
    ended_at = parse_conversation_activity_timestamp(session_ended_at)
    if ended_at is None:
        return False
    if activity.get("is_background") is not True:
        return False
    status = str(activity.get("status") or "").strip().casefold()
    if status != "running":
        return False
    started_at = parse_conversation_activity_timestamp(
        activity.get("started_at")
        or activity.get("updated_at")
        or activity.get("timestamp")
    )
    if started_at is None:
        return True
    return started_at <= ended_at + ACTIVITY_FUTURE_CLOCK_SKEW


async def conversation_runtime_ended_at_map(
    db: "AsyncSession",
    identities: Iterable[tuple[object, object, str, object]],
) -> dict[object, datetime]:
    """Map each document id to its owning session's end time when ended.

    ``identities`` yields ``(document_id, machine_id, tool_id, metadata)`` for
    the documents on one conversation surface. Only sessions currently in the
    terminal ``ended`` state contribute an entry, so a session that reopened
    (state ``running``) is intentionally absent and keeps surfacing its live
    background cards. The lookup is one bounded query over the caller's already
    page-bounded document set.
    """
    keyed: dict[tuple[object, str, str], list[object]] = {}
    for document_id, machine_id, tool_id, metadata in identities:
        if document_id is None or machine_id is None:
            continue
        session_id = conversation_native_id(tool_id, "conversation", metadata)
        if not session_id:
            continue
        keyed.setdefault(
            (machine_id, tool_id, session_id), []
        ).append(document_id)
    if not keyed:
        return {}
    rows = (
        await db.execute(
            select(ConversationRuntime).where(
                or_(
                    *(
                        and_(
                            ConversationRuntime.machine_id == machine_id,
                            ConversationRuntime.tool_id == tool_id,
                            ConversationRuntime.session_id == session_id,
                        )
                        for machine_id, tool_id, session_id in keyed
                    )
                )
            )
        )
    ).scalars().all()
    ended: dict[object, datetime] = {}
    for runtime in rows:
        if runtime.state != "ended" or runtime.ended_at is None:
            continue
        for document_id in keyed.get(
            (runtime.machine_id, runtime.tool_id, runtime.session_id),
            (),
        ):
            ended[document_id] = runtime.ended_at
    return ended


def effective_conversation_activity(
    activity_at: datetime | None,
    source_modified_at: datetime | None,
    synced_at: datetime | None,
) -> datetime | None:
    """Return the outward timestamp for a conversation revision.

    Persisted activity is always a real user/assistant turn. Legacy sources
    without such a timestamp fall back to their source mtime, bounded by the
    moment the revision was observed so a skewed future mtime cannot surface.
    """
    if activity_at is not None:
        return activity_at
    if source_modified_at is not None and synced_at is not None:
        return min(source_modified_at, synced_at)
    return source_modified_at or synced_at


def effective_conversation_activity_expression(
    activity_at,
    source_modified_at,
    synced_at,
):
    """SQL expression matching :func:`effective_conversation_activity`."""
    bounded_source_timestamp = case(
        (source_modified_at.is_(None), synced_at),
        (synced_at.is_(None), source_modified_at),
        (source_modified_at <= synced_at, source_modified_at),
        else_=synced_at,
    )
    return func.coalesce(activity_at, bounded_source_timestamp)


def conversation_list_timestamp_expression(
    category,
    activity_at,
    source_modified_at,
    synced_at,
):
    """Order conversations by activity and other documents by sync time."""
    return case(
        (
            category == "conversation",
            effective_conversation_activity_expression(
                activity_at,
                source_modified_at,
                synced_at,
            ),
        ),
        else_=synced_at,
    )


def conversation_activity_at_query(document_id: object):
    """Select the latest timestamp belonging to a real conversation turn."""
    return select(func.max(ConversationMessage.timestamp)).where(
        ConversationMessage.document_id == document_id,
        ConversationMessage.timestamp.is_not(None),
        ConversationMessage.role.in_(REAL_ACTIVITY_ROLES),
    )


def historical_conversation_activity_query(
    document_ids: Iterable[object],
    as_of: datetime,
):
    """Select per-document real activity visible at a snapshot cutoff."""
    return (
        select(
            ConversationMessage.document_id,
            func.max(ConversationMessage.timestamp),
        )
        .where(
            ConversationMessage.document_id.in_(list(document_ids)),
            ConversationMessage.timestamp.is_not(None),
            ConversationMessage.timestamp <= as_of,
            ConversationMessage.role.in_(REAL_ACTIVITY_ROLES),
        )
        .group_by(ConversationMessage.document_id)
    )


async def refresh_document_activity_at(
    db: "AsyncSession",
    document: "Document",
):
    """Advance conversation time from normalized human/assistant rows only."""
    activity_at = (
        await db.execute(conversation_activity_at_query(document.id))
    ).scalar_one_or_none()
    advance_document_activity(document, activity_at)
    state = document_delivery_state(document)
    return state.activity_at if state is not None else document.activity_at


async def conversation_activity_summaries(
    db: "AsyncSession",
    document_ids: Iterable[Hashable],
) -> dict[Hashable, ConversationActivitySummary]:
    """Return one bounded aggregate query for a page of conversations."""
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {}
    rows = await db.execute(
        select(
            ConversationMessage.document_id,
            func.count().label("message_count"),
            func.count().filter(
                ConversationMessage.role == "user"
            ).label("user_count"),
            func.count().filter(
                ConversationMessage.role == "assistant"
            ).label("assistant_count"),
            func.coalesce(
                func.sum(func.length(ConversationMessage.content)).filter(
                    ConversationMessage.role.in_(REAL_ACTIVITY_ROLES)
                ),
                0,
            ).label("human_character_count"),
        )
        .where(ConversationMessage.document_id.in_(ids))
        .group_by(ConversationMessage.document_id)
    )
    return {
        document_id: ConversationActivitySummary(
            message_count=int(message_count or 0),
            user_count=int(user_count or 0),
            assistant_count=int(assistant_count or 0),
            human_character_count=int(human_character_count or 0),
        )
        for (
            document_id,
            message_count,
            user_count,
            assistant_count,
            human_character_count,
        ) in rows.all()
    }


def is_low_activity_summary(
    user_count: int,
    assistant_count: int,
    human_character_count: int,
) -> bool:
    """Return whether a thread is empty or too slight for the primary list.

    A useful exchange needs input from both sides. A single Q/A is retained
    when it contains enough substance; tiny acknowledgements are tucked into
    the collapsed low-activity section instead.
    """
    if user_count <= 0 or assistant_count <= 0:
        return True
    return (
        user_count + assistant_count <= 2
        and human_character_count < SHORT_EXCHANGE_CHARACTER_LIMIT
    )


def is_low_activity_messages(messages: Iterable[Mapping[str, object]]) -> bool:
    """Classify normalized message dictionaries using the shared heuristic."""
    user_count = 0
    assistant_count = 0
    character_count = 0
    for message in messages:
        role = message.get("role")
        if role == "user":
            user_count += 1
        elif role == "assistant":
            assistant_count += 1
        else:
            continue
        character_count += len(str(message.get("content") or "").strip())

    return is_low_activity_summary(
        user_count,
        assistant_count,
        character_count,
    )
