import json
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


def test_cursor_legacy_wrapper_id_is_resolved_without_weak_matching() -> None:
    assert _native_id_candidates("cursor", "cursor-live-9a91d874") == {
        "cursor-live-9a91d874",
        "9a91d874",
    }
    assert _native_id_candidates("codex", "cursor-live-9a91d874") == {
        "cursor-live-9a91d874",
    }


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
                role="tool",
                content=json.dumps({"orchestrationRunId": run_id}),
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
