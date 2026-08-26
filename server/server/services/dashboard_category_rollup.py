"""Precomputed dashboard document-category counts.

``GET /api/dashboard`` needs a count grouped by tool and document category to
populate every tool card.  Running that GROUP BY over every dashboard document
projection was the dominant cold-request cost on production's multi-million
document corpus.  This service refreshes a much smaller per-machine snapshot
off the request path.  Reads combine the selected machines at query time, so
owner/admin and ordinary-user scoping remains identical to the live query.

The refresh is a full DELETE then INSERT in one transaction. PostgreSQL MVCC
keeps the previous snapshot visible to readers until commit.  A missing or
empty snapshot is deliberately not fatal: the endpoint retains its live-query
fallback for first deploys and failed background refreshes.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Primary-key-safe representation for dashboard projection rows whose source
# document has no machine. Non-admin users never match it; owner/admin reads
# remain unscoped and therefore include the same legacy rows as the live query.
NULL_MACHINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

# asyncpg prepares one statement at a time, so the refresh issues DELETE and
# INSERT separately (inside the caller's one transaction).
_DELETE_SQL = text("DELETE FROM dashboard_document_category_rollups")
_INSERT_SQL = text(
    """
    INSERT INTO dashboard_document_category_rollups
        (machine_id, tool_id, category, document_count)
    SELECT
        COALESCE(
            machine_id,
            '00000000-0000-0000-0000-000000000000'::uuid
        ) AS machine_id,
        tool_id,
        category,
        count(*) AS document_count
    FROM dashboard_document_projections
    GROUP BY 1, 2, 3
    """
)


async def refresh_dashboard_document_category_rollup(
    db: AsyncSession,
) -> int:
    """Recompute the dashboard category snapshot and return rows written."""
    await db.execute(_DELETE_SQL)
    await db.execute(_INSERT_SQL)
    await db.commit()
    count = await db.scalar(
        text("SELECT count(*) FROM dashboard_document_category_rollups")
    )
    return int(count or 0)


async def dashboard_category_rollup_is_populated(db: AsyncSession) -> bool:
    """Return whether a background refresh has produced any snapshot rows."""
    return bool(await db.scalar(text(
        "SELECT 1 FROM dashboard_document_category_rollups LIMIT 1"
    )))


async def dashboard_categories_from_rollup(
    db: AsyncSession,
    *,
    machine_ids: list[uuid.UUID] | None,
) -> dict[str, dict[str, int]]:
    """Return dashboard category counts with the live query's machine scope.

    ``None`` represents owner/admin's unscoped view. An empty list represents
    an ordinary user with no accessible machine and therefore returns nothing.
    """
    if machine_ids == []:
        return {}

    params: dict[str, object] = {}
    where = ""
    if machine_ids is not None:
        params["machine_ids"] = machine_ids
        where = "WHERE machine_id = ANY(:machine_ids)"

    rows = (await db.execute(text(
        f"""
        SELECT tool_id, category, sum(document_count) AS document_count
        FROM dashboard_document_category_rollups
        {where}
        GROUP BY tool_id, category
        """
    ), params)).all()
    categories: dict[str, dict[str, int]] = {}
    for tool_id, category, count in rows:
        categories.setdefault(tool_id, {})[category] = int(count)
    return categories
