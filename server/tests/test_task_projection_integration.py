from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from server.api.tasks import get_tasks
from server.db.models import (
    Base,
    ConversationMessage,
    ConversationTaskState,
    Document,
    Machine,
    Tool,
    User,
)
from server.main import _run_migrations
from server.services.conversation_tasks import (
    backfill_task_projections,
    current_projected_task_state,
    refresh_task_projection,
)
from server.services.ingest_service import _extract_messages
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
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


async def _conversation(session, *, tool_id: str = "claude_code") -> Document:
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="task-test",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    if await session.get(Tool, tool_id) is None:
        session.add(Tool(id=tool_id, display_name=tool_id))
    document = Document(
        id=uuid4(),
        tool_id=tool_id,
        machine_id=machine.id,
        relative_path=f"sessions/{uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Task integration",
        content_hash=str(uuid4()).replace("-", ""),
        file_size_bytes=1,
        metadata_={"session_id": str(uuid4())},
    )
    session.add_all([user, machine, document])
    await session.flush()
    return document


def _snapshot(
    task_id: str,
    content: str,
    status: str,
    *,
    revision: int,
    current: bool,
) -> dict:
    return {
        "version": 1,
        "source": "cursor",
        "revision": revision,
        "is_current": current,
        "quality": "explicit_current" if current else "authoritative",
        "source_ids": [f"source-{revision}"],
        "completed_count": int(status == "completed"),
        "total_count": 1,
        "active_task_id": task_id if status != "completed" else "",
        "tasks": [
            {
                "id": task_id,
                "content": content,
                "status": status,
                "active_form": "",
            }
        ],
    }


@pytest.mark.asyncio
async def test_projection_prefers_latest_explicit_snapshot_and_dedupes(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        rows = [
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                message_type="tool",
                role="tool",
                content="",
                metadata_={"task_state": _snapshot(
                    "1", "Explicit", "pending", revision=1, current=True
                )},
            ),
            ConversationMessage(
                document_id=document.id,
                line_number=2,
                message_type="tool",
                role="tool",
                content="",
                metadata_={"task_state": _snapshot(
                    "1", "Explicit", "pending", revision=2, current=True
                )},
            ),
            ConversationMessage(
                document_id=document.id,
                line_number=3,
                message_type="tool",
                role="tool",
                content="",
                metadata_={"task_state": _snapshot(
                    "1", "Historical", "completed", revision=3, current=False
                )},
            ),
        ]
        session.add_all(rows)
        await session.flush()

        first = await refresh_task_projection(session, document)
        await session.flush()
        second = await refresh_task_projection(session, document)

        assert first is second
        assert first is not None
        assert first.source_message_id == rows[1].id
        assert first.explicit_current is True
        assert first.state["tasks"][0]["content"] == "Explicit"
        assert (
            await session.execute(
                select(ConversationTaskState).where(
                    ConversationTaskState.document_id == document.id
                )
            )
        ).scalar_one() is first
        await session.rollback()


