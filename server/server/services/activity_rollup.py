"""Precomputed activity rollup for the daily calendar.

`list_daily_dates` used to aggregate the full ``conversation_messages`` table
(3.3M+ rows) on every cold cache miss — a ~3s seq scan, on the request path.
This precomputes hourly (UTC) per-machine/per-tool countable-message counts
into a tiny table refreshed in the background (celery-beat), so the endpoint
reads a few thousand rows and applies the user's timezone offset itself. The
hourly grain keeps timezone-boundary counts exact (unlike a UTC-day rollup).

The rollup is a full recompute (DELETE + INSERT in one transaction) — the
source is essentially append-only, the result is small, and MVCC lets readers
see the previous snapshot until commit. A missing/empty rollup is not fatal:
the endpoint falls back to the live aggregation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Same filter the live daily query uses, so the rollup counts exactly the same
# "countable" messages (human/assistant turns, excluding tool noise + system).
_COUNTABLE = (
    "m.timestamp IS NOT NULL "
    "AND m.role IN ('user','assistant') "
    "AND m.content NOT LIKE '[Result]%' "
    "AND m.content NOT LIKE '[Tool:%' "
    "AND d.tool_id <> 'system'"
)

# asyncpg runs one statement per prepared query, so DELETE and INSERT are
# issued separately (in the same transaction — the caller commits once).
_DELETE_SQL = text("DELETE FROM conversation_activity_hourly")
_INSERT_SQL = text(
    f"""
    INSERT INTO conversation_activity_hourly (hour, machine_id, tool_id, message_count)
    SELECT date_trunc('hour', m.timestamp) AS hour,
           COALESCE(d.machine_id, '00000000-0000-0000-0000-000000000000'::uuid) AS machine_id,
           d.tool_id,
           count(*) AS message_count
    FROM conversation_messages m
    JOIN documents d ON m.document_id = d.id
    WHERE {_COUNTABLE}
    GROUP BY 1, 2, 3
    """
)


async def refresh_activity_hourly(db: AsyncSession) -> int:
    """Recompute the hourly rollup. Returns the row count written.

    DELETE + INSERT run in one transaction so readers see the previous
    snapshot (MVCC) until the single commit.
    """
    await db.execute(_DELETE_SQL)
    await db.execute(_INSERT_SQL)
    await db.commit()
    n = await db.scalar(text("SELECT count(*) FROM conversation_activity_hourly"))
    return int(n or 0)


async def rollup_is_populated(db: AsyncSession) -> bool:
    return bool(await db.scalar(text("SELECT 1 FROM conversation_activity_hourly LIMIT 1")))


async def daily_dates_from_rollup(
    db: AsyncSession,
    *,
    machine_ids: list[uuid.UUID] | None,
    cutoff,
    tz_offset: int,
) -> list[dict]:
    """Read the daily calendar from the rollup, applying the tz offset.

    ``machine_ids`` None means no scoping (admin/owner). Mirrors the live
    query's day computation: shift each hour by -tz_offset minutes, take the
    date, group.
    """
    params: dict = {"tz_min": -tz_offset, "cutoff": cutoff}
    where = ["hour >= :cutoff"]
    if machine_ids is not None:
        if not machine_ids:
            return []
        params["mids"] = machine_ids
        where.append("machine_id = ANY(:mids)")
    sql = text(
        f"""
        SELECT (hour + make_interval(mins => :tz_min))::date AS day,
               sum(message_count) AS total,
               array_agg(DISTINCT tool_id) AS tools
        FROM conversation_activity_hourly
        WHERE {' AND '.join(where)}
        GROUP BY day
        ORDER BY day DESC
        """
    )
    rows = (await db.execute(sql, params)).all()
    return [
        {
            "date": str(row.day),
            "document_count": int(row.total),
            "message_count": int(row.total),
            "tools": sorted([t for t in (row.tools or []) if t]),
        }
        for row in rows
    ]
