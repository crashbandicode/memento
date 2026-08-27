"""Ingest API — receives files from collectors on multiple devices."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.models import Document, User
from ..db.session import get_db
from ..middleware.auth import verify_collector_token
from ..services.content_sanitizer import sanitize_content_file
from ..services.conversation_stream import ConversationFileSource
from ..services.device_service import ensure_device
from ..services.document_delivery import (
    delivery_metadata_expression,
    delivery_revision_expression,
    outerjoin_document_delivery,
)
from ..services.conversation_metadata_inbox import (
    defer_conversation_metadata,
    normalized_metadata_session_id,
    resolve_metadata_relative_path,
)
from ..services.ingest_service import (
    STORED_SOURCE_REVISION_KEY,
    DeltaBaseMismatch,
    _get_ingest_semaphore,
    committed_delta_base_for_source,
    ingest_file,
    raw_realtime_writer_enabled,
)
from ..services.ingest_spool import (
    MAX_CHUNK_BYTES,
    ChunkValidationError,
    TerminalSpoolJobError,
    chunk_commit_status,
    has_completion_receipt,
    pending_source_revision_job_id,
    receipt_commit_status,
    stage_delta_payload,
    stage_chunk,
)
from ..services.large_content_store import (
    DATABASE_CONTENT_MAX_BYTES,
    multipart_content_job_id,
    store_large_content,
)
from ..services.orchestration_events import ingest_orchestration_events
from ..services.thread_metadata_service import (
    apply_codex_thread_title_update,
    apply_conversation_activity_update,
    apply_conversation_interaction_update,
)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
logger = logging.getLogger("ingest")
_UPLOAD_STREAM_CHUNK_BYTES = 64 * 1024


async def _stream_upload_to_path(content: UploadFile, target: Path) -> int:
    """Copy one multipart body with fixed-size reads and durable local writes."""
    total = 0
    with target.open("wb", buffering=0) as output:
        while chunk := await content.read(_UPLOAD_STREAM_CHUNK_BYTES):
            output.write(chunk)
            total += len(chunk)
        output.flush()
        os.fsync(output.fileno())
    return total


async def throttle_ingest():
    """Cap concurrent ingest endpoint handlers at 16 (see _get_ingest_semaphore).
    Collector storms beyond that get queued at the semaphore, NOT at the
    DB connection pool, so login / dashboard / search keep their own slots."""
    sem = _get_ingest_semaphore()
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


class IngestFileRequest(BaseModel):
    tool: str
    category: str
    content_type: str
    relative_path: str
    hash: str
    mode: str = "full"
    offset: int = 0
    file_size: int = 0
    sync_strategy: str = "full"
    base_hash: str | None = None
    base_offset: int | None = Field(default=None, ge=0)
    metadata: dict = {}
    timestamp: float | None = None
    authoritative_rebase: bool = Field(default=False, strict=True)
    content: str = ""


class IngestResponse(BaseModel):
    status: str = "ok"
    document_id: str
    message: str = ""
    receipt_id: str | None = None


class IngestChunkStatusRequest(BaseModel):
    upload_id: str = Field(min_length=1, max_length=8192)
    hash: str = Field(min_length=1, max_length=64)


class IngestChunkStatusResponse(BaseModel):
    job_id: str
    status: Literal["completed", "pending", "receiving", "failed", "blocked", "missing"]
    error_type: str | None = None


class IngestReceiptStatusRequest(BaseModel):
    receipt_id: str = Field(min_length=64, max_length=64)


class IngestReceiptStatusResponse(BaseModel):
    receipt_id: str
    status: Literal["accepted", "committed", "failed", "blocked", "receiving", "missing"]
    error_type: str | None = None


class IngestMetadataRequest(BaseModel):
    metadata_type: Literal[
        "codex_thread_title",
        "conversation_activity",
        "conversation_interaction",
    ]
    tool: Literal["codex", "claude_code", "cursor"]
    thread_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    title_kind: Literal["custom", "fallback", "unknown"] = "unknown"
    revision: int | None = Field(default=None, gt=0, le=2**63 - 1)
    relative_path: str | None = Field(default=None, max_length=2000)
    # Cursor can expose opaque composer keys such as ``task-<uuid>``. This is
    # transport data only; database routing always uses the verified value
    # produced by ``normalized_metadata_session_id`` below.
    session_id: str | None = Field(default=None, min_length=1, max_length=512)
    interaction_id: str | None = Field(default=None, min_length=1, max_length=512)
    interaction_status: Literal["pending", "answered", "cancelled"] | None = None
    question_tool: str = Field(default="", max_length=256)
    interaction_input: object = Field(default_factory=dict)
    interaction_response: dict | None = None
    # Optional v1 Claude hook provenance. It remains opaque at the request
    # boundary; the metadata service validates and stores its bounded shape.
    interaction_origin: dict | None = None
    activity_id: str | None = Field(default=None, min_length=1, max_length=512)
    activity_status: Literal[
        "running",
        "completed",
        "failed",
        "cancelled",
    ] | None = None
    activity_tool: str = Field(default="", max_length=256)
    command: object = ""
    timestamp: str = Field(default="", max_length=128)


class IngestMetadataResponse(BaseModel):
    status: Literal["ok", "deferred", "ignored"]
    matched: int
    updated: int
    ignored: int


class OrchestrationLifecycleEventRequest(BaseModel):
    """Bounded, content-free lifecycle record emitted by Claw."""

    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    installation_id: str = Field(min_length=1, max_length=128)
    orchestrator: Literal["claw-orchestrator"]
    orchestrator_version: str = Field(min_length=1, max_length=64)
    event: Literal[
        "run.started",
        "run.status",
        "agent.declared",
        "agent.identity_bound",
        "agent.status",
    ]
    run_id: str = Field(min_length=8, max_length=256)
    run_kind: Literal[
        "session",
        "fanout",
        "council",
        "autoloop",
        "ultraplan",
        "ultrareview",
    ]
    run_status: Literal["running", "completed", "failed", "aborted"] | None = None
    agent_key: str | None = Field(default=None, max_length=256)
    agent_name: str | None = Field(default=None, max_length=256)
    codename: str | None = Field(default=None, max_length=256)
    engine: Literal["claude", "codex", "codex-app", "cursor"] | None = None
    model: str | None = Field(default=None, max_length=256)
    effort: str | None = Field(default=None, max_length=64)
    cwd: str | None = Field(default=None, max_length=4096)
    native_session_id: str | None = Field(default=None, max_length=512)
    agent_status: Literal[
        "declared",
        "idle",
        "running",
        "completed",
        "failed",
        "aborted",
    ] | None = None


class OrchestrationEventBatchRequest(BaseModel):
    events: list[OrchestrationLifecycleEventRequest] = Field(
        min_length=1,
        max_length=500,
    )


class OrchestrationEventBatchResponse(BaseModel):
    accepted: int
    duplicates: int
    linked: int


async def _completed_upload_needs_reprocessing(
    db: AsyncSession,
    *,
    machine_id: UUID,
    meta: dict,
) -> bool:
    """Check whether a receipt predates the database's current source proof."""
    tool_id = meta.get("tool")
    relative_path = meta.get("relative_path")
    expected_hash = meta.get("hash")
    if not all(
        isinstance(value, str) and value
        for value in (
            tool_id,
            relative_path,
            expected_hash,
        )
    ):
        return False

    statement = select(
        delivery_revision_expression(joined=True).label("content_hash"),
        delivery_metadata_expression(joined=True).label("metadata_"),
    ).select_from(Document).where(
        Document.machine_id == machine_id,
        Document.tool_id == tool_id,
        Document.relative_path == relative_path,
    )
    row = (
        await db.execute(outerjoin_document_delivery(statement))
    ).one_or_none()
    if row is None or row.content_hash != expected_hash:
        return True
    if meta.get("category") != "conversation" or meta.get("mode", "full") != "full":
        return False
    stored_metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    return stored_metadata.get(STORED_SOURCE_REVISION_KEY) != expected_hash


