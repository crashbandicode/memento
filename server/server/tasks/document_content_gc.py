"""Hourly Celery entrypoint for delayed document-content object GC."""

from __future__ import annotations

import asyncio
import logging

from ..db.session import async_session_factory
from ..services.document_content_gc import collect_unreferenced_document_content
from .celery_app import celery_app


logger = logging.getLogger("document_content_gc")


async def _run() -> dict[str, int]:
    async with async_session_factory() as db:
        return await collect_unreferenced_document_content(db)


@celery_app.task(
    name="server.tasks.document_content_gc.collect_document_content_gc",
    acks_late=True,
    time_limit=600,
)
def collect_document_content_gc() -> dict[str, int | str]:
    try:
        counts = asyncio.run(_run())
        logger.info(
            "document-content GC completed marked=%d unmarked=%d deleted=%d",
            counts["marked"],
            counts["unmarked"],
            counts["deleted"],
        )
        return counts
    except Exception as error:  # noqa: BLE001
        logger.exception("document-content GC failed: %s", error)
        return {"marked": 0, "unmarked": 0, "deleted": 0, "error": str(error)[:200]}
