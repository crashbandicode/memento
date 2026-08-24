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

from ..db.models import (
    AgentControlCommand,
    AgentControlEvent,
    AgentControlSession,
    ConversationReadModel,
    Document,
    Machine,
    User,
)
from ..db.session import get_db
from ..middleware.auth import get_current_user, verify_collector_token
from ..services.agent_control import (
    KIND_AGENT_APPROVAL_RESPOND,
    KIND_AGENT_INTERACTION_ANSWER,
    KIND_AGENT_SESSION_CLOSE,
    KIND_AGENT_SESSION_RESUME,
    KIND_AGENT_SESSION_START,
    KIND_AGENT_TURN_INTERRUPT,
    KIND_AGENT_TURN_SEND,
    KIND_AGENT_TURN_STEER,
    SESSION_ACTIVE,
    SESSION_STARTING,
    ControlCommandNotFound,
    ControlErrorCodes,
    ControlEventScopeError,
    StaleControlLease,
    UnsupportedCommandKind,
    acknowledge_command,
    admit_command,
    bind_control_session_documents,
    command_public,
    complete_command,
    create_control_session,
    event_public,
    hydrate_repair_payload,
    ingest_control_events,
    lease_commands,
    record_capabilities,
    renew_command_lease,
    session_public,
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


class ControlHeartbeatRequest(BaseModel):
    lease_id: uuid.UUID
    lease_seconds: int = Field(default=300, ge=5, le=300)


@router.post("/commands/{command_id}/heartbeat")
async def heartbeat_command(
    command_id: uuid.UUID,
    req: ControlHeartbeatRequest,
    collector_user: User = Depends(verify_collector_token),
    db: AsyncSession = Depends(get_db),
    x_device_id: str = Header("unknown", alias="X-Device-Id"),
    x_device_name: str = Header("unknown", alias="X-Device-Name"),
    x_device_platform: str = Header("unknown", alias="X-Device-Platform"),
    x_collector_version: str = Header("", alias="X-Collector-Version"),
) -> dict:
    """Extend a live lease while a long command is still executing."""
    machine = await ensure_device(
        db, x_device_id, x_device_name, x_device_platform, user_id=collector_user.id
    )
    try:
        command = await renew_command_lease(
            db,
            machine=machine,
            command_id=command_id,
            lease_id=req.lease_id,
            lease_seconds=req.lease_seconds,
            collector_revision=x_collector_version or None,
        )
    except ControlCommandNotFound as error:
        raise HTTPException(status_code=404, detail="Command not found") from error
    except StaleControlLease as error:
        raise _stale_lease_response(error) from error
    return {
        "status": "renewed",
        "lease_expires_at": (
            command.lease_expires_at.isoformat() if command.lease_expires_at else None
        ),
    }


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
    try:
        result = await ingest_control_events(
            db,
            machine=machine,
            events=[event.model_dump() for event in req.events],
        )
    except ControlEventScopeError as error:
        raise HTTPException(
            status_code=403,
            detail={
                "code": ControlErrorCodes.AUTH_SCOPE_DENIED,
                "field": error.field,
            },
        ) from error
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


# ---------------------------------------------------------------------------
# Browser-facing managed sessions
# ---------------------------------------------------------------------------

class ControlSessionCreateRequest(BaseModel):
    machine_id: uuid.UUID
    tool_id: str = Field(default="codex", pattern="^codex$")
    cwd: str | None = Field(default=None, max_length=1024)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=32)
    sandbox: str | None = Field(
        default=None, pattern="^(read-only|workspace-write|danger-full-access)$"
    )
    # Real Codex 0.147 wire variants — the pinned README's camelCase examples
    # (e.g. "unlessTrusted") are rejected by the live CLI.
    approval_policy: str | None = Field(
        default=None, pattern="^(untrusted|on-request|granular|never)$"
    )
    initial_message: str | None = Field(default=None, max_length=32_768)
    # Resume-under-Memento-control: the exact native thread id of an
    # existing (view-only) conversation, plus its document for binding.
    native_session_id: str | None = Field(default=None, max_length=512)
    document_id: uuid.UUID | None = None


