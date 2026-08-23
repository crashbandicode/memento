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
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    AgentControlCommand,
    AgentControlEvent,
    Base,
    ConversationReadModel,
    Document,
    Machine,
    Tool,
    User,
)
from server.api.control import _validate_resume_binding
from server.services.agent_control import (
    AGENT_KINDS,
    KIND_AGENT_INTERACTION_ANSWER,
    KIND_AGENT_SESSION_CLOSE,
    KIND_AGENT_SESSION_START,
    KIND_AGENT_TURN_SEND,
    KIND_CONVERSATION_REPAIR,
    KIND_DEVICE_RESYNC,
    LEGACY_ACTION_TO_KIND,
    POLICY_FAIL_ONCE_DELIVERED,
    POLICY_RETRY,
    SESSION_ACTIVE,
    SESSION_CLOSED,
    SESSION_FAILED,
    STATE_COMPLETED,
    STATE_DELIVERED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_LEASED,
    STATE_QUEUED,
    _DEFAULT_REDELIVERY_POLICY,
    ControlErrorCodes,
    ControlEventScopeError,
    StaleControlLease,
    UnsupportedCommandKind,
    _supported_kinds,
    acknowledge_command,
    acknowledge_legacy_command,
    admit_command,
    bind_control_session_documents,
    command_public,
    complete_command,
    create_control_session,
    ingest_control_events,
    lease_commands,
    lease_legacy_commands,
    reap_stale_commands,
    renew_command_lease,
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


@requires_postgres
@pytest.mark.asyncio
async def test_lease_renewal_extends_live_leases_and_rejects_stale_fences(
    session_factory,
) -> None:
    async with session_factory() as db:
        user, machine = await _seed(db)
        command, _ = await admit_command(
            db, machine=machine, user_id=user.id, kind=KIND_CONVERSATION_REPAIR
        )
        leased = (await lease_commands(db, machine=machine, lease_seconds=30))[0]
        before = leased.lease_expires_at

        renewed = await renew_command_lease(
            db,
            machine=machine,
            command_id=command.id,
            lease_id=leased.lease_id,
            lease_seconds=300,
        )
        assert renewed.lease_expires_at > before

        with pytest.raises(StaleControlLease):
            await renew_command_lease(
                db,
                machine=machine,
                command_id=command.id,
                lease_id=uuid.uuid4(),  # wrong fencing token
            )

        await complete_command(
            db,
            machine=machine,
            command_id=command.id,
            lease_id=leased.lease_id,
            status=STATE_COMPLETED,
        )
        # Renewal can never resurrect a decided outcome.
        with pytest.raises(StaleControlLease):
            await renew_command_lease(
                db,
                machine=machine,
                command_id=command.id,
                lease_id=leased.lease_id,
            )
        await db.flush()
        events = await _event_types(db, command.id)
        assert "device.lease_renewed" in events


# ---------------------------------------------------------------------------
# Managed sessions (agent.* kinds)
# ---------------------------------------------------------------------------

def test_agent_kind_redelivery_policies_never_double_side_effects() -> None:
    for kind in AGENT_KINDS:
        expected = (
            POLICY_RETRY
            if kind in ("agent.session.close", "agent.turn.interrupt")
            else POLICY_FAIL_ONCE_DELIVERED
        )
        assert _DEFAULT_REDELIVERY_POLICY[kind] == expected


@pytest.mark.asyncio
async def test_agent_kinds_require_machine_capability() -> None:
    legacy_machine = SimpleNamespace(id=uuid.uuid4(), capabilities=None)
    with pytest.raises(UnsupportedCommandKind):
        await admit_command(
            None,
            machine=legacy_machine,
            user_id=uuid.uuid4(),
            kind=KIND_AGENT_TURN_SEND,
        )


def _agent_capabilities() -> dict:
    return {
        "schema_version": 1,
        "control": {"commands": list(AGENT_KINDS)},
        "agents": {"codex": {"adapter": "codex_app_server", "available": True}},
    }


async def _seed_agent_machine(db) -> tuple[User, Machine]:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.test",
        role="owner",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name="agent-control-test",
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
        capabilities=_agent_capabilities(),
    )
    db.add_all([user, machine])
    await db.flush()
    return user, machine


async def _run_command(db, machine, command, *, status, detail=None, error_code=None):
    leased = await lease_commands(db, machine=machine)
    target = next(item for item in leased if item.id == command.id)
    await acknowledge_command(
        db, machine=machine, command_id=command.id, lease_id=target.lease_id
    )
    return await complete_command(
        db,
        machine=machine,
        command_id=command.id,
        lease_id=target.lease_id,
        status=status,
        detail=detail,
        error_code=error_code,
    )