def _claude_tool_row(
    name: str,
    payload: dict,
    *,
    source_id: str,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": source_id,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": source_id,
                        "name": name,
                        "input": payload,
                    }
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_full_and_delta_ingest_keep_authoritative_current_state(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session)
        full = _claude_tool_row(
            "TodoWrite",
            {
                "is_current": True,
                "todos": [
                    {"id": "1", "content": "Inspect", "status": "in_progress"},
                    {"id": "2", "content": "Verify", "status": "pending"},
                ]
            },
            source_id="full",
        )
        await _extract_messages(session, document, full, "full")
        await session.commit()

        delta = _claude_tool_row(
            "TaskUpdate",
            {"taskId": "1", "status": "completed"},
            source_id="delta-update",
        )
        await _extract_messages(session, document, delta, "delta")
        await session.commit()
        state = await current_projected_task_state(session, document.id)

        assert state is not None
        assert state["revision"] == 2
        assert state["quality"] == "explicit_current"
        assert [(task["id"], task["status"]) for task in state["tasks"]] == [
            ("1", "completed"),
            ("2", "pending"),
        ]

        machine = await session.get(Machine, document.machine_id)
        assert machine is not None
        user = await session.get(User, machine.user_id)
        assert user is not None
        task_query = {
            "document_id": document.id,
            "thread_id": None,
            "agent_id": None,
            "subagent_id": None,
            "tool": None,
            "include_history": False,
            "cursor": None,
            "limit": 10,
            "max_tasks": 100,
            "history_limit": 0,
            "db": session,
            "user": user,
        }
        outstanding = await get_tasks(status="outstanding", **task_query)
        completed = await get_tasks(status="completed", **task_query)
        outstanding_tasks = outstanding["root_threads"][0]["agents"][0][
            "task_state"
        ]["tasks"]
        completed_tasks = completed["root_threads"][0]["agents"][0][
            "task_state"
        ]["tasks"]
        assert [(task["id"], task["status"]) for task in outstanding_tasks] == [
            ("2", "pending")
        ]
        assert [(task["id"], task["status"]) for task in completed_tasks] == [
            ("1", "completed")
        ]

        stop = _claude_tool_row(
            "TaskStop",
            {"taskId": "2"},
            source_id="delta-stop",
        )
        await _extract_messages(session, document, stop, "delta")
        await session.commit()
        stopped = await current_projected_task_state(session, document.id)
        assert stopped is not None
        assert [(task["id"], task["status"]) for task in stopped["tasks"]] == [
            ("1", "completed"),
            ("2", "cancelled"),
        ]

        replacement = _claude_tool_row(
            "TodoWrite",
            {"todos": [], "is_current": True},
            source_id="replacement",
        )
        await _extract_messages(session, document, replacement, "full")
        await session.commit()
        empty = await current_projected_task_state(session, document.id)
        assert empty is not None
        assert empty["tasks"] == []
        assert empty["is_current"] is True


@pytest.mark.asyncio
async def test_normalized_backfill_is_transactionally_idempotent(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        session.add(
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                message_type="tool",
                role="tool",
                content="",
                metadata_={
                    "task_state": _snapshot(
                        "1",
                        "Backfill",
                        "pending",
                        revision=1,
                        current=True,
                    )
                },
            )
        )
        await session.flush()

        first = await backfill_task_projections(session, [document.id])
        await session.commit()
        second = await backfill_task_projections(session, [document.id])

        assert first == {"documents": 1, "created_or_updated": 1}
        assert second == {"documents": 1, "created_or_updated": 0}


@pytest.mark.asyncio
async def test_startup_migration_recreates_projection_table_and_indexes(
    session_factory,
) -> None:
    bind = session_factory.kw["bind"]
    async with bind.begin() as connection:
        await connection.execute(text("DROP TABLE conversation_task_states"))
        await connection.run_sync(_run_migrations)
        # Production invokes this on every API restart. A second pass must
        # safely preserve the table and every query-supporting index.
        await connection.run_sync(_run_migrations)
        exists = (
            await connection.execute(
                text("SELECT to_regclass('conversation_task_states')")
            )
        ).scalar_one()
        indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'conversation_task_states'"
                    )
                )
            ).scalars()
        )
    assert exists == "conversation_task_states"
    assert {
        "conversation_task_states_pkey",
        "idx_task_state_machine",
        "idx_task_state_user",
        "idx_task_state_thread",
        "idx_task_state_root",
        "idx_task_state_parent",
        "idx_task_state_agent",
        "idx_task_state_outstanding",
        "idx_task_state_status_counts",
        "idx_task_state_pending",
        "idx_task_state_in_progress",
        "idx_task_state_blocked",
        "idx_task_state_completed",
        "idx_task_state_cancelled",
    } <= indexes
