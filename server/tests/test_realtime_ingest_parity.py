"""Field-level golden gate for legacy, Core, and Phase 2 raw writers.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import settings
from server.db.models import (
    Base,
    ClaudeConversationLineageRecord,
    ConversationMessage,
    ConversationPromptProjection,
    ConversationReadModel,
    ConversationTaskState,
    ConversationUsageEvent,
    DashboardDocumentProjection,
    Document,
    DocumentDeliveryState,
    IngestProjectionCandidate,
    Machine,
    SyncState,
    Tool,
    User,
)
from server.services.document_delivery import document_metadata, store_document_metadata
from server.services.dashboard_projection import refresh_dashboard_document_projection
from server.services.ingest_service import (
    CURRENT_PENDING_QUESTIONS_KEY,
    LIVE_INTERACTION_SIGNALS_KEY,
    PENDING_QUESTION_COUNT_KEY,
    PENDING_QUESTION_RECONCILIATION_VERSION_KEY,
    DeltaBaseMismatch,
    ingest_file,
    reconcile_pending_question_metadata,
)


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
        # Match production's idempotent startup DDL for a shared test database
        # whose existing outbox can be from an earlier Phase 4 revision.
        from server.main import _run_migrations

        await connection.run_sync(_run_migrations)
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
    relative_path: str | None = None
    deferred_projection_fixture: bool = False
    delta_metadata: dict[str, object] | None = None
    authoritative_rebase_fixture: bool = False

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
        name="claude_subagent_transcript",
        tool_id="claude_code",
        metadata={"session_id": "agent-a8219f353e7676f9c"},
        full_rows=(
            {
                "type": "user",
                "uuid": "claude-subagent-user-full",
                "timestamp": "2026-08-04T09:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": "Inspect the raw writer pairing gate.",
                },
            },
            {
                "type": "assistant",
                "uuid": "claude-subagent-assistant-tool",
                "timestamp": "2026-08-04T09:00:01.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will inspect the gate."},
                        {
                            "type": "tool_use",
                            "id": "toolu_subagent_read",
                            "name": "Read",
                            "input": {
                                "file_path": (
                                    "server/server/services/realtime_raw_writer.py"
                                )
                            },
                        },
                    ],
                },
            },
        ),
        delta_rows=(
            {
                "type": "user",
                "uuid": "claude-subagent-tool-result",
                "timestamp": "2026-08-04T09:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_subagent_read",
                            "content": "The pairing gate is flag controlled.",
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "uuid": "claude-subagent-assistant-final",
                "timestamp": "2026-08-04T09:00:03.000Z",
                "message": {
                    "role": "assistant",
                    "content": "The raw transcript DELTA is ready to commit.",
                    "usage": {"input_tokens": 13, "output_tokens": 5},
                },
            },
        ),
        relative_path=(
            "projects/fe4bdc0b-1bbf-4c05-a174-1bd9ea5f4ac5/subagents/"
            "agent-a8219f353e7676f9c.jsonl"
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
        authoritative_rebase_fixture=True,
    ),
    RecordedDeltaSequence(
        name="codex_history_recovered_prompt",
        tool_id="codex",
        metadata={"session_id": "phase-history-recovered"},
        delta_metadata={
            "session_id": "phase-history-recovered",
            "user_history": [{
                "text": "Recovered prompt before the DELTA.",
                "ts": 1_754_093_610,
            }],
            "first_user_message": "Recovered prompt before the DELTA.",
        },
        full_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Persisted response before recovery.",
                },
            },
        ),
        delta_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:20Z",
                "payload": {
                    "type": "agent_message",
                    "message": "DELTA response after recovery.",
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="codex_history_resent_dedup",
        tool_id="codex",
        metadata={
            "session_id": "phase-history-resent",
            "user_history": [{
                "text": "Persist this history prompt once.",
                "ts": 1_754_093_610,
            }],
            "first_user_message": "Persist this history prompt once.",
        },
        delta_metadata={
            "session_id": "phase-history-resent",
            "user_history": [{
                "text": "Persist this history prompt once.",
                "ts": 1_754_093_610,
            }],
            "first_user_message": "Persist this history prompt once.",
        },
        full_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Response that precedes the history row.",
                },
            },
        ),
        delta_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:20Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Repeated-history DELTA response.",
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="codex_history_interleaved_positive_append",
        tool_id="codex",
        metadata={"session_id": "phase-history-interleave"},
        delta_metadata={
            "session_id": "phase-history-interleave",
            "user_history": [{
                "text": "Recovered prompt before normal append.",
                "ts": 1_754_093_610,
            }],
            "first_user_message": "Recovered prompt before normal append.",
        },
        full_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Baseline response.",
                },
            },
        ),
        delta_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:20Z",
                "payload": {
                    "type": "user_message",
                    "message": "Normal positive-line append.",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:30Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Assistant after normal append.",
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="codex_history_entry_bound",
        tool_id="codex",
        metadata={"session_id": "phase-history-bound"},
        delta_metadata={
            "session_id": "phase-history-bound",
            "user_history": [
                {"text": f"Bounded recovery prompt {index}", "ts": 0}
                for index in range(2_001)
            ],
        },
        full_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Bounded history baseline.",
                },
            },
        ),
        delta_rows=(
            {
                "type": "event_msg",
                "timestamp": "2025-08-02T11:00:20Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Bounded history DELTA.",
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
    RecordedDeltaSequence(
        name="claude_background_shell_and_meta_tool",
        tool_id="claude_code",
        metadata={"session_id": "phase0-background-shell"},
        full_rows=(
            {
                "type": "user",
                "uuid": "background-user",
                "timestamp": "2026-08-28T12:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": "Run the verification in the background.",
                },
            },
            {
                "type": "assistant",
                "uuid": "background-shell-launch",
                "timestamp": "2026-08-28T12:00:01.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu-background-shell",
                        "name": "Bash",
                        "input": {
                            "command": "pytest -q",
                            "run_in_background": True,
                        },
                    }],
                },
            },
        ),
        delta_rows=(
            {
                "type": "user",
                "uuid": "background-shell-result",
                "timestamp": "2026-08-28T12:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu-background-shell",
                        "content": (
                            "Command running in background with ID: task-shell-1. "
                            "You will be notified when it completes."
                        ),
                    }],
                },
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "sessionId": "phase0-background-shell",
                "timestamp": "2026-08-28T12:00:03.000Z",
                "content": (
                    "<task-notification>\n"
                    "<task-id>task-shell-1</task-id>\n"
                    "<tool-use-id>toolu-background-shell</tool-use-id>\n"
                    "<status>completed</status>\n"
                    "<summary>Background command completed</summary>\n"
                    "<result>All checks passed.</result>\n"
                    "</task-notification>"
                ),
            },
            {
                "type": "assistant",
                "uuid": "background-feedback",
                "timestamp": "2026-08-28T12:00:04.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu-feedback",
                        "name": "SendFeedback",
                        "input": {
                            "type": "bug",
                            "title": "Background shell parity fixture",
                        },
                    }],
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="claude_entrypoint_origin",
        tool_id="claude_code",
        metadata={"session_id": "phase0-entrypoint-origin"},
        full_rows=(
            {
                "type": "user",
                "uuid": "entrypoint-sdk-cli",
                "entrypoint": "sdk-cli",
                "timestamp": "2026-08-28T15:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": (
                        "MEMENTO-DELEGATE-FROM: 035914ae-8e99-4bbf-a9af-3602cf5019bc\n"
                        "Implement the approved design."
                    ),
                },
            },
        ),
        delta_rows=(
            {
                "type": "user",
                "uuid": "entrypoint-cli",
                "entrypoint": "cli",
                "timestamp": "2026-08-28T15:00:01.000Z",
                "message": {
                    "role": "user",
                    "content": "Please keep the operator messages labeled You.",
                },
            },
            {
                "type": "assistant",
                "uuid": "entrypoint-assistant",
                "timestamp": "2026-08-28T15:00:02.000Z",
                "message": {
                    "role": "assistant",
                    "content": "I will preserve per-message origin.",
                    "usage": {"input_tokens": 6, "output_tokens": 4},
                },
            },
        ),
    ),
    RecordedDeltaSequence(
        name="claude_deferred_lineage_lifecycle",
        tool_id="claude_code",
        metadata={
            "session_id": "agent-deferred-projection",
            "parent_thread_id": "deferred-projection-root",
            "root_session_id": "deferred-projection-root",
            "is_subagent": True,
        },
        full_rows=(
            {
                "type": "user",
                "uuid": "deferred-lineage-root",
                "timestamp": "2026-08-28T16:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": "Carry lineage through the raw DELTA.",
                },
            },
            {
                "type": "assistant",
                "uuid": "deferred-lineage-middle",
                "parentUuid": "deferred-lineage-root",
                "timestamp": "2026-08-28T16:00:01.000Z",
                "message": {
                    "role": "assistant",
                    "content": "The initial lineage is durable.",
                },
            },
        ),
        delta_rows=(
            {
                "type": "assistant",
                "uuid": "deferred-lineage-terminal",
                "parentUuid": "deferred-lineage-middle",
                "timestamp": "2026-08-28T16:00:02.000Z",
                "message": {
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": "The deferred terminal state is complete.",
                },
            },
        ),
        relative_path=(
            "projects/deferred-projection-root/subagents/"
            "agent-deferred-projection.jsonl"
        ),
        deferred_projection_fixture=True,
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


async def _search_document_snapshot(
    session: AsyncSession, document_id: uuid.UUID
) -> str | None:
    return (
        await session.execute(
            text("SELECT content_tsv::text FROM documents WHERE id = :id"),
            {"id": document_id},
        )
    ).scalar()


async def _snapshot(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    relative_path: str | None = None,
    include_lineage: bool = False,
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
                (
                    SyncState.relative_path == relative_path
                    if relative_path is not None
                    else SyncState.relative_path.like("phase0/%")
                ),
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
    snapshot = {
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
    if include_lineage:
        lineage = (
            await session.execute(
                select(ClaudeConversationLineageRecord)
                .where(ClaudeConversationLineageRecord.document_id == document_id)
                .order_by(ClaudeConversationLineageRecord.source_order)
            )
        ).scalars().all()
        snapshot["lineage_records"] = [
            _model_snapshot(
                row,
                (
                    "record_uuid",
                    "parent_uuid",
                    "source_order",
                    "is_sidechain",
                    "is_subagent",
                    "agent_id",
                    "is_eligible",
                    "active",
                ),
            )
            for row in lineage
        ]
        snapshot["search_document"] = await _search_document_snapshot(
            session, document_id
        )
    return snapshot


def _deferred_projection_golden_snapshot(
    snapshot: dict[str, object],
) -> dict[str, object]:
    """Keep the enhanced fixture focused on fields owned by the new kinds."""
    delivery = snapshot["delivery_state"]
    assert isinstance(delivery, dict)
    metadata = delivery["delivery_metadata"]
    assert isinstance(metadata, dict)
    lineage = snapshot["lineage_records"]
    assert isinstance(lineage, list)
    return {
        "lifecycle": {
            key: metadata.get(key)
            for key in (
                "subagent_lifecycle_status",
                "subagent_lifecycle_source",
                "subagent_lifecycle_at",
                "subagent_lifecycle_evidence",
            )
        },
        "lineage_records": [
            {
                key: row[key]
                for key in (
                    "record_uuid",
                    "parent_uuid",
                    "is_subagent",
                    "active",
                )
            }
            for row in lineage
            if isinstance(row, dict)
        ],
    }


def _deferred_search_document_golden_snapshot(
    snapshot: dict[str, object],
) -> dict[str, object]:
    """Capture the projector-applied search document, not just its invalidation."""
    return {"search_document": snapshot["search_document"]}


async def _run_sequence(
    session_factory,
    sequence: RecordedDeltaSequence,
    *,
    use_core_delta_message_staging: bool,
    writer: str | None = None,
    full_writer: str | None = None,
    apply_deferred_projections: bool = False,
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
        relative_path = sequence.relative_path or f"phase0/{sequence.name}.jsonl"

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
                metadata=dict(
                    sequence.delta_metadata
                    if mode == "delta" and sequence.delta_metadata is not None
                    else sequence.metadata
                ),
                timestamp=1_785_672_000.0 + (mode == "delta"),
                machine_id=machine_id,
                user_id=user_id,
                schedule_post_ingest=False,
                authoritative_rebase=authoritative_rebase,
                use_core_delta_message_staging=use_core_delta_message_staging,
                writer=(
                    full_writer
                    if mode == "full" and full_writer is not None
                    else writer
                ),
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
        document_id = document.id
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
        if apply_deferred_projections:
            from server.services.realtime_ingest_projector import (
                process_pending_candidates,
            )

            await process_pending_candidates(session, document_ids=(document_id,))
        snapshot = await _snapshot(
            session,
            document_id=document_id,
            user_id=user_uuid,
            relative_path=relative_path,
            include_lineage=sequence.deferred_projection_fixture,
        )
        await session.commit()
        session.info.pop(_PENDING_REALTIME_EVENTS, None)

        if sequence.authoritative_rebase_fixture:
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
                document_id=document_id,
                user_id=user_uuid,
                relative_path=relative_path,
                include_lineage=sequence.deferred_projection_fixture,
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
        # The raw writer fences from the current delivery projection. The ORM
        # paths retain their in-session Phase 0 view of the last FULL here;
        # a fresh production request reloads that same delivery projection.
        expected_hash = final_hash if writer == "raw" else full_hash
        expected_offset = (
            final_offset if writer == "raw" else len(full.encode("utf-8"))
        )
        assert mismatch.value.expected_hash == expected_hash
        assert mismatch.value.expected_offset == expected_offset
        await session.rollback()
        return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_core_delta_message_staging", "writer", "path_name"),
    (
        (False, "legacy", "current_orm"),
        (True, "core", "phase1_core"),
        (True, "raw", "phase2_raw"),
    ),
)
async def test_recorded_delta_sequences_match_phase0_golden(
    session_factory,
    use_core_delta_message_staging: bool,
    writer: str,
    path_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both writer paths must reproduce the current path's semantic output."""
    from server.config import settings

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", True)
    monkeypatch.setattr(settings, "realtime_ingest_raw_codex_history", True)
    phase0_sequences = tuple(
        sequence
        for sequence in RECORDED_DELTA_SEQUENCES
        if not sequence.deferred_projection_fixture
    )
    actual = {
        sequence.name: await _run_sequence(
            session_factory,
            sequence,
            use_core_delta_message_staging=use_core_delta_message_staging,
            writer=writer,
        )
        for sequence in phase0_sequences
    }
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    expected = {name: expected[name] for name in actual}
    difference = _first_difference(actual, expected)
    assert difference is None, (
        f"{path_name} drifted from the Phase 0 golden at {difference[0]}: "
        f"expected {difference[1]!r}, got {difference[2]!r}"
    )