@requires_postgres
@pytest.mark.asyncio
async def test_managed_session_lifecycle_via_commands_and_events(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed_agent_machine(db)
        session = create_control_session(
            db, machine=machine, user_id=user.id, tool_id="codex", adapter="codex_app_server"
        )
        await db.flush()

        start, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_SESSION_START,
            payload={"control_session_id": str(session.id)},
            control_session_id=session.id,
        )
        await _run_command(
            db, machine, start, status=STATE_COMPLETED, detail={"native_thread_id": "thr_x1"}
        )
        await db.flush()
        await db.refresh(session)
        assert session.state == SESSION_ACTIVE
        assert session.native_session_id == "thr_x1"

        def _event(event_type: str, **extra) -> dict:
            return {
                "schema_version": 1,
                "event_id": str(uuid.uuid4()),
                "event_type": event_type,
                "control_session_id": session.id,
                **extra,
            }

        result = await ingest_control_events(
            db,
            machine=machine,
            events=[
                _event("adapter.turn_started", native_turn_id="turn_9"),
                _event(
                    "adapter.interaction_pending",
                    native_turn_id="turn_9",
                    interaction_id="int-1",
                    details={
                        "kind": "question",
                        "method": "item/tool/requestUserInput",
                        "request": {"questions": [{"id": "q1"}]},
                    },
                ),
            ],
        )
        assert result == {"accepted": 2, "duplicates": 0}
        await db.flush()
        await db.refresh(session)
        assert session.active_native_turn_id == "turn_9"
        assert session.pending_interactions[0]["interaction_id"] == "int-1"
        assert session.pending_interactions[0]["kind"] == "question"

        await ingest_control_events(
            db,
            machine=machine,
            events=[
                _event(
                    "adapter.interaction_resolved",
                    interaction_id="int-1",
                    outcome="answered",
                ),
                _event("adapter.turn_completed", native_turn_id="turn_9", outcome="completed"),
            ],
        )
        await db.flush()
        await db.refresh(session)
        assert session.pending_interactions == []
        assert session.active_native_turn_id is None

        close, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_SESSION_CLOSE,
            payload={"control_session_id": str(session.id)},
            control_session_id=session.id,
            idempotency_key=f"close-{session.id}",
        )
        await _run_command(db, machine, close, status=STATE_COMPLETED, detail={"closed": True})
        await db.flush()
        await db.refresh(session)
        assert session.state == SESSION_CLOSED
        assert session.closed_at is not None


@requires_postgres
@pytest.mark.asyncio
async def test_completed_turn_event_wins_when_it_arrives_before_command_outcome(
    session_factory,
) -> None:
    async with session_factory() as db:
        user, machine = await _seed_agent_machine(db)
        session = create_control_session(
            db, machine=machine, user_id=user.id, tool_id="codex", adapter="codex_app_server"
        )
        session.state = SESSION_ACTIVE
        session.native_session_id = "thr_race"
        await db.flush()
        command, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_TURN_SEND,
            payload={"control_session_id": str(session.id), "text": "fast turn"},
            control_session_id=session.id,
        )
        leased = await lease_commands(db, machine=machine)
        target = next(item for item in leased if item.id == command.id)
        await acknowledge_command(
            db, machine=machine, command_id=command.id, lease_id=target.lease_id
        )

        await ingest_control_events(
            db,
            machine=machine,
            events=[
                {
                    "schema_version": 1,
                    "event_id": str(uuid.uuid4()),
                    "event_type": "adapter.turn_completed",
                    "control_session_id": session.id,
                    "native_turn_id": "turn_fast",
                    "outcome": "completed",
                }
            ],
        )
        await complete_command(
            db,
            machine=machine,
            command_id=command.id,
            lease_id=target.lease_id,
            status=STATE_COMPLETED,
            detail={"native_turn_id": "turn_fast"},
        )
        await db.flush()
        await db.refresh(session)
        assert session.active_native_turn_id is None


@requires_postgres
@pytest.mark.asyncio
async def test_failed_session_start_marks_session_failed(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed_agent_machine(db)
        session = create_control_session(
            db, machine=machine, user_id=user.id, tool_id="codex", adapter="codex_app_server"
        )
        await db.flush()
        start, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_SESSION_START,
            payload={"control_session_id": str(session.id)},
            control_session_id=session.id,
        )
        await _run_command(
            db,
            machine,
            start,
            status=STATE_FAILED,
            error_code=ControlErrorCodes.ADAPTER_PROCESS_FAILED,
        )
        await db.flush()
        await db.refresh(session)
        assert session.state == SESSION_FAILED
        assert session.state_reason == ControlErrorCodes.ADAPTER_PROCESS_FAILED


