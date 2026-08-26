"""Celery beat task to refresh dashboard document-category counts."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from ..db.session import async_session_factory, engine
from ..services.dashboard_category_rollup import (
    refresh_dashboard_document_category_rollup,
)
from .celery_app import celery_app

logger = logging.getLogger("dashboard_category_rollup")

# Arbitrary, stable advisory-lock key ("DSHRCAT").
LOCK_KEY = 0x44534852434154


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
                rows = await refresh_dashboard_document_category_rollup(db)
            return {"refreshed": True, "rows": rows}
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )
            await lock_connection.commit()


@celery_app.task(
    name=(
        "server.tasks.dashboard_category_rollup_task."
        "refresh_dashboard_category_rollup"
    ),
    acks_late=True,
    time_limit=300,
)
def refresh_dashboard_category_rollup() -> dict:
    try:
        return asyncio.run(_run())
    except Exception as error:  # noqa: BLE001
        logger.warning("refresh_dashboard_category_rollup errored: %s", error)
        return {"refreshed": False, "error": str(error)[:200]}
