"""Devices API — view and manage registered collector devices."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AccessLog, ConversationMessage, Document, DocumentVersion, Machine, Project, SyncState, User
from ..db.session import get_db
from ..middleware.auth import get_current_user, verify_collector_token
from ..services.agent_control import (
    LEGACY_ACTION_TO_KIND,
    ControlCommandNotFound,
    UnsupportedCommandKind,
    acknowledge_legacy_command,
    admit_command,
    lease_legacy_commands,
    record_capabilities,
)
from ..services.document_delivery import delivery_synced_expression

router = APIRouter(prefix="/api/devices", tags=["devices"])

# Device commands are admitted into the durable agent-control store
# (services/agent_control.py). The old process-local in-memory queue lost
# every pending command on restart and was invisible across workers.

# In-memory PyPI version cache: {package_name: (version_or_none, expires_monotonic)}
# 5-minute TTL — uses time.monotonic() so clock changes can't break TTL math.
_PYPI_CACHE_TTL = 300.0
_pypi_version_cache: dict[str, tuple[str | None, float]] = {}
_REPAIR_ACTION = "repair-conversations"
_STALE_EMPTY_DEVICE_AGE = timedelta(hours=24)


def _is_visible_device(
    machine: Machine,
    document_count: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Hide abandoned registrations without hiding a real collector.

    Setup/reinstall attempts can register and heartbeat before discovering a
    single transcript. Those zero-content rows used to remain on the Devices
    page forever. A collector that has ever reported its version or delivered
    content remains visible; an unversioned empty registration expires after a
    day, while a fresh first-run registration keeps its setup window.
    """
    if document_count > 0 or machine.collector_version:
        return True
    current = now or datetime.now(timezone.utc)
    last_seen = machine.last_heartbeat or machine.created_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return last_seen >= current - _STALE_EMPTY_DEVICE_AGE


