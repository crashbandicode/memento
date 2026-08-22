"""Durable agent-control command plane.

Replaces the process-local device command queue with authoritative command
rows plus an append-only lifecycle trace ("event-sourcing lite"). Invariants:

- A command is admitted durably (idempotently) before any side effect is
  acknowledged; a duplicate idempotency key replays the original command.
- Delivery happens under an expiring lease fenced by ``lease_id``; delivery
  never closes a command — only an explicit terminal transition does.
- Every transition appends a structural ``agent_control_events`` row whose
  authoritative order is ``received_at_server``. Content is referenced by
  digest, never duplicated into the trace.
- Terminal outcomes are first-writer-wins: a late or duplicate completion
  observes the recorded outcome instead of overwriting it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    AgentControlCommand,
    AgentControlEvent,
    AgentControlSession,
    Document,
    Machine,
)
from ..db.session import queue_realtime_event
from .document_delivery import (
    delivery_file_size_expression,
    delivery_metadata_expression,
    delivery_revision_expression,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

STATE_QUEUED = "queued"
STATE_LEASED = "leased"
STATE_DELIVERED = "delivered"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED, STATE_EXPIRED})

POLICY_RETRY = "retry"
POLICY_FAIL_ONCE_DELIVERED = "fail_once_delivered"

KIND_DEVICE_RESYNC = "device.resync"
KIND_CONVERSATION_REPAIR = "conversation.repair"
KIND_COLLECTOR_UPDATE = "collector.update"
LEGACY_KINDS = frozenset({KIND_DEVICE_RESYNC, KIND_CONVERSATION_REPAIR, KIND_COLLECTOR_UPDATE})
KNOWN_KINDS = frozenset(LEGACY_KINDS)

LEGACY_ACTION_TO_KIND = {
    "resync": KIND_DEVICE_RESYNC,
    "repair-conversations": KIND_CONVERSATION_REPAIR,
    "update": KIND_COLLECTOR_UPDATE,
}
KIND_TO_LEGACY_ACTION = {kind: action for action, kind in LEGACY_ACTION_TO_KIND.items()}

# device.resync clears the collector's durable queue and rescans everything;
# never re-run it just because its outcome report was lost.
_DEFAULT_REDELIVERY_POLICY = {
    KIND_DEVICE_RESYNC: POLICY_FAIL_ONCE_DELIVERED,
    KIND_CONVERSATION_REPAIR: POLICY_RETRY,
    KIND_COLLECTOR_UPDATE: POLICY_RETRY,
}

# Undelivered command time-to-live before it expires as collector-offline.
_DEFAULT_QUEUED_TTL = timedelta(hours=1)
DEFAULT_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 300
_REPAIR_BATCH_SIZE = 2
_STORED_SOURCE_REVISION_KEY = "_stored_source_revision_hash"


class ControlErrorCodes:
    """Stable machine-readable failure codes. Tests depend on these strings."""

    COLLECTOR_OFFLINE = "transport.collector_offline"
    CONNECTION_LOST = "transport.connection_lost"
    DELIVERY_EXHAUSTED = "transport.delivery_exhausted"
    DUPLICATE = "admission.duplicate"
    STALE_LEASE = "admission.stale_lease"
    STALE_TURN = "admission.stale_turn"
    UNKNOWN_COMMAND = "admission.unknown_command"
    UNSUPPORTED = "capability.unsupported"
    ADAPTER_PROCESS_FAILED = "adapter.process_failed"
    AGENT_REQUEST_REJECTED = "agent.request_rejected"
    AGENT_TIMEOUT = "agent.timeout"
    EXECUTION_FAILED = "command.execution_failed"
    OUTCOME_UNREPORTED = "command.outcome_unreported"
    RECONCILIATION_EVENT_MISSING = "reconciliation.event_missing"
    RECONCILIATION_TRANSCRIPT_MISMATCH = "reconciliation.transcript_mismatch"
    AUTH_SCOPE_DENIED = "auth.scope_denied"


class ControlCommandNotFound(Exception):
    pass


class StaleControlLease(Exception):
    def __init__(self, command: AgentControlCommand) -> None:
        super().__init__(ControlErrorCodes.STALE_LEASE)
        self.command = command


class UnsupportedCommandKind(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(ControlErrorCodes.UNSUPPORTED)
        self.kind = kind


class ControlEventScopeError(Exception):
    """A collector event referenced state outside its authenticated machine."""

    def __init__(self, field: str) -> None:
        super().__init__(ControlErrorCodes.AUTH_SCOPE_DENIED)
        self.field = field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def server_revision() -> str | None:
    return os.environ.get("MEMENTO_SERVER_REVISION") or None


def command_public(command: AgentControlCommand) -> dict:
    """Bounded API projection of one authoritative command row."""
    return {
        "id": str(command.id),
        "trace_id": str(command.trace_id),
        "idempotency_key": command.idempotency_key,
        "machine_id": str(command.machine_id),
        "kind": command.kind,
        "action": KIND_TO_LEGACY_ACTION.get(command.kind),
        "payload": dict(command.payload or {}),
        "control_session_id": (
            str(command.control_session_id) if command.control_session_id else None
        ),
        "document_id": str(command.document_id) if command.document_id else None,
        "native_session_id": command.native_session_id,
        "native_turn_id": command.native_turn_id,
        "interaction_id": command.interaction_id,
        "state": command.state,
        "redelivery_policy": command.redelivery_policy,
        "lease_id": str(command.lease_id) if command.lease_id else None,
        "lease_expires_at": (
            command.lease_expires_at.isoformat() if command.lease_expires_at else None
        ),
        "delivery_attempts": command.delivery_attempts,
        "max_delivery_attempts": command.max_delivery_attempts,
        "expires_at": command.expires_at.isoformat() if command.expires_at else None,
        "error_code": command.error_code,
        "outcome": command.outcome,
        "created_at": command.created_at.isoformat() if command.created_at else None,
        "delivered_at": command.delivered_at.isoformat() if command.delivered_at else None,
        "terminal_at": command.terminal_at.isoformat() if command.terminal_at else None,
    }


def event_public(event: AgentControlEvent) -> dict:
    return {
        "id": event.id,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "origin": event.origin,
        "command_id": str(event.command_id) if event.command_id else None,
        "trace_id": str(event.trace_id) if event.trace_id else None,
        "parent_event_id": event.parent_event_id,
        "outcome": event.outcome,
        "error_code": event.error_code,
        "adapter": event.adapter,
        "adapter_version": event.adapter_version,
        "collector_revision": event.collector_revision,
        "server_revision": event.server_revision,
        "occurred_at_device": (
            event.occurred_at_device.isoformat() if event.occurred_at_device else None
        ),
        "elapsed_ms": event.elapsed_ms,
        "received_at_server": (
            event.received_at_server.isoformat() if event.received_at_server else None
        ),
        "payload_digest": event.payload_digest,
        "details": dict(event.details or {}),
    }


def append_event(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    event_type: str,
    command: AgentControlCommand | None = None,
    origin: str = "server",
    outcome: str | None = None,
    error_code: str | None = None,
    details: dict | None = None,
    collector_revision: str | None = None,
) -> AgentControlEvent:
    """Append one server-originated structural lifecycle event."""
    event = AgentControlEvent(
        event_id=str(uuid.uuid4()),
        machine_id=machine_id,
        command_id=command.id if command is not None else None,
        control_session_id=command.control_session_id if command is not None else None,
        trace_id=command.trace_id if command is not None else None,
        event_type=event_type,
        origin=origin,
        document_id=command.document_id if command is not None else None,
        native_session_id=command.native_session_id if command is not None else None,
        native_turn_id=command.native_turn_id if command is not None else None,
        interaction_id=command.interaction_id if command is not None else None,
        collector_revision=collector_revision,
        server_revision=server_revision(),
        outcome=outcome,
        error_code=error_code,
        details=details or {},
    )
    db.add(event)
    return event


def _publish_command_state(db: AsyncSession, command: AgentControlCommand) -> None:
    queue_realtime_event(
        db,
        "control_command",
        {
            "command_id": str(command.id),
            "trace_id": str(command.trace_id),
            "machine_id": str(command.machine_id),
            "document_id": str(command.document_id) if command.document_id else None,
            "kind": command.kind,
            "state": command.state,
            "error_code": command.error_code,
        },
        user_id=str(command.user_id),
    )


def _supported_kinds(machine: Machine) -> frozenset[str]:
    """Kinds the machine's collector can execute.

    Collectors that have never reported capabilities are legacy short-poll
    builds: they handle exactly the legacy command set.
    """
    capabilities = machine.capabilities or {}
    control = capabilities.get("control") if isinstance(capabilities, dict) else None
    declared = control.get("commands") if isinstance(control, dict) else None
    if isinstance(declared, list):
        return frozenset(str(kind) for kind in declared) | LEGACY_KINDS
    return LEGACY_KINDS


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

async def admit_command(
    db: AsyncSession,
    *,
    machine: Machine,
    user_id: uuid.UUID,
    kind: str,
    payload: dict | None = None,
    idempotency_key: str | None = None,
    document_id: uuid.UUID | None = None,
    control_session_id: uuid.UUID | None = None,
    native_session_id: str | None = None,
    native_turn_id: str | None = None,
    interaction_id: str | None = None,
    redelivery_policy: str | None = None,
    queued_ttl: timedelta | None = None,
    max_delivery_attempts: int | None = None,
) -> tuple[AgentControlCommand, bool]:
    """Durably admit one command; a repeated idempotency key replays the original.

    Returns ``(command, created)``. Raises :class:`UnsupportedCommandKind`
    when neither the legacy set nor the machine's reported capabilities can
    execute ``kind`` — an honest admission-time rejection instead of a
    command that could never be delivered.
    """
    if kind not in KNOWN_KINDS and kind not in _supported_kinds(machine):
        raise UnsupportedCommandKind(kind)

    now = _now()
    command_id = uuid.uuid4()
    key = idempotency_key or str(command_id)
    values = {
        "id": command_id,
        "trace_id": uuid.uuid4(),
        "idempotency_key": key,
        "machine_id": machine.id,
        "user_id": user_id,
        "kind": kind,
        "payload": payload or {},
        "control_session_id": control_session_id,
        "document_id": document_id,
        "native_session_id": native_session_id,
        "native_turn_id": native_turn_id,
        "interaction_id": interaction_id,
        "state": STATE_QUEUED,
        "redelivery_policy": (
            redelivery_policy
            or _DEFAULT_REDELIVERY_POLICY.get(kind, POLICY_RETRY)
        ),
        "delivery_attempts": 0,
        "max_delivery_attempts": max_delivery_attempts or 5,
        "expires_at": now + (queued_ttl or _DEFAULT_QUEUED_TTL),
    }
    inserted_id = (
        await db.execute(
            pg_insert(AgentControlCommand)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["machine_id", "idempotency_key"])
            .returning(AgentControlCommand.id)
        )
    ).scalar_one_or_none()

    command = (
        await db.execute(
            select(AgentControlCommand).where(
                AgentControlCommand.machine_id == machine.id,
                AgentControlCommand.idempotency_key == key,
            )
        )
    ).scalar_one()

    if inserted_id is None:
        append_event(
            db,
            machine_id=machine.id,
            event_type="server.duplicate_suppressed",
            command=command,
            outcome="duplicate",
            error_code=ControlErrorCodes.DUPLICATE,
        )
        return command, False

    append_event(
        db,
        machine_id=machine.id,
        event_type="server.admitted",
        command=command,
        outcome="accepted",
        details={"kind": kind},
    )
    _publish_command_state(db, command)
    return command, True


# ---------------------------------------------------------------------------
# Lazy reaping — no dedicated poller; runs inside lease/status requests
# ---------------------------------------------------------------------------

async def reap_stale_commands(db: AsyncSession, *, machine_id: uuid.UUID) -> int:
    """Advance overdue non-terminal commands for one machine.

    - queued past ``expires_at``           → expired (collector offline)
    - leased past its lease                → requeued, or delivery-exhausted
    - delivered past its lease, ``retry``  → requeued (idempotent kinds only)
    - delivered past its lease, otherwise  → failed (outcome unreported);
      never re-runs a destructive command whose completion report was lost.
    """
    now = _now()
    stale = (
        await db.execute(
            select(AgentControlCommand)
            .where(
                AgentControlCommand.machine_id == machine_id,
                or_(
                    and_(
                        AgentControlCommand.state == STATE_QUEUED,
                        AgentControlCommand.expires_at.isnot(None),
                        AgentControlCommand.expires_at <= now,
                    ),
                    and_(
                        AgentControlCommand.state.in_(
                            (STATE_LEASED, STATE_DELIVERED)
                        ),
                        AgentControlCommand.lease_expires_at.isnot(None),
                        AgentControlCommand.lease_expires_at <= now,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    changed = 0
    for command in stale:
        if command.state == STATE_QUEUED:
            command.state = STATE_EXPIRED
            command.error_code = ControlErrorCodes.COLLECTOR_OFFLINE
            command.terminal_at = now
            append_event(
                db,
                machine_id=machine_id,
                event_type="server.command_expired",
                command=command,
                outcome="expired",
                error_code=ControlErrorCodes.COLLECTOR_OFFLINE,
            )
            _publish_command_state(db, command)
            changed += 1
            continue

        append_event(
            db,
            machine_id=machine_id,
            event_type="server.lease_expired",
            command=command,
            outcome="lease_expired",
        )
        retriable = (
            command.redelivery_policy == POLICY_RETRY
            or command.state == STATE_LEASED
        )
        if retriable and command.delivery_attempts < command.max_delivery_attempts:
            command.state = STATE_QUEUED
            command.lease_id = None
            command.lease_expires_at = None
            append_event(
                db,
                machine_id=machine_id,
                event_type="server.command_requeued",
                command=command,
                outcome="requeued",
                details={"delivery_attempts": command.delivery_attempts},
            )
        else:
            command.state = STATE_FAILED
            command.error_code = (
                ControlErrorCodes.DELIVERY_EXHAUSTED
                if retriable
                else ControlErrorCodes.OUTCOME_UNREPORTED
            )
            command.terminal_at = now
            append_event(
                db,
                machine_id=machine_id,
                event_type="server.delivery_exhausted"
                if retriable
                else "server.outcome_unreported",
                command=command,
                outcome="failed",
                error_code=command.error_code,
            )
        _publish_command_state(db, command)
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# Lease / deliver / complete
# ---------------------------------------------------------------------------

async def lease_commands(
    db: AsyncSession,
    *,
    machine: Machine,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    limit: int = 8,
    kinds: frozenset[str] | None = None,
    collector_revision: str | None = None,
) -> list[AgentControlCommand]:
    """Lease up to ``limit`` queued commands the caller is able to execute."""
    await reap_stale_commands(db, machine_id=machine.id)
    now = _now()
    lease_seconds = max(5, min(int(lease_seconds), MAX_LEASE_SECONDS))
    eligible_kinds = kinds if kinds is not None else _supported_kinds(machine)

    commands = (
        await db.execute(
            select(AgentControlCommand)
            .where(
                AgentControlCommand.machine_id == machine.id,
                AgentControlCommand.state == STATE_QUEUED,
                AgentControlCommand.kind.in_(sorted(eligible_kinds)),
            )
            .order_by(AgentControlCommand.created_at)
            .limit(max(1, min(int(limit), 16)))
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    for command in commands:
        command.state = STATE_LEASED
        command.lease_id = uuid.uuid4()
        command.lease_expires_at = now + timedelta(seconds=lease_seconds)
        command.delivery_attempts += 1
        append_event(
            db,
            machine_id=machine.id,
            event_type="server.lease_acquired",
            command=command,
            outcome="leased",
            collector_revision=collector_revision,
            details={"delivery_attempts": command.delivery_attempts},
        )
        _publish_command_state(db, command)
    return list(commands)


async def _load_owned_command(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    command_id: uuid.UUID,
) -> AgentControlCommand:
    command = (
        await db.execute(
            select(AgentControlCommand)
            .where(
                AgentControlCommand.id == command_id,
                AgentControlCommand.machine_id == machine_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if command is None:
        raise ControlCommandNotFound(str(command_id))
    return command


async def acknowledge_command(
    db: AsyncSession,
    *,
    machine: Machine,
    command_id: uuid.UUID,
    lease_id: uuid.UUID,
    collector_revision: str | None = None,
) -> AgentControlCommand:
    """Record device acknowledgement (delivered). Fenced by the lease id."""
    command = await _load_owned_command(db, machine_id=machine.id, command_id=command_id)
    if command.state == STATE_DELIVERED and command.lease_id == lease_id:
        return command  # idempotent retry of the same acknowledgement
    if command.state != STATE_LEASED or command.lease_id != lease_id:
        raise StaleControlLease(command)
    command.state = STATE_DELIVERED
    command.delivered_at = _now()
    append_event(
        db,
        machine_id=machine.id,
        event_type="device.acknowledged",
        command=command,
        origin="collector",
        outcome="delivered",
        collector_revision=collector_revision,
    )
    _publish_command_state(db, command)
    return command


async def complete_command(
    db: AsyncSession,
    *,
    machine: Machine,
    command_id: uuid.UUID,
    lease_id: uuid.UUID,
    status: str,
    error_code: str | None = None,
    detail: dict | None = None,
    occurred_at_device: datetime | None = None,
    elapsed_ms: int | None = None,
    collector_revision: str | None = None,
) -> AgentControlCommand:
    """Record the terminal outcome. First writer wins; duplicates replay it."""
    if status not in (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED):
        raise ValueError(f"invalid terminal status: {status}")
    command = await _load_owned_command(db, machine_id=machine.id, command_id=command_id)

    if command.state in TERMINAL_STATES:
        if command.state != status or command.lease_id != lease_id:
            append_event(
                db,
                machine_id=machine.id,
                event_type="server.duplicate_outcome_suppressed",
                command=command,
                outcome="duplicate",
                error_code=ControlErrorCodes.DUPLICATE,
                details={"reported_status": status},
            )
        return command

    if command.lease_id != lease_id or command.state not in (
        STATE_LEASED,
        STATE_DELIVERED,
    ):
        raise StaleControlLease(command)

    command.state = status
    command.error_code = error_code
    command.outcome = detail or {}
    command.terminal_at = _now()
    event = AgentControlEvent(
        event_id=str(uuid.uuid4()),
        machine_id=machine.id,
        command_id=command.id,
        control_session_id=command.control_session_id,
        trace_id=command.trace_id,
        event_type="device.completed" if status == STATE_COMPLETED else f"device.{status}",
        origin="collector",
        document_id=command.document_id,
        native_session_id=command.native_session_id,
        native_turn_id=command.native_turn_id,
        interaction_id=command.interaction_id,
        collector_revision=collector_revision,
        server_revision=server_revision(),
        occurred_at_device=occurred_at_device,
        elapsed_ms=elapsed_ms,
        outcome=status,
        error_code=error_code,
        details=detail or {},
    )
    db.add(event)
    _publish_command_state(db, command)
    return command


async def ingest_control_events(
    db: AsyncSession,
    *,
    machine: Machine,
    events: list[dict],
) -> dict[str, int]:
    """Idempotently persist collector-originated lifecycle events.

    Replays after collector restarts are absorbed by the
    ``(machine_id, event_id)`` uniqueness fence, mirroring the proven
    orchestration receipt pattern.
    """
    command_ids = {
        event.get("command_id") for event in events if event.get("command_id")
    }
    session_ids = {
        event.get("control_session_id")
        for event in events
        if event.get("control_session_id")
    }
    document_ids = {
        event.get("document_id") for event in events if event.get("document_id")
    }
    owned_commands = {
        command.id: command
        for command in (
            await db.execute(
                select(AgentControlCommand).where(
                    AgentControlCommand.machine_id == machine.id,
                    AgentControlCommand.id.in_(command_ids),
                )
            )
        ).scalars().all()
    } if command_ids else {}
    owned_session_ids = set((
        await db.execute(
            select(AgentControlSession.id).where(
                AgentControlSession.machine_id == machine.id,
                AgentControlSession.id.in_(session_ids),
            )
        )
    ).scalars().all()) if session_ids else set()
    owned_document_ids = set((
        await db.execute(
            select(Document.id).where(
                Document.machine_id == machine.id,
                Document.id.in_(document_ids),
            )
        )
    ).scalars().all()) if document_ids else set()

    # Foreign keys alone do not express machine ownership. Validate the whole
    # batch before inserting any row so a leaked collector token cannot forge
    # trace links to another device's command, session, or document.
    for event in events:
        command_id = event.get("command_id")
        control_session_id = event.get("control_session_id")
        document_id = event.get("document_id")
        if command_id and command_id not in owned_commands:
            raise ControlEventScopeError("command_id")
        if control_session_id and control_session_id not in owned_session_ids:
            raise ControlEventScopeError("control_session_id")
        if document_id and document_id not in owned_document_ids:
            raise ControlEventScopeError("document_id")
        command = owned_commands.get(command_id)
        if command is not None:
            if event.get("trace_id") not in (None, command.trace_id):
                raise ControlEventScopeError("trace_id")
            if (
                command.control_session_id is not None
                and control_session_id not in (None, command.control_session_id)
            ):
                raise ControlEventScopeError("control_session_id")
            if (
                command.document_id is not None
                and document_id not in (None, command.document_id)
            ):
                raise ControlEventScopeError("document_id")

    accepted = duplicates = 0
    for event in events:
        command = owned_commands.get(event.get("command_id"))
        inserted = (
            await db.execute(
                pg_insert(AgentControlEvent)
                .values(
                    schema_version=int(event.get("schema_version") or 1),
                    event_id=str(event["event_id"]),
                    machine_id=machine.id,
                    command_id=event.get("command_id"),
                    control_session_id=(
                        command.control_session_id
                        if command is not None
                        else event.get("control_session_id")
                    ),
                    trace_id=(
                        command.trace_id
                        if command is not None
                        else event.get("trace_id")
                    ),
                    parent_event_id=event.get("parent_event_id"),
                    event_type=str(event["event_type"]),
                    origin="collector",
                    document_id=(
                        command.document_id
                        if command is not None and command.document_id is not None
                        else event.get("document_id")
                    ),
                    native_session_id=(
                        command.native_session_id
                        if command is not None and command.native_session_id
                        else event.get("native_session_id")
                    ),
                    native_turn_id=(
                        command.native_turn_id
                        if command is not None and command.native_turn_id
                        else event.get("native_turn_id")
                    ),
                    interaction_id=(
                        command.interaction_id
                        if command is not None and command.interaction_id
                        else event.get("interaction_id")
                    ),
                    adapter=event.get("adapter"),
                    adapter_version=event.get("adapter_version"),
                    collector_revision=event.get("collector_revision"),
                    server_revision=server_revision(),
                    occurred_at_device=event.get("occurred_at_device"),
                    elapsed_ms=event.get("elapsed_ms"),
                    outcome=event.get("outcome"),
                    error_code=event.get("error_code"),
                    payload_digest=event.get("payload_digest"),
                    details=event.get("details") or {},
                )
                .on_conflict_do_nothing(index_elements=["machine_id", "event_id"])
                .returning(AgentControlEvent.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            duplicates += 1
        else:
            accepted += 1
    return {"accepted": accepted, "duplicates": duplicates}


# ---------------------------------------------------------------------------
# Capability snapshot + repair hydration
# ---------------------------------------------------------------------------

def record_capabilities(
    machine: Machine,
    capabilities: dict | None,
    *,
    collector_version: str | None = None,
) -> None:
    """Persist the collector's bounded capability snapshot on its machine row."""
    now = _now()
    machine.last_heartbeat = now
    if collector_version:
        machine.collector_version = collector_version
    if capabilities is not None and capabilities != machine.capabilities:
        machine.capabilities = capabilities
        machine.capabilities_updated_at = now


