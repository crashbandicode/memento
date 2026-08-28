import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    Base,
    ConversationMessage,
    Document,
    Machine,
    OrchestrationAgent,
    OrchestrationRun,
    Tool,
    User,
)
from server.services.orchestration_events import (
    _native_id_candidates,
    _normalized_agent_status,
    extract_orchestration_run_ids,
    ingest_orchestration_events,
    orchestration_agent_summary,
    reconcile_orchestration_for_document,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)


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


def test_extracts_only_explicit_orchestration_correlation_fields() -> None:
    payload = {
        "content": "ordinary id fanout-run-that-must-not-match",
        "tool_result": {
            "orchestrationRunId": "fanout-1cb8078a-2328-4ad4-95b5-71f34fe5a707",
        },
    }

    assert extract_orchestration_run_ids(payload) == {
        "fanout-1cb8078a-2328-4ad4-95b5-71f34fe5a707",
    }


def test_extracts_nested_and_json_escaped_correlation_fields() -> None:
    payload = (
        '{"result":"{\\"orchestration_run_id\\":'
        '\\"council-e28fdb11-3abf-431e-b1f4-3c509369dfe4\\"}"}'
    )

    assert extract_orchestration_run_ids(payload) == {
        "council-e28fdb11-3abf-431e-b1f4-3c509369dfe4",
    }


def test_extracts_cursor_markdown_correlation_field_without_matching_prompt() -> None:
    payload = {
        "prompt": "Wait, stop the session, and report orchestrationRunId plus native id.",
        "result": (
            "Session completed and stopped.\n\n"
            "- **orchestrationRunId:** `session-180f7970-199`\n"
            "- **native child session id:** `7af8ed9b-1889-4fed-bf57-ecd7252f8de9`"
        ),
    }

    assert extract_orchestration_run_ids(payload) == {"session-180f7970-199"}


def test_cursor_legacy_wrapper_id_is_resolved_without_weak_matching() -> None:
    assert _native_id_candidates("cursor", "cursor-live-9a91d874") == {
        "cursor-live-9a91d874",
        "9a91d874",
    }
    assert _native_id_candidates("codex", "cursor-live-9a91d874") == {
        "cursor-live-9a91d874",
    }


def test_failed_orchestration_agent_summary_is_visible_without_transcript() -> None:
    now = datetime(2026, 8, 20, 20, 46, tzinfo=timezone.utc)
    run = OrchestrationRun(
        id=uuid4(),
        machine_id=uuid4(),
        user_id=uuid4(),
        installation_id="install-yoga",
        external_run_id="session-596e88ad-720",
        orchestrator="claw-orchestrator",
        orchestrator_version="5.0.0-memento.4",
        run_kind="session",
        status="failed",
        started_at=now,
        ended_at=now,
        last_event_at=now,
    )
    agent = OrchestrationAgent(
        id=uuid4(),
        run_id=run.id,
        agent_key="agent-main",
        agent_name="MEMENTO-CLAW-TEST-YOGA-CLAUDE-TO-CURSOR-F3",
        engine="cursor",
        model="gemini-3.7-flash-high",
        effort="auto",
        status="failed",
        last_event_at=now,
    )

    summary = orchestration_agent_summary(run, agent)

    assert summary["id"] is None
    assert summary["document_ready"] is False
    assert summary["orchestration"] == "claw"
    assert summary["status"] == "failed"
    assert summary["tool_id"] == "cursor"
    assert summary["model"] == "gemini-3.7-flash-high"


def test_external_statuses_map_to_existing_subagent_lifecycle_vocabulary() -> None:
    assert _normalized_agent_status("declared") == "running"
    assert _normalized_agent_status("idle") == "running"
    assert _normalized_agent_status("aborted") == "interrupted"
    assert _normalized_agent_status("failed") == "failed"