def _session_start_options(
    req: "ControlSessionCreateRequest", *, resume: bool
) -> dict:
    """Command options for a session start/resume.

    Fresh managed sessions must not silently inherit the machine's local
    codex defaults: a workstation configured with approval "never" +
    danger-full-access would make every managed command run unprompted.
    Omitted fields default to a safe posture; permissive modes stay
    available but only by explicit request. Resume keeps the original
    thread's configuration untouched.
    """
    sandbox = req.sandbox if resume else (req.sandbox or "workspace-write")
    approval_policy = (
        req.approval_policy if resume else (req.approval_policy or "on-request")
    )
    return {
        key: value
        for key, value in (
            ("cwd", req.cwd),
            ("model", req.model),
            ("effort", req.effort),
            ("sandbox", sandbox),
            ("approval_policy", approval_policy),
        )
        if value is not None
    }


class ControlMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=32_768)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=32)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ControlAnswerRequest(BaseModel):
    answers: dict

    @field_validator("answers")
    @classmethod
    def _bounded_answers(cls, value: dict) -> dict:
        return _bounded_json_dict(value, "answers") or {}


class ControlApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(accept|acceptForSession|decline|cancel)$")
    # Optional subset for permission requests; omitted = grant as requested.
    granted_permissions: dict | None = None

    @field_validator("granted_permissions")
    @classmethod
    def _bounded_granted(cls, value: dict | None) -> dict | None:
        return _bounded_json_dict(value, "granted_permissions")


class ControlInterruptRequest(BaseModel):
    turn_id: str | None = Field(default=None, max_length=256)


async def _load_visible_session(
    db: AsyncSession, session_id: uuid.UUID, user: User
) -> AgentControlSession:
    session = await db.get(AgentControlSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if user.role not in ("admin", "owner") and session.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _session_machine(db: AsyncSession, session: AgentControlSession) -> Machine:
    machine = await db.get(Machine, session.machine_id)
    if machine is None:
        raise HTTPException(status_code=409, detail="Session machine no longer exists")
    return machine


def _unsupported_response(error: UnsupportedCommandKind) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": ControlErrorCodes.UNSUPPORTED, "kind": error.kind},
    )


def _pending_interaction(
    session: AgentControlSession, interaction_id: str, *, kind: str
) -> dict:
    for item in session.pending_interactions or []:
        if item.get("interaction_id") == interaction_id:
            if item.get("kind") != kind:
                break
            return item
    raise HTTPException(
        status_code=409,
        detail={
            "code": ControlErrorCodes.STALE_TURN,
            "reason": "interaction_not_pending",
            "interaction_id": interaction_id,
        },
    )


async def _validate_resume_binding(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    document_id: uuid.UUID | None,
    native_session_id: str | None,
) -> None:
    """Prove that an explicitly bound transcript is the requested Codex thread.

    A native id without a document remains valid: ingest can bind the session
    later by exact read-model identity. Once the browser supplies a document,
    however, accepting an unchecked UUID would let a managed session attach to
    a transcript from another device/tool/thread.
    """
    if document_id is None:
        return
    if not native_session_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "session.binding_mismatch", "reason": "native_session_required"},
        )
    binding = (
        await db.execute(
            select(Document, ConversationReadModel)
            .join(
                ConversationReadModel,
                ConversationReadModel.document_id == Document.id,
            )
            .where(
                Document.id == document_id,
                Document.machine_id == machine_id,
                Document.tool_id == "codex",
                Document.category == "conversation",
                ConversationReadModel.machine_id == machine_id,
                ConversationReadModel.tool_id == "codex",
                ConversationReadModel.thread_id == native_session_id,
            )
        )
    ).first()
    if binding is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "session.binding_mismatch", "reason": "conversation_identity"},
        )


