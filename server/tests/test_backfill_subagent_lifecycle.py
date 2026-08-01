from __future__ import annotations

import uuid
from dataclasses import replace

from server.scripts.backfill_subagent_lifecycle import (
    LifecycleRow,
    plan_lifecycle_repairs,
)
from server.services.subagent_lifecycle import subagent_runtime_from_metadata


DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
MACHINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000202")


def _row(
    row_id: int,
    *,
    tool_use_id: str,
    label: str,
    thread_id: str | None = None,
    status: str = "running",
) -> LifecycleRow:
    event = {
        "version": 2,
        "source": "claude_agent",
        "activity_type": "subagent",
        "task_kind": "subagent",
        "task_id": tool_use_id,
        "agent_tool_use_id": tool_use_id,
        "kind": "started",
        "status": status,
        "label": label,
        "started_at": f"2026-08-01T13:42:2{row_id}Z",
    }
    if thread_id:
        event["agent_thread_id"] = thread_id
    return LifecycleRow(
        id=row_id,
        document_id=DOCUMENT_ID,
        machine_id=MACHINE_ID,
        tool_id="claude_code",
        line_number=row_id,
        content=f"{label} started",
        metadata={"source_id": f"source-{row_id}", "agent_event": event},
    )


def test_duplicate_source_event_coalesces_and_recovers_actual_runtime() -> None:
    rows = [
        _row(
            1,
            tool_use_id="toolu-stale",
            label="Tune stale threshold to CLEAN_PERIOD",
        ),
        _row(
            2,
            tool_use_id="toolu-stale",
            label="Tune stale threshold to CLEAN_PERIOD",
            thread_id="aa53b331b57f1bde5",
            status="async_launched",
        ),
    ]
    runtime = {
        (MACHINE_ID, "claude_code", "toolu-stale"): {
            "model": "claude-opus-4-8",
            "model_family": "anthropic",
            "reasoning_effort": "xhigh",
        }
    }

    updates, deletes, stats = plan_lifecycle_repairs(
        rows,
        runtime_by_tool_use=runtime,
        runtime_by_thread={},
    )

    assert deletes == [2]
    assert len(updates) == 1
    event = updates[0].metadata["agent_event"]
    assert event["agent_thread_id"] == "aa53b331b57f1bde5"
    assert event["started_at"] == "2026-08-01T13:42:21Z"
    assert event["status"] == "async_launched"
    assert event["model"] == "claude-opus-4-8"
    assert event["reasoning_effort"] == "xhigh"
    assert stats.duplicate_events_coalesced == 1
    assert stats.model_recovered == 1
    assert stats.effort_recovered == 1


def test_distinct_agents_with_same_description_are_preserved() -> None:
    rows = [
        _row(1, tool_use_id="toolu-one", label="Same description"),
        _row(2, tool_use_id="toolu-two", label="Same description"),
    ]

    updates, deletes, stats = plan_lifecycle_repairs(
        rows,
        runtime_by_tool_use={},
        runtime_by_thread={},
    )

    assert updates == []
    assert deletes == []
    assert stats.distinct_same_description_agents_preserved == 2
    assert stats.missing_model_metadata == 2


def test_lifecycle_backfill_plan_is_idempotent() -> None:
    original = _row(
        1,
        tool_use_id="toolu-one",
        label="Recover model",
        thread_id="agent-one",
    )
    runtime = {
        (MACHINE_ID, "claude_code", "toolu-one"): {
            "model": "claude-opus-4-8",
            "model_family": "anthropic",
            "reasoning_effort": "xhigh",
        }
    }
    updates, deletes, _ = plan_lifecycle_repairs(
        [original],
        runtime_by_tool_use=runtime,
        runtime_by_thread={},
    )
    assert len(updates) == 1
    assert deletes == []
    repaired = replace(
        original,
        metadata=updates[0].metadata,
        content=updates[0].content,
    )

    second_updates, second_deletes, second_stats = plan_lifecycle_repairs(
        [repaired],
        runtime_by_tool_use=runtime,
        runtime_by_thread={},
    )

    assert second_updates == []
    assert second_deletes == []
    assert second_stats.model_recovered == 0
    assert second_stats.effort_recovered == 0


def test_child_metadata_runtime_uses_authoritative_parser_state() -> None:
    assert subagent_runtime_from_metadata({
        "_assistant_model": "claude-opus-4-8",
        "_assistant_reasoning_effort": "xhigh",
    }) == {
        "model": "claude-opus-4-8",
        "model_family": "anthropic",
        "reasoning_effort": "xhigh",
    }
    assert subagent_runtime_from_metadata({}) == {}
