"""Phase 0 golden gate for the Phase 1 conversation DELTA writer.

The recorded source shapes below are copied from the parser/integration fixtures
for Claude queue rows, Codex transport mirrors, and Cursor state projections.
They exercise the mutation shapes Phase 1 deliberately leaves ORM-backed as
well as each sequence's newly appended DELTA row.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.db.models import (
    Base,
    ConversationMessage,
    ConversationPromptProjection,
    ConversationReadModel,
    ConversationTaskState,
    ConversationUsageEvent,
    DashboardDocumentProjection,
    DocumentDeliveryState,
    Machine,
    SyncState,
    Tool,
    User,
)
from server.services.ingest_service import DeltaBaseMismatch, ingest_file


TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
GOLDEN_PATH = (
    Path(__file__).parent / "fixtures" / "realtime_ingest_parity_golden.json"
)
_PENDING_REALTIME_EVENTS = "memento_pending_realtime_events"

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
    yield async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    await engine.dispose()


@dataclass(frozen=True)
class RecordedDeltaSequence:
    name: str
    tool_id: str
    metadata: dict[str, object]
    full_rows: tuple[dict[str, object], ...]
    delta_rows: tuple[dict[str, object], ...]

    @property
    def full(self) -> str:
        return "\n".join(_json_line(row) for row in self.full_rows)

    @property
    def delta(self) -> str:
        return "\n".join(_json_line(row) for row in self.delta_rows)


def _json_line(row: dict[str, object]) -> str:
    return json.dumps(row, separators=(",", ":"), sort_keys=True)


RECORDED_DELTA_SEQUENCES = (
    RecordedDeltaSequence(
        name="claude_queued_rows",
        tool_id="claude_code",
        metadata={"session_id": "phase0-claude-session"},
        full_rows=(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "sessionId": "phase0-claude-session",
                "timestamp": "2026-08-01T10:00:00.000Z",
                "content": "Review the queued release plan.",
            },
        ),
        delta_rows=(
            {
                "type": "user",
                "uuid": "claude-canonical-user-1",
                "timestamp": "2026-08-01T10:00:01.000Z",
                "message": {
                    "role": "user",
                    "content": "Review the queued release plan.",
                },
            },
            {
                "type": "assistant",
                "uuid": "claude-assistant-1",
                "timestamp": "2026-08-01T10:00:02.000Z",
                "message": {
                    "role": "assistant",
                    "content": "The queued plan is ready for review.",
                    "usage": {"input_tokens": 8, "output_tokens": 3},
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="codex_transport_pair",
        tool_id="codex",
        metadata={"session_id": "phase0-codex-session"},
        full_rows=(
            {
                "type": "response_item",
                "timestamp": "2026-08-02T11:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Use Core staging."}],
                },
            },
        ),
        delta_rows=(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T11:00:00Z",
                "payload": {
                    "type": "user_message",
                    "client_id": "codex-user-1",
                    "message": "Use Core staging.",
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-08-02T11:00:01Z",
                "payload": {
                    "turn_id": "phase0-codex-turn",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T11:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 21, "output_tokens": 5}
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T11:00:03Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Core staging preserves the transcript.",
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="cursor_state_delta",
        tool_id="cursor",
        metadata={
            "session_id": "phase0-cursor-session",
            "source": "cursor_state_v1",
        },
        full_rows=(
            {
                "type": "user",
                "role": "user",
                "id": "cursor-user-1",
                "timestamp": "2026-08-03T12:00:00Z",
                "message": {"content": "Inspect the writer state."},
            },
            {
                "type": "assistant",
                "role": "assistant",
                "id": "cursor-assistant-1",
                "timestamp": "2026-08-03T12:00:01Z",
                "message": {"content": "The writer is running."},
            },
        ),
        delta_rows=(
            {
                "type": "assistant",
                "role": "assistant",
                "id": "cursor-assistant-1",
                "timestamp": "2026-08-03T12:00:01Z",
                "message": {"content": "The writer committed its update."},
            },
            {
                "type": "cursor_state_task",
                "role": "tool",
                "id": "cursor-current-task",
                "timestamp": "2026-08-03T12:00:02Z",
                "tool_name": "Task progress 0/1",
                "tool_input": json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "verify",
                                "content": "Verify Core parity",
                                "status": "pending",
                            }
                        ],
                        "is_current": True,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "content": "0 of 1 tasks complete\n○ Verify Core parity",
            },
        ),
    ),
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, uuid.UUID):
        return "<uuid>"
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _model_snapshot(model: object, fields: Iterable[str]) -> dict[str, object]:
    return {
        field: _json_value(getattr(model, field))
        for field in fields
    }


def _first_difference(
    actual: object,
    expected: object,
    *,
    path: str = "$",
) -> tuple[str, object, object] | None:
    """Return the first field-level golden drift for an actionable failure."""
    if type(actual) is not type(expected):
        return path, expected, actual
    if isinstance(expected, dict):
        actual_dict = actual
        for key in sorted(set(expected) | set(actual_dict), key=str):
            child_path = f"{path}.{key}"
            if key not in expected:
                return child_path, "<missing>", actual_dict[key]
            if key not in actual_dict:
                return child_path, expected[key], "<missing>"
            difference = _first_difference(
                actual_dict[key],
                expected[key],
                path=child_path,
            )
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        actual_list = actual
        if len(actual_list) != len(expected):
            return f"{path}.length", len(expected), len(actual_list)
        for index, (actual_item, expected_item) in enumerate(
            zip(actual_list, expected)
        ):
            difference = _first_difference(
                actual_item,
                expected_item,
                path=f"{path}[{index}]",
            )
            if difference is not None:
                return difference
        return None
    if actual != expected:
        return path, expected, actual
    return None


async def _snapshot(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, object]:
    messages = (
        await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.document_id == document_id)
            .order_by(ConversationMessage.line_number)
        )
    ).scalars().all()
    usage_events = (
        await session.execute(
            select(ConversationUsageEvent)
            .where(ConversationUsageEvent.document_id == document_id)
            .order_by(ConversationUsageEvent.source_id)
        )
    ).scalars().all()
    prompts = (
        await session.execute(
            select(ConversationPromptProjection)
            .where(ConversationPromptProjection.document_id == document_id)
            .order_by(ConversationPromptProjection.line_number)
        )
    ).scalars().all()
    delivery = await session.get(DocumentDeliveryState, document_id)
    read_model = await session.get(ConversationReadModel, document_id)
    task_state = await session.get(ConversationTaskState, document_id)
    dashboard = await session.get(DashboardDocumentProjection, document_id)
    sync_state = (
        await session.execute(
            select(SyncState).where(
                SyncState.machine_id == (
                    select(Machine.id)
                    .where(Machine.user_id == user_id)
                    .limit(1)
                    .scalar_subquery()
                ),
                SyncState.relative_path.like("phase0/%"),
            )
        )
    ).scalar_one()
    events = []
    for event_type, data, scoped_user_id in session.info.get(
        _PENDING_REALTIME_EVENTS,
        [],
    ):
        event_data = dict(data)
        if event_data.get("document_id") == str(document_id):
            event_data["document_id"] = "<document>"
        events.append(
            {
                "event_type": event_type,
                "owner_scoped": scoped_user_id == str(user_id),
                "data": _json_value(event_data),
            }
        )
    return {
        "messages": [
            _model_snapshot(
                message,
                (
                    "line_number",
                    "message_type",
                    "role",
                    "content",
                    "metadata_",
                    "timestamp",
                ),
            )
            for message in messages
        ],
        "usage_events": [
            _model_snapshot(
                event,
                (
                    "tool_id",
                    "source_id",
                    "source",
                    "occurred_at",
                    "model",
                    "reasoning_effort",
                    "service_tier",
                    "attribution_status",
                    "input_tokens",
                    "uncached_input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                ),
            )
            for event in usage_events
        ],
        "delivery_state": (
            _model_snapshot(
                delivery,
                (
                    "revision_hash",
                    "file_size_bytes",
                    "delivery_metadata",
                    "source_modified_at",
                    "activity_at",
                ),
            )
            if delivery is not None
            else None
        ),
        "sync_state": _model_snapshot(
            sync_state,
            ("tool_id", "relative_path", "last_hash", "last_offset"),
        ),
        "read_model": (
            _model_snapshot(
                read_model,
                (
                    "tool_id",
                    "thread_id",
                    "root_thread_id",
                    "parent_thread_id",
                    "agent_id",
                    "agent_tool_use_id",
                    "agent_depth",
                    "is_subagent",
                    "message_count",
                    "user_message_count",
                    "assistant_message_count",
                    "human_character_count",
                    "projected_through_line",
                    "latest_assistant_line",
                    "generation",
                    "projection_version",
                    "pending_interactions",
                    "inferred_responses",
                    "live_activities",
                    "agent_events",
                    "runtime",
                    "lifecycle",
                    "latest_human_at",
                ),
            )
            if read_model is not None
            else None
        ),
        "prompt_projections": [
            _model_snapshot(prompt, ("line_number", "content", "timestamp"))
            for prompt in prompts
        ],
        "task_projection": (
            _model_snapshot(
                task_state,
                (
                    "tool_id",
                    "thread_id",
                    "root_thread_id",
                    "parent_thread_id",
                    "agent_id",
                    "agent_path",
                    "agent_depth",
                    "source_line_number",
                    "source_ids",
                    "revision",
                    "state",
                    "state_hash",
                    "explicit_current",
                    "quality",
                    "projection_version",
                    "pending_count",
                    "in_progress_count",
                    "blocked_count",
                    "completed_count",
                    "cancelled_count",
                    "outstanding_count",
                    "total_count",
                ),
            )
            if task_state is not None
            else None
        ),
        "dashboard_projection": (
            _model_snapshot(
                dashboard,
                (
                    "tool_id",
                    "category",
                    "visibility",
                    "title",
                    "relative_path",
                    "file_size_bytes",
                    "source_modified_at",
                    "activity_at",
                    "session_id",
                    "root_thread_id",
                    "parent_thread_id",
                    "is_subagent",
                    "is_archived",
                    "hierarchy_metadata",
                    "message_count",
                    "user_message_count",
                    "assistant_message_count",
                    "human_character_count",
                    "pending_question_count",
                    "agent_mode",
                    "projection_version",
                ),
            )
            if dashboard is not None
            else None
        ),
        "staged_sse_events": events,
    }


async def _run_sequence(
    session_factory,
    sequence: RecordedDeltaSequence,
    *,
    use_core_delta_message_staging: bool,
) -> dict[str, object]:
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"phase0-{sequence.name}-{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name=f"phase0-{sequence.name}",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, sequence.tool_id) is None:
            session.add(Tool(id=sequence.tool_id, display_name=sequence.tool_id))
        session.add_all((user, machine))
        await session.commit()

        machine_id = machine.id
        user_uuid = user.id
        user_id = str(user_uuid)
        relative_path = f"phase0/{sequence.name}.jsonl"

        async def ingest_recorded(
            *,
            content: str,
            content_hash: str,
            mode: str,
            file_size: int,
            offset: int,
            base_hash: str | None = None,
            base_offset: int | None = None,
            authoritative_rebase: bool = False,
        ):
            return await ingest_file(
                session,
                tool_id=sequence.tool_id,
                category="conversation",
                content_type="jsonl",
                relative_path=relative_path,
                content=content,
                content_hash=content_hash,
                file_size=file_size,
                mode=mode,
                offset=offset,
                base_hash=base_hash,
                base_offset=base_offset,
                metadata=dict(sequence.metadata),
                timestamp=1_785_672_000.0 + (mode == "delta"),
                machine_id=machine_id,
                user_id=user_id,
                schedule_post_ingest=False,
                authoritative_rebase=authoritative_rebase,
                use_core_delta_message_staging=use_core_delta_message_staging,
            )

        full = sequence.full
        full_hash = _hash(full)
        document = await ingest_recorded(
            content=full,
            content_hash=full_hash,
            file_size=len(full.encode("utf-8")),
            mode="full",
            offset=len(full.encode("utf-8")),
        )
        await session.commit()
        session.info.pop(_PENDING_REALTIME_EVENTS, None)

        full_retry = await ingest_recorded(
            content=full,
            content_hash=full_hash,
            file_size=len(full.encode("utf-8")),
            mode="full",
            offset=len(full.encode("utf-8")),
        )
        assert getattr(full_retry, "_memento_ingest_disposition") == "idempotent"
        await session.rollback()

        delta = sequence.delta
        final_snapshot = f"{full}\n{delta}"
        final_hash = _hash(final_snapshot)
        final_offset = len(final_snapshot.encode("utf-8"))
        await ingest_recorded(
            content=delta,
            content_hash=final_hash,
            file_size=len(delta.encode("utf-8")),
            mode="delta",
            offset=final_offset,
            base_hash=full_hash,
            base_offset=len(full.encode("utf-8")),
        )
        await session.flush()
        snapshot = await _snapshot(
            session,
            document_id=document.id,
            user_id=user_uuid,
        )
        await session.commit()
        session.info.pop(_PENDING_REALTIME_EVENTS, None)

        if sequence.tool_id == "codex":
            # An invisible transport record changes the raw FULL revision
            # without changing normalized rows.  This tests the authoritative
            # rebase branch separately from the DELTA writer's golden output.
            rebase_content = "\n".join((
                final_snapshot,
                _json_line({"type": "turn_context", "payload": {}}),
            ))
            rebase_hash = _hash(rebase_content)
            await ingest_recorded(
                content=rebase_content,
                content_hash=rebase_hash,
                file_size=len(rebase_content.encode("utf-8")),
                mode="full",
                offset=len(rebase_content.encode("utf-8")),
                authoritative_rebase=True,
            )
            await session.flush()
            rebased_snapshot = await _snapshot(
                session,
                document_id=document.id,
                user_id=user_uuid,
            )
            assert rebased_snapshot["messages"] == snapshot["messages"]
            assert rebased_snapshot["usage_events"] == snapshot["usage_events"]
            assert (
                rebased_snapshot["prompt_projections"]
                == snapshot["prompt_projections"]
            )
            assert rebased_snapshot["read_model"]["message_count"] == 2
            assert rebased_snapshot["read_model"]["latest_assistant_line"] == 2
            assert rebased_snapshot["delivery_state"]["revision_hash"] == rebase_hash
            assert rebased_snapshot["sync_state"]["last_hash"] == rebase_hash
            await session.rollback()

        if sequence.tool_id == "cursor":
            delta_retry = await ingest_recorded(
                content=delta,
                content_hash=final_hash,
                file_size=len(delta.encode("utf-8")),
                mode="delta",
                offset=final_offset,
                base_hash=full_hash,
                base_offset=len(full.encode("utf-8")),
            )
            assert getattr(delta_retry, "_memento_ingest_disposition") == "idempotent"
        else:
            stale_retry = await ingest_recorded(
                content=delta,
                content_hash="0" * 64,
                file_size=len(delta.encode("utf-8")),
                mode="delta",
                offset=final_offset - 1,
                base_hash=full_hash,
                base_offset=len(full.encode("utf-8")),
            )
            assert (
                getattr(stale_retry, "_memento_ingest_disposition")
                == "stale_delta"
            )
        await session.rollback()

        with pytest.raises(DeltaBaseMismatch) as mismatch:
            await ingest_recorded(
                content=delta,
                content_hash="f" * 64,
                file_size=len(delta.encode("utf-8")),
                mode="delta",
                offset=final_offset + len(delta.encode("utf-8")),
                base_hash="0" * 64,
                base_offset=final_offset,
            )
        # Phase 0 captures the current synchronous fence exactly: the
        # document remains the last verified FULL source even while delivery
        # and sync projections reflect its DELTA tail.
        assert mismatch.value.expected_hash == full_hash
        assert mismatch.value.expected_offset == len(full.encode("utf-8"))
        await session.rollback()
        return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_core_delta_message_staging", "path_name"),
    ((False, "current_orm"), (True, "phase1_core")),
)
async def test_recorded_delta_sequences_match_phase0_golden(
    session_factory,
    use_core_delta_message_staging: bool,
    path_name: str,
) -> None:
    """Both writer paths must reproduce the current path's semantic output."""
    actual = {
        sequence.name: await _run_sequence(
            session_factory,
            sequence,
            use_core_delta_message_staging=use_core_delta_message_staging,
        )
        for sequence in RECORDED_DELTA_SEQUENCES
    }
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    difference = _first_difference(actual, expected)
    assert difference is None, (
        f"{path_name} drifted from the Phase 0 golden at {difference[0]}: "
        f"expected {difference[1]!r}, got {difference[2]!r}"
    )