@requires_postgres
@pytest.mark.asyncio
async def test_expired_session_start_fails_session_as_collector_offline(
    session_factory,
) -> None:
    async with session_factory() as db:
        user, machine = await _seed_agent_machine(db)
        session = create_control_session(
            db, machine=machine, user_id=user.id, tool_id="codex", adapter="codex_app_server"
        )
        await db.flush()
        start, _ = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_SESSION_START,
            payload={"control_session_id": str(session.id)},
            control_session_id=session.id,
        )
        start.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await db.flush()
        await reap_stale_commands(db, machine_id=machine.id)
        await db.flush()
        await db.refresh(session)
        assert session.state == SESSION_FAILED
        assert session.state_reason == ControlErrorCodes.COLLECTOR_OFFLINE


@requires_postgres
@pytest.mark.asyncio
async def test_answer_command_carries_interaction_fences(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed_agent_machine(db)
        session = create_control_session(
            db, machine=machine, user_id=user.id, tool_id="codex", adapter="codex_app_server"
        )
        await db.flush()
        answer, created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_INTERACTION_ANSWER,
            payload={
                "control_session_id": str(session.id),
                "interaction_id": "int-7",
                "answers": {"q1": {"answers": ["left"]}},
            },
            idempotency_key="answer-int-7",
            control_session_id=session.id,
            native_turn_id="turn_7",
            interaction_id="int-7",
        )
        assert created is True
        assert answer.interaction_id == "int-7"
        assert answer.native_turn_id == "turn_7"

        # A duplicate tap replays the identical command.
        replay, created = await admit_command(
            db,
            machine=machine,
            user_id=user.id,
            kind=KIND_AGENT_INTERACTION_ANSWER,
            payload={
                "control_session_id": str(session.id),
                "interaction_id": "int-7",
                "answers": {"q1": {"answers": ["left"]}},
            },
            idempotency_key="answer-int-7",
            control_session_id=session.id,
            interaction_id="int-7",
        )
        assert created is False
        assert replay.id == answer.id


@requires_postgres
@pytest.mark.asyncio
async def test_bind_control_session_documents_by_exact_thread_id(session_factory) -> None:
    async with session_factory() as db:
        user, machine = await _seed_agent_machine(db)
        if await db.get(Tool, "codex") is None:
            db.add(Tool(id="codex", display_name="Codex"))
        session = create_control_session(
            db,
            machine=machine,
            user_id=user.id,
            tool_id="codex",
            adapter="codex_app_server",
            native_session_id="thr_bind_1",
        )
        document = Document(
            id=uuid.uuid4(),
            tool_id="codex",
            machine_id=machine.id,
            relative_path=f"sessions/{uuid.uuid4()}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Managed session transcript",
            content_hash=uuid.uuid4().hex,
            file_size_bytes=1,
        )
        db.add(document)
        await db.flush()
        db.add(
            ConversationReadModel(
                document_id=document.id,
                machine_id=machine.id,
                tool_id="codex",
                thread_id="thr_bind_1",
            )
        )
        await db.flush()

        bound = await bind_control_session_documents(db, machine_id=machine.id)
        assert bound == 1
        await db.flush()
        await db.refresh(session)
        assert session.document_id == document.id


@requires_postgres
@pytest.mark.asyncio
async def test_resume_binding_requires_exact_machine_tool_and_thread(session_factory) -> None:
    async with session_factory() as db:
        _user, machine = await _seed_agent_machine(db)
        if await db.get(Tool, "codex") is None:
            db.add(Tool(id="codex", display_name="Codex"))
        document = Document(
            id=uuid.uuid4(),
            tool_id="codex",
            machine_id=machine.id,
            relative_path=f"sessions/{uuid.uuid4()}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Resume target",
            content_hash=uuid.uuid4().hex,
            file_size_bytes=1,
        )
        db.add(document)
        await db.flush()
        db.add(
            ConversationReadModel(
                document_id=document.id,
                machine_id=machine.id,
                tool_id="codex",
                thread_id="thr_exact",
            )
        )
        await db.flush()

        await _validate_resume_binding(
            db,
            machine_id=machine.id,
            document_id=document.id,
            native_session_id="thr_exact",
        )

        with pytest.raises(HTTPException) as wrong_thread:
            await _validate_resume_binding(
                db,
                machine_id=machine.id,
                document_id=document.id,
                native_session_id="thr_other",
            )
        assert wrong_thread.value.status_code == 409
        assert wrong_thread.value.detail["code"] == "session.binding_mismatch"

        with pytest.raises(HTTPException) as missing_native:
            await _validate_resume_binding(
                db,
                machine_id=machine.id,
                document_id=document.id,
                native_session_id=None,
            )
        assert missing_native.value.status_code == 409
