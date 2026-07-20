"""Durable finalization of large chunked uploads."""

from __future__ import annotations

import asyncio
import logging
import re
from copy import deepcopy
from contextlib import nullcontext
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, text

from ..db.models import Document, SyncState
from ..db.session import async_session_factory
from ..services.content_sanitizer import sanitize_content_file
from ..services.device_service import DeviceOwnershipError, ensure_device
from ..services.ingest_revision import committed_full_supersedes
from ..services.ingest_service import DeltaBaseMismatch, ingest_file
from ..services.large_content_store import store_large_content
from ..services.ingest_spool import (
    DEFAULT_SPOOL_ROOT,
    ChunkValidationError,
    assemble_job,
    blocked_job_ids,
    cleanup_completion_receipts,
    cleanup_stale_incomplete_jobs,
    complete_and_remove_job,
    failed_job_ids,
    mark_job_blocked,
    mark_job_failed,
    next_ready_source_head,
    ready_job_ids_in_recovery_order,
    ready_manifest,
    ready_job_ids,
    ready_delta_chain_job_ids,
    read_ready_job_bytes,
    record_job_attempt,
    select_ready_source_head,
    source_identity,
    spool_job_lock,
    spool_source_lock,
    try_ready_manifest_metadata,
)
from .celery_app import INGEST_RECOVERY_EXPIRES_SECONDS, celery_app

logger = logging.getLogger("ingest_spool")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_FINALIZE_RETRIES = 12
DATABASE_CONTENT_MAX_BYTES = 64 * 1024 * 1024


class RetryLimitExceeded(RuntimeError):
    """Raised when a durable job exhausted its persisted attempt budget."""


_existing_full_supersedes = committed_full_supersedes


def _preflight_full_supersedes(**revision) -> bool:
    """Skip only different-hash stale work; same hashes need the DB lock path."""
    return revision["existing_hash"] != revision["incoming_hash"] and (
        committed_full_supersedes(**revision)
    )