@requires_postgres
@pytest.mark.asyncio
async def test_event_retry_and_arrival_order_converge_to_exact_documents(
    session_factory,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-test",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        for tool_id, display_name in (
            ("codex", "Codex"),
            ("claude_code", "Claude Code"),
        ):
            if await db.get(Tool, tool_id) is None:
                db.add(Tool(id=tool_id, display_name=display_name))
        db.add_all([user, machine])
        run_id = f"fanout-{uuid4()}"
        child_session_id = str(uuid4())
        parent = Document(
            id=uuid4(),
            tool_id="codex",
            machine_id=machine.id,
            relative_path=f"sessions/{uuid4()}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Parent",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": str(uuid4())},
        )
        child = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Child",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": child_session_id},
        )
        db.add_all([parent, child])
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=parent.id,
                line_number=1,
                role="assistant",
                content=(
                    "Session completed and stopped.\n\n"
                    f"- **orchestrationRunId:** `{run_id}`"
                ),
            )
        )
        await db.flush()
        occurred_at = datetime.now(timezone.utc)
        event = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "occurred_at": occurred_at,
            "installation_id": str(uuid4()),
            "orchestrator": "claw-orchestrator",
            "orchestrator_version": "5.0.0-memento.1",
            "event": "agent.identity_bound",
            "run_id": run_id,
            "run_kind": "fanout",
            "agent_key": "reviewer",
            "agent_name": "Review implementation",
            "codename": "Sentinel",
            "engine": "claude",
            "model": "claude-opus-4-1",
            "effort": "high",
            "cwd": "C:\\work",
            "native_session_id": child_session_id,
            "agent_status": "running",
        }

        first = await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[event],
        )
        retry = await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[event],
        )
        await db.flush()

        run = (await db.execute(select(OrchestrationRun))).scalar_one()
        agent = (await db.execute(select(OrchestrationAgent))).scalar_one()
        await db.refresh(child)
        assert first == {"accepted": 1, "duplicates": 0, "linked": 1}
        assert retry == {"accepted": 0, "duplicates": 1, "linked": 0}
        assert run.parent_document_id == parent.id
        assert agent.document_id == child.id
        assert child.metadata_["orchestration_parent_document_id"] == str(parent.id)
        assert child.metadata_["orchestration_agent_name"] == "Review implementation"


@requires_postgres
@pytest.mark.asyncio
async def test_parentless_delegate_is_stamped_before_parent_linkage(
    session_factory,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-parentless",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "claude_code") is None:
            db.add(Tool(id="claude_code", display_name="Claude Code"))
        db.add_all([user, machine])
        child_session_id = str(uuid4())
        child = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Orphan delegate",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": child_session_id},
        )
        db.add(child)
        await db.flush()
        occurred_at = datetime.now(timezone.utc)
        event = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "occurred_at": occurred_at,
            "installation_id": str(uuid4()),
            "orchestrator": "claw-orchestrator",
            "orchestrator_version": "5.0.0-memento.1",
            "event": "agent.identity_bound",
            "run_id": f"session-{uuid4()}",
            "run_kind": "session",
            "agent_key": "implementer",
            "agent_name": "Implement visibility",
            "engine": "claude",
            "native_session_id": child_session_id,
            "agent_status": "running",
        }

        result = await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[event],
        )
        await db.flush()
        await db.refresh(child)
        run = (await db.execute(select(OrchestrationRun))).scalar_one()

        assert result["accepted"] == 1
        assert result["linked"] == 1
        assert run.parent_document_id is None
        assert child.metadata_["orchestration"] == "claw"
        assert child.metadata_["is_subagent"] is True
        assert "orchestration_parent_document_id" not in (child.metadata_ or {})


@requires_postgres
@pytest.mark.asyncio
async def test_handoff_successor_is_not_stamped_as_claw_delegate(
    session_factory,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-handoff",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "claude_code") is None:
            db.add(Tool(id="claude_code", display_name="Claude Code"))
        db.add_all([user, machine])
        parent_session_id = str(uuid4())
        child_session_id = str(uuid4())
        run_id = f"session-{uuid4()}"
        parent = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{parent_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Predecessor",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": parent_session_id},
        )
        child = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Successor",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={
                "session_id": child_session_id,
                "first_user_message": f"MEMENTO-HANDOFF-FROM: {parent_session_id}\n",
            },
        )
        db.add_all([parent, child])
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=parent.id,
                line_number=1,
                role="assistant",
                content=f"orchestrationRunId: {run_id}",
            )
        )
        db.add(
            ConversationMessage(
                document_id=child.id,
                line_number=1,
                role="user",
                content=f"MEMENTO-HANDOFF-FROM: {parent_session_id}\nContinue the work.",
            )
        )
        await db.flush()
        occurred_at = datetime.now(timezone.utc)
        await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[{
                "schema_version": 1,
                "event_id": str(uuid4()),
                "occurred_at": occurred_at,
                "installation_id": str(uuid4()),
                "orchestrator": "claw-orchestrator",
                "orchestrator_version": "5.0.0-memento.1",
                "event": "agent.identity_bound",
                "run_id": run_id,
                "run_kind": "session",
                "agent_key": "successor",
                "agent_name": "Resume",
                "engine": "claude",
                "native_session_id": child_session_id,
                "agent_status": "running",
            }],
        )
        await db.flush()
        await db.refresh(child)
        run = (await db.execute(select(OrchestrationRun))).scalar_one()

        assert run.parent_document_id == parent.id
        assert (child.metadata_ or {}).get("orchestration") != "claw"
        assert (child.metadata_ or {}).get("is_subagent") is not True


