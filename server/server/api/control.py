"""Agent-control API — durable command channel and lifecycle trace.

Collector-facing routes deliver commands under expiring leases and record
device/agent outcomes; browser-facing routes expose authoritative command
state and the append-only event timeline. Browser mutations stay on their
existing surfaces (``/api/devices/.../command``), which admit into this
durable store.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AgentControlCommand, AgentControlEvent, Machine, User
from ..db.session import get_db
from ..middleware.auth import get_current_user, verify_collector_token
from ..services.agent_control import (
    ControlCommandNotFound,
    ControlErrorCodes,
    StaleControlLease,
    acknowledge_command,
    command_public,
    complete_command,
    event_public,
    hydrate_repair_payload,
    ingest_control_events,
    lease_commands,
    record_capabilities,
)
from ..services.device_service import ensure_device

router = APIRouter(prefix="/api/control", tags=["control"])

_MAX_POLL_WAIT_SECONDS = 25
_POLL_RETRY_INTERVAL = 1.0
_MAX_JSON_FIELD_BYTES = 16_384


def _bounded_json_dict(value: dict | None, field_name: str) -> dict | None:
    if value is None:
        return None
    if len(json.dumps(value, default=str)) > _MAX_JSON_FIELD_BYTES:
        raise ValueError(f"{field_name} exceeds {_MAX_JSON_FIELD_BYTES} bytes")
    return value


class ControlPollRequest(BaseModel):
    wait_seconds: int = Field(default=20, ge=0, le=_MAX_POLL_WAIT_SECONDS)
    max_commands: int = Field(default=8, ge=1, le=16)
    lease_seconds: int = Field(default=60, ge=5, le=300)
    collector_version: str | None = Field(default=None, max_length=64)
    capabilities: dict | None = None

    @field_validator("capabilities")
    @classmethod
    def _bounded_capabilities(cls, value: dict | None) -> dict | None:
        return _bounded_json_dict(value, "capabilities")


class ControlAckRequest(BaseModel):
    lease_id: uuid.UUID


class ControlCompleteRequest(BaseModel):
    lease_id: uuid.UUID
    status: str = Field(pattern="^(completed|failed|cancelled)$")
    error_code: str | None = Field(default=None, max_length=64)
    detail: dict | None = None
    occurred_at_device: datetime | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)

    @field_validator("detail")
    @classmethod
    def _bounded_detail(cls, value: dict | None) -> dict | None:
        return _bounded_json_dict(value, "detail")


class ControlLifecycleEventIn(BaseModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    event_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=96)
    command_id: uuid.UUID | None = None
    control_session_id: uuid.UUID | None = None
    trace_id: uuid.UUID | None = None
    parent_event_id: str | None = Field(default=None, max_length=128)
    document_id: uuid.UUID | None = None
    native_session_id: str | None = Field(default=None, max_length=512)
    native_turn_id: str | None = Field(default=None, max_length=256)
    interaction_id: str | None = Field(default=None, max_length=256)
    adapter: str | None = Field(default=None, max_length=64)
    adapter_version: str | None = Field(default=None, max_length=64)
    collector_revision: str | None = Field(default=None, max_length=64)
    occurred_at_device: datetime | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)
    outcome: str | None = Field(default=None, max_length=32)
    error_code: str | None = Field(default=None, max_length=64)
    payload_digest: str | None = Field(default=None, max_length=80)
    details: dict = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _bounded_details(cls, value: dict) -> dict:
        return _bounded_json_dict(value, "details") or {}


class ControlEventBatchRequest(BaseModel):
    events: list[ControlLifecycleEventIn] = Field(min_length=1, max_length=500)


def _stale_lease_response(error: StaleControlLease) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": ControlErrorCodes.STALE_LEASE,
            "state": error.command.state,
            "command_id": str(error.command.id),
        },
    )


async def _served_command(db: AsyncSession, command: AgentControlCommand) -> dict:
    served = command_public(command)
    served["payload"] = await hydrate_repair_payload(db, command)
    return served


@router.post("/poll")
async def poll_commands(
    req: ControlPollRequest,
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown", alias="X-Device-Id"),
    x_device_name: str = Header("unknown", alias="X-Device-Name"),
    x_device_platform: str = Header("unknown", alias="X-Device-Platform"),
) -> dict:
    """Durable long-poll: lease queued commands for this collector machine.

    Each attempt commits independently, so heartbeat/capability updates and
    lazy reaping persist even when the poll returns empty. The session holds
    no database connection while sleeping between attempts.
    """
    machine = await ensure_device(
        db,
        x_device_id,
        x_device_name,
        x_device_platform,
        user_id=collector_user.id,
    )
    record_capabilities(
        machine, req.capabilities, collector_version=req.collector_version
    )
    machine_id = machine.id

    deadline = time.monotonic() + req.wait_seconds
    while True:
        commands = await lease_commands(
            db,
            machine=machine,
            lease_seconds=req.lease_seconds,
            limit=req.max_commands,
            collector_revision=req.collector_version,
        )
        served = [await _served_command(db, command) for command in commands]
        await db.commit()
        if served or time.monotonic() >= deadline:
            return {
                "commands": served,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        await asyncio.sleep(_POLL_RETRY_INTERVAL)
        machine = await db.get(Machine, machine_id)
        if machine is None:
            raise HTTPException(status_code=404, detail="Device not found")


@router.post("/commands/{command_id}/ack")
async def ack_command(
    command_id: uuid.UUID,
    req: ControlAckRequest,
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown", alias="X-Device-Id"),
    x_device_name: str = Header("unknown", alias="X-Device-Name"),
    x_device_platform: str = Header("unknown", alias="X-Device-Platform"),
    x_collector_version: str = Header("", alias="X-Collector-Version"),
) -> dict:
    machine = await ensure_device(
        db, x_device_id, x_device_name, x_device_platform, user_id=collector_user.id
    )
    try:
        command = await acknowledge_command(
            db,
            machine=machine,
            command_id=command_id,
            lease_id=req.lease_id,
            collector_revision=x_collector_version or None,
        )
    except ControlCommandNotFound as error:
        raise HTTPException(status_code=404, detail="Command not found") from error
    except StaleControlLease as error:
        raise _stale_lease_response(error) from error
    return {"status": "acknowledged", "command": command_public(command)}


@router.post("/commands/{command_id}/complete")
async def finish_command(
    command_id: uuid.UUID,
    req: ControlCompleteRequest,
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown", alias="X-Device-Id"),
    x_device_name: str = Header("unknown", alias="X-Device-Name"),
    x_device_platform: str = Header("unknown", alias="X-Device-Platform"),
    x_collector_version: str = Header("", alias="X-Collector-Version"),
) -> dict:
    machine = await ensure_device(
        db, x_device_id, x_device_name, x_device_platform, user_id=collector_user.id
    )
    try:
        command = await complete_command(
            db,
            machine=machine,
            command_id=command_id,
            lease_id=req.lease_id,
            status=req.status,
            error_code=req.error_code,
            detail=req.detail,
            occurred_at_device=req.occurred_at_device,
            elapsed_ms=req.elapsed_ms,
            collector_revision=x_collector_version or None,
        )
    except ControlCommandNotFound as error:
        raise HTTPException(status_code=404, detail="Command not found") from error
    except StaleControlLease as error:
        raise _stale_lease_response(error) from error
    return {"status": command.state, "command": command_public(command)}


@router.post("/events")
async def ingest_events(
    req: ControlEventBatchRequest,
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown", alias="X-Device-Id"),
    x_device_name: str = Header("unknown", alias="X-Device-Name"),
    x_device_platform: str = Header("unknown", alias="X-Device-Platform"),
) -> dict:
    machine = await ensure_device(
        db, x_device_id, x_device_name, x_device_platform, user_id=collector_user.id
    )
    result = await ingest_control_events(
        db,
        machine=machine,
        events=[event.model_dump() for event in req.events],
    )
    return result


# ---------------------------------------------------------------------------
# Browser-facing read models (trace explorer / control activity)
# ---------------------------------------------------------------------------

async def _load_visible_command(
    db: AsyncSession, command_id: uuid.UUID, user: User
) -> AgentControlCommand:
    command = await db.get(AgentControlCommand, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Command not found")
    if user.role not in ("admin", "owner") and command.user_id != user.id:
        raise HTTPException(status_code=404, detail="Command not found")
    return command


@router.get("/commands/{command_id}")
async def get_command(
    command_id: uuid.UUID,
    include_events: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    command = await _load_visible_command(db, command_id, user)
    payload = {"command": command_public(command)}
    if include_events:
        events = (
            await db.execute(
                select(AgentControlEvent)
                .where(AgentControlEvent.command_id == command.id)
                .order_by(AgentControlEvent.id)
                .limit(500)
            )
        ).scalars().all()
        payload["events"] = [event_public(event) for event in events]
    return payload


@router.get("/commands/{command_id}/events")
async def get_command_events(
    command_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    command = await _load_visible_command(db, command_id, user)
    events = (
        await db.execute(
            select(AgentControlEvent)
            .where(AgentControlEvent.command_id == command.id)
            .order_by(AgentControlEvent.id)
            .limit(500)
        )
    ).scalars().all()
    return {
        "command_id": str(command.id),
        "trace_id": str(command.trace_id),
        "events": [event_public(event) for event in events],
    }


@router.get("/commands")
async def list_commands(
    machine_id: uuid.UUID | None = None,
    state: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    query = select(AgentControlCommand).order_by(
        AgentControlCommand.created_at.desc()
    ).limit(max(1, min(limit, 100)))
    if user.role not in ("admin", "owner"):
        query = query.where(AgentControlCommand.user_id == user.id)
    if machine_id is not None:
        query = query.where(AgentControlCommand.machine_id == machine_id)
    if state:
        query = query.where(AgentControlCommand.state == state)
    if kind:
        query = query.where(AgentControlCommand.kind == kind)
    commands = (await db.execute(query)).scalars().all()
    return [command_public(command) for command in commands]