def _reject_synthetic_metadata_file_upload(
    *,
    category: object,
    mode: object,
    sync_strategy: object,
    relative_path: object,
) -> None:
    """Keep metadata queue records out of legacy content-ingest endpoints."""
    category_value = str(category or "").strip().lower()
    mode_value = str(mode or "").strip().lower()
    strategy_value = str(sync_strategy or "").strip().lower()
    path_value = str(relative_path or "").replace("\\", "/").lstrip("/").casefold()
    if (
        category_value == "metadata"
        or mode_value == "metadata"
        or strategy_value == "metadata"
        or path_value.startswith("__metadata__/")
    ):
        raise HTTPException(
            status_code=400,
            detail="metadata updates must use /api/ingest/metadata",
        )


def _validated_authoritative_rebase(meta: dict) -> bool:
    value = meta.get("authoritative_rebase", False)
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=400,
            detail="authoritative_rebase must be a boolean",
        )
    if value and meta.get("mode", "full") != "full":
        raise HTTPException(
            status_code=400,
            detail="only a full snapshot can be an authoritative rebase",
        )
    return value


async def _ingest_with_delta_guard(**kwargs):
    try:
        return await ingest_file(**kwargs)
    except DeltaBaseMismatch as exc:
        raise _delta_mismatch_response(exc) from exc


def _delta_mismatch_response(exc: DeltaBaseMismatch) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "delta_base_mismatch",
            "expected_hash": exc.expected_hash,
            "expected_offset": exc.expected_offset,
        },
    )


async def _terminal_spool_job_response(
    db: AsyncSession,
    *,
    machine_id: str,
    user_id: str,
    meta: dict,
    error: TerminalSpoolJobError,
) -> HTTPException:
    """Translate retained terminal staging state into a collector action."""

    if meta.get("mode") == "delta":
        expected_hash, expected_offset = await committed_delta_base_for_source(
            db,
            tool_id=str(meta.get("tool") or ""),
            relative_path=str(meta.get("relative_path") or ""),
            machine_id=machine_id,
            user_id=user_id,
        )
        return _delta_mismatch_response(
            DeltaBaseMismatch(
                expected_hash=expected_hash,
                expected_offset=expected_offset,
            )
        )
    return HTTPException(
        status_code=409,
        detail={
            "code": "spool_job_terminal",
            "reason": error.blocked_reason or error.error_type or "terminal",
        },
    )