@pytest.mark.asyncio
async def test_deferred_claude_lineage_lifecycle_matches_golden(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw DELTAs reproduce legacy lineage and terminal lifecycle via outbox."""
    sequence = next(
        item
        for item in RECORDED_DELTA_SEQUENCES
        if item.deferred_projection_fixture
    )
    monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", True)
    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", True)
    snapshot = await _run_sequence(
        session_factory,
        sequence,
        use_core_delta_message_staging=True,
        writer="raw",
        full_writer="legacy",
        apply_deferred_projections=True,
    )
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    actual = _deferred_projection_golden_snapshot(snapshot)
    expected = golden[sequence.name]
    difference = _first_difference(actual, expected)
    assert difference is None, (
        "raw deferred lineage/lifecycle drifted from the legacy golden at "
        f"{difference[0]}: expected {difference[1]!r}, got {difference[2]!r}"
    )
    search_actual = _deferred_search_document_golden_snapshot(snapshot)
    search_expected = golden["claude_deferred_search_document"]
    search_difference = _first_difference(search_actual, search_expected)
    assert search_difference is None, (
        "raw deferred search document drifted from the golden at "
        f"{search_difference[0]}: expected {search_difference[1]!r}, "
        f"got {search_difference[2]!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_core_delta_message_staging", "writer"),
    (
        (False, "legacy"),
        (True, "core"),
        (True, "raw"),
    ),
)
async def test_historic_meta_interaction_does_not_rehydrate_pending_count(
    session_factory,
    use_core_delta_message_staging: bool,
    writer: str,
) -> None:
    """A delta rebuilds historic SendFeedback badge state in every writer."""
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"meta-pending-{writer}-{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name=f"meta-pending-{writer}",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "claude_code") is None:
            session.add(Tool(id="claude_code", display_name="claude_code"))
        session.add_all((user, machine))
        await session.commit()

        full = _json_line({
            "type": "user",
            "uuid": f"meta-pending-full-{writer}",
            "timestamp": "2026-08-28T12:00:00Z",
            "message": {"role": "user", "content": "Initial context."},
        })
        full_hash = _hash(full)
        document = await ingest_file(
            session,
            tool_id="claude_code",
            category="conversation",
            content_type="jsonl",
            relative_path=f"phase0/meta-pending-{writer}.jsonl",
            content=full,
            content_hash=full_hash,
            file_size=len(full.encode("utf-8")),
            mode="full",
            offset=len(full.encode("utf-8")),
            metadata={"session_id": f"meta-pending-{writer}"},
            timestamp=1_788_000_000.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            use_core_delta_message_staging=use_core_delta_message_staging,
            writer=writer,
        )
        document_id = document.id
        persisted_document = await session.get(Document, document_id)
        assert persisted_document is not None
        historic_interaction = {
            "id": "feedback-1",
            "kind": "question",
            "source": "claude_code",
            "tool_name": "SendFeedback",
            "questions": [],
        }
        session.add(ConversationMessage(
            document_id=document_id,
            line_number=2,
            role="tool",
            message_type="tool_use",
            content="[SendFeedback]",
            metadata_={
                "source_id": f"historic-feedback-{writer}",
                "interaction": historic_interaction,
            },
            timestamp=datetime(2026, 8, 28, 12, 0, 1, tzinfo=timezone.utc),
        ))
        store_document_metadata(
            persisted_document,
            {
                **document_metadata(persisted_document),
                CURRENT_PENDING_QUESTIONS_KEY: ["feedback-1"],
                PENDING_QUESTION_COUNT_KEY: 1,
                PENDING_QUESTION_RECONCILIATION_VERSION_KEY: 3,
            },
        )
        await session.commit()

        delta = _json_line({
            "type": "assistant",
            "uuid": f"meta-pending-delta-{writer}",
            "timestamp": "2026-08-28T12:00:02Z",
            "message": {"role": "assistant", "content": "Tail update."},
        })
        final = f"{full}\n{delta}"
        await ingest_file(
            session,
            tool_id="claude_code",
            category="conversation",
            content_type="jsonl",
            relative_path=f"phase0/meta-pending-{writer}.jsonl",
            content=delta,
            content_hash=_hash(final),
            file_size=len(delta.encode("utf-8")),
            mode="delta",
            offset=len(final.encode("utf-8")),
            base_hash=full_hash,
            base_offset=len(full.encode("utf-8")),
            metadata={"session_id": f"meta-pending-{writer}"},
            timestamp=1_788_000_001.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            use_core_delta_message_staging=use_core_delta_message_staging,
            writer=writer,
        )
        await session.commit()
        await session.refresh(persisted_document)

        metadata = document_metadata(persisted_document)
        assert CURRENT_PENDING_QUESTIONS_KEY not in metadata
        assert metadata.get(PENDING_QUESTION_COUNT_KEY, 0) == 0
        dashboard = await session.get(DashboardDocumentProjection, document_id)
        assert dashboard is None or dashboard.pending_question_count == 0


@pytest.mark.asyncio
async def test_pending_question_reconciliation_refreshes_dashboard_projection(
    session_factory,
) -> None:
    """The startup v4 repair updates the durable dashboard badge too."""
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"meta-reconcile-{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="meta-reconcile",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "claude_code") is None:
            session.add(Tool(id="claude_code", display_name="claude_code"))
        session.add_all((user, machine))
        await session.commit()

        full = _json_line({
            "type": "user",
            "uuid": "meta-reconcile-full",
            "timestamp": "2026-08-28T12:00:00Z",
            "message": {"role": "user", "content": "Initial context."},
        })
        document = await ingest_file(
            session,
            tool_id="claude_code",
            category="conversation",
            content_type="jsonl",
            relative_path="phase0/meta-reconcile.jsonl",
            content=full,
            content_hash=_hash(full),
            file_size=len(full.encode("utf-8")),
            mode="full",
            offset=len(full.encode("utf-8")),
            metadata={"session_id": "meta-reconcile"},
            timestamp=1_788_000_100.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            use_core_delta_message_staging=False,
            writer="legacy",
        )
        document_id = document.id
        persisted_document = await session.get(Document, document_id)
        assert persisted_document is not None
        session.add(ConversationMessage(
            document_id=document_id,
            line_number=2,
            role="tool",
            message_type="tool_use",
            content="[SendFeedback]",
            metadata_={
                "source_id": "historic-reconcile-feedback",
                "interaction": {
                    "id": "feedback-reconcile-1",
                    "kind": "question",
                    "source": "claude_code",
                    "tool_name": "SendFeedback",
                    "questions": [],
                },
            },
            timestamp=datetime(2026, 8, 28, 12, 0, 1, tzinfo=timezone.utc),
        ))
        store_document_metadata(
            persisted_document,
            {
                **document_metadata(persisted_document),
                CURRENT_PENDING_QUESTIONS_KEY: ["feedback-reconcile-1"],
                PENDING_QUESTION_COUNT_KEY: 1,
                PENDING_QUESTION_RECONCILIATION_VERSION_KEY: 3,
            },
        )
        await refresh_dashboard_document_projection(session, persisted_document)
        await session.commit()

        updated = await reconcile_pending_question_metadata(session)
        await session.refresh(persisted_document)
        dashboard = await session.get(DashboardDocumentProjection, document_id)

        assert updated == 1
        assert CURRENT_PENDING_QUESTIONS_KEY not in document_metadata(persisted_document)
        assert document_metadata(persisted_document).get(
            PENDING_QUESTION_COUNT_KEY,
            0,
        ) == 0
        assert dashboard is not None
        assert dashboard.pending_question_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_core_delta_message_staging", "writer"),
    (
        (False, "legacy"),
        (True, "core"),
        (True, "raw"),
    ),
)
async def test_version_transition_retains_live_only_pending_interaction(
    session_factory,
    use_core_delta_message_staging: bool,
    writer: str,
) -> None:
    """A v3 delta retains a legitimate live-only interaction through v4."""
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"live-transition-{writer}-{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name=f"live-transition-{writer}",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "claude_code") is None:
            session.add(Tool(id="claude_code", display_name="claude_code"))
        session.add_all((user, machine))
        await session.commit()

        full = _json_line({
            "type": "user",
            "uuid": f"live-transition-full-{writer}",
            "timestamp": "2026-08-28T12:00:00Z",
            "message": {"role": "user", "content": "Initial context."},
        })
        full_hash = _hash(full)
        document = await ingest_file(
            session,
            tool_id="claude_code",
            category="conversation",
            content_type="jsonl",
            relative_path=f"phase0/live-transition-{writer}.jsonl",
            content=full,
            content_hash=full_hash,
            file_size=len(full.encode("utf-8")),
            mode="full",
            offset=len(full.encode("utf-8")),
            metadata={"session_id": f"live-transition-{writer}"},
            timestamp=1_788_000_200.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            use_core_delta_message_staging=use_core_delta_message_staging,
            writer=writer,
        )
        document_id = document.id
        persisted_document = await session.get(Document, document_id)
        assert persisted_document is not None
        live_interaction = {
            "id": "ask-live-1",
            "kind": "question",
            "source": "claude_code",
            "tool_name": "AskUserQuestion",
            "questions": [],
        }
        store_document_metadata(
            persisted_document,
            {
                **document_metadata(persisted_document),
                CURRENT_PENDING_QUESTIONS_KEY: ["ask-live-1"],
                PENDING_QUESTION_COUNT_KEY: 1,
                PENDING_QUESTION_RECONCILIATION_VERSION_KEY: 3,
                LIVE_INTERACTION_SIGNALS_KEY: {
                    "ask-live-1": {
                        "timestamp": "2026-08-28T12:00:01Z",
                        "interaction": live_interaction,
                    },
                },
            },
        )
        await session.commit()

        delta = _json_line({
            "type": "assistant",
            "uuid": f"live-transition-delta-{writer}",
            "timestamp": "2026-08-28T12:00:02Z",
            "message": {"role": "assistant", "content": "Tail update."},
        })
        final = f"{full}\n{delta}"
        await ingest_file(
            session,
            tool_id="claude_code",
            category="conversation",
            content_type="jsonl",
            relative_path=f"phase0/live-transition-{writer}.jsonl",
            content=delta,
            content_hash=_hash(final),
            file_size=len(delta.encode("utf-8")),
            mode="delta",
            offset=len(final.encode("utf-8")),
            base_hash=full_hash,
            base_offset=len(full.encode("utf-8")),
            metadata={"session_id": f"live-transition-{writer}"},
            timestamp=1_788_000_201.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            use_core_delta_message_staging=use_core_delta_message_staging,
            writer=writer,
        )
        await session.commit()
        await session.refresh(persisted_document)

        metadata = document_metadata(persisted_document)
        assert metadata[CURRENT_PENDING_QUESTIONS_KEY] == ["ask-live-1"]
        assert metadata[PENDING_QUESTION_COUNT_KEY] == 1
        assert metadata[PENDING_QUESTION_RECONCILIATION_VERSION_KEY] == 4
        assert LIVE_INTERACTION_SIGNALS_KEY in metadata
        dashboard = await session.get(DashboardDocumentProjection, document_id)
        assert dashboard is not None
        assert dashboard.pending_question_count == 1


@pytest.mark.asyncio
async def test_coalesced_raw_chain_matches_same_frames_one_by_one(
    session_factory,
) -> None:
    """Phase 3's one-transaction chain preserves the Phase 0 row golden."""
    from server.services.realtime_raw_writer import (
        ingest_conversation_raw,
        ingest_conversation_raw_chain,
    )

    sequence = next(item for item in RECORDED_DELTA_SEQUENCES if item.tool_id == "codex")
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(), email=f"phase3-chain-{uuid.uuid4()}@example.test",
            role="viewer", status="active",
        )
        machine = Machine(
            id=uuid.uuid4(), name="phase3-chain",
            collector_token_hash=str(uuid.uuid4()), user_id=user.id,
        )
        session.add_all((user, machine))
        if await session.get(Tool, "codex") is None:
            session.add(Tool(id="codex", display_name="codex"))
        await session.commit()

        fragments = tuple(_json_line(row) for row in sequence.delta_rows)

        async def seed(path: str):
            full_hash = _hash(sequence.full)
            return await ingest_conversation_raw(
                tool_id="codex", category="conversation", content_type="jsonl",
                relative_path=path, content=sequence.full, content_hash=full_hash,
                file_size=len(sequence.full.encode("utf-8")), mode="full",
                offset=len(sequence.full.encode("utf-8")), metadata=dict(sequence.metadata),
                timestamp=1_785_672_000.0, machine_id=machine.id, user_id=user.id,
                base_hash=None, base_offset=None, database_url=TEST_DATABASE_URL,
            )

        individual_path = f"phase0/phase3-individual-{uuid.uuid4()}.jsonl"
        chain_path = f"phase0/phase3-chain-{uuid.uuid4()}.jsonl"
        individual_document, _ = await seed(individual_path)
        chain_document, _ = await seed(chain_path)
        current = sequence.full
        current_hash = _hash(current)
        current_offset = len(current.encode("utf-8"))
        chain_frames: list[dict[str, object]] = []
        for index, fragment in enumerate(fragments, start=1):
            next_snapshot = f"{current}\n{fragment}"
            next_hash = _hash(next_snapshot)
            next_offset = len(next_snapshot.encode("utf-8"))
            common = {
                "tool_id": "codex", "category": "conversation",
                "content_type": "jsonl", "content": fragment,
                "content_hash": next_hash, "file_size": len(fragment.encode("utf-8")),
                "mode": "delta", "offset": next_offset,
                "metadata": dict(sequence.metadata),
                "timestamp": 1_785_672_000.0 + index,
                "machine_id": machine.id, "user_id": user.id,
                "base_hash": current_hash, "base_offset": current_offset,
            }
            await ingest_conversation_raw(
                relative_path=individual_path,
                **common,
                database_url=TEST_DATABASE_URL,
            )
            chain_frames.append({**common, "relative_path": chain_path})
            current, current_hash, current_offset = next_snapshot, next_hash, next_offset

        committed, event = await ingest_conversation_raw_chain(
            frames=chain_frames,
            database_url=TEST_DATABASE_URL,
        )
        assert committed.id == chain_document.id
        assert event is not None
        assert event["data"]["changes"]
        individual = await _snapshot(
            session,
            document_id=individual_document.id,
            user_id=user.id,
            relative_path=individual_path,
        )
        coalesced = await _snapshot(
            session,
            document_id=chain_document.id,
            user_id=user.id,
            relative_path=chain_path,
        )
        # IDs and paths are intentionally excluded by _snapshot.  The final
        # messages, delivery/sync fence, and all live projections must match.
        for snapshot in (individual, coalesced):
            snapshot["sync_state"]["relative_path"] = "<path>"
            if snapshot["dashboard_projection"] is not None:
                snapshot["dashboard_projection"]["relative_path"] = "<path>"
                snapshot["dashboard_projection"]["title"] = "<path-title>"
        assert coalesced == individual

        # A restart after COMMIT but before receipt/SSE cleanup replays the
        # exact chain. It must produce a safe post-commit invalidation even
        # though the final revision is already idempotent.
        retry, retry_event = await ingest_conversation_raw_chain(
            frames=chain_frames,
            database_url=TEST_DATABASE_URL,
        )
        assert retry.disposition == "idempotent"
        assert retry_event is not None
        assert "conversation.messages" in retry_event["data"]["changes"]

        # A successor can arrive while the previous drain is committing. If
        # the process dies before removing the head marker, restart sees the
        # already-committed prefix plus new frames and must resume the suffix.
        resumed_path = f"phase0/phase3-resumed-{uuid.uuid4()}.jsonl"
        resumed_document, _ = await seed(resumed_path)
        resumed_frames = [
            {**frame, "relative_path": resumed_path} for frame in chain_frames
        ]
        await ingest_conversation_raw(
            **resumed_frames[0],
            database_url=TEST_DATABASE_URL,
        )
        resumed, resumed_event = await ingest_conversation_raw_chain(
            frames=resumed_frames,
            database_url=TEST_DATABASE_URL,
        )
        assert resumed.id == resumed_document.id
        assert resumed_event is not None
        resumed_snapshot = await _snapshot(
            session,
            document_id=resumed_document.id,
            user_id=user.id,
            relative_path=resumed_path,
        )
        resumed_snapshot["sync_state"]["relative_path"] = "<path>"
        if resumed_snapshot["dashboard_projection"] is not None:
            resumed_snapshot["dashboard_projection"]["relative_path"] = "<path>"
            resumed_snapshot["dashboard_projection"]["title"] = "<path-title>"
        assert resumed_snapshot == individual


