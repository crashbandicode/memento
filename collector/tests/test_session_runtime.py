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


def test_execution_privilege_is_platform_specific(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_platform_name", lambda: "nt")
    monkeypatch.setattr(runtime, "_windows_process_is_elevated", lambda: True)
    assert runtime.execution_privilege() == "administrator"

    monkeypatch.setattr(runtime, "_platform_name", lambda: "posix")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0, raising=False)
    assert runtime.execution_privilege() == "root"