async def _enqueue_spool_job(job_id: str) -> None:
    """Best-effort Celery acceleration for an already durable spool job."""
    try:
        from ..tasks.ingest_spool import process_spooled_ingest

        await asyncio.to_thread(
            process_spooled_ingest.apply_async,
            args=[job_id],
            queue="ingest",
            retry=False,
        )
    except Exception:
        logger.exception(
            "Ready spool job %s could not be queued; recovery will retry",
            job_id,
        )


async def _stage_delta_behind_pending_revision(
    *,
    meta: dict,
    content_bytes: bytes | None = None,
    content_path: Path | None = None,
    user_id: str,
    device_id: str,
    device_name: str,
    device_platform: str,
    supports_commit_receipts: bool,
) -> IngestResponse | None:
    """Durably queue a delta whose exact base is awaiting DB commit.

    Returning ``None`` means the mismatch is genuine and the collector should
    perform its normal full-resync recovery.  A matching durable predecessor
    keeps the fast synchronous path unchanged while preventing an active large
    transcript from repeatedly uploading complete snapshots.
    """
    if (content_bytes is None) == (content_path is None):
        raise ValueError("exactly one delta payload source is required")
    # A 2xx queued response is unsafe for collectors that cannot distinguish
    # ACCEPTED from COMMITTED. Let those callers take the normal 409/FULL path.
    if not supports_commit_receipts:
        return None
    payload_size = (
        len(content_bytes)
        if content_bytes is not None
        else content_path.stat().st_size
    )
    base_hash = meta.get("base_hash")
    base_offset = meta.get("base_offset")
    if (
        meta.get("mode", "full") != "delta"
        or not isinstance(base_hash, str)
        or not base_hash
        or not isinstance(base_offset, int)
        or isinstance(base_offset, bool)
        or payload_size <= 0
    ):
        return None

    pending_job_id = await asyncio.to_thread(
        pending_source_revision_job_id,
        user_id=user_id,
        device_id=device_id,
        tool=str(meta.get("tool", "")),
        relative_path=str(meta.get("relative_path", "")),
        content_hash=base_hash,
        offset=base_offset,
    )
    if pending_job_id is None:
        return None

    content_hash = str(meta.get("hash", ""))
    spool_meta = {
        **meta,
        "mode": "delta",
        "file_size": payload_size,
        "upload_id": f"deferred-delta/{base_hash}/{content_hash}",
    }
    total_chunks = (payload_size + MAX_CHUNK_BYTES - 1) // MAX_CHUNK_BYTES
    staged = None
    source = content_path.open("rb", buffering=0) if content_path is not None else None
    try:
        for chunk_index in range(total_chunks):
            if content_bytes is not None:
                start = chunk_index * MAX_CHUNK_BYTES
                chunk_data = content_bytes[start : start + MAX_CHUNK_BYTES]
            else:
                assert source is not None
                chunk_data = await asyncio.to_thread(
                    source.read,
                    MAX_CHUNK_BYTES,
                )
            staged = await asyncio.to_thread(
                stage_chunk,
                meta={
                    **spool_meta,
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                },
                chunk_data=chunk_data,
                user_id=user_id,
                device_id=device_id,
                device_name=device_name,
                device_platform=device_platform,
            )
            if staged.complete and not staged.should_enqueue:
                break
    finally:
        if source is not None:
            source.close()

    if staged is None or not staged.complete:
        raise RuntimeError("dependent delta was not durably staged")
    if staged.should_enqueue:
        await _enqueue_spool_job(staged.job_id)
        status = "accepted"
        message = f"Delta accepted behind pending revision {pending_job_id}"
    else:
        status = "committed"
        message = "Delta was already durably ingested"
    return IngestResponse(
        document_id=f"{status}:{staged.job_id}",
        status=status,
        receipt_id=staged.job_id,
        message=message,
    )


async def _ingest_or_stage_dependent_delta(
    *,
    db: AsyncSession,
    ingest_kwargs: dict,
    spool_meta: dict,
    content_bytes: bytes | None = None,
    content_path: Path | None = None,
    user_id: str,
    device_id: str,
    device_name: str,
    device_platform: str,
    success_message: str,
    supports_commit_receipts: bool = False,
) -> IngestResponse:
    try:
        doc = await ingest_file(**ingest_kwargs)
    except DeltaBaseMismatch:
        # Ensure the validated device exists before a durable acknowledgement
        # can let the background worker race ahead of this request.
        await db.commit()
        queued = await _stage_delta_behind_pending_revision(
            meta=spool_meta,
            content_bytes=content_bytes,
            content_path=content_path,
            user_id=user_id,
            device_id=device_id,
            device_name=device_name,
            device_platform=device_platform,
            supports_commit_receipts=supports_commit_receipts,
        )
        if queued is not None:
            return queued
        # The predecessor may have committed between the first database read
        # and the durable queue lookup. Retry once before declaring a genuine
        # conflict so that this narrow race cannot trigger another FULL upload.
        try:
            doc = await ingest_file(**ingest_kwargs)
        except DeltaBaseMismatch as retry_exc:
            raise _delta_mismatch_response(retry_exc) from retry_exc
    return IngestResponse(document_id=str(doc.id), message=success_message)