@router.get("")
async def list_devices(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """List all registered collector devices with their stats."""
    machines_q = select(Machine).order_by(Machine.name)
    if _user.role not in ("admin", "owner"):
        machines_q = machines_q.where(Machine.user_id == _user.id)
    machines = list((await db.execute(machines_q)).scalars().all())
    if not machines:
        return []

    machine_ids = [m.id for m in machines]

    # One GROUP BY replaces the per-machine COUNT + DISTINCT round-trips.
    stats_q = (
        select(Document.machine_id, Document.tool_id, func.count())
        .where(Document.machine_id.in_(machine_ids), Document.tool_id != "system")
        .group_by(Document.machine_id, Document.tool_id)
    )
    totals_by_machine: dict = {}
    tools_by_machine: dict = {}
    for mid, tid, n in (await db.execute(stats_q)).all():
        totals_by_machine[mid] = totals_by_machine.get(mid, 0) + n
        tools_by_machine.setdefault(mid, []).append(tid)

    items = []
    for m in machines:
        document_count = totals_by_machine.get(m.id, 0)
        if not _is_visible_device(m, document_count):
            continue
        items.append({
            "id": str(m.id),
            "name": m.name,
            "device_id": m.collector_token_hash,
            "collector_version": m.collector_version,
            "last_heartbeat": m.last_heartbeat.isoformat() if m.last_heartbeat else None,
            "created_at": m.created_at.isoformat(),
            "document_count": document_count,
            "tools": tools_by_machine.get(m.id, []),
        })

    return items


async def _verify_device_ownership(
    db: AsyncSession, device_db_id: uuid.UUID, user: User,
) -> Machine:
    """Fetch a machine and verify the user has access. Raises 404 if not found or not owned."""
    result = await db.execute(select(Machine).where(Machine.id == device_db_id))
    machine = result.scalar_one_or_none()
    if not machine:
        raise HTTPException(status_code=404, detail="Device not found")
    if user.role not in ("admin", "owner") and machine.user_id != user.id:
        raise HTTPException(status_code=404, detail="Device not found")
    return machine


@router.get("/{device_db_id}/discovery")
async def get_device_discovery(
    device_db_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get discovery data (tool paths, projects) for a device."""
    machine = await _verify_device_ownership(db, device_db_id, _user)

    # Find the discovery document for this device
    doc_result = await db.execute(
        select(Document).where(
            Document.tool_id == "system",
            Document.category == "discovery",
            Document.machine_id == device_db_id,
        ).order_by(delivery_synced_expression().desc()).limit(1)
    )
    doc = doc_result.scalar_one_or_none()

    if not doc or not doc.content:
        return {"device_id": str(device_db_id), "tools": {}}

    try:
        tools = json.loads(doc.content)
    except Exception:
        tools = {}

    return {
        "device_id": str(device_db_id),
        "device_name": machine.name,
        "synced_at": doc.synced_at.isoformat(),
        "tools": tools,
    }


async def _purge_device_data(
    db: AsyncSession, device_db_id: uuid.UUID, include_system: bool = False,
) -> dict:
    """Delete all data tied to a device: documents and everything that references them,
    plus this device's sync_state. Also cleans up orphaned knowledge entities and projects.

    include_system=True deletes discovery/system docs too (used for full device deletion).
    """
    from ..db.models import DocumentEmbedding, KnowledgeEntity, KnowledgeObservation, KnowledgeRelation

    doc_q = select(Document.id).where(Document.machine_id == device_db_id)
    if not include_system:
        doc_q = doc_q.where(Document.tool_id != "system")
    doc_ids = [r[0] for r in (await db.execute(doc_q)).all()]
    count = len(doc_ids)

    batch_size = 500
    for i in range(0, len(doc_ids), batch_size):
        batch = doc_ids[i:i + batch_size]
        await db.execute(delete(AccessLog).where(AccessLog.document_id.in_(batch)))
        await db.execute(delete(ConversationMessage).where(ConversationMessage.document_id.in_(batch)))
        await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(batch)))
        await db.execute(delete(DocumentEmbedding).where(DocumentEmbedding.document_id.in_(batch)))
        await db.execute(delete(KnowledgeObservation).where(KnowledgeObservation.source_document_id.in_(batch)))
        await db.execute(delete(Document).where(Document.id.in_(batch)))

    # Drop knowledge entities that have no observations left (fully orphaned by the purge)
    orphan_entity_ids = [r[0] for r in (await db.execute(
        select(KnowledgeEntity.id).where(
            ~KnowledgeEntity.id.in_(
                select(KnowledgeObservation.entity_id).where(KnowledgeObservation.entity_id.isnot(None))
            )
        )
    )).all()]
    if orphan_entity_ids:
        await db.execute(delete(KnowledgeRelation).where(
            KnowledgeRelation.source_id.in_(orphan_entity_ids) | KnowledgeRelation.target_id.in_(orphan_entity_ids)
        ))
        await db.execute(delete(KnowledgeEntity).where(KnowledgeEntity.id.in_(orphan_entity_ids)))

    # Drop projects with no docs left
    orphan_ids = [r[0] for r in (await db.execute(
        select(Project.id).where(
            ~Project.id.in_(select(Document.project_id).where(Document.project_id.isnot(None)))
        )
    )).all()]
    if orphan_ids:
        await db.execute(delete(Project).where(Project.id.in_(orphan_ids)))

    await db.execute(delete(SyncState).where(SyncState.machine_id == device_db_id))

    return {
        "documents_deleted": count,
        "orphaned_entities_deleted": len(orphan_entity_ids),
        "orphaned_projects_deleted": len(orphan_ids),
    }


@router.delete("/{device_db_id}")
async def delete_device(
    device_db_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Delete a device and ALL its associated data (documents, messages, embeddings,
    knowledge observations, sync state, orphaned projects/entities)."""
    machine = await _verify_device_ownership(db, device_db_id, _user)

    stats = await _purge_device_data(db, device_db_id, include_system=True)
    await db.execute(delete(Machine).where(Machine.id == device_db_id))

    return {"status": "deleted", "device_id": str(device_db_id), "name": machine.name, **stats}


@router.delete("/{device_db_id}/purge")
async def purge_device(
    device_db_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Delete all documents + related data but keep the device record (used before resync)."""
    machine = await _verify_device_ownership(db, device_db_id, _user)
    stats = await _purge_device_data(db, device_db_id, include_system=False)
    return {"status": "purged", "device_id": str(device_db_id), "name": machine.name, **stats}


# ---------------------------------------------------------------------------
# Device commands — server → collector communication
# ---------------------------------------------------------------------------

@router.post("/{device_db_id}/command")
async def send_command(
    device_db_id: uuid.UUID,
    action: str = "resync",
    document_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Send a command to a collector device (picked up on next poll)."""
    machine = await _verify_device_ownership(db, device_db_id, _user)

    # Resync: clear graph request state + embeddings + observations for this
    # device's documents so knowledge regenerates from fresh ingest.
    if action == "resync":
        from sqlalchemy import text
        from ..db.models import DocumentEmbedding, KnowledgeObservation
        doc_ids_result = await db.execute(
            select(Document.id).where(Document.machine_id == device_db_id)
        )
        doc_ids = [r[0] for r in doc_ids_result.all()]
        if doc_ids:
            for i in range(0, len(doc_ids), 500):
                batch = doc_ids[i:i + 500]
                await db.execute(delete(DocumentEmbedding).where(DocumentEmbedding.document_id.in_(batch)))
                await db.execute(delete(KnowledgeObservation).where(KnowledgeObservation.source_document_id.in_(batch)))
            await db.execute(text(
                "UPDATE documents SET "
                "metadata = metadata - '_graph_hash' - '_graph_attempt_hash', "
                "knowledge_status = 'pending', knowledge_attempts = 0, "
                "knowledge_retry_at = NULL, knowledge_failure_kind = NULL "
                "WHERE machine_id = :mid"
            ), {"mid": device_db_id})

    kind = LEGACY_ACTION_TO_KIND.get(action)
    if kind is None:
        raise HTTPException(status_code=400, detail=f"Unknown command action: {action}")

    payload: dict | None = None
    if action == _REPAIR_ACTION and document_id is not None:
        repair_document = (
            await db.execute(
                select(Document.tool_id, Document.relative_path).where(
                    Document.id == document_id,
                    Document.machine_id == machine.id,
                    Document.category == "conversation",
                    Document.tool_id.in_(("codex", "claude_code", "cursor")),
                )
            )
        ).one_or_none()
        if repair_document is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        payload = {
            "paths": [
                {
                    "tool_name": repair_document.tool_id,
                    "relative_path": repair_document.relative_path,
                }
            ]
        }

    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=_user.id,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
            document_id=document_id if action == _REPAIR_ACTION else None,
        )
    except UnsupportedCommandKind as error:
        raise HTTPException(
            status_code=409,
            detail={"code": str(error), "kind": error.kind},
        ) from error
    return {
        "status": "queued",
        "command_id": str(command.id),
        "trace_id": str(command.trace_id),
        "action": action,
        "device": machine.name,
    }


@router.post("/command-by-collector-id")
async def send_command_by_collector_id(
    collector_id: str,
    action: str = "resync",
    idempotency_key: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Send a command using the collector's device_id (survives purge)."""
    # Authorize: non-admin can only command devices they own. Without this,
    # a logged-in user could guess/obtain another user's collector token hash
    # and force resync/purge on their device.
    result = await db.execute(
        select(Machine).where(Machine.collector_token_hash == collector_id)
    )
    machine = result.scalar_one_or_none()
    if _user.role not in ("admin", "owner"):
        if not machine or machine.user_id != _user.id:
            raise HTTPException(status_code=404, detail="Device not found")
    if machine is None:
        # Durable commands are keyed to a registered machine row; an unknown
        # collector id used to enqueue into a black hole no collector polled.
        raise HTTPException(status_code=404, detail="Device not found")
    kind = LEGACY_ACTION_TO_KIND.get(action)
    if kind is None:
        raise HTTPException(status_code=400, detail=f"Unknown command action: {action}")
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=_user.id,
            kind=kind,
            idempotency_key=idempotency_key,
        )
    except UnsupportedCommandKind as error:
        raise HTTPException(
            status_code=409,
            detail={"code": str(error), "kind": error.kind},
        ) from error
    return {
        "status": "queued",
        "command_id": str(command.id),
        "trace_id": str(command.trace_id),
        "action": action,
    }


async def _fetch_pypi_version(client: httpx.AsyncClient, package: str) -> str | None:
    """Fetch the latest version of a package from PyPI. Returns None on any failure."""
    resp = await client.get(f"https://pypi.org/pypi/{package}/json")
    resp.raise_for_status()
    return resp.json()["info"]["version"]


async def _get_cached_pypi_version(client: httpx.AsyncClient, package: str) -> str | None:
    """Return cached version if fresh, else fetch + cache. None on fetch failure."""
    now = time.monotonic()
    cached = _pypi_version_cache.get(package)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        version = await _fetch_pypi_version(client, package)
    except Exception:
        version = None
    _pypi_version_cache[package] = (version, now + _PYPI_CACHE_TTL)
    return version


@router.get("/collector-latest-version")
async def get_collector_latest_version(
    _user: User = Depends(get_current_user),
) -> dict:
    """Return the latest available collector + MCP memory versions from PyPI.

    Cached for 5 minutes in-process. Returns null for any package whose PyPI
    fetch failed (never 500s) so the admin UI can still render.
    """
    from datetime import datetime, timezone

    async with httpx.AsyncClient(timeout=5.0) as client:
        results = await asyncio.gather(
            _get_cached_pypi_version(client, "memento-brain-collector"),
            _get_cached_pypi_version(client, "memento-brain-memory"),
            return_exceptions=True,
        )

    collector_v = results[0] if isinstance(results[0], (str, type(None))) else None
    memory_v = results[1] if isinstance(results[1], (str, type(None))) else None

    return {
        "collector": collector_v,
        "memory": memory_v,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def _legacy_collector_machine(
    db: AsyncSession, device_id: str, collector_user: User
) -> Machine | None:
    """Resolve a legacy poll's machine, enforcing token/device ownership."""
    result = await db.execute(
        select(Machine).where(Machine.collector_token_hash == device_id)
    )
    machine = result.scalar_one_or_none()
    if machine is None:
        return None
    if machine.user_id is not None and machine.user_id != collector_user.id:
        raise HTTPException(status_code=403, detail="Device belongs to another user")
    return machine


@router.get("/commands")
async def get_commands(
    x_device_id: str = Header(..., alias="X-Device-Id"),
    x_collector_version: str = Header("", alias="X-Collector-Version"),
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Legacy short-poll for collectors <= 0.0.40. Serves the durable store.

    Newer collectors use ``POST /api/control/poll`` (long-poll, leases,
    outcome reporting). This route stays wire-compatible — same command
    shape, heartbeat/version refresh, serve-time repair-path hydration —
    and now verifies the collector token those clients always sent.
    """
    machine = await _legacy_collector_machine(db, x_device_id, collector_user)
    if machine is None:
        return []
    record_capabilities(machine, None, collector_version=x_collector_version or None)
    return await lease_legacy_commands(
        db, machine=machine, collector_version=x_collector_version or None
    )


@router.post("/commands/{cmd_id}/ack")
async def ack_command(
    cmd_id: uuid.UUID,
    x_device_id: str = Header(..., alias="X-Device-Id"),
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Legacy ack (collectors <= 0.0.40 acknowledge before executing).

    Terminalizes the durable command honestly: the outcome records that no
    execution result was observed, because the legacy protocol never
    reported one.
    """
    machine = await _legacy_collector_machine(db, x_device_id, collector_user)
    if machine is None:
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        await acknowledge_legacy_command(db, machine=machine, command_id=cmd_id)
    except ControlCommandNotFound as error:
        raise HTTPException(status_code=404, detail="Command not found") from error
    return {"status": "acked", "command_id": str(cmd_id)}
