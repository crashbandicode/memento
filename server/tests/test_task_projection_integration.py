from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from server.api.tasks import get_tasks
from server.db.models import (
    Base,
    CanvasArtifactReference,
    ConversationMetadataInbox,
    ConversationMessage,
    ConversationPromptProjection,
    ConversationReadModel,
    ConversationTaskState,
    Document,
    DocumentDeliveryState,
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
    CURSOR_PROJECTION_ORDER_KEY,
    CursorProjectionOrderMismatch,
    DeltaBaseMismatch,
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


def _cursor_row(
    source_id: str,
    role: str,
    content: str,
    *,
    timestamp: str,
) -> str:
    return json.dumps({
        "type": role,
        "role": role,
        "id": source_id,
        "timestamp": timestamp,
        "message": {"content": content},
    })


def _cursor_tool_row(
    source_id: str,
    content: str,
    *,
    timestamp: str,
    status: str = "completed",
    tool_name: str = "Read",
    tool_input: dict | None = None,
) -> str:
    return json.dumps({
        "type": "cursor_state_tool",
        "role": "tool",
        "id": source_id,
        "timestamp": timestamp,
        "tool_name": tool_name,
        "tool_input": json.dumps(
            tool_input or {"path": f"{source_id}.py"}
        ),
        "content": content,
        "tool_call_id": f"call-{source_id}",
        "tool_status": status,
    })


def _cursor_task_row(source_id: str, *, timestamp: str) -> str:
    tasks = [{"id": "verify", "content": "Verify order", "status": "pending"}]
    return json.dumps({
        "type": "cursor_state_task",
        "role": "tool",
        "id": source_id,
        "timestamp": timestamp,
        "tool_name": "Task progress 0/1",
        "tool_input": json.dumps({"tasks": tasks, "is_current": True}),
        "content": "0 of 1 tasks complete\n○ Verify order",
    })


@pytest.mark.asyncio
async def test_cursor_projection_delta_updates_stable_rows_in_place(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        document.metadata_ = {
            **document.metadata_,
            "source": "cursor_state_v1",
        }
        user = _cursor_row(
            "user-1",
            "user",
            "Inspect the worker.",
            timestamp="2026-08-08T04:00:00Z",
        )
        assistant = _cursor_row(
            "assistant-1",
            "assistant",
            "The worker is still running.",
            timestamp="2026-08-08T04:00:01Z",
        )
        full_snapshot = f"{user}\n{assistant}"
        await _extract_messages(
            session,
            document,
            full_snapshot,
            "full",
        )
        base_hash = hashlib.sha256(full_snapshot.encode()).hexdigest()
        base_offset = len(full_snapshot.encode())
        document.content = full_snapshot
        document.content_hash = base_hash
        document.file_size_bytes = base_offset
        _set_stored_source_identity(
            document,
            full_snapshot,
            revision_hash=base_hash,
        )
        session.add(SyncState(
            machine_id=document.machine_id,
            tool_id=document.tool_id,
            relative_path=document.relative_path,
            last_hash=base_hash,
            last_offset=base_offset,
        ))
        await session.commit()
        initial_rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        assistant_id = initial_rows[1].id

        updated_assistant = _cursor_row(
            "assistant-1",
            "assistant",
            "The worker completed successfully.",
            timestamp="2026-08-08T04:00:01Z",
        )
        appended = _cursor_row(
            "assistant-2",
            "assistant",
            "No restart is required.",
            timestamp="2026-08-08T04:00:02Z",
        )
        delta = f"{updated_assistant}\n{appended}"
        final_snapshot = f"{user}\n{delta}"
        first_delta_hash = hashlib.sha256(final_snapshot.encode()).hexdigest()
        first_delta_offset = len(final_snapshot.encode())
        machine = await session.get(Machine, document.machine_id)
        assert machine is not None
        await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=document.relative_path,
            content=delta,
            content_hash=first_delta_hash,
            file_size=len(delta.encode()),
            mode="delta",
            offset=first_delta_offset,
            base_hash=base_hash,
            base_offset=base_offset,
            metadata={
                **document.metadata_,
                "source": "cursor_state_v1",
            },
            timestamp=1_786_162_000.0,
            machine_id=document.machine_id,
            user_id=str(machine.user_id),
            schedule_post_ingest=False,
        )
        await session.commit()

        rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        read_model = await session.get(ConversationReadModel, document.id)

        assert len(rows) == 3
        assert rows[1].id == assistant_id
        assert rows[1].line_number == 2
        assert rows[1].content == "The worker completed successfully."
        assert rows[2].metadata_["source_id"] == "assistant-2"
        assert read_model is not None
        assert read_model.message_count == 3
        assert read_model.latest_assistant_line == 3

        shorter_assistant = _cursor_row(
            "assistant-1",
            "assistant",
            "Done.",
            timestamp="2026-08-08T04:00:01Z",
        )
        shorter_snapshot = f"{user}\n{shorter_assistant}\n{appended}"
        assert len(shorter_snapshot.encode()) < first_delta_offset
        await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=document.relative_path,
            content=shorter_assistant,
            content_hash=hashlib.sha256(shorter_snapshot.encode()).hexdigest(),
            file_size=len(shorter_assistant.encode()),
            mode="delta",
            offset=len(shorter_snapshot.encode()),
            base_hash=first_delta_hash,
            base_offset=first_delta_offset,
            metadata={
                **document.metadata_,
                "source": "cursor_state_v1",
            },
            timestamp=1_786_162_001.0,
            machine_id=document.machine_id,
            user_id=str(machine.user_id),
            schedule_post_ingest=False,
        )
        await session.commit()
        final_rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        assert len(final_rows) == 3
        assert final_rows[1].id == assistant_id
        assert final_rows[1].content == "Done."

        replayed = await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=document.relative_path,
            content=shorter_assistant,
            content_hash=hashlib.sha256(shorter_snapshot.encode()).hexdigest(),
            file_size=len(shorter_assistant.encode()),
            mode="delta",
            offset=len(shorter_snapshot.encode()),
            base_hash=first_delta_hash,
            base_offset=first_delta_offset,
            metadata={
                **document.metadata_,
                "source": "cursor_state_v1",
            },
            timestamp=1_786_162_001.0,
            machine_id=document.machine_id,
            user_id=str(machine.user_id),
            schedule_post_ingest=False,
        )
        assert getattr(replayed, "_memento_ingest_disposition") == "idempotent"


