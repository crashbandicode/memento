"""Durable finalization of large chunked uploads."""

from __future__ import annotations

import asyncio
import logging
import re
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text

from ..db.session import async_session_factory
from ..services.content_sanitizer import sanitize_content_file
from ..services.device_service import DeviceOwnershipError, ensure_device
from ..services.ingest_service import ingest_file
from ..services.large_content_store import store_large_content
from ..services.ingest_spool import (
    DEFAULT_SPOOL_ROOT,
    ChunkValidationError,
    assemble_job,
    cleanup_completion_receipts,
    cleanup_stale_incomplete_jobs,
    failed_job_ids,
    mark_job_complete,
    mark_job_failed,
    ready_job_ids,
    record_job_attempt,
    remove_job,
    spool_job_lock,
)
from .celery_app import celery_app

logger = logging.getLogger("ingest_spool")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FINALIZE_RETRIES = 12
DATABASE_CONTENT_MAX_BYTES = 64 * 1024 * 1024


async def _ingest_ready_job(job_id: str) -> dict:
    manifest, payload_path = assemble_job(job_id)
    meta = manifest["meta"]
    user_id = UUID(str(manifest["user_id"]))
    payload_bytes = payload_path.stat().st_size
    externalize_content = (
        payload_bytes > DATABASE_CONTENT_MAX_BYTES
        and meta.get("category") == "conversation"
    )
    # Device registration/heartbeat is its own short transaction. Holding the
    # machine row lock through a multi-minute transcript parse starves normal
    # heartbeat and dashboard traffic.
    async with async_session_factory() as device_db:
        machine = await ensure_device(
            device_db,
            str(manifest["device_id"]),
            str(manifest["device_name"]),
            str(manifest["device_platform"]),
            user_id=user_id,
        )
        machine_id = str(machine.id)
        await device_db.commit()

    content_s3_key = None
    content_had_sensitive = False
    ingest_path = payload_path
    if externalize_content:
        sanitized = await asyncio.to_thread(
            sanitize_content_file,
            payload_path,
            payload_path.with_name("sanitized.bin"),
        )
        ingest_path = sanitized.path
        content_had_sensitive = sanitized.had_sensitive
        content_s3_key = await asyncio.to_thread(
            store_large_content,
            ingest_path,
            user_id=str(user_id),
            device_id=str(manifest["device_id"]),
            job_id=job_id,
        )

    file_content = await asyncio.to_thread(
        ingest_path.read_text,
        encoding="utf-8",
        errors="replace",
    )
    async with async_session_factory() as db:
        # The compose-wide 120s guard is intentionally short for interactive
        # queries. A serialized, durable 270MB transcript ingest can exceed it.
        await db.execute(text("SET LOCAL statement_timeout = '25min'"))
        await db.execute(
            text("SET LOCAL idle_in_transaction_session_timeout = '25min'")
        )
        doc = await ingest_file(
            db=db,
            tool_id=meta["tool"],
            category=meta["category"],
            content_type=meta["content_type"],
            relative_path=meta["relative_path"],
            content=file_content,
            content_hash=meta["hash"],
            file_size=payload_path.stat().st_size,
            mode=meta.get("mode", "full"),
            offset=meta.get("offset", 0),
            metadata=dict(meta.get("metadata", {})),
            timestamp=meta.get("timestamp"),
            machine_id=machine_id,
            user_id=str(user_id),
            schedule_post_ingest=False,
            persist_content=not externalize_content,
            content_s3_key=content_s3_key,
            content_already_sanitized=externalize_content,
            content_had_sensitive=content_had_sensitive,
        )
        await db.commit()

    document_id = str(doc.id)
    # `asyncio.run()` closes this task's event loop on return, so the API's
    # normal in-loop background task would be cancelled. Queue one exact
    # document after commit; the periodic pending scanners remain a fallback
    # if Redis is briefly unavailable.
    try:
        from .post_ingest import process_document_post_ingest

        process_document_post_ingest.apply_async(
            args=[document_id, str(doc.tool_id), str(meta["category"])],
            retry=False,
        )
    except Exception:
        logger.exception(
            "Post-ingest follow-up could not be queued for %s", document_id
        )
    mark_job_complete(job_id, document_id=document_id)
    remove_job(job_id)
    return {
        "status": "ingested",
        "job_id": job_id,
        "document_id": document_id,
        "bytes": payload_bytes,
    }


@celery_app.task(
    bind=True,
    name="server.tasks.ingest_spool.process_spooled_ingest",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=None,
    soft_time_limit=1700,
    time_limit=1800,
)
def process_spooled_ingest(self, job_id: str) -> dict:
    """Ingest one ready job; serialize large finalizers across workers."""
    if not _JOB_ID_RE.fullmatch(job_id):
        return {"status": "invalid", "job_id": job_id}

    with spool_job_lock(
        job_id,
        root=DEFAULT_SPOOL_ROOT,
        purpose="process",
        blocking=False,
    ) as acquired:
        if not acquired:
            return {"status": "already_processing", "job_id": job_id}

        if job_id not in ready_job_ids():
            return {"status": "missing_or_incomplete", "job_id": job_id}
        attempts = record_job_attempt(job_id)
        if attempts > MAX_FINALIZE_RETRIES:
            mark_job_failed(
                job_id,
                error_type="RetryLimitExceeded",
                attempts=attempts,
            )
            return {
                "status": "quarantined",
                "job_id": job_id,
                "error_type": "RetryLimitExceeded",
                "attempts": attempts,
            }
        try:
            return asyncio.run(_ingest_ready_job(job_id))
        except Exception as exc:
            permanent = isinstance(
                exc,
                (
                    ChunkValidationError,
                    DeviceOwnershipError,
                    KeyError,
                    TypeError,
                    ValueError,
                ),
            )
            retry_limit = (
                2 if isinstance(exc, SoftTimeLimitExceeded) else MAX_FINALIZE_RETRIES
            )
            if permanent or attempts >= retry_limit:
                logger.exception(
                    "Spool ingest quarantined for %s after %s attempt(s)",
                    job_id,
                    attempts,
                )
                mark_job_failed(
                    job_id,
                    error_type=type(exc).__name__,
                    attempts=attempts,
                )
                return {
                    "status": "quarantined",
                    "job_id": job_id,
                    "error_type": type(exc).__name__,
                    "attempts": attempts,
                }
            countdown = min(2 ** min(attempts, 8), 300)
            logger.exception(
                "Spool ingest failed for %s; retrying in %ss", job_id, countdown
            )
            raise self.retry(exc=exc, countdown=countdown, max_retries=None)


@celery_app.task(
    name="server.tasks.ingest_spool.recover_spooled_ingests",
    acks_late=True,
)
def recover_spooled_ingests() -> dict:
    """Requeue durable ready jobs after API/Redis/worker restarts."""
    stale_removed = cleanup_stale_incomplete_jobs()
    receipts_removed = cleanup_completion_receipts()
    job_ids = ready_job_ids()
    failed_count = len(failed_job_ids())
    for job_id in job_ids:
        process_spooled_ingest.apply_async(args=[job_id], queue="ingest")
    return {
        "status": "queued",
        "count": len(job_ids),
        "stale_incomplete_removed": stale_removed,
        "completion_receipts_removed": receipts_removed,
        "quarantined_count": failed_count,
    }