@router.post("/metadata", response_model=IngestMetadataResponse)
async def ingest_metadata_endpoint(
    req: IngestMetadataRequest,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> IngestMetadataResponse:
    """Apply trusted lightweight source state without ingesting file content."""
    machine = await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=_collector_user.id,
    )
    relative_path = req.relative_path or ""
    routing_session_id = normalized_metadata_session_id(
        req.tool,
        relative_path,
        req.session_id,
    )
    if relative_path:
        relative_path = await resolve_metadata_relative_path(
            db,
            machine_id=machine.id,
            user_id=_collector_user.id,
            tool_id=req.tool,
            relative_path=relative_path,
            session_id=routing_session_id,
        )
    if req.metadata_type == "codex_thread_title":
        if (
            req.tool != "codex"
            or req.thread_id is None
            or req.title is None
            or req.revision is None
        ):
            raise HTTPException(status_code=422, detail="invalid Codex title update")
        result = await apply_codex_thread_title_update(
            db,
            machine_id=machine.id,
            thread_id=req.thread_id,
            title=req.title,
            title_kind=req.title_kind,
            revision=req.revision,
            relative_path=relative_path or None,
            user_id=_collector_user.id,
        )
    elif req.metadata_type == "conversation_interaction":
        if (
            req.relative_path is None
            or req.interaction_id is None
            or req.interaction_status is None
        ):
            raise HTTPException(
                status_code=422,
                detail="invalid conversation interaction update",
            )
        result = await apply_conversation_interaction_update(
            db,
            machine_id=machine.id,
            user_id=_collector_user.id,
            tool_id=req.tool,
            relative_path=relative_path,
            interaction_id=req.interaction_id,
            interaction_status=req.interaction_status,
            question_tool=req.question_tool,
            interaction_input=req.interaction_input,
            interaction_response=req.interaction_response,
            interaction_origin=req.interaction_origin,
            timestamp=req.timestamp,
        )
    else:
        if (
            req.relative_path is None
            or req.activity_id is None
            or req.activity_status is None
        ):
            raise HTTPException(
                status_code=422,
                detail="invalid conversation activity update",
            )
        result = await apply_conversation_activity_update(
            db,
            machine_id=machine.id,
            user_id=_collector_user.id,
            tool_id=req.tool,
            relative_path=relative_path,
            activity_id=req.activity_id,
            activity_status=req.activity_status,
            activity_tool=req.activity_tool,
            command=req.command,
            timestamp=req.timestamp,
        )
    if result.valid and result.matched == 0:
        # Metadata has its own durable ordering boundary. A content upload may
        # still be in flight, or a relocatable Cursor thread may not have
        # established its canonical path yet. Persist the latest signal once
        # instead of making every collector retry it forever.
        payload = req.model_dump(mode="json")
        payload["relative_path"] = req.relative_path
        payload["session_id"] = routing_session_id
        deferred = await defer_conversation_metadata(
            db,
            machine_id=machine.id,
            user_id=_collector_user.id,
            payload=payload,
        )
        if deferred:
            return IngestMetadataResponse(
                status="deferred",
                matched=0,
                updated=0,
                ignored=0,
            )
    return IngestMetadataResponse(
        status="ok" if result.valid else "ignored",
        matched=result.matched,
        updated=result.updated,
        ignored=result.ignored,
    )


@router.post(
    "/orchestration-events",
    response_model=OrchestrationEventBatchResponse,
)
async def ingest_orchestration_event_batch(
    req: OrchestrationEventBatchRequest,
    collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> OrchestrationEventBatchResponse:
    """Durably ingest Claw lifecycle metadata for this collector machine."""
    machine = await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=collector_user.id,
    )
    result = await ingest_orchestration_events(
        db,
        machine_id=machine.id,
        user_id=collector_user.id,
        events=(event.model_dump() for event in req.events),
    )
    return OrchestrationEventBatchResponse(**result)


_ASYNC_DELTA_CAPABILITY = "realtime_ingest_async_admission_v1"


def _collector_supports_async_delta_admission(capabilities: str | None) -> bool:
    return _ASYNC_DELTA_CAPABILITY in {
        capability.strip()
        for capability in str(capabilities or "").split(",")
        if capability.strip()
    }