@pytest.mark.asyncio
async def test_cursor_projection_delta_inserts_before_bounded_mutable_tail(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        document.metadata_ = {
            **document.metadata_,
            "source": "cursor_state_v1",
        }
        timestamp = "2026-08-08T04:00:00Z"
        stable_rows = [
            _cursor_tool_row(
                f"stable-{index}",
                f"Stable output {index}",
                timestamp=timestamp,
            )
            for index in range(5)
        ]
        mutable_tail = _cursor_tool_row(
            "mutable-tail",
            "Status: running",
            timestamp=timestamp,
            status="running",
            tool_name="Shell",
            tool_input={"command": "python worker.py"},
        )
        task_tail = _cursor_task_row("current-task", timestamp=timestamp)
        full_snapshot = "\n".join([*stable_rows, task_tail, mutable_tail])
        await _extract_messages(
            session,
            document,
            full_snapshot,
            "full",
        )
        base_hash = hashlib.sha256(full_snapshot.encode()).hexdigest()
        base_offset = len(full_snapshot.encode())
        document.content = full_snapshot
        document.content_hash = base_hash
        document.file_size_bytes = base_offset
        _set_stored_source_identity(
            document,
            full_snapshot,
            revision_hash=base_hash,
        )
        session.add(SyncState(
            machine_id=document.machine_id,
            tool_id=document.tool_id,
            relative_path=document.relative_path,
            last_hash=base_hash,
            last_offset=base_offset,
        ))
        await session.commit()
        initial = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        initial_identity = {
            row.metadata_["source_id"]: (row.id, row.line_number)
            for row in initial
        }
        initial_read_model = await session.get(
            ConversationReadModel,
            document.id,
        )
        initial_task_projection = await session.get(
            ConversationTaskState,
            document.id,
        )
        assert initial_read_model is not None
        assert len(initial_read_model.live_activities) == 1
        assert initial_task_projection is not None
        assert initial_task_projection.source_line_number == 6
        task_message_id = initial_task_projection.source_message_id

        inserted_rows = [
            _cursor_tool_row(
                f"inserted-{index}",
                f"Inserted output {index}",
                timestamp=timestamp,
            )
            for index in range(1, 4)
        ]
        appended_row = _cursor_tool_row(
            "appended-4",
            "Appended output 4",
            timestamp=timestamp,
        )
        completed_tail = _cursor_tool_row(
            "mutable-tail",
            "Tail completed.",
            timestamp=timestamp,
            tool_name="Shell",
            tool_input={"command": "python worker.py"},
        )
        delta = "\n".join([*inserted_rows, completed_tail, appended_row])
        final_snapshot = "\n".join([
            *stable_rows[:3],
            inserted_rows[0],
            *stable_rows[3:],
            *inserted_rows[1:],
            task_tail,
            completed_tail,
            appended_row,
        ])
        final_hash = hashlib.sha256(final_snapshot.encode()).hexdigest()
        ordering_hint = {
            "version": 1,
            "base_count": 7,
            "groups": [
                {
                    "after_source_id": "stable-2",
                    "before_source_id": "stable-3",
                    "source_ids": ["inserted-1"],
                },
                {
                    "after_source_id": "stable-4",
                    "before_source_id": "current-task",
                    "source_ids": ["inserted-2", "inserted-3"],
                },
                {
                    "after_source_id": "mutable-tail",
                    "before_source_id": None,
                    "source_ids": ["appended-4"],
                },
            ],
        }
        machine = await session.get(Machine, document.machine_id)
        assert machine is not None

        await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=document.relative_path,
            content=delta,
            content_hash=final_hash,
            file_size=len(delta.encode()),
            mode="delta",
            offset=len(final_snapshot.encode()),
            base_hash=base_hash,
            base_offset=base_offset,
            metadata={
                **document.metadata_,
                CURSOR_PROJECTION_ORDER_KEY: ordering_hint,
            },
            timestamp=1_786_162_002.0,
            machine_id=document.machine_id,
            user_id=str(machine.user_id),
            schedule_post_ingest=False,
        )
        await session.commit()

        rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        source_order = [row.metadata_["source_id"] for row in rows]
        refreshed_document = await session.get(
            Document,
            document.id,
            populate_existing=True,
        )
        read_model = await session.get(
            ConversationReadModel,
            document.id,
            populate_existing=True,
        )
        task_projection = await session.get(
            ConversationTaskState,
            document.id,
            populate_existing=True,
        )
        delivery_state = await session.get(
            DocumentDeliveryState,
            document.id,
            populate_existing=True,
        )

        assert source_order == [
            "stable-0",
            "stable-1",
            "stable-2",
            "inserted-1",
            "stable-3",
            "stable-4",
            "inserted-2",
            "inserted-3",
            "current-task",
            "mutable-tail",
            "appended-4",
        ]
        assert len({row.line_number for row in rows}) == len(rows)
        assert [row.line_number for row in rows] == list(range(1, 12))
        final_by_source = {
            row.metadata_["source_id"]: row
            for row in rows
        }
        for index in range(3):
            row = final_by_source[f"stable-{index}"]
            assert (row.id, row.line_number) == initial_identity[f"stable-{index}"]
        for index in range(3, 5):
            row = final_by_source[f"stable-{index}"]
            assert row.id == initial_identity[f"stable-{index}"][0]
            assert row.line_number == initial_identity[f"stable-{index}"][1] + 1
        mutable = final_by_source["mutable-tail"]
        assert mutable.id == initial_identity["mutable-tail"][0]
        assert mutable.line_number == 10
        assert mutable.content == "Tail completed."
        assert refreshed_document is not None
        assert refreshed_document.content == full_snapshot
        assert refreshed_document.content_hash == base_hash
        assert refreshed_document.file_size_bytes == base_offset
        assert delivery_state is not None
        assert delivery_state.revision_hash == final_hash
        assert delivery_state.file_size_bytes == len(final_snapshot.encode())
        assert CURSOR_PROJECTION_ORDER_KEY not in delivery_state.delivery_metadata
        assert read_model is not None
        assert read_model.message_count == 11
        assert read_model.projected_through_line == 11
        assert read_model.live_activities == []
        assert task_projection is not None
        assert task_projection.source_message_id == task_message_id
        assert task_projection.source_line_number == 9

        replayed = await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=document.relative_path,
            content=delta,
            content_hash=final_hash,
            file_size=len(delta.encode()),
            mode="delta",
            offset=len(final_snapshot.encode()),
            base_hash=base_hash,
            base_offset=base_offset,
            metadata={
                **document.metadata_,
                CURSOR_PROJECTION_ORDER_KEY: ordering_hint,
            },
            timestamp=1_786_162_002.0,
            machine_id=document.machine_id,
            user_id=str(machine.user_id),
            schedule_post_ingest=False,
        )
        assert getattr(replayed, "_memento_ingest_disposition") == "idempotent"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
            )
        ) == 11


