"""Per-document post-ingest processing for durable spool finalizers."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Literal
from uuid import UUID, uuid4

import redis
from sqlalchemy import select, text

from ..config import settings
from ..db.models import Document
from ..db.session import post_ingest_engine, post_ingest_session_factory
from ..services.ingest_service import _run_post_ingest_inner
from .celery_app import celery_app

logger = logging.getLogger("post_ingest_task")

_SUPPORTED_CATEGORIES = {"conversation", "memory", "learning", "plan", "identity"}
_MIN_QUIET_SECONDS = 120
_MAX_QUIET_SECONDS = 300
POST_INGEST_CONTENTION_RETRY_SECONDS = 30
POST_INGEST_COALESCE_RECHECK_SECONDS = 5
POST_INGEST_COALESCE_TTL_SECONDS = 60 * 60
_POST_INGEST_COALESCE_PREFIX = "memento:post-ingest:coalesce:v1"
_redis_client: redis.Redis | None = None
_redis_client_pid: int | None = None

_CLAIM_COALESCED_SCHEDULE_SCRIPT = """
local current_token = redis.call('HGET', KEYS[1], 'token')
redis.call('HSET', KEYS[1], 'revision', ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
if current_token then
    return 0
end
redis.call('HSET', KEYS[1], 'token', ARGV[2])
return 1
"""

_COMPLETE_COALESCED_SCHEDULE_SCRIPT = """
local current_token = redis.call('HGET', KEYS[1], 'token')
if current_token ~= ARGV[1] then
    return 0
end
local current_revision = redis.call('HGET', KEYS[1], 'revision')
if ARGV[2] ~= '' and current_revision ~= ARGV[2] then
    return 2
end
redis.call('DEL', KEYS[1])
return 1
"""


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Actively-written transcripts otherwise re-run a full CPU-heavy BGE-M3 pass
# after every append. Keep the quiet period deliberately bounded: shorter than
# two minutes does not debounce normal agent turns, while longer than five makes
# semantic search feel broken after an agent stops.
POST_INGEST_QUIET_SECONDS = _bounded_env_int(
    "MEMENTO_POST_INGEST_QUIET_SECONDS",
    180,
    _MIN_QUIET_SECONDS,
    _MAX_QUIET_SECONDS,
)
CONVERSATION_QUIET_WINDOW_MIN_BYTES = _bounded_env_int(
    "MEMENTO_POST_INGEST_LARGE_CONVERSATION_BYTES",
    4 * 1024 * 1024,
    0,
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
    """Delay configured conversations without delaying their ingest commit."""
    if (
        category == "conversation"
        and file_size_bytes >= CONVERSATION_QUIET_WINDOW_MIN_BYTES
    ):
        return POST_INGEST_QUIET_SECONDS
    return None


def _coalesce_key(document_id: UUID | str) -> str:
    return f"{_POST_INGEST_COALESCE_PREFIX}:{document_id}"


def _get_redis_client() -> redis.Redis:
    """Return one lazy Redis client per process (safe across Celery forks)."""
    global _redis_client, _redis_client_pid
    process_id = os.getpid()
    if _redis_client is None or _redis_client_pid != process_id:
        if _redis_client is not None:
            try:
                _redis_client.close()
            except Exception:
                pass
        _redis_client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        _redis_client_pid = process_id
    return _redis_client


def _claim_coalesced_schedule(
    document_id: UUID | str,
    revision: str,
    token: str,
) -> bool:
    result = _get_redis_client().eval(
        _CLAIM_COALESCED_SCHEDULE_SCRIPT,
        1,
        _coalesce_key(document_id),
        revision,
        token,
        POST_INGEST_COALESCE_TTL_SECONDS,
    )
    return int(result) == 1


def _coalesced_token_is_current(document_id: UUID | str, token: str) -> bool:
    return _get_redis_client().hget(_coalesce_key(document_id), "token") == token


def _complete_coalesced_schedule(
    document_id: UUID | str,
    token: str,
    processed_revision: str | None,
) -> Literal["stale", "complete", "updated"]:
    result = int(
        _get_redis_client().eval(
            _COMPLETE_COALESCED_SCHEDULE_SCRIPT,
            1,
            _coalesce_key(document_id),
            token,
            processed_revision or "",
        )
    )
    return {0: "stale", 1: "complete", 2: "updated"}[result]


async def schedule_coalesced_post_ingest(
    document_id: UUID | str,
    tool_id: str,
    category: str,
    revision: str,
    *,
    countdown: int,
) -> bool:
    """Queue one quiet-window wake-up while updating its latest revision.

    Each ingest refreshes the Redis revision, but only the caller that creates
    the per-document token sends a Celery message. The one live task retries
    against ``Document.synced_at`` until the transcript is actually quiet.
    """
    token = uuid4().hex
    try:
        claimed = await asyncio.to_thread(
            _claim_coalesced_schedule,
            document_id,
            revision,
            token,
        )
    except Exception:
        # Redis is also the Celery broker. The minute recovery scanner remains
        # the durable fallback rather than creating an unbounded direct queue.
        logger.exception("Could not coalesce post-ingest for %s", document_id)
        return False
    if not claimed:
        return False

    try:
        process_document_post_ingest.apply_async(
            args=[str(document_id), tool_id, category, None, token],
            countdown=countdown,
            retry=False,
        )
    except Exception:
        try:
            await asyncio.to_thread(
                _complete_coalesced_schedule,
                document_id,
                token,
                None,
            )
        except Exception:
            logger.exception(
                "Could not release failed post-ingest claim for %s", document_id
            )
        raise
    return True


def _quiet_seconds_remaining(
    state: _DocumentState,
    *,
    now: datetime | None = None,
) -> int:
    if (
        state.category != "conversation"
        or state.file_size_bytes < CONVERSATION_QUIET_WINDOW_MIN_BYTES
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
        return {
            "status": "processed",
            "document_id": str(document_id),
            "revision": effective_revision,
        }


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
    coalesce_token: str | None = None,
) -> dict:
    del tool_id, category  # Current values are loaded after the revision fence.
    try:
        if coalesce_token:
            try:
                if not _coalesced_token_is_current(document_id, coalesce_token):
                    return {"status": "stale", "document_id": document_id}
            except Exception:
                # PostgreSQL remains authoritative if Redis is briefly
                # unavailable. The completion attempt below can still clear a
                # recovered marker; its TTL bounds any orphaned state.
                logger.warning(
                    "Could not verify post-ingest token for %s", document_id,
                    exc_info=True,
                )

        result = asyncio.run(
            _process_document_post_ingest(UUID(document_id), expected_revision)
        )
        if result["status"] == "deferred":
            raise self.retry(countdown=result["countdown"])
        if coalesce_token:
            try:
                completion = _complete_coalesced_schedule(
                    document_id,
                    coalesce_token,
                    result.get("revision") if result["status"] == "processed" else None,
                )
            except Exception:
                logger.warning(
                    "Could not complete post-ingest token for %s",
                    document_id,
                    exc_info=True,
                )
            else:
                if completion == "updated":
                    raise self.retry(countdown=POST_INGEST_COALESCE_RECHECK_SECONDS)
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