async def _ingest_ready_job(
    job_id: str,
    manifest: dict,
    *,
    delta_chain: tuple[tuple[str, dict], ...] = (),
) -> dict:
    payload_jobs = delta_chain or ((job_id, manifest),)
    if len(payload_jobs) > 1:
        first_meta = payload_jobs[0][1]["meta"]
        manifest = deepcopy(payload_jobs[-1][1])
        manifest["meta"] = dict(manifest["meta"])
        manifest["meta"]["base_hash"] = first_meta.get("base_hash")
        manifest["meta"]["base_offset"] = first_meta.get("base_offset")
        manifest["meta"]["file_size"] = sum(
            int(candidate["meta"]["file_size"]) for _, candidate in payload_jobs
        )
    meta = manifest["meta"]
    user_id = UUID(str(manifest["user_id"]))
    payload_bytes = int(meta["file_size"])
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

    if meta.get("mode", "full") == "full" and not meta.get(
        "authoritative_rebase", False
    ):
        async with async_session_factory() as preflight_db:
            existing = (
                await preflight_db.execute(
                    select(
                        Document.id,
                        Document.content_hash,
                        Document.source_modified_at,
                        Document.file_size_bytes,
                    ).where(
                        Document.tool_id == meta["tool"],
                        Document.relative_path == meta["relative_path"],
                        Document.machine_id == machine_id,
                    )
                )
            ).one_or_none()
            sync_state = (
                await preflight_db.execute(
                    select(SyncState.last_hash, SyncState.last_offset).where(
                        SyncState.tool_id == meta["tool"],
                        SyncState.relative_path == meta["relative_path"],
                        SyncState.machine_id == machine_id,
                    )
                )
            ).one_or_none()
        existing_offset = 0
        if (
            existing is not None
            and sync_state is not None
            and sync_state.last_hash == existing.content_hash
        ):
            existing_offset = int(sync_state.last_offset or 0)
        if existing is not None and _preflight_full_supersedes(
            existing_hash=existing.content_hash,
            existing_timestamp=existing.source_modified_at,
            existing_offset=existing_offset,
            existing_size=existing.file_size_bytes,
            incoming_hash=meta["hash"],
            incoming_timestamp=meta.get("timestamp"),
            incoming_offset=meta.get("offset", 0),
            incoming_size=payload_bytes,
        ):
            document_id = str(existing.id)
            return {
                "status": "superseded_by_committed_full",
                "job_id": job_id,
                "document_id": document_id,
                "bytes": payload_bytes,
            }

    content_s3_key = None
    content_had_sensitive = False
    if len(payload_jobs) > 1:
        payload = await asyncio.to_thread(
            lambda: b"".join(
                read_ready_job_bytes(
                    candidate_id,
                    manifest=candidate_manifest,
                )[1]
                for candidate_id, candidate_manifest in payload_jobs
            )
        )
        file_content = payload.decode("utf-8", errors="replace")
        ingested_payload_bytes = len(payload)
    else:
        manifest, payload_path = assemble_job(job_id, manifest=manifest)
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
        ingested_payload_bytes = ingest_path.stat().st_size
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
            file_size=ingested_payload_bytes,
            mode=meta.get("mode", "full"),
            offset=meta.get("offset", 0),
            base_hash=meta.get("base_hash"),
            base_offset=meta.get("base_offset"),
            authoritative_rebase=bool(meta.get("authoritative_rebase", False)),
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
    disposition = getattr(doc, "_memento_ingest_disposition", None)
    if disposition not in {"idempotent", "stale_delta", "superseded"}:
        # `asyncio.run()` closes this task's event loop on return, so the API's
        # normal in-loop background task would be cancelled. Queue one exact
        # document after commit; periodic pending scanners remain a fallback.
        try:
            from .post_ingest import (
                initial_post_ingest_countdown,
                process_document_post_ingest,
                schedule_coalesced_post_ingest,
            )

            countdown = initial_post_ingest_countdown(
                str(meta["category"]),
                int(doc.file_size_bytes),
            )
            if countdown is not None:
                await schedule_coalesced_post_ingest(
                    document_id,
                    str(doc.tool_id),
                    str(meta["category"]),
                    str(doc.content_hash),
                    countdown=countdown,
                )
            else:
                process_document_post_ingest.apply_async(
                    args=[
                        document_id,
                        str(doc.tool_id),
                        str(meta["category"]),
                        str(doc.content_hash),
                    ],
                    retry=False,
                )
        except Exception:
            logger.exception(
                "Post-ingest follow-up could not be queued for %s", document_id
            )
    return {
        "status": disposition or "ingested",
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
    """Ingest one source head; serialize source mutation and durable cleanup."""
    if not _JOB_ID_RE.fullmatch(job_id):
        return {"status": "invalid", "job_id": job_id}

    initial_manifest = try_ready_manifest_metadata(job_id)
    initial_identity = (
        source_identity(initial_manifest) if initial_manifest is not None else None
    )
    if initial_identity is not None:
        source_context = spool_source_lock(
            initial_identity,
            root=DEFAULT_SPOOL_ROOT,
            blocking=False,
        )
    else:
        source_context = nullcontext(True)

    next_job_id = None
    with source_context as source_acquired:
        if not source_acquired:
            process_spooled_ingest.apply_async(
                args=[job_id],
                queue="ingest",
                countdown=2,
            )
            return {"status": "source_busy", "job_id": job_id}

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

            manifest_error: Exception | None = None
            manifest = None
            identity = initial_identity
            cohort: tuple[str, ...] = ()
            process_current = True
            try:
                manifest = ready_manifest(job_id)
                identity = source_identity(manifest)
                if initial_identity is None:
                    next_job_id = job_id
                    result = {
                        "status": "retry_for_source_lock",
                        "job_id": job_id,
                    }
                    manifest = None
                    process_current = False
                elif identity != initial_identity:
                    raise ChunkValidationError(
                        "spool source identity changed while acquiring its lock"
                    )
                else:
                    head_job_id, cohort = select_ready_source_head(job_id)
                    if head_job_id is None:
                        result = {
                            "status": "blocked_by_failed_source_revision",
                            "job_id": job_id,
                        }
                        manifest = None
                        process_current = False
                    elif head_job_id != job_id:
                        next_job_id = head_job_id
                        result = {
                            "status": "deferred_for_source_head",
                            "job_id": job_id,
                            "source_head_job_id": head_job_id,
                        }
                        manifest = None
                        process_current = False
                    else:
                        result = None
            except Exception as exc:
                manifest_error = exc
                result = None

            if process_current:
                attempts = record_job_attempt(job_id)
                try:
                    if attempts > MAX_FINALIZE_RETRIES:
                        raise RetryLimitExceeded
                    if manifest_error is not None:
                        raise manifest_error
                    if manifest is None:
                        raise ChunkValidationError("spool manifest is unavailable")
                    delta_chain_ids = ready_delta_chain_job_ids(job_id)
                    delta_chain: list[tuple[str, dict]] = []
                    for candidate_id in delta_chain_ids:
                        try:
                            candidate_manifest = ready_manifest(candidate_id)
                        except Exception:
                            if candidate_id == job_id:
                                raise
                            break
                        delta_chain.append((candidate_id, candidate_manifest))
                    result = asyncio.run(
                        _ingest_ready_job(
                            job_id,
                            manifest,
                            delta_chain=tuple(delta_chain),
                        )
                    )
                    document_id = result["document_id"]
                    coalesced_delta_jobs = 0
                    for candidate_id, _ in delta_chain[1:]:
                        if complete_and_remove_job(
                            candidate_id,
                            document_id=document_id,
                        ):
                            coalesced_delta_jobs += 1
                    removed_cohort = 0
                    blocked_cohort = 0
                    validated_cohort = []
                    for candidate_id in cohort:
                        candidate = try_ready_manifest_metadata(candidate_id)
                        if (
                            candidate is None
                            or identity is None
                            or source_identity(candidate) != identity
                        ):
                            continue
                        validated_cohort.append((candidate_id, candidate))
                    rebase_committed = result.get("status") not in {
                        "superseded",
                        "superseded_by_committed_full",
                    }
                    for candidate_id, candidate in validated_cohort:
                        if not rebase_committed:
                            continue
                        retain_as_evidence = (
                            candidate["meta"].get("mode", "full") == "delta"
                            or (
                                DEFAULT_SPOOL_ROOT / candidate_id / "failed.json"
                            ).exists()
                        )
                        if retain_as_evidence:
                            mark_job_blocked(
                                candidate_id,
                                superseding_job_id=job_id,
                                document_id=document_id,
                            )
                            blocked_cohort += 1
                        elif candidate["meta"].get("mode", "full") == "full" and (
                            complete_and_remove_job(
                                candidate_id,
                                document_id=document_id,
                            )
                        ):
                            removed_cohort += 1
                    complete_and_remove_job(job_id, document_id=document_id)
                    result["superseded_spool_jobs"] = removed_cohort
                    result["blocked_rebase_jobs"] = blocked_cohort
                    result["coalesced_delta_jobs"] = coalesced_delta_jobs
                    if identity is not None:
                        next_job_id = next_ready_source_head(identity)
                except Exception as exc:
                    permanent = isinstance(
                        exc,
                        (
                            ChunkValidationError,
                            DeltaBaseMismatch,
                            DeviceOwnershipError,
                            KeyError,
                            RetryLimitExceeded,
                            TypeError,
                            ValueError,
                        ),
                    )
                    retry_limit = (
                        2
                        if isinstance(exc, SoftTimeLimitExceeded)
                        else MAX_FINALIZE_RETRIES
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
                        result = {
                            "status": "quarantined",
                            "job_id": job_id,
                            "error_type": type(exc).__name__,
                            "attempts": attempts,
                        }
                        if identity is not None:
                            next_job_id = next_ready_source_head(identity)
                    else:
                        countdown = min(2 ** min(attempts, 8), 300)
                        logger.exception(
                            "Spool ingest failed for %s; retrying in %ss",
                            job_id,
                            countdown,
                        )
                        raise self.retry(
                            exc=exc,
                            countdown=countdown,
                            max_retries=None,
                        )

    if next_job_id is not None:
        process_spooled_ingest.apply_async(
            args=[next_job_id],
            queue="ingest",
            countdown=1,
        )
    return result


@celery_app.task(
    name="server.tasks.ingest_spool.recover_spooled_ingests",
    acks_late=True,
)
def recover_spooled_ingests() -> dict:
    """Requeue durable ready jobs after API/Redis/worker restarts."""
    stale_removed = cleanup_stale_incomplete_jobs()
    receipts_removed = cleanup_completion_receipts()
    job_ids = ready_job_ids_in_recovery_order()
    failed_count = len(failed_job_ids())
    blocked_count = len(blocked_job_ids())
    for job_id in job_ids:
        process_spooled_ingest.apply_async(
            args=[job_id],
            queue="ingest",
            expires=INGEST_RECOVERY_EXPIRES_SECONDS,
        )
    return {
        "status": "queued",
        "count": len(job_ids),
        "stale_incomplete_removed": stale_removed,
        "completion_receipts_removed": receipts_removed,
        "quarantined_count": failed_count,
        "blocked_count": blocked_count,
    }