@pytest.mark.asyncio
async def test_cursor_projection_full_then_sparse_deltas_with_filtered_rows(
    session_factory,
) -> None:
    """Projection counts may exceed normalized rows without changing hash safety."""
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="cursor-projection-count-test",
            collector_token_hash=str(uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="cursor"))
        session.add_all([user, machine])
        await session.flush()

        timestamp = "2026-08-08T04:00:00Z"
        session_id = str(uuid4())
        relative_path = (
            "projects/empty-window/agent-transcripts/"
            f"{session_id}/{session_id}.jsonl"
        )
        metadata = {
            "source": "cursor_state_v1",
            "session_id": session_id,
        }
        stable_0 = _cursor_row(
            "stable-0",
            "user",
            "Inspect the projection.",
            timestamp=timestamp,
        )
        # Cursor's projection can retain source records with no normalized
        # content. They remain part of the collector's source-order baseline
        # and content hash, but intentionally produce no read-model row.
        filtered = _cursor_row(
            "filtered-empty",
            "assistant",
            "",
            timestamp=timestamp,
        )
        stable_1 = _cursor_tool_row(
            "stable-1",
            "First result",
            timestamp=timestamp,
        )
        stable_2 = _cursor_tool_row(
            "stable-2",
            "Mutable tail",
            timestamp=timestamp,
            status="running",
            tool_name="Shell",
            tool_input={"command": "python worker.py"},
        )
        full_rows = [stable_0, filtered, stable_1, stable_2]
        full_snapshot = "\n".join(full_rows)
        full_hash = hashlib.sha256(full_snapshot.encode()).hexdigest()
        full_size = len(full_snapshot.encode())

        document = await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=relative_path,
            content=full_snapshot,
            content_hash=full_hash,
            file_size=full_size,
            mode="full",
            offset=full_size,
            metadata=metadata,
            timestamp=1_786_162_000.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
        )
        await session.commit()

        initial_rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        assert [row.metadata_["source_id"] for row in initial_rows] == [
            "stable-0",
            "stable-1",
            "stable-2",
        ]
        initial_sync = (
            await session.execute(
                select(SyncState).where(
                    SyncState.machine_id == machine.id,
                    SyncState.tool_id == "cursor",
                    SyncState.relative_path == relative_path,
                )
            )
        ).scalar_one()
        assert (initial_sync.last_hash, initial_sync.last_offset) == (
            full_hash,
            full_size,
        )

        inserted_1 = _cursor_tool_row(
            "inserted-1",
            "Late first result",
            timestamp=timestamp,
        )
        first_rows = [stable_0, filtered, stable_1, inserted_1, stable_2]
        first_snapshot = "\n".join(first_rows)
        first_hash = hashlib.sha256(first_snapshot.encode()).hexdigest()
        first_size = len(first_snapshot.encode())
        first_hint = {
            "version": 1,
            "base_count": len(full_rows),
            "groups": [{
                "after_source_id": "stable-1",
                "before_source_id": "stable-2",
                "source_ids": ["inserted-1"],
            }],
        }
        await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=relative_path,
            content=inserted_1,
            content_hash=first_hash,
            file_size=len(inserted_1.encode()),
            mode="delta",
            offset=first_size,
            base_hash=full_hash,
            base_offset=full_size,
            metadata={
                **metadata,
                CURSOR_PROJECTION_ORDER_KEY: first_hint,
            },
            timestamp=1_786_162_001.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
        )
        await session.commit()

        inserted_2 = _cursor_tool_row(
            "inserted-2",
            "Late second result",
            timestamp=timestamp,
        )
        second_rows = [
            stable_0,
            filtered,
            stable_1,
            inserted_1,
            inserted_2,
            stable_2,
        ]
        second_snapshot = "\n".join(second_rows)
        second_hash = hashlib.sha256(second_snapshot.encode()).hexdigest()
        second_size = len(second_snapshot.encode())
        second_hint = {
            "version": 1,
            "base_count": len(first_rows),
            "groups": [{
                "after_source_id": "inserted-1",
                "before_source_id": "stable-2",
                "source_ids": ["inserted-2"],
            }],
        }
        await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=relative_path,
            content=inserted_2,
            content_hash=second_hash,
            file_size=len(inserted_2.encode()),
            mode="delta",
            offset=second_size,
            base_hash=first_hash,
            base_offset=first_size,
            metadata={
                **metadata,
                CURSOR_PROJECTION_ORDER_KEY: second_hint,
            },
            timestamp=1_786_162_002.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
        )
        await session.commit()

        final_rows = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        assert [row.metadata_["source_id"] for row in final_rows] == [
            "stable-0",
            "stable-1",
            "inserted-1",
            "inserted-2",
            "stable-2",
        ]
        final_sync = (
            await session.execute(
                select(SyncState).where(
                    SyncState.machine_id == machine.id,
                    SyncState.tool_id == "cursor",
                    SyncState.relative_path == relative_path,
                )
            )
        ).scalar_one()
        final_delivery = await session.get(
            DocumentDeliveryState,
            document.id,
            populate_existing=True,
        )
        assert final_delivery is not None
        assert (final_sync.last_hash, final_sync.last_offset) == (
            second_hash,
            second_size,
        )
        assert (
            final_delivery.revision_hash,
            final_delivery.file_size_bytes,
        ) == (second_hash, second_size)

        replayed = await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=relative_path,
            content=inserted_2,
            content_hash=second_hash,
            file_size=len(inserted_2.encode()),
            mode="delta",
            offset=second_size,
            base_hash=first_hash,
            base_offset=first_size,
            metadata={
                **metadata,
                CURSOR_PROJECTION_ORDER_KEY: second_hint,
            },
            timestamp=1_786_162_002.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
        )
        assert getattr(replayed, "_memento_ingest_disposition") == "idempotent"

        with pytest.raises(DeltaBaseMismatch) as stale:
            await ingest_file(
                session,
                tool_id="cursor",
                category="conversation",
                content_type="jsonl",
                relative_path=relative_path,
                content=inserted_1,
                content_hash=first_hash,
                file_size=len(inserted_1.encode()),
                mode="delta",
                offset=first_size,
                base_hash=full_hash,
                base_offset=full_size,
                metadata={
                    **metadata,
                    CURSOR_PROJECTION_ORDER_KEY: first_hint,
                },
                timestamp=1_786_162_001.0,
                machine_id=machine.id,
                user_id=str(user.id),
                schedule_post_ingest=False,
            )
        assert stale.value.expected_hash == second_hash
        assert stale.value.expected_offset == second_size


