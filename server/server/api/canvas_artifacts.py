"""Authenticated Canvas artifact ingestion, inventory, and isolated rendering."""

from __future__ import annotations

import json
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
)
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User
from ..db.session import get_db
from ..middleware.auth import get_current_user, verify_collector_token
from ..services.canvas_artifact_store import (
    MAX_COMPILED_BYTES,
    MAX_RUNTIME_BYTES,
    MAX_SOURCE_BYTES,
    artifact_for_user,
    blob_content,
    inventory_summary,
    pending_machine_canvases,
    record_canvas_outcome,
    render_shell,
    store_captured_canvas,
)
from ..services.device_service import ensure_device

router = APIRouter(prefix="/api/canvas-artifacts", tags=["canvas-artifacts"])


class CanvasOutcomeRequest(BaseModel):
    reference_ids: list[uuid.UUID] = Field(min_length=1, max_length=128)
    path_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(pattern=r"^(missing|rejected|unsupported|unchanged)$")
    reason: str = Field(default="", max_length=128)


async def _collector_machine(
    db: AsyncSession,
    user: User,
    device_id: str,
    device_name: str,
    device_platform: str,
):
    return await ensure_device(
        db,
        device_id,
        device_name,
        device_platform,
        user_id=user.id,
    )


@router.get("/pending")
async def pending_canvas_artifacts(
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> dict:
    """Return only exact references owned by the polling source device."""
    machine = await _collector_machine(
        db,
        collector_user,
        x_device_id,
        x_device_name,
        x_device_platform,
    )
    pending = await pending_machine_canvases(db, machine.id)
    return {"artifacts": pending, "bounded": True}


@router.post("/outcome")
async def submit_canvas_outcome(
    request: CanvasOutcomeRequest,
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> dict:
    machine = await _collector_machine(
        db,
        collector_user,
        x_device_id,
        x_device_name,
        x_device_platform,
    )
    updated = await record_canvas_outcome(
        db,
        machine_id=machine.id,
        reference_ids=request.reference_ids,
        path_hash=request.path_hash,
        status=request.status,
        reason=request.reason,
    )
    return {"status": request.status, "updated": updated}


@router.post("/upload")
async def upload_canvas_artifact(
    metadata: str = Form(...),
    source: UploadFile = File(...),
    compiled: UploadFile | None = File(None),
    runtime: UploadFile | None = File(None),
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown"),
    x_device_name: str = Header("unknown"),
    x_device_platform: str = Header("unknown"),
) -> dict:
    try:
        parsed_metadata = json.loads(metadata)
        if not isinstance(parsed_metadata, dict):
            raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid artifact metadata") from exc

    source_bytes = await source.read(MAX_SOURCE_BYTES + 1)
    compiled_bytes = (
        await compiled.read(MAX_COMPILED_BYTES + 1) if compiled is not None else None
    )
    runtime_bytes = (
        await runtime.read(MAX_RUNTIME_BYTES + 1) if runtime is not None else None
    )
    machine = await _collector_machine(
        db,
        collector_user,
        x_device_id,
        x_device_name,
        x_device_platform,
    )
    artifact, outcome, updated = await store_captured_canvas(
        db,
        user=collector_user,
        machine=machine,
        metadata=parsed_metadata,
        source=source_bytes,
        compiled=compiled_bytes,
        runtime=runtime_bytes,
    )
    return {
        "status": outcome,
        "artifact_id": str(artifact.id),
        "render_mode": artifact.render_mode,
        "updated": updated,
    }


@router.get("/inventory")
async def get_canvas_inventory(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Return resumable backfill outcomes grouped by device, tool, and path hash."""
    return await inventory_summary(db, user)


@router.get("/{artifact_id}/render", response_class=HTMLResponse)
async def render_canvas_artifact(
    artifact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HTMLResponse:
    artifact = await artifact_for_user(db, artifact_id, user)
    if artifact.render_mode != "interactive":
        raise HTTPException(status_code=409, detail="Canvas is static-only")
    runtime = await blob_content(db, artifact.runtime_hash)
    compiled = await blob_content(db, artifact.compiled_hash)
    return HTMLResponse(
        render_shell(runtime, compiled),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{artifact_id}/source", response_class=PlainTextResponse)
async def get_canvas_source(
    artifact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlainTextResponse:
    artifact = await artifact_for_user(db, artifact_id, user)
    source = await blob_content(db, artifact.source_hash)
    return PlainTextResponse(
        source.decode("utf-8"),
        media_type="text/typescript",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="{artifact.name}.canvas.tsx"',
            "X-Content-Type-Options": "nosniff",
        },
    )