async def repair_paths_for_machine(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    limit: int = _REPAIR_BATCH_SIZE,
) -> list[dict]:
    """Conversations whose stored source revision trails the delivered one.

    Serve-time hydration for ``conversation.repair`` commands queued without
    explicit paths, preserving the legacy poll-time enrichment behavior.
    """
    rows = await db.execute(
        select(Document.tool_id, Document.relative_path)
        .where(
            Document.machine_id == machine_id,
            Document.category == "conversation",
            Document.tool_id.in_(("codex", "claude_code", "cursor")),
            func.coalesce(
                delivery_metadata_expression()[
                    _STORED_SOURCE_REVISION_KEY
                ].as_string(),
                "",
            ) != delivery_revision_expression(),
        )
        .order_by(delivery_file_size_expression(), Document.id)
        .limit(limit)
    )
    return [
        {"tool_name": tool_name, "relative_path": relative_path}
        for tool_name, relative_path in rows.all()
    ]


async def hydrate_repair_payload(
    db: AsyncSession,
    command: AgentControlCommand,
) -> dict:
    """Return the command payload, filling missing repair paths at serve time."""
    payload = dict(command.payload or {})
    if command.kind == KIND_CONVERSATION_REPAIR and not payload.get("paths"):
        payload["paths"] = await repair_paths_for_machine(
            db, machine_id=command.machine_id
        )
    return payload


