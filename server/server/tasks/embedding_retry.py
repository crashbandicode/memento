"""Celery task: retry documents whose embedding pipeline previously failed.

The API path calls ``generate_document_embeddings`` once inline after ingest.
If the host-side BGE-M3 server is down / timed out, the document gets marked
``embedding_status='failed'`` and would otherwise sit there forever with no
vectors. This beat task scans for those and retries, backing off by attempt
count.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from ..db.models import Document
from ..db.session import async_session_factory
from ..services.embedding_service import generate_document_embeddings
from .celery_app import celery_app

logger = logging.getLogger("embedding_retry")

MAX_ATTEMPTS = 5  # Give up after this many tries — stays 'failed' for manual review
BATCH_SIZE = 20


async def _run() -> dict:
    async with async_session_factory() as db:
        docs = (await db.execute(
            select(Document)
            .where(
                # Pending = post-ingest started but never finished (e.g. host
                # was down during initial attempt). Failed = retry-eligible.
                # Both should be picked up here; only 'ok'/'skipped' stay put.
                Document.embedding_status.in_(("failed", "pending")),
                Document.embedding_attempts < MAX_ATTEMPTS,
            )
            .limit(BATCH_SIZE)
        )).scalars().all()

        retried = 0
        recovered = 0
        for doc in docs:
            try:
                n = await generate_document_embeddings(db, doc)
                retried += 1
                if n > 0:
                    recovered += 1
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning("Retry crashed for %s: %s", doc.relative_path, e)

        return {"scanned": len(docs), "retried": retried, "recovered": recovered}


@celery_app.task(
    name="server.tasks.embedding_retry.retry_failed_embeddings",
    # No autoretry here: the beat schedule already re-fires every 15 min, and
    # autoretry caused asyncio event-loop conflicts when the retry attempt
    # queued before the previous run's engine/connection pool had finished
    # teardown ("Future attached to a different loop" / "another operation
    # is in progress"). The failed docs simply wait for the next beat tick.
    acks_late=True,
)
def retry_failed_embeddings() -> dict:
    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.warning("retry_failed_embeddings errored: %s", e)
        return {"scanned": 0, "retried": 0, "recovered": 0, "error": str(e)[:200]}
