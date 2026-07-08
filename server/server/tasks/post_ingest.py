"""Per-document post-ingest processing for durable spool finalizers."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from uuid import UUID

from sqlalchemy import select, text

from ..db.models import Document
from ..db.session import post_ingest_engine, post_ingest_session_factory
from ..services.ingest_service import _run_post_ingest_inner
from .celery_app import celery_app

logger = logging.getLogger("post_ingest_task")

_SUPPORTED_CATEGORIES = {"conversation", "memory", "learning", "plan", "identity"}
_MIN_QUIET_SECONDS = 120
_MAX_QUIET_SECONDS = 300
POST_INGEST_CONTENTION_RETRY_SECONDS = 30


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Large, actively-written transcripts otherwise re-run a full CPU-heavy BGE-M3
# pass after every append. Keep the setting deliberately bounded: shorter than
# two minutes does not debounce normal agent turns, while longer than five makes
# semantic search feel broken after an agent stops.
POST_INGEST_QUIET_SECONDS = _bounded_env_int(
    "MEMENTO_POST_INGEST_QUIET_SECONDS",
    180,
    _MIN_QUIET_SECONDS,
    _MAX_QUIET_SECONDS,
)
LARGE_CONVERSATION_BYTES = _bounded_env_int(
    "MEMENTO_POST_INGEST_LARGE_CONVERSATION_BYTES",
    4 * 1024 * 1024,
    1024 * 1024,
    1024 * 1024 * 1024,
)


@dataclass(frozen=True)
class _DocumentState:
    tool_id: str
    category: str
    content_hash: str
    file_size_bytes: int
    synced_at: datetime | None
    embedding_status: str
    knowledge_status: str


def initial_post_ingest_countdown(category: str, file_size_bytes: int) -> int | None:
    """Delay obviously-large conversations without delaying their ingest commit."""
    if category == "conversation" and file_size_bytes >= LARGE_CONVERSATION_BYTES:
        return POST_INGEST_QUIET_SECONDS
    return None


def _quiet_seconds_remaining(
    state: _DocumentState,
    *,
    now: datetime | None = None,
) -> int:
    if (
        state.category != "conversation"
        or state.file_size_bytes < LARGE_CONVERSATION_BYTES
        or state.synced_at is None
    ):
        return 0

    current_time = now or datetime.now(timezone.utc)
    synced_at = state.synced_at
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (current_time - synced_at).total_seconds())
    return max(0, ceil(POST_INGEST_QUIET_SECONDS - age_seconds))


async def _load_document_state(document_id: UUID) -> _DocumentState | None:
    async with post_ingest_session_factory() as db:
        row = (
            await db.execute(
                select(
                    Document.tool_id,
                    Document.category,
                    Document.content_hash,
                    Document.file_size_bytes,
                    Document.synced_at,
                    Document.embedding_status,
                    Document.knowledge_status,
                ).where(Document.id == document_id)
            )
        ).one_or_none()
    return _DocumentState(*row) if row is not None else None


@asynccontextmanager
async def _document_post_ingest_lock(document_id: UUID):
    """Serialize duplicate deliveries without adding persistent lock state."""
    # The session-level advisory lock must keep one connection open while a
    # model call runs. Use the isolated post-ingest pool so long inference can
    # never consume a user-facing API connection.
    async with post_ingest_engine.connect() as connection:
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:id, 0))"),
                {"id": str(document_id)},
            )
        )
        await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:id, 0))"),
                    {"id": str(document_id)},
                )
                await connection.commit()


async def _process_document_post_ingest(
    document_id: UUID,
    expected_revision: str | None,
) -> dict:
    async with _document_post_ingest_lock(document_id) as acquired:
        if not acquired:
            return {
                "status": "deferred",
                "document_id": str(document_id),
                "countdown": POST_INGEST_CONTENTION_RETRY_SECONDS,
                "reason": "locked",
            }

        state = await _load_document_state(document_id)
        if state is None:
            return {"status": "missing", "document_id": str(document_id)}
        if state.category not in _SUPPORTED_CATEGORIES:
            return {"status": "skipped", "document_id": str(document_id)}

        # The task payload names the exact committed revision that caused it.
        # Never let a delayed delivery start work for that old snapshot after a
        # newer sync has replaced it; the newer task (or minute scanner) owns it.
        if expected_revision and state.content_hash != expected_revision:
            return {
                "status": "superseded",
                "document_id": str(document_id),
                "current_revision": state.content_hash,
            }
        # Old queued tasks have only three arguments. Fence those deliveries to
        # the revision observed by this preflight so a concurrent ingest cannot
        # make a legacy task bypass the new revision's quiet window.
        effective_revision = expected_revision or state.content_hash

        # Graph extraction has its own content-hash idempotence check. Do not
        # skip solely because status is already OK: the raw revision can change
        # outside the bounded embedding window while graph input still changes.
        if state.embedding_status == "processing":
            return {
                "status": "deferred",
                "document_id": str(document_id),
                "countdown": POST_INGEST_CONTENTION_RETRY_SECONDS,
                "reason": "embedding_processing",
            }

        quiet_seconds = _quiet_seconds_remaining(state)
        if quiet_seconds:
            return {
                "status": "deferred",
                "document_id": str(document_id),
                "countdown": quiet_seconds,
            }

        await _run_post_ingest_inner(
            document_id,
            state.tool_id,
            state.category,
            effective_revision,
        )
        return {"status": "processed", "document_id": str(document_id)}


@celery_app.task(
    bind=True,
    name="server.tasks.post_ingest.process_document_post_ingest",
    # A lost task is safe to leave pending: the minute embedding/knowledge
    # scanners recover it. Early acknowledgement avoids duplicate graph work
    # if a worker dies after completing the DB writes but before ACKing.
    acks_late=False,
    max_retries=None,
    # A whole-document BGE-M3 request may legitimately take up to 20 minutes
    # under the shared-host CPU cap. Keep a margin beyond its client deadline
    # for database writes and knowledge extraction while bounding a wedged task.
    time_limit=1500,
)
def process_document_post_ingest(
    self,
    document_id: str,
    tool_id: str,
    category: str,
    expected_revision: str | None = None,
) -> dict:
    del tool_id, category  # Current values are loaded after the revision fence.
    try:
        result = asyncio.run(
            _process_document_post_ingest(UUID(document_id), expected_revision)
        )
        if result["status"] == "deferred":
            raise self.retry(countdown=result["countdown"])
        return result
    except Exception as exc:
        # Celery's Retry is intentionally raised back to the worker so the
        # replacement message is recorded instead of being reported as failed.
        from celery.exceptions import Retry

        if isinstance(exc, Retry):
            raise
        logger.exception("Post-ingest task failed for %s", document_id)
        return {
            "status": "failed",
            "document_id": document_id,
            "error_type": type(exc).__name__,
        }
