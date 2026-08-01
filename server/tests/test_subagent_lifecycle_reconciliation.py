from __future__ import annotations

import json

import pytest

from server.services.conversation_hierarchy import merge_subagent_event_summaries
from server.services.subagent_lifecycle import (
    child_lifecycle_evidence,
    enrich_lifecycle_status,
    persisted_child_lifecycle,
    reconcile_child_lifecycle_metadata,
)


def _claude_assistant(stop_reason: str | None, timestamp: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": "Result"}],
        },
    })


def test_async_child_remains_running_without_terminal_marker() -> None:
    evidence = child_lifecycle_evidence(
        "claude_code",
        {"is_subagent": True},
        _claude_assistant(None, "2026-08-01T12:00:00Z"),
    )
    summaries = merge_subagent_event_summaries([], [{
        "activity_type": "subagent",
        "kind": "started",
        "agent_tool_use_id": "toolu-live",
        "agent_thread_id": "agent-live",
        "timestamp": "2026-08-01T11:59:00Z",
    }])

    assert evidence is None
    assert summaries[0]["status"] == "running"
    assert summaries[0]["completed_at"] is None


def test_explicit_claude_child_completion_resolves_one_card() -> None:
    evidence = child_lifecycle_evidence(
        "claude_code",
        {"is_subagent": True},
        _claude_assistant("end_turn", "2026-08-01T12:05:00Z"),
    )
    assert evidence == {
        "status": "completed",
        "source": "claude_child_transcript",
        "timestamp": "2026-08-01T12:05:00Z",
        "evidence": "assistant.stop_reason=end_turn",
    }

    event = enrich_lifecycle_status({
        "activity_type": "subagent",
        "kind": "started",
        "status": "async_launched",
        "agent_tool_use_id": "toolu-complete",
        "agent_thread_id": "agent-complete",
        "started_at": "2026-08-01T12:00:00Z",
    }, evidence)
    summaries = merge_subagent_event_summaries([], [event])

    assert summaries[0]["status"] == "completed"
    assert summaries[0]["completed_at"] == "2026-08-01T12:05:00Z"


@pytest.mark.parametrize(
    ("tool_id", "metadata", "content", "expected"),
    [
        ("cursor", {"composer_status": "failed"}, "", "failed"),
        ("cursor", {"composer_status": "cancelled"}, "", "cancelled"),
        (
            "codex",
            {},
            json.dumps({
                "type": "event_msg",
                "timestamp": "2026-08-01T12:00:00Z",
                "payload": {"type": "turn_aborted", "reason": "superseded"},
            }),
            "interrupted",
        ),
    ],
)
def test_failed_cancelled_and_interrupted_sources_remain_distinct(
    tool_id: str,
    metadata: dict,
    content: str,
    expected: str,
) -> None:
    evidence = child_lifecycle_evidence(tool_id, metadata, content)
    assert evidence is not None
    assert evidence["status"] == expected


def test_terminal_metadata_is_sticky_across_late_running_reparse() -> None:
    completed, changed = reconcile_child_lifecycle_metadata({}, {
        "status": "completed",
        "source": "claude_child_transcript",
        "timestamp": "2026-08-01T12:05:00Z",
    })
    assert changed is True

    reparsed, changed = reconcile_child_lifecycle_metadata(completed, {
        "status": "running",
        "source": "cursor_composer_state",
        "timestamp": "2026-08-01T12:06:00Z",
    })
    assert changed is False
    assert persisted_child_lifecycle(reparsed)["status"] == "completed"

    summaries = merge_subagent_event_summaries(
        [{
            "id": "child",
            "session_id": "agent-sticky",
            "status": "completed",
            "completed_at": "2026-08-01T12:05:00Z",
        }],
        [{
            "kind": "started",
            "agent_thread_id": "agent-sticky",
            "timestamp": "2026-08-01T12:06:00Z",
        }],
    )
    assert summaries[0]["status"] == "completed"


def test_duplicate_started_plus_terminal_coalesces_to_one_summary() -> None:
    events = [
        {
            "kind": "started",
            "agent_tool_use_id": "toolu-one",
            "label": "Same task",
            "timestamp": "2026-08-01T12:00:00Z",
        },
        {
            "kind": "started",
            "agent_tool_use_id": "toolu-one",
            "agent_thread_id": "agent-one",
            "label": "Same task",
            "timestamp": "2026-08-01T12:00:01Z",
        },
        {
            "kind": "completed",
            "agent_tool_use_id": "toolu-one",
            "agent_thread_id": "agent-one",
            "label": "Same task",
            "timestamp": "2026-08-01T12:05:00Z",
        },
    ]
    summaries = merge_subagent_event_summaries([], events)

    assert len(summaries) == 1
    assert summaries[0]["status"] == "completed"


def test_missing_source_requires_authoritative_evidence_and_never_uses_age() -> None:
    stale_timestamp = "2020-01-01T00:00:00Z"
    assert child_lifecycle_evidence(
        "claude_code",
        {"is_subagent": True, "last_timestamp": stale_timestamp},
        _claude_assistant(None, stale_timestamp),
    ) is None
    assert child_lifecycle_evidence(
        "claude_code",
        {
            "is_subagent": True,
            "subagent_source_state": "missing",
            "subagent_source_state_authoritative": False,
        },
        "",
    ) is None

    missing = child_lifecycle_evidence(
        "claude_code",
        {
            "is_subagent": True,
            "subagent_source_state": "missing",
            "subagent_source_state_authoritative": True,
            "subagent_source_state_at": "2026-08-01T12:10:00Z",
        },
        "",
    )
    assert missing is not None
    assert missing["status"] == "disconnected"


def test_lifecycle_metadata_reconciliation_is_idempotent() -> None:
    evidence = {
        "status": "completed",
        "source": "claude_child_transcript",
        "timestamp": "2026-08-01T12:05:00Z",
        "evidence": "assistant.stop_reason=end_turn",
    }
    first, first_changed = reconcile_child_lifecycle_metadata({}, evidence)
    second, second_changed = reconcile_child_lifecycle_metadata(first, evidence)

    assert first_changed is True
    assert second_changed is False
    assert second == first