@router.post("/sessions")
async def create_session(
    req: ControlSessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Start (or resume, when ``native_session_id`` is given) a managed session.

    The session row and its start command are created in one transaction:
    durable admission before any side effect is acknowledged.
    """
    machine = await db.get(Machine, req.machine_id)
    if machine is None or (
        user.role not in ("admin", "owner") and machine.user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="Device not found")

    await _validate_resume_binding(
        db,
        machine_id=machine.id,
        document_id=req.document_id,
        native_session_id=req.native_session_id,
    )

    resume = bool(req.native_session_id)
    session = create_control_session(
        db,
        machine=machine,
        user_id=user.id,
        tool_id=req.tool_id,
        adapter="codex_app_server",
        native_session_id=req.native_session_id,
        document_id=req.document_id,
    )
    await db.flush()

    options = _session_start_options(req, resume=resume)
    payload: dict = {"control_session_id": str(session.id), "options": options}
    if resume:
        kind = KIND_AGENT_SESSION_RESUME
        payload["native_session_id"] = req.native_session_id
    else:
        kind = KIND_AGENT_SESSION_START
        if req.initial_message:
            payload["initial_message"] = req.initial_message
            payload["client_message_id"] = f"memento-{session.id}"
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=kind,
            payload=payload,
            control_session_id=session.id,
            document_id=req.document_id,
            native_session_id=req.native_session_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"session": session_public(session), "command": command_public(command)}


@router.get("/sessions")
async def list_sessions(
    machine_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    state: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    query = select(AgentControlSession).order_by(
        AgentControlSession.created_at.desc()
    ).limit(max(1, min(limit, 100)))
    if user.role not in ("admin", "owner"):
        query = query.where(AgentControlSession.user_id == user.id)
    if machine_id is not None:
        query = query.where(AgentControlSession.machine_id == machine_id)
    if document_id is not None:
        query = query.where(AgentControlSession.document_id == document_id)
    if state:
        query = query.where(AgentControlSession.state == state)
    sessions = (await db.execute(query)).scalars().all()
    for target_machine_id in {s.machine_id for s in sessions if s.document_id is None}:
        await bind_control_session_documents(db, machine_id=target_machine_id)
    return [session_public(session) for session in sessions]


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    session = await _load_visible_session(db, session_id, user)
    if session.document_id is None and session.native_session_id:
        await bind_control_session_documents(db, machine_id=session.machine_id)
    return {"session": session_public(session)}


@router.post("/sessions/{session_id}/messages")
async def send_session_message(
    session_id: uuid.UUID,
    req: ControlMessageRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    session = await _load_visible_session(db, session_id, user)
    if session.state not in (SESSION_ACTIVE, SESSION_STARTING):
        raise HTTPException(
            status_code=409,
            detail={"code": ControlErrorCodes.STALE_TURN, "state": session.state},
        )
    machine = await _session_machine(db, session)
    idempotency_key = req.idempotency_key or f"send-{uuid.uuid4()}"
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_TURN_SEND,
            payload={
                "control_session_id": str(session.id),
                "text": req.text,
                "model": req.model,
                "effort": req.effort,
                "client_message_id": idempotency_key,
            },
            idempotency_key=idempotency_key,
            control_session_id=session.id,
            document_id=session.document_id,
            native_session_id=session.native_session_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"command": command_public(command)}


class ControlSteerRequest(BaseModel):
    text: str = Field(min_length=1, max_length=32_768)
    expected_turn_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str | None = Field(default=None, max_length=128)


@router.post("/sessions/{session_id}/steer")
async def steer_session_turn(
    session_id: uuid.UUID,
    req: ControlSteerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Append input to the ACTIVE turn, fenced by the exact expected turn id."""
    session = await _load_visible_session(db, session_id, user)
    expected_turn_id = req.expected_turn_id or session.active_native_turn_id
    if not expected_turn_id:
        raise HTTPException(
            status_code=409,
            detail={"code": ControlErrorCodes.STALE_TURN, "reason": "no_active_turn"},
        )
    machine = await _session_machine(db, session)
    idempotency_key = req.idempotency_key or f"steer-{uuid.uuid4()}"
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_TURN_STEER,
            payload={
                "control_session_id": str(session.id),
                "text": req.text,
                "expected_turn_id": expected_turn_id,
                "client_message_id": idempotency_key,
            },
            idempotency_key=idempotency_key,
            control_session_id=session.id,
            document_id=session.document_id,
            native_session_id=session.native_session_id,
            native_turn_id=expected_turn_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"command": command_public(command)}