@pytest.mark.asyncio
async def test_raw_history_repeat_commits_and_title_matches_legacy_with_search_candidate(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 4.5 keeps redundant Codex history raw and ports title precedence."""
    from server.config import settings
    from server.services.realtime_raw_writer import ingest_conversation_raw

    monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", True)
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(), email=f"phase45-{uuid.uuid4()}@example.test",
            role="viewer", status="active",
        )
        machine = Machine(
            id=uuid.uuid4(), name="phase45",
            collector_token_hash=str(uuid.uuid4()), user_id=user.id,
        )
        session.add_all((user, machine))
        await session.commit()

        codex_full = _json_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T12:00:00Z",
                "payload": {"type": "agent_message", "message": "Seed response."},
            }
        )
        codex_full_hash = _hash(codex_full)
        codex_path = f"phase45/history-{uuid.uuid4()}.jsonl"
        codex_document, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=codex_path, content=codex_full,
            content_hash=codex_full_hash,
            file_size=len(codex_full.encode("utf-8")), mode="full",
            offset=len(codex_full.encode("utf-8")),
            metadata={"session_id": "phase45-history"}, timestamp=1_785_672_000.0,
            machine_id=machine.id, user_id=user.id, base_hash=None, base_offset=None,
            database_url=TEST_DATABASE_URL,
        )
        history_at = datetime.fromtimestamp(1_785_672_000.0, tz=timezone.utc)
        session.add(
            ConversationMessage(
                document_id=codex_document.id,
                line_number=2,
                message_type="history_user_message",
                role="user",
                content="Use the raw writer.",
                metadata_={"source_id": "codex-history:0"},
                timestamp=history_at,
            )
        )
        await session.commit()
        codex_delta = _json_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Raw commit."},
            }
        )
        codex_final = f"{codex_full}\n{codex_delta}"
        codex_committed, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=codex_path, content=codex_delta,
            content_hash=_hash(codex_final), file_size=len(codex_delta.encode("utf-8")),
            mode="delta", offset=len(codex_final.encode("utf-8")),
            metadata={
                "session_id": "phase45-history",
                "user_history": [{"text": "Use the raw writer.", "ts": 1_785_672_000}],
                "first_user_message": "Use the raw writer.",
            },
            timestamp=1_785_672_001.0, machine_id=machine.id, user_id=user.id,
            base_hash=codex_full_hash, base_offset=len(codex_full.encode("utf-8")),
            database_url=TEST_DATABASE_URL,
        )
        assert codex_committed.id == codex_document.id
        assert codex_committed.disposition == "committed"

        claude_full = _json_line(
            {
                "type": "assistant",
                "uuid": "phase45-claude-full",
                "timestamp": "2026-08-27T12:00:00Z",
                "message": {"role": "assistant", "content": "Seed title."},
            }
        )
        claude_delta = _json_line(
            {
                "type": "assistant",
                "uuid": "phase45-claude-delta",
                "timestamp": "2026-08-27T12:00:01Z",
                "message": {"role": "assistant", "content": "Updated title."},
            }
        )
        claude_full_hash = _hash(claude_full)
        initial_metadata = {
            "session_id": "phase45-title",
            "source_title_kind": "claude_ai_title",
            "title": "Initial Claude title",
        }
        raw_path = f"phase45/title-raw-{uuid.uuid4()}.jsonl"
        legacy_path = f"phase45/title-legacy-{uuid.uuid4()}.jsonl"
        raw_document, _ = await ingest_conversation_raw(
            tool_id="claude_code", category="conversation", content_type="jsonl",
            relative_path=raw_path, content=claude_full,
            content_hash=claude_full_hash,
            file_size=len(claude_full.encode("utf-8")), mode="full",
            offset=len(claude_full.encode("utf-8")), metadata=initial_metadata,
            timestamp=1_787_500_800.0, machine_id=machine.id, user_id=user.id,
            base_hash=None, base_offset=None, database_url=TEST_DATABASE_URL,
        )
        legacy_document = await ingest_file(
            session, tool_id="claude_code", category="conversation",
            content_type="jsonl", relative_path=legacy_path, content=claude_full,
            content_hash=claude_full_hash, file_size=len(claude_full.encode("utf-8")),
            mode="full", offset=len(claude_full.encode("utf-8")),
            metadata=initial_metadata, timestamp=1_787_500_800.0,
            machine_id=machine.id, user_id=str(user.id), base_hash=None,
            base_offset=None, schedule_post_ingest=False, writer="legacy",
        )
        await session.commit()
        updated_metadata = {
            "session_id": "phase45-title",
            "source_title_kind": "claude_ai_title",
            "title": "Selected Claude title",
        }
        claude_final = f"{claude_full}\n{claude_delta}"
        raw_committed, _ = await ingest_conversation_raw(
            tool_id="claude_code", category="conversation", content_type="jsonl",
            relative_path=raw_path, content=claude_delta,
            content_hash=_hash(claude_final),
            file_size=len(claude_delta.encode("utf-8")), mode="delta",
            offset=len(claude_final.encode("utf-8")), metadata=updated_metadata,
            timestamp=1_787_500_801.0, machine_id=machine.id, user_id=user.id,
            base_hash=claude_full_hash, base_offset=len(claude_full.encode("utf-8")),
            database_url=TEST_DATABASE_URL,
        )
        await ingest_file(
            session, tool_id="claude_code", category="conversation",
            content_type="jsonl", relative_path=legacy_path, content=claude_delta,
            content_hash=_hash(claude_final),
            file_size=len(claude_delta.encode("utf-8")), mode="delta",
            offset=len(claude_final.encode("utf-8")), metadata=updated_metadata,
            timestamp=1_787_500_801.0, machine_id=machine.id, user_id=str(user.id),
            base_hash=claude_full_hash, base_offset=len(claude_full.encode("utf-8")),
            schedule_post_ingest=False, writer="legacy",
        )
        await session.commit()
        raw_title = await session.scalar(
            select(Document.title).where(Document.id == raw_committed.id)
        )
        legacy_title = await session.scalar(
            select(Document.title).where(Document.id == legacy_document.id)
        )
        assert raw_title == legacy_title == "Selected Claude title"
        search_candidate = await session.scalar(
            select(IngestProjectionCandidate).where(
                IngestProjectionCandidate.document_id == raw_document.id,
                IngestProjectionCandidate.revision_hash == _hash(claude_final),
                IngestProjectionCandidate.kind == "search",
            )
        )
        assert search_candidate is not None


@pytest.mark.asyncio
async def test_history_noop_proof_matches_legacy_source_dedup_cases(
    session_factory,
) -> None:
    """The raw shortcut defers whenever legacy history reconciliation mutates."""
    from server.services.realtime_raw_writer import (
        RawWriterUnsupported,
        ingest_conversation_raw,
    )

    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(), email=f"phase45-history-{uuid.uuid4()}@example.test",
            role="viewer", status="active",
        )
        machine = Machine(
            id=uuid.uuid4(), name="phase45-history",
            collector_token_hash=str(uuid.uuid4()), user_id=user.id,
        )
        session.add_all((user, machine))
        await session.commit()

        async def seed_pair(
            *,
            full: str,
            metadata: dict[str, object],
            stem: str,
        ) -> tuple[Document, Document, str, str, str]:
            full_hash = _hash(full)
            raw_path = f"phase45/{stem}-raw-{uuid.uuid4()}.jsonl"
            legacy_path = f"phase45/{stem}-legacy-{uuid.uuid4()}.jsonl"
            raw_document, _ = await ingest_conversation_raw(
                tool_id="codex", category="conversation", content_type="jsonl",
                relative_path=raw_path, content=full, content_hash=full_hash,
                file_size=len(full.encode("utf-8")), mode="full",
                offset=len(full.encode("utf-8")), metadata=metadata,
                timestamp=1_785_672_000.0, machine_id=machine.id, user_id=user.id,
                base_hash=None, base_offset=None, database_url=TEST_DATABASE_URL,
            )
            legacy_document = await ingest_file(
                session, tool_id="codex", category="conversation",
                content_type="jsonl", relative_path=legacy_path, content=full,
                content_hash=full_hash, file_size=len(full.encode("utf-8")),
                mode="full", offset=len(full.encode("utf-8")), metadata=metadata,
                timestamp=1_785_672_000.0, machine_id=machine.id,
                user_id=str(user.id), base_hash=None, base_offset=None,
                schedule_post_ingest=False, writer="legacy",
            )
            await session.commit()
            return raw_document, legacy_document, raw_path, legacy_path, full_hash

        async def snapshot_live_state(
            *,
            document_id: uuid.UUID,
            path: str,
        ) -> dict[str, object]:
            snapshot = await _snapshot(
                session,
                document_id=document_id,
                user_id=user.id,
                relative_path=path,
            )
            return {
                "messages": snapshot["messages"],
                "read_model": snapshot["read_model"],
                "prompt_projections": snapshot["prompt_projections"],
            }

        prompt = "Deduplicate this recovered prompt."
        recovered_full = _json_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T12:00:00Z",
                "payload": {"type": "agent_message", "message": "Seed response."},
            }
        )
        recovered_metadata = {"session_id": "phase45-recovered-match"}
        raw_document, legacy_document, raw_path, legacy_path, full_hash = await seed_pair(
            full=recovered_full,
            metadata=recovered_metadata,
            stem="recovered-match",
        )
        history_at = datetime.fromtimestamp(1_785_672_000.0, tz=timezone.utc)
        for document in (raw_document, legacy_document):
            session.add(
                ConversationMessage(
                    document_id=document.id,
                    line_number=2,
                    message_type="history_user_message",
                    role="user",
                    content=prompt,
                    metadata_={"source_id": "codex-history:0"},
                    timestamp=history_at,
                )
            )
        await session.commit()
        matching_user_delta = _json_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T12:00:01Z",
                "payload": {
                    "type": "user_message",
                    "client_id": "phase45-history-user",
                    "message": prompt,
                },
            }
        )
        matching_final = f"{recovered_full}\n{matching_user_delta}"
        matching_metadata = {
            "session_id": "phase45-recovered-match",
            "user_history": [{"text": prompt, "ts": 1_785_672_000}],
            "first_user_message": prompt,
        }
        raw_delta_kwargs = {
            "tool_id": "codex",
            "category": "conversation",
            "content_type": "jsonl",
            "content": matching_user_delta,
            "content_hash": _hash(matching_final),
            "file_size": len(matching_user_delta.encode("utf-8")),
            "mode": "delta",
            "offset": len(matching_final.encode("utf-8")),
            "metadata": matching_metadata,
            "timestamp": 1_785_672_001.0,
            "machine_id": machine.id,
            "user_id": user.id,
            "base_hash": full_hash,
            "base_offset": len(recovered_full.encode("utf-8")),
        }
        with pytest.raises(RawWriterUnsupported):
            await ingest_conversation_raw(
                relative_path=raw_path,
                database_url=TEST_DATABASE_URL,
                **raw_delta_kwargs,
            )
        raw_fallback_document = await ingest_file(
            session,
            relative_path=raw_path,
            schedule_post_ingest=False,
            writer="raw",
            **raw_delta_kwargs,
        )
        legacy_delta_kwargs = dict(raw_delta_kwargs)
        legacy_delta_kwargs["user_id"] = str(user.id)
        legacy_result = await ingest_file(
            session,
            relative_path=legacy_path,
            schedule_post_ingest=False,
            writer="legacy",
            **legacy_delta_kwargs,
        )
        await session.commit()
        raw_state = await snapshot_live_state(
            document_id=raw_fallback_document.id,
            path=raw_path,
        )
        legacy_state = await snapshot_live_state(
            document_id=legacy_result.id,
            path=legacy_path,
        )
        assert raw_state == legacy_state
        assert len(raw_state["messages"]) == 2
        assert [item["message_type"] for item in raw_state["messages"]] == [
            "agent_message",
            "user_message",
        ]

        represented_full = _json_line(
            {
                "type": "response_item",
                "timestamp": "2026-08-02T12:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )
        represented_metadata = {"session_id": "phase45-ordinary-match"}
        raw_document, legacy_document, raw_path, legacy_path, full_hash = await seed_pair(
            full=represented_full,
            metadata=represented_metadata,
            stem="ordinary-match",
        )
        repeat_delta = _json_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T12:00:01Z",
                "payload": {"type": "agent_message", "message": "Repeat committed."},
            }
        )
        repeat_final = f"{represented_full}\n{repeat_delta}"
        repeat_metadata = {
            "session_id": "phase45-ordinary-match",
            "user_history": [{"text": prompt, "ts": 1_785_672_000}],
            "first_user_message": prompt,
        }
        raw_repeat_document, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=raw_path, content=repeat_delta,
            content_hash=_hash(repeat_final), file_size=len(repeat_delta.encode("utf-8")),
            mode="delta", offset=len(repeat_final.encode("utf-8")),
            metadata=repeat_metadata, timestamp=1_785_672_001.0,
            machine_id=machine.id, user_id=user.id, base_hash=full_hash,
            base_offset=len(represented_full.encode("utf-8")),
            database_url=TEST_DATABASE_URL,
        )
        legacy_repeat_document = await ingest_file(
            session, tool_id="codex", category="conversation",
            content_type="jsonl", relative_path=legacy_path, content=repeat_delta,
            content_hash=_hash(repeat_final), file_size=len(repeat_delta.encode("utf-8")),
            mode="delta", offset=len(repeat_final.encode("utf-8")),
            metadata=repeat_metadata, timestamp=1_785_672_001.0,
            machine_id=machine.id, user_id=str(user.id), base_hash=full_hash,
            base_offset=len(represented_full.encode("utf-8")),
            schedule_post_ingest=False, writer="legacy",
        )
        await session.commit()
        assert await snapshot_live_state(
            document_id=raw_repeat_document.id,
            path=raw_path,
        ) == await snapshot_live_state(
            document_id=legacy_repeat_document.id,
            path=legacy_path,
        )


@pytest.mark.asyncio
async def test_raw_ambiguous_commit_rereads_fence_and_retry_is_idempotent(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost commit response converges from delivery/sync state, never rows."""
    from server.services import realtime_raw_writer
    from server.services.realtime_raw_writer import (
        RawWriterCommitUncertain,
        ingest_conversation_raw,
    )

    sequence = next(item for item in RECORDED_DELTA_SEQUENCES if item.tool_id == "codex")
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(), email=f"raw-ambiguous-{uuid.uuid4()}@example.test",
            role="viewer", status="active",
        )
        machine = Machine(
            id=uuid.uuid4(), name="raw-ambiguous",
            collector_token_hash=str(uuid.uuid4()), user_id=user.id,
        )
        session.add_all((user, machine))
        if await session.get(Tool, "codex") is None:
            session.add(Tool(id="codex", display_name="codex"))
        await session.commit()
        path = f"phase2/ambiguous-{uuid.uuid4()}.jsonl"
        full_hash = _hash(sequence.full)
        document, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=path, content=sequence.full, content_hash=full_hash,
            file_size=len(sequence.full.encode("utf-8")), mode="full",
            offset=len(sequence.full.encode("utf-8")), metadata=dict(sequence.metadata),
            timestamp=1_785_672_000.0, machine_id=machine.id, user_id=user.id,
            base_hash=None, base_offset=None, database_url=TEST_DATABASE_URL,
        )
        final = f"{sequence.full}\n{sequence.delta}"
        final_hash = _hash(final)
        final_offset = len(final.encode("utf-8"))
        committed, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=path, content=sequence.delta, content_hash=final_hash,
            file_size=len(sequence.delta.encode("utf-8")), mode="delta",
            offset=final_offset, metadata=dict(sequence.metadata),
            timestamp=1_785_672_001.0, machine_id=machine.id, user_id=user.id,
            base_hash=full_hash, base_offset=len(sequence.full.encode("utf-8")),
            database_url=TEST_DATABASE_URL, simulate_ambiguous_commit=True,
        )
        assert committed.id == document.id
        retry, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=path, content=sequence.delta, content_hash=final_hash,
            file_size=len(sequence.delta.encode("utf-8")), mode="delta",
            offset=final_offset, metadata=dict(sequence.metadata),
            timestamp=1_785_672_001.0, machine_id=machine.id, user_id=user.id,
            base_hash=full_hash, base_offset=len(sequence.full.encode("utf-8")),
            database_url=TEST_DATABASE_URL,
        )
        assert retry.disposition == "idempotent"
        assert await session.scalar(
            select(func.count()).select_from(ConversationMessage).where(
                ConversationMessage.document_id == document.id
            )
        ) == 2
        second_delta = _json_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T11:00:04Z",
                "payload": {
                    "type": "agent_message",
                    "message": "The chained raw delivery committed once.",
                },
            }
        )
        second_final = f"{final}\n{second_delta}"
        second_hash = _hash(second_final)
        second_offset = len(second_final.encode("utf-8"))
        chained, _ = await ingest_conversation_raw(
            tool_id="codex", category="conversation", content_type="jsonl",
            relative_path=path, content=second_delta, content_hash=second_hash,
            file_size=len(second_delta.encode("utf-8")), mode="delta",
            offset=second_offset, metadata=dict(sequence.metadata),
            timestamp=1_785_672_002.0, machine_id=machine.id, user_id=user.id,
            base_hash=final_hash, base_offset=final_offset,
            database_url=TEST_DATABASE_URL,
        )
        assert chained.id == document.id
        assert await session.scalar(
            select(func.count()).select_from(ConversationMessage).where(
                ConversationMessage.document_id == document.id
            )
        ) == 3
        delivery = await session.get(DocumentDeliveryState, document.id)
        sync = await session.scalar(
            select(SyncState).where(
                SyncState.machine_id == machine.id,
                SyncState.tool_id == "codex",
                SyncState.relative_path == path,
            )
        )
        assert delivery is not None and delivery.revision_hash == second_hash
        assert sync is not None and sync.last_hash == second_hash
        assert sync.last_offset == second_offset

        async def uncertain_commit(**_kwargs):
            raise RawWriterCommitUncertain("unknown commit result")

        monkeypatch.setattr(
            realtime_raw_writer,
            "ingest_conversation_raw",
            uncertain_commit,
        )
        with pytest.raises(RawWriterCommitUncertain):
            await ingest_file(
                session,
                tool_id="codex",
                category="conversation",
                content_type="jsonl",
                relative_path=path,
                content=second_delta,
                content_hash=second_hash,
                file_size=len(second_delta.encode("utf-8")),
                mode="delta",
                offset=second_offset,
                metadata=dict(sequence.metadata),
                timestamp=1_785_672_002.0,
                machine_id=machine.id,
                user_id=str(user.id),
                base_hash=final_hash,
                base_offset=final_offset,
                schedule_post_ingest=False,
                writer="raw",
            )


