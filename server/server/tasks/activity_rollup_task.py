"""Celery beat task: refresh the daily activity rollup in the background.

Moves the expensive full-history aggregation off the request path. The daily
calendar endpoint reads the precomputed hourly rollup; this recomputes it every
few minutes. A PostgreSQL advisory lock keeps overlapping ticks from doubling
the work.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from ..db.session import async_session_factory, engine
from ..services.activity_rollup import refresh_activity_hourly
from .celery_app import celery_app

logger = logging.getLogger("activity_rollup")

# Arbitrary, stable advisory-lock key ("ACTVROLL").
LOCK_KEY = 0x41435456524F4C4C


async def _run() -> dict:
    async with engine.connect() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
        )
        await lock_connection.commit()
        if not acquired:
            return {"refreshed": False, "locked": True}
        try:
            async with async_session_factory() as db:
                rows = await refresh_activity_hourly(db)
            return {"refreshed": True, "rows": rows}
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )
            await lock_connection.commit()


@celery_app.task(
    name="server.tasks.activity_rollup_task.refresh_activity_rollup",
    acks_late=True,
    time_limit=300,
)
def refresh_activity_rollup() -> dict:
    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh_activity_rollup errored: %s", e)
        return {"refreshed": False, "error": str(e)[:200]}
