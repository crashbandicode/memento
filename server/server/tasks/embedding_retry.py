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
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, text

from ..db.models import Document
from ..db.session import async_session_factory, engine
from ..services.embedding_service import (
    EMBEDDING_PROCESSING_STALE_AFTER,
    generate_document_embeddings,
)
from .celery_app import celery_app
from .post_ingest import (
    CONVERSATION_QUIET_WINDOW_MIN_BYTES,
    POST_INGEST_QUIET_SECONDS,
)

logger = logging.getLogger("embedding_retry")

MAX_ATTEMPTS = 5  # Give up after this many tries — stays 'failed' for manual review
BATCH_SIZE = 1
RETRY_LOCK_KEY = 0x4D454D454D424544


async def _run_locked() -> dict:
    seen_ids = set()
    scanned = 0
    retried = 0
    recovered = 0
    for _ in range(BATCH_SIZE):
        stale_before = (
            datetime.now(timezone.utc) - EMBEDDING_PROCESSING_STALE_AFTER
        )
        quiet_before = datetime.now(timezone.utc) - timedelta(
            seconds=POST_INGEST_QUIET_SECONDS
        )
        async with async_session_factory() as db:
            statement = (
                select(Document)
                .where(
                    or_(
                        Document.embedding_status.in_(("failed", "pending")),
                        and_(
                            Document.embedding_status == "processing",
                            or_(
                                Document.embedding_claimed_at.is_(None),
                                Document.embedding_claimed_at < stale_before,
                            ),
                        ),
                    ),
                    Document.embedding_attempts < MAX_ATTEMPTS,
                    # Do not let the minute fallback scanner bypass the
                    # post-ingest debounce for a transcript that is still
                    # growing. It becomes eligible automatically after the
                    # same quiet window, preserving eventual recovery.
                    or_(
                        Document.category != "conversation",
                        Document.file_size_bytes
                        < CONVERSATION_QUIET_WINDOW_MIN_BYTES,
                        Document.synced_at.is_(None),
                        Document.synced_at <= quiet_before,
                    ),
                )
                .order_by(Document.updated_at, Document.id)
                .limit(1)
            )
            if seen_ids:
                statement = statement.where(Document.id.notin_(seen_ids))
            doc = (await db.execute(statement)).scalar_one_or_none()
            if doc is None:
                break
            seen_ids.add(doc.id)
            scanned += 1
            try:
                n = await generate_document_embeddings(db, doc)
                retried += 1
                if n > 0:
                    recovered += 1
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning("Retry crashed for %s: %s", doc.relative_path, e)

    return {"scanned": scanned, "retried": retried, "recovered": recovered}


async def _run() -> dict:
    # A retry can legitimately run longer than the one-minute beat interval.
    # Hold a PostgreSQL session-level lock (outside a transaction) so later
    # ticks exit instead of materializing stale Documents or racing status.
    async with engine.connect() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": RETRY_LOCK_KEY},
        )
        await lock_connection.commit()
        if not acquired:
            return {"scanned": 0, "retried": 0, "recovered": 0, "locked": True}
        try:
            return await _run_locked()
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": RETRY_LOCK_KEY},
            )
            await lock_connection.commit()


@celery_app.task(
    name="server.tasks.embedding_retry.retry_failed_embeddings",
    # No autoretry here: the beat schedule already re-fires every minute, and
    # autoretry caused asyncio event-loop conflicts when the retry attempt
    # queued before the previous run's engine/connection pool had finished
    # teardown ("Future attached to a different loop" / "another operation
    # is in progress"). The failed docs simply wait for the next beat tick.
    acks_late=True,
    # One sweep handles exactly one document; align with the capped-host
    # 1200-second HTTP deadline plus database cleanup margin instead of
    # inheriting the global 600-second limit.
    time_limit=1500,
)
def retry_failed_embeddings() -> dict:
    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.warning("retry_failed_embeddings errored: %s", e)
        return {"scanned": 0, "retried": 0, "recovered": 0, "error": str(e)[:200]}