def _admission_identity(*, meta: dict, user_id: str, device_id: str) -> str:
    """Stable source/revision proof excluding the mutable payload bytes."""
    envelope = {
        "user_id": user_id, "device_id": device_id, "tool": meta.get("tool"),
        "category": meta.get("category"), "content_type": meta.get("content_type"),
        "relative_path": meta.get("relative_path"), "mode": meta.get("mode"),
        "hash": meta.get("hash"), "offset": meta.get("offset"),
        "base_hash": meta.get("base_hash"), "base_offset": meta.get("base_offset"),
        "timestamp": meta.get("timestamp"), "metadata": meta.get("metadata", {}),
    }
    encoded = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(b"memento:admission-envelope:v1\0" + encoded).hexdigest()


async def _admit_realtime_delta(
    *,
    db: AsyncSession,
    machine_id: str,
    meta: dict,
    payload: bytes,
    user_id: str,
    device_id: str,
    device_name: str,
    device_platform: str,
) -> IngestResponse:
    """Fsync a capability-negotiated guarded DELTA and return ACCEPTED only."""
    if not (
        meta.get("category") == "conversation"
        and meta.get("content_type") == "jsonl"
        and meta.get("mode") == "delta"
        and isinstance(meta.get("base_hash"), str)
        and meta.get("base_hash")
        and isinstance(meta.get("base_offset"), int)
        and not isinstance(meta.get("base_offset"), bool)
    ):
        raise HTTPException(
            status_code=422,
            detail="realtime admission requires a guarded conversation DELTA",
        )
    admission_identity = _admission_identity(
        meta=meta, user_id=user_id, device_id=device_id,
    )
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    delivery_identity = hashlib.sha256(
        (
            "memento:delivery-identity:v1\0"
            + admission_identity + "\0" + payload_sha256
        ).encode("ascii")
    ).hexdigest()
    try:
        staged = await asyncio.to_thread(
            stage_delta_payload,
            meta={
                **meta,
                "upload_id": f"realtime/{delivery_identity}",
                "admission_identity": admission_identity,
                "delivery_identity": delivery_identity,
                "payload_sha256": payload_sha256,
                "realtime_admission": True,
                "file_size": len(payload),
            },
            payload=payload,
            user_id=user_id,
            device_id=device_id,
            device_name=device_name,
            device_platform=device_platform,
        )
    except TerminalSpoolJobError as exc:
        raise await _terminal_spool_job_response(
            db,
            machine_id=machine_id,
            user_id=user_id,
            meta=meta,
            error=exc,
        ) from exc
    except ChunkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    status = "committed" if not staged.should_enqueue else "accepted"
    return IngestResponse(
        status=status,
        document_id=f"{status}:{staged.job_id}",
        receipt_id=staged.job_id,
        message=(
            "Delta database transaction already committed"
            if status == "committed"
            else "Delta payload fsynced; awaiting database commit receipt"
        ),
    )


@router.post("/file", response_model=IngestResponse)
async def ingest_file_endpoint(
    req: IngestFileRequest,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
    x_collector_capabilities: str | None = Header(default=None),
) -> IngestResponse:
    """Ingest a file from the collector (JSON payload, for files < 1MB)."""
    _reject_synthetic_metadata_file_upload(
        category=req.category,
        mode=req.mode,
        sync_strategy=req.sync_strategy,
        relative_path=req.relative_path,
    )
    machine = await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=_collector_user.id,
    )
    measured_size = len(req.content.encode("utf-8"))
    raw_enabled = raw_realtime_writer_enabled(
        owner_id=str(_collector_user.id),
        device_id=x_device_id,
        tool_id=req.tool,
        category=req.category,
    )
    if (
        settings.realtime_ingest_spool_deltas
        and raw_enabled
        and _collector_supports_async_delta_admission(x_collector_capabilities)
        and req.category == "conversation"
        and req.content_type == "jsonl"
        and req.mode == "delta"
    ):
        # Device ownership is committed before ACCEPTED can outlive this HTTP
        # request.  The actual source fence remains in the drain's raw tx.
        await db.commit()
        return await _admit_realtime_delta(
            db=db,
            machine_id=str(machine.id),
            meta=req.model_dump(exclude={"content"}),
            payload=req.content.encode("utf-8"),
            user_id=str(_collector_user.id),
            device_id=x_device_id,
            device_name=x_device_name,
            device_platform=x_device_platform,
        )
    writer = None
    if raw_enabled:
        # Device creation is deliberately outside the raw ingest transaction.
        # Commit it before acquiring the asyncpg connection so the raw FK and
        # authenticated owner scope are both visible on first contact.
        await db.commit()
        writer = "raw"

    return await _ingest_or_stage_dependent_delta(
        db=db,
        ingest_kwargs={
            "db": db,
            "tool_id": req.tool,
            "category": req.category,
            "content_type": req.content_type,
            "relative_path": req.relative_path,
            "content": req.content,
            "content_hash": req.hash,
            "file_size": max(max(0, int(req.file_size or 0)), measured_size),
            "mode": req.mode,
            "offset": req.offset,
            "metadata": req.metadata,
            "timestamp": req.timestamp,
            "machine_id": str(machine.id),
            "user_id": str(_collector_user.id),
            "base_hash": req.base_hash,
            "base_offset": req.base_offset,
            "authoritative_rebase": req.authoritative_rebase,
            "writer": writer,
        },
        spool_meta=req.model_dump(exclude={"content"}),
        content_bytes=req.content.encode("utf-8"),
        user_id=str(_collector_user.id),
        device_id=x_device_id,
        device_name=x_device_name,
        device_platform=x_device_platform,
        success_message="Ingested successfully",
        supports_commit_receipts=_collector_supports_async_delta_admission(
            x_collector_capabilities
        ),
    )


