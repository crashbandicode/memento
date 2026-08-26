"""Precomputed dashboard conversation-message activity.

During a dashboard-projection rollout, legacy conversation rows still need the
message counts used for their activity badges.  Calculating those counts in
``GET /api/dashboard`` groups transcript rows and evaluates ``length(content)``
over the multi-million-row ``conversation_messages`` table.  This service
keeps the exact per-document aggregate in a compact background snapshot so the
request path only looks up its bounded set of legacy document ids.

The refresh replaces the whole snapshot in one transaction.  PostgreSQL MVCC
keeps the preceding snapshot visible until commit.  If no snapshot is present,
the dashboard deliberately retains its original live-query fallback.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_DELETE_SQL = text("DELETE FROM dashboard_conversation_message_rollups")
_INSERT_SQL = text(
    """
    INSERT INTO dashboard_conversation_message_rollups
        (
            document_id,
            message_count,
            user_message_count,
            assistant_message_count,
            human_character_count
        )
    SELECT
        document_id,
        count(*) AS message_count,
        count(*) FILTER (WHERE role = 'user') AS user_message_count,
        count(*) FILTER (WHERE role = 'assistant') AS assistant_message_count,
        COALESCE(
            sum(length(content)) FILTER (
                WHERE role IN ('user', 'assistant')
            ),
            0
        ) AS human_character_count
    FROM conversation_messages
    GROUP BY document_id
    """
)


async def refresh_dashboard_conversation_message_rollup(
    db: AsyncSession,
) -> int:
    """Recompute message activity for every conversation document."""
    await db.execute(_DELETE_SQL)
    await db.execute(_INSERT_SQL)
    await db.commit()
    count = await db.scalar(text(
        "SELECT count(*) FROM dashboard_conversation_message_rollups"
    ))
    return int(count or 0)


async def dashboard_conversation_message_rollup_is_populated(
    db: AsyncSession,
) -> bool:
    """Return whether a background refresh has produced any snapshot rows."""
    return bool(await db.scalar(text(
        "SELECT 1 FROM dashboard_conversation_message_rollups LIMIT 1"
    )))


async def dashboard_message_activity_from_rollup(
    db: AsyncSession,
    *,
    document_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int, int, int]]:
    """Return exact activity for the already dashboard-scoped document ids."""
    if not document_ids:
        return {}

    rows = (await db.execute(text(
        """
        SELECT
            document_id,
            message_count,
            user_message_count,
            assistant_message_count,
            human_character_count
        FROM dashboard_conversation_message_rollups
        WHERE document_id = ANY(:document_ids)
        """
    ), {"document_ids": document_ids})).all()
    return {
        document_id: (
            int(message_count),
            int(user_message_count),
            int(assistant_message_count),
            int(human_character_count),
        )
        for (
            document_id,
            message_count,
            user_message_count,
            assistant_message_count,
            human_character_count,
        ) in rows
    }
