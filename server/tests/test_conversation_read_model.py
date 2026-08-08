from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_read_model import (
    _Accumulator,
    _prompt_projection_value,
    conversation_prompt_rows_statement,
    conversation_read_rows_statement,
)


def _row(
    line_number: int,
    *,
    role: str,
    content: str,
    metadata: dict | None = None,
    timestamp: datetime | None = None,
):
    return SimpleNamespace(
        id=line_number,
        document_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        line_number=line_number,
        role=role,
        message_type=role,
        content=content,
        metadata_=metadata or {},
        timestamp=timestamp,
    )


def test_incremental_projection_query_is_high_water_bounded() -> None:
    document_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    statement = conversation_read_rows_statement(
        document_id,
        after_line=40_000,
        dirty_line_numbers=[39_998],
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled).upper()

    assert "CONVERSATION_MESSAGES.LINE_NUMBER >" in sql
    assert "CONVERSATION_MESSAGES.LINE_NUMBER IN" in sql
    assert "ORDER BY CONVERSATION_MESSAGES.LINE_NUMBER" in sql
    assert "COUNT(" not in sql
    assert "OFFSET" not in sql
    assert "JSONB_EXTRACT_PATH_TEXT" not in sql


def test_prompt_projection_query_is_keyset_bounded() -> None:
    statement = conversation_prompt_rows_statement(
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        after_line=40_000,
    )
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "CONVERSATION_PROMPT_PROJECTIONS.LINE_NUMBER >" in sql
    assert "ORDER BY CONVERSATION_PROMPT_PROJECTIONS.LINE_NUMBER" in sql
    assert "COUNT(" not in sql
    assert "OFFSET" not in sql
    assert "JSONB" not in sql


def test_accumulator_materializes_prompts_and_interaction_resolution() -> None:
    now = datetime(2026, 8, 7, 16, tzinfo=UTC)
    interaction = {
        "id": "question-1",
        "kind": "question",
        "source": "codex",
        "tool_name": "request_user_input",
        "questions": [],
    }
    accumulator = _Accumulator()
    accumulator.observe(
        _row(
            1,
            role="assistant",
            content="Choose",
            metadata={"interaction": interaction},
            timestamp=now,
        )
    )
    accumulator.observe(
        _row(
            2,
            role="user",
            content="Use the first option.",
            timestamp=now + timedelta(seconds=1),
        )
    )
    values = accumulator.values()
    prompt = _prompt_projection_value(
        _row(
            2,
            role="user",
            content="Use the first option.",
            timestamp=now + timedelta(seconds=1),
        )
    )

    assert values["pending_interactions"] == []
    assert prompt is not None
    assert prompt["line_number"] == 2
    assert (
        values["inferred_responses"][0]["response"]["interaction_id"]
        == "question-1"
    )
    assert values["latest_human_at"].startswith("2026-08-07T16:00:01")


def test_accumulator_materializes_shell_and_agent_state() -> None:
    now = datetime.now(UTC)
    accumulator = _Accumulator()
    accumulator.observe(
        _row(
            10,
            role="tool",
            content="run",
            metadata={
                "tool_call_id": "shell-1",
                "tool_name": "exec_command",
                "tool_input": '{"command":"pytest -q"}',
                "agent_event": {
                    "kind": "started",
                    "activity_type": "subagent",
                    "agent_thread_id": "agent-1",
                    "label": "Review",
                },
            },
            timestamp=now,
        )
    )
    values = accumulator.values()
    assert values["live_activities"][0]["command"] == "pytest -q"
    assert values["agent_events"][0]["event"]["agent_thread_id"] == "agent-1"

    accumulator.observe(
        _row(
            11,
            role="tool",
            content="done",
            metadata={"tool_call_id": "shell-1", "tool_status": "completed"},
            timestamp=now + timedelta(seconds=1),
        )
    )
    assert accumulator.values()["live_activities"] == []
