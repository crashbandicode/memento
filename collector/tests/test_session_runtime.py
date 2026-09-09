from __future__ import annotations

import json
from datetime import UTC, datetime

import collector.session_runtime as runtime


def test_hook_records_forward_only_lifecycle_and_privilege(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(runtime, "execution_privilege", lambda: "administrator")
    timestamps = iter(
        [
            datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            datetime(2026, 9, 8, 12, 5, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(runtime, "_utc_now", lambda: next(timestamps))

    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    start = runtime.record_hook_observation(
        {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "transcript_path": f"C:/Users/intpa/.codex/sessions/{session_id}.jsonl",
        }
    )
    end = runtime.record_hook_observation(
        {
            "hook_event_name": "SessionEnd",
            "session_id": session_id,
            "transcript_path": f"C:/Users/intpa/.codex/sessions/{session_id}.jsonl",
        }
    )

    assert start is True
    assert end is True
    marker = json.loads((tmp_path / "codex" / f"{session_id}.json").read_text())
    assert marker == {
        "metadata_type": "conversation_runtime",
        "tool": "codex",
        "session_id": session_id,
        "runtime_state": "ended",
        "runtime_privilege": "administrator",
        "timestamp": "2026-09-08T12:05:00Z",
    }


def test_cursor_hook_identity_and_poller_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(runtime, "execution_privilege", lambda: "standard")
    monkeypatch.setattr(
        runtime,
        "_utc_now",
        lambda: datetime(2026, 9, 8, 13, 0, tzinfo=UTC),
    )
    session_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    assert runtime.record_hook_observation(
        {
            "hook_event_name": "postToolUse",
            "conversation_id": session_id,
        }
    )

    poller = runtime.SessionRuntimePoller(directory=tmp_path)
    assert poller.needs_poll() is True
    records = poller.poll()
    assert poller.needs_poll() is False
    assert records == {
        "cursor": {
            f"cursor:{session_id}": {
                "metadata_type": "conversation_runtime",
                "tool": "cursor",
                "session_id": session_id,
                "runtime_state": "running",
                "runtime_privilege": "standard",
                "timestamp": "2026-09-08T13:00:00Z",
            }
        }
    }


def test_claude_session_end_emits_background_retire_signal(
    tmp_path,
    monkeypatch,
) -> None:
    """SessionEnd is the deterministic signal that retires background cards.

    A launched-and-forgotten ``run_in_background`` shell keeps its projected
    card 'running' with no completion event. The collector already reports the
    owning Claude session's lifecycle here: a still-open session records
    'running' (never a retire signal), and SessionEnd records 'ended', which
    the server maps to retiring that session's still-running background cards.
    """
    monkeypatch.setattr(runtime, "_runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(runtime, "execution_privilege", lambda: "standard")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    timestamps = iter(
        [
            datetime(2026, 9, 9, 11, 0, tzinfo=UTC),
            datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(runtime, "_utc_now", lambda: next(timestamps))

    session_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    transcript = f"C:/Users/intpa/.claude/projects/proj/{session_id}.jsonl"
    marker_path = tmp_path / "claude_code" / f"{session_id}.json"

    # A still-open session (PostToolUse fires when the background Bash call
    # returns) is 'running' -- it must NOT emit the retire signal.
    assert runtime.record_hook_observation(
        {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": transcript,
            "tool_name": "Bash",
        }
    )
    assert json.loads(marker_path.read_text())["runtime_state"] == "running"

    # SessionEnd flips the same marker to 'ended' -- the retire trigger.
    assert runtime.record_hook_observation(
        {
            "hook_event_name": "SessionEnd",
            "session_id": session_id,
            "transcript_path": transcript,
        }
    )
    ended = json.loads(marker_path.read_text())
    assert ended["tool"] == "claude_code"
    assert ended["runtime_state"] == "ended"
    assert ended["timestamp"] == "2026-09-09T12:00:00Z"


def test_execution_privilege_is_platform_specific(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_platform_name", lambda: "nt")
    monkeypatch.setattr(runtime, "_windows_process_is_elevated", lambda: True)
    assert runtime.execution_privilege() == "administrator"

    monkeypatch.setattr(runtime, "_platform_name", lambda: "posix")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0, raising=False)
    assert runtime.execution_privilege() == "root"
