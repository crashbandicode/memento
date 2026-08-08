from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from server.api.tasks import get_tasks
from server.db.models import (
    Base,
    ConversationMetadataInbox,
    ConversationMessage,
    ConversationPromptProjection,
    ConversationReadModel,
    ConversationTaskState,
    Document,
    DocumentVersion,
    Machine,
    SyncState,
    Tool,
    User,
)
from server.main import _run_migrations
from server.services.conversation_tasks import (
    backfill_task_projections,
    current_projected_task_state,
    refresh_task_projection,
)
from server.services.conversation_metadata_inbox import (
    apply_deferred_conversation_metadata,
    defer_conversation_metadata,
)
from server.services.conversation_read_model import refresh_conversation_read_model
from server.services.ingest_service import (
    LIVE_SHELL_ACTIVITIES_KEY,
    _extract_messages,
    _set_stored_source_identity,
    ingest_file,
)
from sqlalchemy import func, select, text
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


@pytest.mark.asyncio
async def test_incremental_projection_updates_mutated_source_row(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        row = ConversationMessage(
            document_id=document.id,
            line_number=1,
            message_type="tool",
            role="tool",
            content="",
            metadata_={
                "task_state": _snapshot(
                    "1", "Mutable", "pending", revision=1, current=True
                )
            },
        )
        session.add(row)
        await session.flush()
        projection = await refresh_task_projection(session, document)
        assert projection is not None

        row.metadata_ = {
            "task_state": _snapshot(
                "1", "Mutable", "completed", revision=2, current=True
            )
        }
        await refresh_task_projection(
            session,
            document,
            candidate_rows=[row],
            replace=False,
        )

        assert projection.source_message_id == row.id
        assert projection.revision == 2
        assert projection.completed_count == 1
        assert projection.state["tasks"][0]["status"] == "completed"
        await session.rollback()


@pytest.mark.asyncio
async def test_prompt_projection_updates_only_candidate_rows(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        first = ConversationMessage(
            document_id=document.id,
            line_number=1,
            message_type="user",
            role="user",
            content="First prompt",
            metadata_={},
        )
        session.add(first)
        await session.flush()
        await refresh_conversation_read_model(
            session,
            document,
            mode="full",
        )

        second = ConversationMessage(
            document_id=document.id,
            line_number=2,
            message_type="user",
            role="user",
            content="Second prompt",
            metadata_={},
        )
        session.add(second)
        await session.flush()
        await refresh_conversation_read_model(
            session,
            document,
            mode="delta",
        )
        prompts = (
            await session.execute(
                select(ConversationPromptProjection)
                .where(
                    ConversationPromptProjection.document_id == document.id
                )
                .order_by(ConversationPromptProjection.line_number)
            )
        ).scalars().all()
        assert [(item.line_number, item.content) for item in prompts] == [
            (1, "First prompt"),
            (2, "Second prompt"),
        ]

        first.content = "[AUTO HEALTH-CHECK — runs every 5 min]\nCheck status."
        await refresh_conversation_read_model(
            session,
            document,
            mode="delta",
            dirty_line_numbers=[1],
        )
        remaining = (
            await session.execute(
                select(ConversationPromptProjection)
                .where(
                    ConversationPromptProjection.document_id == document.id
                )
                .order_by(ConversationPromptProjection.line_number)
            )
        ).scalars().all()
        assert [(item.line_number, item.content) for item in remaining] == [
            (2, "Second prompt"),
        ]
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
async def test_full_rebase_preserves_unchanged_message_prefix(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session)
        first = _claude_tool_row(
            "Read",
            {"file_path": "one.py"},
            source_id="stable-first",
        )
        await _extract_messages(session, document, first, "full")
        await session.commit()
        original = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        assert len(original) == 1
        original_id = original[0].id
        initial_read_model = await session.get(ConversationReadModel, document.id)
        assert initial_read_model is not None
        initial_generation = initial_read_model.generation

        second = _claude_tool_row(
            "Read",
            {"file_path": "two.py"},
            source_id="new-second",
        )
        await _extract_messages(session, document, f"{first}\n{second}", "full")
        await session.commit()
        rebased = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()

        assert len(rebased) == 2
        assert rebased[0].id == original_id
        assert rebased[1].id != original_id
        read_model = await session.get(ConversationReadModel, document.id)
        assert read_model is not None
        assert read_model.generation == initial_generation
        assert read_model.message_count == 2
        assert read_model.projected_through_line == 2

        changed_first = _claude_tool_row(
            "Read",
            {"file_path": "updated.py"},
            source_id="stable-first",
        )
        await _extract_messages(
            session,
            document,
            f"{changed_first}\n{second}",
            "full",
        )
        await session.commit()
        changed_read_model = await session.get(
            ConversationReadModel,
            document.id,
        )
        assert changed_read_model is not None
        assert changed_read_model.generation == initial_generation + 1
        assert changed_read_model.message_count == 2


@pytest.mark.asyncio
async def test_metadata_inbox_replays_across_cursor_path_promotion(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="metadata-source",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="cursor"))
        session.add_all([user, machine])
        await session.flush()
        session_id = uuid4()
        old_path = (
            "projects/empty-window/agent-transcripts/"
            f"{session_id}/{session_id}.jsonl"
        )
        assert await defer_conversation_metadata(
            session,
            machine_id=machine.id,
            user_id=user.id,
            payload={
                "metadata_type": "conversation_activity",
                "tool": "cursor",
                "relative_path": old_path,
                "session_id": str(session_id),
                "activity_id": "shell-promoted",
                "activity_status": "running",
                "activity_tool": "Shell",
                "command": "python worker.py",
                "timestamp": "2026-08-07T16:00:00Z",
            },
        )
        document = Document(
            id=uuid4(),
            tool_id="cursor",
            machine_id=machine.id,
            relative_path=(
                "projects/real-workspace/agent-transcripts/"
                f"{session_id}/{session_id}.jsonl"
            ),
            category="conversation",
            content_type="jsonl",
            content_hash="c" * 64,
            file_size_bytes=1,
            metadata_={"session_id": str(session_id)},
        )
        session.add(document)
        await session.flush()

        assert await apply_deferred_conversation_metadata(
            session,
            document=document,
            user_id=user.id,
        ) == 1
        await session.flush()
        assert "shell-promoted" in document.metadata_[LIVE_SHELL_ACTIVITIES_KEY]
        assert (
            await session.scalar(
                select(func.count()).select_from(ConversationMetadataInbox)
            )
        ) == 0


@pytest.mark.asyncio
async def test_conversation_delta_keeps_immutable_raw_snapshot(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session)
        raw_snapshot = _claude_tool_row(
            "Read",
            {"file_path": "snapshot.py"},
            source_id="snapshot",
        )
        base_hash = "a" * 64
        document.content = raw_snapshot
        document.content_hash = base_hash
        document.file_size_bytes = len(raw_snapshot.encode("utf-8"))
        _set_stored_source_identity(
            document,
            raw_snapshot,
            revision_hash=base_hash,
        )
        session.add(
            SyncState(
                machine_id=document.machine_id,
                tool_id=document.tool_id,
                relative_path=document.relative_path,
                last_hash=base_hash,
                last_offset=document.file_size_bytes,
            )
        )
        user_id = (
            await session.execute(
                select(Machine.user_id).where(Machine.id == document.machine_id)
            )
        ).scalar_one()
        await session.commit()

        delta = _claude_tool_row(
            "Read",
            {"file_path": "delta.py"},
            source_id="delta",
        )
        new_offset = document.file_size_bytes + len(delta.encode("utf-8"))
        await ingest_file(
            session,
            tool_id=document.tool_id,
            category="conversation",
            content_type="jsonl",
            relative_path=document.relative_path,
            content=delta,
            content_hash="b" * 64,
            file_size=len(delta.encode("utf-8")),
            mode="delta",
            offset=new_offset,
            base_hash=base_hash,
            base_offset=document.file_size_bytes,
            metadata={},
            timestamp=1_700_000_200.0,
            machine_id=document.machine_id,
            user_id=str(user_id),
            schedule_post_ingest=False,
        )
        await session.commit()

        assert document.content == raw_snapshot
        assert document.content_hash == "b" * 64
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
            )
        ) == 0


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
        read_model = await session.get(ConversationReadModel, document.id)

        assert state is not None
        assert read_model is not None
        assert read_model.message_count == 2
        assert read_model.projected_through_line == 2
        assert read_model.projection_version == 1
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
        await connection.execute(text("DROP TABLE dashboard_projection_state"))
        await connection.execute(text("DROP TABLE dashboard_document_projections"))
        await connection.execute(text("DROP TABLE conversation_prompt_projections"))
        await connection.execute(text("DROP TABLE conversation_read_models"))
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
        read_exists = (
            await connection.execute(
                text("SELECT to_regclass('conversation_read_models')")
            )
        ).scalar_one()
        prompt_exists = (
            await connection.execute(
                text("SELECT to_regclass('conversation_prompt_projections')")
            )
        ).scalar_one()
        dashboard_exists = (
            await connection.execute(
                text("SELECT to_regclass('dashboard_document_projections')")
            )
        ).scalar_one()
        dashboard_state_exists = (
            await connection.execute(
                text("SELECT to_regclass('dashboard_projection_state')")
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
        read_indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'conversation_read_models'"
                    )
                )
            ).scalars()
        )
        prompt_indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'conversation_prompt_projections'"
                    )
                )
            ).scalars()
        )
        dashboard_indexes = set(
            (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'dashboard_document_projections'"
                    )
                )
            ).scalars()
        )
        read_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'conversation_read_models'"
                    )
                )
            ).scalars()
        )
    assert exists == "conversation_task_states"
    assert read_exists == "conversation_read_models"
    assert prompt_exists == "conversation_prompt_projections"
    assert dashboard_exists == "dashboard_document_projections"
    assert dashboard_state_exists == "dashboard_projection_state"
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
    assert {
        "conversation_read_models_pkey",
        "idx_conversation_read_root",
        "idx_conversation_read_thread",
        "idx_conversation_read_agent",
        "idx_conversation_read_tool_use",
    } <= read_indexes
    assert {
        "conversation_prompt_projections_pkey",
        "idx_conversation_prompt_line",
    } <= prompt_indexes
    assert {
        "user_message_count",
        "assistant_message_count",
        "human_character_count",
    } <= read_columns
    assert {
        "dashboard_document_projections_pkey",
        "idx_dashboard_projection_machine",
        "idx_dashboard_projection_machine_tool_category",
        "idx_dashboard_projection_project_session",
        "idx_dashboard_projection_root",
        "idx_dashboard_projection_activity",
        "idx_dashboard_projection_synced",
        "idx_dashboard_projection_effective_activity",
    } <= dashboard_indexes