# ---------------------------------------------------------------------------
# Legacy short-poll compatibility (collector <= 0.0.40)
# ---------------------------------------------------------------------------

async def lease_legacy_commands(
    db: AsyncSession,
    *,
    machine: Machine,
    collector_version: str | None = None,
) -> list[dict]:
    """Serve pending legacy commands in the old wire shape.

    The old collector acks before executing; its GET is modeled as a lease
    and its ack as the terminal transition. The lease is short because the
    legacy poll retries every 10 seconds.
    """
    commands = await lease_commands(
        db,
        machine=machine,
        lease_seconds=45,
        kinds=LEGACY_KINDS,
        collector_revision=collector_version,
    )
    served: list[dict] = []
    for command in commands:
        payload = await hydrate_repair_payload(db, command)
        item: dict = {
            "id": str(command.id),
            "action": KIND_TO_LEGACY_ACTION.get(command.kind, command.kind),
            "created_at": command.created_at.timestamp() if command.created_at else 0.0,
        }
        item.update(payload)
        served.append(item)
    return served


async def acknowledge_legacy_command(
    db: AsyncSession,
    *,
    machine: Machine,
    command_id: uuid.UUID,
) -> AgentControlCommand:
    """Terminalize a legacy ack-before-execute command honestly.

    Old collectors report no outcome, so the recorded terminal state marks
    the execution result as unobserved rather than inventing success detail.
    """
    command = await _load_owned_command(db, machine_id=machine.id, command_id=command_id)
    if command.state in TERMINAL_STATES:
        return command
    command.state = STATE_COMPLETED
    command.outcome = {"legacy_ack": True, "execution_observed": False}
    command.delivered_at = command.delivered_at or _now()
    command.terminal_at = _now()
    append_event(
        db,
        machine_id=machine.id,
        event_type="server.legacy_ack_completed",
        command=command,
        origin="collector",
        outcome=STATE_COMPLETED,
        details={"legacy_ack": True},
    )
    _publish_command_state(db, command)
    return command
