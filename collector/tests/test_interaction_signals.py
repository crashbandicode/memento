from __future__ import annotations

import json
from pathlib import Path

from collector.interaction_signals import (
    extract_content_activity_updates,
    extract_conversation_activity_updates,
    extract_conversation_interaction_updates,
)


def _write(path: Path, *records: dict) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def test_claude_question_is_emitted_before_canonical_delta(tmp_path: Path) -> None:
    path = tmp_path / "thread.jsonl"
    question = {
        "type": "assistant",
        "timestamp": "2026-07-24T23:04:05Z",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-question",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [{"question": "Continue?", "header": "Next"}]
                    },
                }
            ]
        },
    }
    _write(path, question)

    records = extract_conversation_interaction_updates(
        path,
        tool_name="claude_code",
        relative_path="projects/thread.jsonl",
    )

    signal = next(iter(records.values()))
    assert signal["interaction_id"] == "toolu-question"
    assert signal["interaction_status"] == "pending"

    answer = {
        "type": "user",
        "timestamp": "2026-07-24T23:07:30Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-question",
                    "content": "The user answered: Continue.",
                }
            ]
        },
    }
    _write(path, question, answer)

    records = extract_conversation_interaction_updates(
        path,
        tool_name="claude_code",
        relative_path="projects/thread.jsonl",
    )
    assert next(iter(records.values()))["interaction_status"] == "answered"


def test_codex_and_cursor_question_resolution(tmp_path: Path) -> None:
    codex = tmp_path / "codex.jsonl"
    _write(
        codex,
        {
            "timestamp": "2026-07-24T20:00:00Z",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "call-1",
                "arguments": json.dumps(
                    {"questions": [{"id": "ship", "question": "Ship it?"}]}
                ),
            },
        },
        {
            "timestamp": "2026-07-24T20:00:02Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"answers":{"ship":"yes"}}',
            },
        },
    )
    codex_signal = next(
        iter(
            extract_conversation_interaction_updates(
                codex,
                tool_name="codex",
                relative_path="sessions/thread.jsonl",
            ).values()
        )
    )
    assert codex_signal["interaction_status"] == "answered"

    cursor = tmp_path / "cursor.jsonl"
    _write(
        cursor,
        {
            "role": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "cursor-question",
                        "name": "AskQuestion",
                        "input": {
                            "questions": [{"id": "scope", "prompt": "Which scope?"}]
                        },
                    }
                ]
            },
        },
        {
            "role": "user",
            "message": {"content": [{"type": "text", "text": "The API"}]},
        },
    )
    cursor_signal = next(
        iter(
            extract_conversation_interaction_updates(
                cursor,
                tool_name="cursor",
                relative_path="projects/thread.jsonl",
            ).values()
        )
    )
    assert cursor_signal["interaction_status"] == "answered"


def test_cursor_state_plan_mode_request_tracks_native_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cursor-state.jsonl"
    base = {
        "type": "cursor_state_tool",
        "role": "tool",
        "id": "plan-1:tool",
        "tool_name": "switch_mode",
        "tool_call_id": "call-plan-1",
        "tool_input": json.dumps({
            "fromModeId": "agent",
            "toModeId": "plan",
            "explanation": "Confirm the design first.",
        }),
    }

    for status, expected in (
        ("loading", "pending"),
        ("cancelled", "cancelled"),
        ("completed", "answered"),
    ):
        _write(
            path,
            {
                **base,
                "tool_status": status,
                "content": f"Status: {status}",
            },
        )
        signal = next(iter(extract_conversation_interaction_updates(
            path,
            tool_name="cursor",
            relative_path="projects/thread.jsonl",
        ).values()))
        assert signal["interaction_status"] == expected
        assert signal["interaction_input"]["toModeId"] == "plan"


def test_cursor_compat_plan_mode_request_and_non_plan_false_positive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cursor.jsonl"
    request = {
        "role": "assistant",
        "message": {
            "content": [{
                "type": "toolCall",
                "call_id": "call-plan-1",
                "name": "switch_mode",
                "arguments": {
                    "fromModeId": "agent",
                    "toModeId": "plan",
                    "explanation": "Confirm the design first.",
                },
            }],
        },
    }
    _write(path, request)
    signal = next(iter(extract_conversation_interaction_updates(
        path,
        tool_name="cursor",
        relative_path="projects/thread.jsonl",
    ).values()))
    assert signal["interaction_status"] == "pending"

    _write(
        path,
        request,
        {
            "role": "assistant",
            "message": {
                "content": [{
                    "type": "toolResult",
                    "call_id": "call-plan-1",
                    "output": "{}",
                }],
            },
        },
    )
    signal = next(iter(extract_conversation_interaction_updates(
        path,
        tool_name="cursor",
        relative_path="projects/thread.jsonl",
    ).values()))
    assert signal["interaction_status"] == "answered"

    not_plan = {
        **request,
        "message": {
            "content": [{
                **request["message"]["content"][0],
                "arguments": {
                    "fromModeId": "plan",
                    "toModeId": "agent",
                },
            }],
        },
    }
    _write(path, not_plan)
    assert extract_conversation_interaction_updates(
        path,
        tool_name="cursor",
        relative_path="projects/thread.jsonl",
    ) == {}


def test_shell_activity_tracks_codex_call_until_output(tmp_path: Path) -> None:
    path = tmp_path / "codex-shell.jsonl"
    call = {
        "type": "response_item",
        "timestamp": "2026-08-04T17:00:00Z",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "call_id": "call-shell-1",
            "arguments": '{"command":"python -m pytest -q"}',
        },
    }
    _write(path, call)

    running = extract_conversation_activity_updates(
        path,
        tool_name="codex",
        relative_path="sessions/thread.jsonl",
    )
    signal = next(iter(running.values()))
    assert signal["activity_id"] == "call-shell-1"
    assert signal["activity_status"] == "running"
    assert signal["command"] == "python -m pytest -q"

    output = {
        "type": "response_item",
        "timestamp": "2026-08-04T17:00:05Z",
        "payload": {
            "type": "function_call_output",
            "call_id": "call-shell-1",
            "output": "12 passed",
        },
    }
    _write(path, call, output)
    completed = extract_conversation_activity_updates(
        path,
        tool_name="codex",
        relative_path="sessions/thread.jsonl",
    )
    assert next(iter(completed.values()))["activity_status"] == "completed"


def test_claude_background_shell_activity_preserves_background_flag(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claude-shell.jsonl"
    _write(path, {
        "type": "assistant",
        "timestamp": "2026-08-28T17:00:00Z",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu-background",
                "name": "Bash",
                "input": {
                    "command": "pytest -q",
                    "run_in_background": True,
                },
            }],
        },
    })

    records = extract_conversation_activity_updates(
        path,
        tool_name="claude_code",
        relative_path="projects/demo/thread.jsonl",
    )

    signal = next(iter(records.values()))
    assert signal["activity_status"] == "running"
    assert signal["is_background"] is True


def test_cursor_state_shell_activity_uses_native_status() -> None:
    content = json.dumps({
        "type": "cursor_state_tool",
        "timestamp": "2026-08-04T17:01:00Z",
        "tool_name": "PowerShell",
        "tool_input": '{"command":"Start-Sleep -Seconds 30"}',
        "tool_call_id": "cursor-shell-1",
        "tool_status": "running",
    })

    records = extract_content_activity_updates(
        content,
        tool_name="cursor",
        relative_path="projects/demo/thread.jsonl",
    )
    signal = next(iter(records.values()))
    assert signal["activity_status"] == "running"
    assert signal["command"] == "Start-Sleep -Seconds 30"