@router.post("/sessions/{session_id}/interactions/{interaction_id}/answer")
async def answer_session_interaction(
    session_id: uuid.UUID,
    interaction_id: str,
    req: ControlAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    session = await _load_visible_session(db, session_id, user)
    pending = _pending_interaction(session, interaction_id, kind="question")
    machine = await _session_machine(db, session)
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_INTERACTION_ANSWER,
            payload={
                "control_session_id": str(session.id),
                "interaction_id": interaction_id,
                "answers": req.answers,
            },
            # Duplicate taps and mobile reconnects replay the same command.
            idempotency_key=f"answer-{interaction_id}",
            control_session_id=session.id,
            document_id=session.document_id,
            native_session_id=session.native_session_id,
            native_turn_id=pending.get("native_turn_id") or None,
            interaction_id=interaction_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"command": command_public(command)}


@router.post("/sessions/{session_id}/interactions/{interaction_id}/approval")
async def respond_session_approval(
    session_id: uuid.UUID,
    interaction_id: str,
    req: ControlApprovalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    session = await _load_visible_session(db, session_id, user)
    pending = _pending_interaction(session, interaction_id, kind="approval")
    machine = await _session_machine(db, session)
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_APPROVAL_RESPOND,
            payload={
                "control_session_id": str(session.id),
                "interaction_id": interaction_id,
                "decision": req.decision,
                **(
                    {"granted_permissions": req.granted_permissions}
                    if req.granted_permissions is not None
                    else {}
                ),
            },
            idempotency_key=f"approval-{interaction_id}",
            control_session_id=session.id,
            document_id=session.document_id,
            native_session_id=session.native_session_id,
            native_turn_id=pending.get("native_turn_id") or None,
            interaction_id=interaction_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"command": command_public(command)}


@router.post("/sessions/{session_id}/interrupt")
async def interrupt_session(
    session_id: uuid.UUID,
    req: ControlInterruptRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    session = await _load_visible_session(db, session_id, user)
    turn_id = req.turn_id or session.active_native_turn_id
    if not turn_id:
        raise HTTPException(
            status_code=409,
            detail={"code": ControlErrorCodes.STALE_TURN, "reason": "no_active_turn"},
        )
    machine = await _session_machine(db, session)
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_TURN_INTERRUPT,
            payload={"control_session_id": str(session.id), "turn_id": turn_id},
            idempotency_key=f"interrupt-{session.id}-{turn_id}",
            control_session_id=session.id,
            document_id=session.document_id,
            native_session_id=session.native_session_id,
            native_turn_id=turn_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"command": command_public(command)}


@router.post("/sessions/{session_id}/close")
async def close_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    session = await _load_visible_session(db, session_id, user)
    machine = await _session_machine(db, session)
    try:
        command, _created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_SESSION_CLOSE,
            payload={"control_session_id": str(session.id)},
            idempotency_key=f"close-{session.id}",
            control_session_id=session.id,
            document_id=session.document_id,
            native_session_id=session.native_session_id,
        )
    except UnsupportedCommandKind as error:
        raise _unsupported_response(error) from error
    # Commit before returning: yield-dependency teardown runs after the
    # response is sent, so an immediate read could otherwise miss the row.
    await db.commit()
    return {"command": command_public(command)}


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
