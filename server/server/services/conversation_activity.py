"""Shared conversation activity classification for list surfaces."""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select

from ..db.models import ConversationMessage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..db.models import Document


SHORT_EXCHANGE_CHARACTER_LIMIT = 120
REAL_ACTIVITY_ROLES = ("user", "assistant")


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
    """Persist conversation time independently from collector sync time."""
    activity_at = (
        await db.execute(conversation_activity_at_query(document.id))
    ).scalar_one_or_none()
    document.activity_at = activity_at
    return activity_at


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