@pytest.mark.asyncio
async def test_cursor_projection_delta_reconciles_canvas_references_exactly(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        document.metadata_ = {
            **document.metadata_,
            "source": "cursor_state_v1",
        }
        timestamp = "2026-08-08T04:00:00Z"
        paths = {
            name: (
                f"/home/me/.cursor/projects/demo/canvases/{name}.canvas.tsx"
            )
            for name in ("removed", "retained", "added", "new-row")
        }

        def reference(name: str) -> str:
            return f"[{name}.canvas.tsx]({paths[name]})"

        initial = _cursor_tool_row(
            "canvas-stable",
            f"Open {reference('removed')} and {reference('retained')}.",
            timestamp=timestamp,
        )
        await _extract_messages(session, document, initial, "full")
        await session.flush()
        initial_message = (
            await session.execute(
                select(ConversationMessage).where(
                    ConversationMessage.document_id == document.id
                )
            )
        ).scalar_one()
        initial_references = (
            await session.execute(
                select(CanvasArtifactReference).where(
                    CanvasArtifactReference.document_id == document.id
                )
            )
        ).scalars().all()
        by_path = {
            item.recorded_path: item
            for item in initial_references
        }
        retained = by_path[paths["retained"]]
        retained.status = "missing"
        retained.reason = "source_missing"
        retained.attempt_count = 3
        retained_id = retained.id
        await session.commit()

        changed = _cursor_tool_row(
            "canvas-stable",
            f"Open {reference('retained')} and {reference('added')}.",
            timestamp=timestamp,
        )
        appended = _cursor_tool_row(
            "canvas-new",
            f"Tool output mentions {reference('new-row')}.",
            timestamp=timestamp,
        )
        delta = f"{changed}\n{appended}"
        await _extract_messages(session, document, delta, "delta")
        await session.commit()

        messages = (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        references = (
            await session.execute(
                select(CanvasArtifactReference)
                .where(CanvasArtifactReference.document_id == document.id)
                .order_by(CanvasArtifactReference.recorded_path)
            )
        ).scalars().all()
        final_by_path = {item.recorded_path: item for item in references}

        assert len(messages) == 2
        assert messages[0].id == initial_message.id
        assert set(final_by_path) == {
            paths["retained"],
            paths["added"],
            paths["new-row"],
        }
        assert final_by_path[paths["retained"]].id == retained_id
        assert final_by_path[paths["retained"]].status == "missing"
        assert final_by_path[paths["retained"]].reason == "source_missing"
        assert final_by_path[paths["retained"]].attempt_count == 3
        assert final_by_path[paths["added"]].status == "discovered"
        assert final_by_path[paths["added"]].message_id == messages[0].id
        assert final_by_path[paths["new-row"]].message_id == messages[1].id
        assert len({
            (item.message_id, item.path_hash)
            for item in references
        }) == len(references)

        await _extract_messages(session, document, delta, "delta")
        await session.commit()
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CanvasArtifactReference)
                .where(CanvasArtifactReference.document_id == document.id)
            )
        ) == 3