@requires_postgres
@pytest.mark.asyncio
async def test_tangent_branch_is_not_stamped_as_claw_delegate(
    session_factory,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-tangent",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "claude_code") is None:
            db.add(Tool(id="claude_code", display_name="Claude Code"))
        db.add_all([user, machine])
        parent_session_id = str(uuid4())
        child_session_id = str(uuid4())
        run_id = f"session-{uuid4()}"
        parent = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{parent_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Parent",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": parent_session_id},
        )
        child = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Tangent",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={
                "session_id": child_session_id,
                "first_user_message": f"MEMENTO-TANGENT-FROM: {parent_session_id}\n",
            },
        )
        db.add_all([parent, child])
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=parent.id,
                line_number=1,
                role="assistant",
                content=f"orchestrationRunId: {run_id}",
            )
        )
        db.add(
            ConversationMessage(
                document_id=child.id,
                line_number=1,
                role="user",
                content=f"MEMENTO-TANGENT-FROM: {parent_session_id}\nExplore separately.",
            )
        )
        await db.flush()
        occurred_at = datetime.now(timezone.utc)
        await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[{
                "schema_version": 1,
                "event_id": str(uuid4()),
                "occurred_at": occurred_at,
                "installation_id": str(uuid4()),
                "orchestrator": "claw-orchestrator",
                "orchestrator_version": "5.0.0-memento.1",
                "event": "agent.identity_bound",
                "run_id": run_id,
                "run_kind": "session",
                "agent_key": "tangent",
                "agent_name": "Explore",
                "engine": "claude",
                "native_session_id": child_session_id,
                "agent_status": "running",
            }],
        )
        await db.flush()
        await db.refresh(child)
        run = (await db.execute(select(OrchestrationRun))).scalar_one()

        assert run.parent_document_id == parent.id
        assert (child.metadata_ or {}).get("orchestration") != "claw"
        assert (child.metadata_ or {}).get("is_subagent") is not True


@requires_postgres
@pytest.mark.asyncio
async def test_delegate_marker_classifies_and_links_parent(
    session_factory,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-marker",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "cursor") is None:
            db.add(Tool(id="cursor", display_name="Cursor"))
        db.add_all([user, machine])
        parent_session_id = str(uuid4())
        child_session_id = str(uuid4())
        parent = Document(
            id=uuid4(),
            tool_id="cursor",
            machine_id=machine.id,
            relative_path=f"agent-transcripts/{parent_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Orchestrator",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": parent_session_id},
        )
        child = Document(
            id=uuid4(),
            tool_id="cursor",
            machine_id=machine.id,
            relative_path=f"agent-transcripts/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Delegate",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": child_session_id},
        )
        db.add_all([parent, child])
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=child.id,
                line_number=1,
                role="user",
                content=(
                    f"MEMENTO-DELEGATE-FROM: {parent_session_id}\n"
                    "Implement the visibility design."
                ),
            )
        )
        await db.flush()

        changed = await reconcile_orchestration_for_document(db, child)
        await db.flush()
        await db.refresh(child)

        assert changed == 1
        assert child.metadata_["orchestration"] == "claw"
        assert child.metadata_["is_subagent"] is True
        assert child.metadata_["orchestration_parent_document_id"] == str(parent.id)