@router.post("/file/upload", response_model=IngestResponse)
async def ingest_file_upload(
    metadata: str = Form(...),
    content: UploadFile = File(...),
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
    x_collector_capabilities: str | None = Header(default=None),
) -> IngestResponse:
    """Ingest a large file via multipart upload."""
    meta = json.loads(metadata)
    _reject_synthetic_metadata_file_upload(
        category=meta.get("category"),
        mode=meta.get("mode"),
        sync_strategy=meta.get("sync_strategy"),
        relative_path=meta.get("relative_path"),
    )
    reported_size = max(0, int(meta.get("file_size") or 0))
    upload_size = getattr(content, "size", None)
    known_size = max(
        reported_size,
        upload_size if isinstance(upload_size, int) else 0,
    )
    stream_conversation = (
        meta.get("category") == "conversation"
        and meta.get("content_type") == "jsonl"
        and meta.get("mode", "full") in {"full", "delta"}
    )
    if stream_conversation:
        machine = await ensure_device(
            db,
            x_device_id,
            x_device_name,
            x_device_platform,
            user_id=_collector_user.id,
        )
        raw_writer = raw_realtime_writer_enabled(
            owner_id=str(_collector_user.id),
            device_id=x_device_id,
            tool_id=str(meta.get("tool") or ""),
            category=str(meta.get("category") or ""),
        )
        if raw_writer:
            await db.commit()
        with tempfile.TemporaryDirectory(prefix="memento-multipart-") as temporary:
            temporary_root = Path(temporary)
            raw_path = temporary_root / "payload.bin"
            measured_size = await _stream_upload_to_path(content, raw_path)
            if (
                settings.realtime_ingest_spool_deltas
                and raw_writer
                and _collector_supports_async_delta_admission(
                    x_collector_capabilities
                )
                and meta.get("mode") == "delta"
            ):
                return await _admit_realtime_delta(
                    db=db,
                    machine_id=str(machine.id),
                    meta={**meta, "file_size": measured_size},
                    payload=raw_path.read_bytes(),
                    user_id=str(_collector_user.id),
                    device_id=x_device_id,
                    device_name=x_device_name,
                    device_platform=x_device_platform,
                )
            if max(known_size, measured_size) <= DATABASE_CONTENT_MAX_BYTES:
                file_content = raw_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                return await _ingest_or_stage_dependent_delta(
                    db=db,
                    ingest_kwargs={
                        "db": db,
                        "tool_id": meta["tool"],
                        "category": meta["category"],
                        "content_type": meta["content_type"],
                        "relative_path": meta["relative_path"],
                        "content": file_content,
                        "content_hash": meta["hash"],
                        "file_size": max(reported_size, measured_size),
                        "mode": meta.get("mode", "full"),
                        "offset": meta.get("offset", 0),
                        "metadata": meta.get("metadata", {}),
                        "timestamp": meta.get("timestamp"),
                        "machine_id": str(machine.id),
                        "user_id": str(_collector_user.id),
                        "base_hash": meta.get("base_hash"),
                        "base_offset": meta.get("base_offset"),
                        "authoritative_rebase": _validated_authoritative_rebase(meta),
                        "writer": "raw" if raw_writer else None,
                    },
                    spool_meta=meta,
                    content_bytes=file_content.encode("utf-8"),
                    user_id=str(_collector_user.id),
                    device_id=x_device_id,
                    device_name=x_device_name,
                    device_platform=x_device_platform,
                    success_message="Uploaded successfully",
                    supports_commit_receipts=_collector_supports_async_delta_admission(
                        x_collector_capabilities
                    ),
                )
            sanitized = await asyncio.to_thread(
                sanitize_content_file,
                raw_path,
                temporary_root / "sanitized.bin",
            )
            conversation_source = await asyncio.to_thread(
                ConversationFileSource.inspect,
                sanitized.path,
            )
            mode = meta.get("mode", "full")
            content_s3_key = None
            # Legacy deployments retain their job-keyed large-object behavior
            # until the rollout flag is flipped. Once enabled, ingest_file's
            # single transaction-owned finalizer owns the immutable key.
            if mode == "full" and not settings.document_content_minio_enabled:
                job_id = multipart_content_job_id(
                    user_id=str(_collector_user.id),
                    device_id=x_device_id,
                    relative_path=str(meta["relative_path"]),
                    content_hash=str(meta["hash"]),
                )
                content_s3_key = await asyncio.to_thread(
                    store_large_content,
                    sanitized.path,
                    user_id=str(_collector_user.id),
                    device_id=x_device_id,
                    job_id=job_id,
                )
            return await _ingest_or_stage_dependent_delta(
                db=db,
                ingest_kwargs={
                    "db": db,
                    "tool_id": meta["tool"],
                    "category": meta["category"],
                    "content_type": meta["content_type"],
                    "relative_path": meta["relative_path"],
                    "content": "",
                    "content_hash": meta["hash"],
                    "file_size": conversation_source.size,
                    "mode": mode,
                    "offset": meta.get("offset", 0),
                    "metadata": meta.get("metadata", {}),
                    "timestamp": meta.get("timestamp"),
                    "machine_id": str(machine.id),
                    "user_id": str(_collector_user.id),
                    "base_hash": meta.get("base_hash"),
                    "base_offset": meta.get("base_offset"),
                    "authoritative_rebase": _validated_authoritative_rebase(meta),
                    "persist_content": mode != "full",
                    "content_s3_key": content_s3_key,
                    "content_already_sanitized": True,
                    "content_had_sensitive": sanitized.had_sensitive,
                    "conversation_source": conversation_source,
                },
                spool_meta={**meta, "file_size": measured_size},
                content_path=raw_path,
                user_id=str(_collector_user.id),
                device_id=x_device_id,
                device_name=x_device_name,
                device_platform=x_device_platform,
                success_message="Uploaded successfully",
                supports_commit_receipts=_collector_supports_async_delta_admission(
                    x_collector_capabilities
                ),
            )

    file_content = (await content.read()).decode("utf-8", errors="replace")
    measured_size = len(file_content.encode("utf-8"))
    machine = await ensure_device(
        db, x_device_id, x_device_name, x_device_platform, user_id=_collector_user.id
    )
    raw_writer = raw_realtime_writer_enabled(
        owner_id=str(_collector_user.id),
        device_id=x_device_id,
        tool_id=str(meta.get("tool") or ""),
        category=str(meta.get("category") or ""),
    )
    if raw_writer:
        await db.commit()

    return await _ingest_or_stage_dependent_delta(
        db=db,
        ingest_kwargs={
            "db": db,
            "tool_id": meta["tool"],
            "category": meta["category"],
            "content_type": meta["content_type"],
            "relative_path": meta["relative_path"],
            "content": file_content,
            "content_hash": meta["hash"],
            "file_size": max(reported_size, measured_size),
            "mode": meta.get("mode", "full"),
            "offset": meta.get("offset", 0),
            "metadata": meta.get("metadata", {}),
            "timestamp": meta.get("timestamp"),
            "machine_id": str(machine.id),
            "user_id": str(_collector_user.id),
            "base_hash": meta.get("base_hash"),
            "base_offset": meta.get("base_offset"),
            "authoritative_rebase": _validated_authoritative_rebase(meta),
            "writer": "raw" if raw_writer else None,
        },
        spool_meta=meta,
        content_bytes=file_content.encode("utf-8"),
        user_id=str(_collector_user.id),
        device_id=x_device_id,
        device_name=x_device_name,
        device_platform=x_device_platform,
        success_message="Uploaded successfully",
        supports_commit_receipts=_collector_supports_async_delta_admission(
            x_collector_capabilities
        ),
    )


