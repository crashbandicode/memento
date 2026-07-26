from __future__ import annotations

import json
from pathlib import Path

from collector.interaction_signals import extract_conversation_interaction_updates


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
