"""Celery beat task to refresh dashboard conversation-message activity."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from ..db.session import async_session_factory, engine
from ..services.dashboard_conversation_message_rollup import (
    refresh_dashboard_conversation_message_rollup,
)
from .celery_app import celery_app

logger = logging.getLogger("dashboard_conversation_message_rollup")

# Arbitrary, stable advisory-lock key ("DSHMSG").
LOCK_KEY = 0x4453484D5347


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
                rows = await refresh_dashboard_conversation_message_rollup(db)
            return {"refreshed": True, "rows": rows}
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
            )
            await lock_connection.commit()


# The Celery task function must NOT share the name of the imported service
# function above, or the def would shadow it at module scope and _run() would
# call this 0-arg task instead of the service. Celery registers by name= below,
# so the Python function name is free to differ.
@celery_app.task(
    name=(
        "server.tasks.dashboard_conversation_message_rollup_task."
        "refresh_dashboard_conversation_message_rollup"
    ),
    acks_late=True,
    time_limit=300,
)
def refresh_conversation_message_rollup_task() -> dict:
    try:
        return asyncio.run(_run())
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "refresh_dashboard_conversation_message_rollup errored: %s",
            error,
        )
        return {"refreshed": False, "error": str(error)[:200]}
