"""Ingest API — receives files from collectors on multiple devices."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User
from ..db.session import get_db
from ..middleware.auth import verify_collector_token
from ..services.device_service import ensure_device
from ..services.ingest_service import ingest_file, _get_ingest_semaphore
from ..services.ingest_spool import (
    MAX_CHUNK_BYTES,
    ChunkValidationError,
    stage_chunk,
)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
logger = logging.getLogger("ingest")


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
    metadata: dict = {}
    timestamp: float | None = None
    content: str = ""


class IngestResponse(BaseModel):
    status: str = "ok"
    document_id: str
    message: str = ""


@router.post("/file", response_model=IngestResponse)
async def ingest_file_endpoint(
    req: IngestFileRequest,
    _collector_user: User = Depends(verify_collector_token),
    _throttle: None = Depends(throttle_ingest),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> IngestResponse:
    """Ingest a file from the collector (JSON payload, for files < 1MB)."""
    machine = await ensure_device(db, x_device_id, x_device_name, x_device_platform, user_id=_collector_user.id)

    doc = await ingest_file(
        db=db,
        tool_id=req.tool,
        category=req.category,
        content_type=req.content_type,
        relative_path=req.relative_path,
        content=req.content,
        content_hash=req.hash,
        file_size=req.file_size or len(req.content.encode("utf-8")),
        mode=req.mode,
        offset=req.offset,
        metadata=req.metadata,
        timestamp=req.timestamp,
        machine_id=str(machine.id),
        user_id=str(_collector_user.id),
    )
    return IngestResponse(document_id=str(doc.id), message="Ingested successfully")


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
) -> IngestResponse:
    """Ingest a large file via multipart upload."""
    meta = json.loads(metadata)
    file_content = (await content.read()).decode("utf-8", errors="replace")
    machine = await ensure_device(db, x_device_id, x_device_name, x_device_platform, user_id=_collector_user.id)

    doc = await ingest_file(
        db=db,
        tool_id=meta["tool"],
        category=meta["category"],
        content_type=meta["content_type"],
        relative_path=meta["relative_path"],
        content=file_content,
        content_hash=meta["hash"],
        file_size=meta.get("file_size", len(file_content.encode("utf-8"))),
        mode=meta.get("mode", "full"),
        offset=meta.get("offset", 0),
        metadata=meta.get("metadata", {}),
        timestamp=meta.get("timestamp"),
        machine_id=str(machine.id),
        user_id=str(_collector_user.id),
    )
    return IngestResponse(document_id=str(doc.id), message="Uploaded successfully")


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
    machine = await ensure_device(db, x_device_id, x_device_name, x_device_platform, user_id=_collector_user.id)
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
        metadata={"source_table": req.get("source_table"), "db_path": req.get("db_path")},
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
        raise HTTPException(status_code=400, detail="metadata must be valid JSON") from exc
    if not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="metadata must be a JSON object")
    chunk_data = await content.read(MAX_CHUNK_BYTES + 1)

    # Validate device ownership in a short, committed transaction before any
    # durable acknowledgement. This transaction does not span parsing/ingest.
    await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=_collector_user.id,
    )
    await db.commit()
    try:
        job_id, complete = await asyncio.to_thread(
            stage_chunk,
            meta=meta,
            chunk_data=chunk_data,
            user_id=str(_collector_user.id),
            device_id=x_device_id,
            device_name=x_device_name,
            device_platform=x_device_platform,
        )
    except ChunkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not complete:
        return IngestResponse(
            document_id="pending",
            message=f"Chunk {int(meta['chunk_index']) + 1}/{int(meta['total_chunks'])} received",
        )

    # The ready marker and every chunk are fsynced before this response. Celery
    # is an acceleration path; the periodic recovery task owns enqueueing if
    # Redis is momentarily unavailable here.
    try:
        from ..tasks.ingest_spool import process_spooled_ingest
        await asyncio.to_thread(
            process_spooled_ingest.apply_async,
            args=[job_id],
            queue="ingest",
            retry=False,
        )
    except Exception:
        logger.exception("Ready spool job %s could not be queued; recovery will retry", job_id)
    return IngestResponse(
        document_id=f"queued:{job_id}",
        message=f"Received {int(meta['total_chunks'])} chunks; durable ingest queued",
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
    machine = await ensure_device(db, device_id, req.get("device_name", ""), req.get("platform", ""), user_id=_collector_user.id)

    # Clean up paths in discovery data (URL decode, strip \\?\)
    from urllib.parse import unquote
    import re as _re
    tools_data = req.get("tools", {})
    for tool_info in tools_data.values():
        if isinstance(tool_info, dict):
            if "root" in tool_info:
                tool_info["root"] = _re.sub(r"^\\\\?\?\\", "", unquote(tool_info["root"]))
            for proj in tool_info.get("projects", []):
                if "path" in proj:
                    proj["path"] = _re.sub(r"^\\\\?\?\\", "", unquote(proj["path"]))

    discovery_content = json.dumps(tools_data, indent=2, ensure_ascii=False)
    await ingest_file(
        db=db, tool_id="system", category="discovery", content_type="json",
        relative_path=f"discovery/{device_id}.json",
        content=discovery_content, content_hash=f"discovery-{device_id}",
        file_size=len(discovery_content), mode="full", offset=0,
        metadata={"device_id": device_id, "device_name": req.get("device_name", ""),
                  "platform": req.get("platform", ""), "tool_count": len(req.get("tools", {}))},
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
    machine = await ensure_device(db, x_device_id, x_device_name, x_device_platform, user_id=_collector_user.id)
    return {
        "status": "ok",
        "device_id": str(machine.id),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