@router.post("/sqlite-rows", response_model=IngestResponse)
async def ingest_sqlite_rows(
    req: dict,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> IngestResponse:
    """Ingest exported SQLite rows as JSON."""
    machine = await ensure_device(
        db, x_device_id, x_device_name, x_device_platform, user_id=_collector_user.id
    )
    content = json.dumps(req.get("rows", []), ensure_ascii=False)
    doc = await ingest_file(
        db=db,
        tool_id=req["tool"],
        category="state",
        content_type="sqlite_export",
        relative_path=f"{req.get('db_path', 'unknown')}/{req.get('source_table', 'unknown')}",
        content=content,
        content_hash="",
        file_size=len(content.encode("utf-8")),
        mode="delta" if req.get("last_rowid", 0) > 0 else "full",
        offset=req.get("last_rowid", 0),
        metadata={
            "source_table": req.get("source_table"),
            "db_path": req.get("db_path"),
        },
        machine_id=str(machine.id),
        user_id=str(_collector_user.id),
    )
    return IngestResponse(document_id=str(doc.id), message="SQLite rows ingested")


@router.post("/file/chunk", response_model=IngestResponse)
async def ingest_file_chunk(
    metadata: str = Form(...),
    content: UploadFile = File(...),
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> IngestResponse:
    """Durably stage a chunk and enqueue finalization after the last one."""
    try:
        meta = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="metadata must be valid JSON"
        ) from exc
    if not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="metadata must be a JSON object")
    _reject_synthetic_metadata_file_upload(
        category=meta.get("category"),
        mode=meta.get("mode"),
        sync_strategy=meta.get("sync_strategy"),
        relative_path=meta.get("relative_path"),
    )
    chunk_data = await content.read(MAX_CHUNK_BYTES + 1)

    # Validate device ownership in a short, committed transaction before any
    # durable acknowledgement. This transaction does not span parsing/ingest.
    machine = await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=_collector_user.id,
    )
    await db.commit()
    user_id = str(_collector_user.id)
    force_reprocess = False
    if has_completion_receipt(
        meta=meta,
        user_id=user_id,
        device_id=x_device_id,
    ):
        force_reprocess = await _completed_upload_needs_reprocessing(
            db,
            machine_id=machine.id,
            meta=meta,
        )
    try:
        staged = await asyncio.to_thread(
            stage_chunk,
            meta=meta,
            chunk_data=chunk_data,
            user_id=user_id,
            device_id=x_device_id,
            device_name=x_device_name,
            device_platform=x_device_platform,
            force_reprocess=force_reprocess,
        )
    except TerminalSpoolJobError as exc:
        raise await _terminal_spool_job_response(
            db,
            machine_id=str(machine.id),
            user_id=user_id,
            meta=meta,
            error=exc,
        ) from exc
    except ChunkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not staged.complete:
        return IngestResponse(
            document_id="pending",
            message=f"Chunk {int(meta['chunk_index']) + 1}/{int(meta['total_chunks'])} received",
        )
    if not staged.should_enqueue:
        return IngestResponse(
            document_id=f"completed:{staged.job_id}",
            message="Upload was already durably ingested",
        )

    # The ready marker and every chunk are fsynced before this response. Celery
    # is an acceleration path; the periodic recovery task owns enqueueing if
    # Redis is momentarily unavailable here.
    await _enqueue_spool_job(staged.job_id)
    return IngestResponse(
        document_id=f"queued:{staged.job_id}",
        message=f"Received {int(meta['total_chunks'])} chunks; durable ingest queued",
    )