@pytest.mark.asyncio
async def test_cursor_projection_delta_rejects_malformed_and_far_tail_hints(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="cursor")
        document.metadata_ = {
            **document.metadata_,
            "source": "cursor_state_v1",
        }
        timestamp = "2026-08-08T04:00:00Z"
        full = "\n".join(
            _cursor_tool_row(
                f"stable-{index}",
                f"Stable {index}",
                timestamp=timestamp,
            )
            for index in range(40)
        )
        await _extract_messages(session, document, full, "full")
        await session.flush()
        inserted = _cursor_tool_row(
            "inserted",
            "Unsafe insertion",
            timestamp=timestamp,
        )

        with pytest.raises(
            CursorProjectionOrderMismatch,
            match="ordering bounds",
        ) as malformed:
            await _extract_messages(
                session,
                document,
                inserted,
                "delta",
                cursor_projection_order={
                    "version": 1,
                    "base_count": 40,
                    "groups": "malformed",
                },
            )
        assert malformed.value.expected_hash is None
        assert malformed.value.expected_offset == 0
        with pytest.raises(
            CursorProjectionOrderMismatch,
            match="ordering bounds",
        ):
            await _extract_messages(
                session,
                document,
                inserted,
                "delta",
                cursor_projection_order={
                    "version": 1,
                    "base_count": 39,
                    "groups": [{
                        "after_source_id": "stable-38",
                        "before_source_id": "stable-39",
                        "source_ids": ["inserted"],
                    }],
                },
            )
        with pytest.raises(
            CursorProjectionOrderMismatch,
            match="too far from tail",
        ):
            await _extract_messages(
                session,
                document,
                inserted,
                "delta",
                cursor_projection_order={
                    "version": 1,
                    "base_count": 40,
                    "groups": [{
                        "after_source_id": "stable-0",
                        "before_source_id": "stable-1",
                        "source_ids": ["inserted"],
                    }],
                },
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ConversationMessage)
                .where(ConversationMessage.document_id == document.id)
            )
        ) == 40
        await session.rollback()