@pytest.mark.asyncio
async def test_raw_full_minio_ambiguous_put_never_commits_a_pointer(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed/ambiguous verified PUT leaves no document pointer to expose."""
    from botocore.exceptions import ClientError
    from server.config import settings
    from server.services import large_content_store
    from server.services.realtime_raw_writer import RawWriterFailure, ingest_conversation_raw

    class AmbiguousPutClient:
        def get_object(self, *, Bucket, Key):
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        def put_object(self, **_kwargs):
            raise TimeoutError("ambiguous PUT")

    monkeypatch.setattr(settings, "document_content_minio_enabled", True)
    monkeypatch.setattr(large_content_store, "_client", lambda: AmbiguousPutClient())
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(), email=f"raw-minio-{uuid.uuid4()}@example.test",
            role="viewer", status="active",
        )
        machine = Machine(
            id=uuid.uuid4(), name="raw-minio", collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        session.add_all((user, machine))
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="cursor"))
        await session.commit()
        path = f"phase2/minio-{uuid.uuid4()}.jsonl"
        content = _json_line({"type": "user", "role": "user", "id": "u", "message": {"content": "safe"}})
        with pytest.raises(RawWriterFailure):
            await ingest_conversation_raw(
                tool_id="cursor", category="conversation", content_type="jsonl",
                relative_path=path, content=content, content_hash=_hash(content),
                file_size=len(content.encode("utf-8")), mode="full",
                offset=len(content.encode("utf-8")), metadata={"session_id": "raw-minio"},
                timestamp=1_785_672_000.0, machine_id=machine.id, user_id=user.id,
                base_hash=None, base_offset=None, database_url=TEST_DATABASE_URL,
            )
        assert await session.scalar(
            select(func.count()).select_from(Document).where(
                Document.machine_id == machine.id,
                Document.relative_path == path,
            )
        ) == 0