@router.post("/file/chunk/status", response_model=IngestChunkStatusResponse)
async def ingest_file_chunk_status(
    req: IngestChunkStatusRequest,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    x_device_id: str = Header("unknown"),
) -> IngestChunkStatusResponse:
    """Report whether a durably accepted chunk upload committed to PostgreSQL."""
    result = await asyncio.to_thread(
        chunk_commit_status,
        meta={"upload_id": req.upload_id, "hash": req.hash},
        user_id=str(_collector_user.id),
        device_id=x_device_id,
    )
    return IngestChunkStatusResponse(
        job_id=result.job_id,
        status=result.status,
        error_type=result.error_type,
    )


@router.post("/file/receipt/status", response_model=IngestReceiptStatusResponse)
async def ingest_file_receipt_status(
    req: IngestReceiptStatusRequest,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    x_device_id: str = Header("unknown"),
) -> IngestReceiptStatusResponse:
    """Report ACCEPTED separately from the terminal database receipt."""
    try:
        result = await asyncio.to_thread(
            receipt_commit_status,
            receipt_id=req.receipt_id,
            user_id=str(_collector_user.id),
            device_id=x_device_id,
        )
    except ChunkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IngestReceiptStatusResponse(
        receipt_id=result.job_id,
        status=result.status,
        error_type=result.error_type,
    )


@router.post("/discovery")
async def ingest_discovery(
    req: dict,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive tool discovery data from a collector."""
    device_id = req.get("device_id", "unknown")
    machine = await ensure_device(
        db,
        device_id,
        req.get("device_name", ""),
        req.get("platform", ""),
        user_id=_collector_user.id,
    )

    # Clean up paths in discovery data (URL decode, strip \\?\)
    import re as _re
    from urllib.parse import unquote

    tools_data = req.get("tools", {})
    for tool_info in tools_data.values():
        if isinstance(tool_info, dict):
            if "root" in tool_info:
                tool_info["root"] = _re.sub(
                    r"^\\\\?\?\\", "", unquote(tool_info["root"])
                )
            for proj in tool_info.get("projects", []):
                if "path" in proj:
                    proj["path"] = _re.sub(r"^\\\\?\?\\", "", unquote(proj["path"]))

    discovery_content = json.dumps(tools_data, indent=2, ensure_ascii=False)
    await ingest_file(
        db=db,
        tool_id="system",
        category="discovery",
        content_type="json",
        relative_path=f"discovery/{device_id}.json",
        content=discovery_content,
        content_hash=f"discovery-{device_id}",
        file_size=len(discovery_content),
        mode="full",
        offset=0,
        metadata={
            "device_id": device_id,
            "device_name": req.get("device_name", ""),
            "platform": req.get("platform", ""),
            "tool_count": len(req.get("tools", {})),
        },
        machine_id=str(machine.id),
        user_id=str(_collector_user.id),
    )
    return {"status": "ok", "tools_discovered": len(req.get("tools", {}))}


@router.get("/status")
async def ingest_status() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.post("/heartbeat")
async def heartbeat(
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> dict:
    """Collector heartbeat — also registers/updates the device."""
    machine = await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=_collector_user.id,
        touch_heartbeat=True,
    )
    return {
        "status": "ok",
        # ``device_id`` is the collector's persistent public identity. Older
        # responses returned the database UUID under this name, which invited
        # clients to persist it and register a fresh machine on the next run.
        "device_id": machine.collector_token_hash,
        "machine_id": str(machine.id),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