@pytest.mark.asyncio
async def test_codex_history_only_rebuilds_projection_when_rows_change(
    session_factory,
    monkeypatch,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="codex")
        first = json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Initial prompt",
            },
        })
        history = [{"text": "Initial prompt", "ts": 0}]
        await _extract_messages(
            session,
            document,
            first,
            "full",
            user_history=history,
        )
        await session.flush()

        from server.services import conversation_read_model as read_model_service

        original_refresh = read_model_service.refresh_conversation_read_model
        refresh_calls: list[dict] = []

        async def recording_refresh(*args, **kwargs):
            refresh_calls.append(dict(kwargs))
            return await original_refresh(*args, **kwargs)

        monkeypatch.setattr(
            read_model_service,
            "refresh_conversation_read_model",
            recording_refresh,
        )

        await _extract_messages(
            session,
            document,
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "First reply",
                },
            }),
            "delta",
            user_history=history,
        )

        assert refresh_calls[-1]["mode"] == "delta"
        assert refresh_calls[-1]["force_full"] is False

        await _extract_messages(
            session,
            document,
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "Second reply",
                },
            }),
            "delta",
            user_history=[
                *history,
                {"text": "Interrupted prompt", "ts": 0},
            ],
        )

        assert refresh_calls[-1]["mode"] == "delta"
        assert refresh_calls[-1]["force_full"] is True
        recovered = (
            await session.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.document_id == document.id,
                    ConversationMessage.role == "user",
                )
                .order_by(ConversationMessage.line_number)
            )
        ).scalars().all()
        assert [row.content for row in recovered] == [
            "Initial prompt",
            "Interrupted prompt",
        ]
        projection = await session.get(ConversationReadModel, document.id)
        assert projection is not None
        assert projection.user_message_count == 2
        await session.rollback()


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
            ("2", "pending"),
        ]

        cancel = _claude_tool_row(
            "TaskUpdate",
            {"taskId": "2", "status": "cancelled"},
            source_id="delta-cancel",
        )
        await _extract_messages(session, document, cancel, "delta")
        await session.commit()
        cancelled = await current_projected_task_state(session, document.id)
        assert cancelled is not None
        assert [(task["id"], task["status"]) for task in cancelled["tasks"]] == [
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
async def test_normalized_backfill_retires_projection_after_parser_removes_source(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _conversation(session, tool_id="claude_code")
        message = ConversationMessage(
            document_id=document.id,
            line_number=1,
            message_type="tool",
            role="tool",
            content="",
            metadata_={
                "task_state": _snapshot(
                    "opaque",
                    "Task #opaque",
                    "cancelled",
                    revision=1,
                    current=False,
                )
            },
        )
        session.add(message)
        await session.flush()
        assert (await backfill_task_projections(session, [document.id])) == {
            "documents": 1,
            "created_or_updated": 1,
        }
        await session.delete(message)
        await session.flush()

        assert (await backfill_task_projections(session, [document.id])) == {
            "documents": 1,
            "created_or_updated": 1,
        }
        assert await current_projected_task_state(session, document.id) is None


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
