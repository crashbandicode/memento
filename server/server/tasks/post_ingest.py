"""Per-document post-ingest processing for durable spool finalizers."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from ..services.ingest_service import _run_post_ingest_inner
from .celery_app import celery_app

logger = logging.getLogger("post_ingest_task")


@celery_app.task(
    name="server.tasks.post_ingest.process_document_post_ingest",
    # A lost task is safe to leave pending: the 15-minute embedding/knowledge
    # scanners recover it. Early acknowledgement avoids duplicate graph work
    # if a worker dies after completing the DB writes but before ACKing.
    acks_late=False,
    time_limit=600,
)
def process_document_post_ingest(
    document_id: str,
    tool_id: str,
    category: str,
) -> dict:
    if category not in ("conversation", "memory", "learning", "plan", "identity"):
        return {"status": "skipped", "document_id": document_id}
    try:
        asyncio.run(
            _run_post_ingest_inner(UUID(document_id), tool_id, category)
        )
        return {"status": "processed", "document_id": document_id}
    except Exception as exc:
        logger.exception("Post-ingest task failed for %s", document_id)
        return {
            "status": "failed",
            "document_id": document_id,
            "error_type": type(exc).__name__,
        }
