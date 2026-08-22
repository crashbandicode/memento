"""Agent-control command plane: state machine, idempotency, leases, trace.

Pure tests cover vocabulary and admission guards. The PostgreSQL-gated
integration suite proves the durable lifecycle, duplicate suppression,
lease-expiry policy, first-writer-wins outcomes, and event-receipt
idempotency against a real database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    AgentControlCommand,
    AgentControlEvent,
    Base,
    Machine,
    User,
)
from server.services.agent_control import (
    KIND_CONVERSATION_REPAIR,
    KIND_DEVICE_RESYNC,
    LEGACY_ACTION_TO_KIND,
    POLICY_FAIL_ONCE_DELIVERED,
    STATE_COMPLETED,
    STATE_DELIVERED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_LEASED,
    STATE_QUEUED,
    ControlErrorCodes,
    ControlEventScopeError,
    StaleControlLease,
    UnsupportedCommandKind,
    _supported_kinds,
    acknowledge_command,
    acknowledge_legacy_command,
    admit_command,
    command_public,
    complete_command,
    ingest_control_events,
    lease_commands,
    lease_legacy_commands,
    reap_stale_commands,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)


# ---------------------------------------------------------------------------
# Pure tests
# ---------------------------------------------------------------------------

def test_legacy_action_mapping_is_bijective() -> None:
    assert LEGACY_ACTION_TO_KIND == {
        "resync": "device.resync",
        "repair-conversations": "conversation.repair",
        "update": "collector.update",
    }


def test_machines_without_capabilities_support_only_legacy_kinds() -> None:
    machine = SimpleNamespace(capabilities=None)
    kinds = _supported_kinds(machine)
    assert kinds == frozenset(
        {"device.resync", "conversation.repair", "collector.update"}
    )


def test_reported_capabilities_extend_but_never_shrink_legacy_kinds() -> None:
    machine = SimpleNamespace(
        capabilities={
            "control": {"commands": ["agent.send_message", "device.resync"]}
        }
    )
    kinds = _supported_kinds(machine)
    assert "agent.send_message" in kinds
    assert "conversation.repair" in kinds


@pytest.mark.asyncio
async def test_unsupported_kind_is_rejected_at_admission_without_db_use() -> None:
    machine = SimpleNamespace(id=uuid.uuid4(), capabilities=None)
    with pytest.raises(UnsupportedCommandKind) as caught:
        await admit_command(
            None,  # admission guard fires before any database access
            machine=machine,
            user_id=uuid.uuid4(),
            kind="agent.send_message",
        )
    assert str(caught.value) == ControlErrorCodes.UNSUPPORTED
    assert caught.value.kind == "agent.send_message"


def test_command_public_exposes_legacy_action_alias() -> None:
    command = AgentControlCommand(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        idempotency_key="key",
        machine_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind=KIND_DEVICE_RESYNC,
        payload={},
        state=STATE_QUEUED,
        redelivery_policy=POLICY_FAIL_ONCE_DELIVERED,
        delivery_attempts=0,
        max_delivery_attempts=5,
    )
    public = command_public(command)
    assert public["kind"] == "device.resync"
    assert public["action"] == "resync"
    assert public["state"] == "queued"


# ---------------------------------------------------------------------------
# PostgreSQL integration
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def session_factory():
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(db) -> tuple[User, Machine]:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.test",
        role="owner",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name="control-test",
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
    )
    db.add_all([user, machine])
    await db.flush()
    return user, machine


async def _event_types(db, command_id) -> list[str]:
    rows = await db.execute(
        select(AgentControlEvent.event_type)
        .where(AgentControlEvent.command_id == command_id)
        .order_by(AgentControlEvent.id)
    )
    return [row[0] for row in rows.all()]


@requires_postgres
@pytest.mark.asyncio
async def test_full_lifecycle_with_duplicate_admission_replay(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)

        command, created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_CONVERSATION_REPAIR,
            payload={"paths": [{"tool_name": "codex", "relative_path": "a.jsonl"}]},
            idempotency_key="repair-1",
        )
        assert created is True
        assert command.state == STATE_QUEUED

        replayed, replay_created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_CONVERSATION_REPAIR,
            idempotency_key="repair-1",
        )
        assert replay_created is False
        assert replayed.id == command.id

        leased = await lease_commands(db, machine=machine, lease_seconds=60)
        assert [item.id for item in leased] == [command.id]
        assert leased[0].state == STATE_LEASED
        assert leased[0].delivery_attempts == 1
        lease_id = leased[0].lease_id

        acked = await acknowledge_command(
            db, machine=machine, command_id=command.id, lease_id=lease_id
        )
        assert acked.state == STATE_DELIVERED

        done = await complete_command(
            db,
            machine=machine,
            command_id=command.id,
            lease_id=lease_id,
            status=STATE_COMPLETED,
            detail={"queued": 1},
            elapsed_ms=42,
        )
        assert done.state == STATE_COMPLETED
        assert done.outcome == {"queued": 1}
        await db.flush()

        events = await _event_types(db, command.id)
        assert events == [
            "server.admitted",
            "server.duplicate_suppressed",
            "server.lease_acquired",
            "device.acknowledged",
            "device.completed",
        ]


@requires_postgres
@pytest.mark.asyncio
async def test_stale_lease_and_duplicate_outcome_are_fenced(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)
        command, _ = await admit_command(
            db, machine=machine, user_id=user.id, kind=KIND_CONVERSATION_REPAIR
        )
        leased = (await lease_commands(db, machine=machine))[0]

        with pytest.raises(StaleControlLease):
            await acknowledge_command(
                db,
                machine=machine,
                command_id=command.id,
                lease_id=uuid.uuid4(),  # wrong fencing token
            )

        await acknowledge_command(
            db, machine=machine, command_id=command.id, lease_id=leased.lease_id
        )
        await complete_command(
            db,
            machine=machine,
            command_id=command.id,
            lease_id=leased.lease_id,
            status=STATE_FAILED,
            error_code=ControlErrorCodes.EXECUTION_FAILED,
        )

        # A late conflicting completion observes the recorded outcome.
        replay = await complete_command(
            db,
            machine=machine,
            command_id=command.id,
            lease_id=uuid.uuid4(),
            status=STATE_COMPLETED,
        )
        assert replay.state == STATE_FAILED
        assert replay.error_code == ControlErrorCodes.EXECUTION_FAILED
        await db.flush()
        events = await _event_types(db, command.id)
        assert events[-1] == "server.duplicate_outcome_suppressed"


@requires_postgres
@pytest.mark.asyncio
async def test_reaper_expires_requeues_and_fails_closed(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)
        past = datetime.now(timezone.utc) - timedelta(seconds=5)

        # 1. Queued past TTL → expired as collector-offline.
        offline, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_CONVERSATION_REPAIR,
            idempotency_key="offline",
        )
        offline.expires_at = past

        # 2. Leased but never delivered → requeued for another attempt.
        lost_lease, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_CONVERSATION_REPAIR,
            idempotency_key="lost-lease",
        )
        lost_lease.state = STATE_LEASED
        lost_lease.lease_id = uuid.uuid4()
        lost_lease.lease_expires_at = past
        lost_lease.delivery_attempts = 1

        # 3. Destructive command delivered but unreported → fails closed.
        destructive, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_DEVICE_RESYNC,
            idempotency_key="resync",
        )
        destructive.state = STATE_DELIVERED
        destructive.lease_id = uuid.uuid4()
        destructive.lease_expires_at = past
        destructive.delivery_attempts = 1
        await db.flush()

        await reap_stale_commands(db, machine_id=machine.id)
        await db.flush()

        await db.refresh(offline)
        await db.refresh(lost_lease)
        await db.refresh(destructive)
        assert offline.state == STATE_EXPIRED
        assert offline.error_code == ControlErrorCodes.COLLECTOR_OFFLINE
        assert lost_lease.state == STATE_QUEUED
        assert lost_lease.lease_id is None
        assert destructive.state == STATE_FAILED
        assert destructive.error_code == ControlErrorCodes.OUTCOME_UNREPORTED


@requires_postgres
@pytest.mark.asyncio
async def test_delivery_attempts_are_bounded(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)
        command, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_CONVERSATION_REPAIR,
            max_delivery_attempts=2,
        )
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        for _attempt in range(2):
            leased = await lease_commands(db, machine=machine)
            assert leased and leased[0].id == command.id
            leased[0].lease_expires_at = past
            await db.flush()

        final = await lease_commands(db, machine=machine)
        assert final == []
        await db.refresh(command)
        assert command.state == STATE_FAILED
        assert command.error_code == ControlErrorCodes.DELIVERY_EXHAUSTED


@requires_postgres
@pytest.mark.asyncio
async def test_collector_event_batch_is_idempotent(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)
        command, _ = await admit_command(
            db, machine=machine, user_id=user.id, kind=KIND_CONVERSATION_REPAIR
        )
        event = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "event_type": "collector.execution_started",
            "command_id": command.id,
            "trace_id": command.trace_id,
            "occurred_at_device": datetime.now(timezone.utc),
            "elapsed_ms": 3,
            "outcome": "started",
            "details": {"kind": command.kind},
        }

        first = await ingest_control_events(db, machine=machine, events=[event])
        replay = await ingest_control_events(db, machine=machine, events=[event])

        assert first == {"accepted": 1, "duplicates": 0}
        assert replay == {"accepted": 0, "duplicates": 1}


@requires_postgres
@pytest.mark.asyncio
async def test_collector_events_cannot_reference_another_machine_command(
    session_factory,
) -> None:
    async with session_factory() as db:
        owner, owner_machine = await _seed(db)
        other_owner, other_machine = await _seed(db)
        other_command, _ = await admit_command(
            db,
            machine=other_machine,
            user_id=other_owner.id,
            kind=KIND_CONVERSATION_REPAIR,
        )
        event = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "event_type": "collector.execution_started",
            "command_id": other_command.id,
            "trace_id": other_command.trace_id,
            "details": {},
        }

        with pytest.raises(ControlEventScopeError) as caught:
            await ingest_control_events(
                db,
                machine=owner_machine,
                events=[event],
            )

        assert caught.value.field == "command_id"
        rows = await db.execute(
            select(AgentControlEvent).where(
                AgentControlEvent.machine_id == owner_machine.id,
                AgentControlEvent.event_id == event["event_id"],
            )
        )
        assert rows.scalar_one_or_none() is None


@requires_postgres
@pytest.mark.asyncio
async def test_legacy_shortpoll_serves_and_terminalizes_honestly(
    session_factory,
) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)
        command, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_DEVICE_RESYNC,
        )

        served = await lease_legacy_commands(db, machine=machine)
        assert len(served) == 1
        assert served[0]["id"] == str(command.id)
        assert served[0]["action"] == "resync"
        assert isinstance(served[0]["created_at"], float)

        acked = await acknowledge_legacy_command(
            db, machine=machine, command_id=command.id
        )
        assert acked.state == STATE_COMPLETED
        assert acked.outcome == {"legacy_ack": True, "execution_observed": False}

        # Terminal command must never be served again.
        assert await lease_legacy_commands(db, machine=machine) == []