@requires_postgres
@pytest.mark.asyncio
async def test_handoff_without_normalized_rows_is_not_stamped(
    session_factory,
    monkeypatch,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-handoff-raw",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "claude_code") is None:
            db.add(Tool(id="claude_code", display_name="Claude Code"))
        db.add_all([user, machine])
        parent_session_id = str(uuid4())
        child_session_id = str(uuid4())
        run_id = f"session-{uuid4()}"
        parent = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{parent_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Predecessor",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": parent_session_id},
        )
        child = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Successor",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": child_session_id},
        )
        db.add_all([parent, child])
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=parent.id,
                line_number=1,
                role="assistant",
                content=f"orchestrationRunId: {run_id}",
            )
        )
        await db.flush()

        async def fake_prefix(_db, document, *, max_chars):
            if document.id == child.id:
                return f"MEMENTO-HANDOFF-FROM: {parent_session_id}\nContinue.\n"
            return None

        monkeypatch.setattr(
            "server.services.orchestration_events.document_content_prefix",
            fake_prefix,
        )
        await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[{
                "schema_version": 1,
                "event_id": str(uuid4()),
                "occurred_at": datetime.now(timezone.utc),
                "installation_id": str(uuid4()),
                "orchestrator": "claw-orchestrator",
                "orchestrator_version": "5.0.0-memento.1",
                "event": "agent.identity_bound",
                "run_id": run_id,
                "run_kind": "session",
                "agent_key": "successor",
                "agent_name": "Resume",
                "engine": "claude",
                "native_session_id": child_session_id,
                "agent_status": "running",
            }],
        )
        await db.flush()
        await db.refresh(child)
        assert (child.metadata_ or {}).get("orchestration") != "claw"
        assert (child.metadata_ or {}).get("is_subagent") is not True


@requires_postgres
@pytest.mark.asyncio
async def test_invalid_delegate_marker_is_not_stamped(session_factory) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-invalid-marker",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "cursor") is None:
            db.add(Tool(id="cursor", display_name="Cursor"))
        db.add_all([user, machine])
        child = Document(
            id=uuid4(),
            tool_id="cursor",
            machine_id=machine.id,
            relative_path="agent-transcripts/invalid.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Invalid marker",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": str(uuid4())},
        )
        db.add(child)
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=child.id,
                line_number=1,
                role="user",
                content="MEMENTO-DELEGATE-FROM: not-a-uuid\nwork",
            )
        )
        await db.flush()

        changed = await reconcile_orchestration_for_document(db, child)
        await db.flush()
        await db.refresh(child)

        assert changed == 0
        assert (child.metadata_ or {}).get("orchestration") != "claw"


@requires_postgres
@pytest.mark.asyncio
async def test_invalid_handoff_prefix_does_not_exempt_native_id_stamp(
    session_factory,
) -> None:
    async with session_factory() as db:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="orchestration-invalid-handoff",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "claude_code") is None:
            db.add(Tool(id="claude_code", display_name="Claude Code"))
        db.add_all([user, machine])
        child_session_id = str(uuid4())
        run_id = f"session-{uuid4()}"
        child = Document(
            id=uuid4(),
            tool_id="claude_code",
            machine_id=machine.id,
            relative_path=f"projects/test/{child_session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="False handoff",
            content_hash=uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": child_session_id},
        )
        db.add(child)
        await db.flush()
        db.add(
            ConversationMessage(
                document_id=child.id,
                line_number=1,
                role="user",
                content="MEMENTO-HANDOFF-FROM: not-a-uuid\nContinue.",
            )
        )
        await db.flush()
        await ingest_orchestration_events(
            db,
            machine_id=machine.id,
            user_id=user.id,
            events=[{
                "schema_version": 1,
                "event_id": str(uuid4()),
                "occurred_at": datetime.now(timezone.utc),
                "installation_id": str(uuid4()),
                "orchestrator": "claw-orchestrator",
                "orchestrator_version": "5.0.0-memento.1",
                "event": "agent.identity_bound",
                "run_id": run_id,
                "run_kind": "session",
                "agent_key": "false-handoff",
                "agent_name": "Resume",
                "engine": "claude",
                "native_session_id": child_session_id,
                "agent_status": "running",
            }],
        )
        await db.flush()
        await db.refresh(child)
        assert child.metadata_["orchestration"] == "claw"
        assert child.metadata_["is_subagent"] is True
