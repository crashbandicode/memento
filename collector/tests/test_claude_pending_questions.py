from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from collector import claude_pending_questions as pending_module
from collector.claude_pending_questions import (
    extract_claude_pending_interaction_updates,
)


@pytest.fixture
def pending_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    directory = tmp_path / "pending"
    directory.mkdir()
    monkeypatch.setattr(pending_module, "_pending_directory", lambda: directory)
    return directory


def _write_side_file(
    directory: Path,
    *,
    session_id: str,
    transcript_path: Path | None,
    status: str,
) -> None:
    (directory / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path or ""),
                "interaction_id": "toolu-question",
                "question_tool": "AskUserQuestion",
                "interaction_input": {
                    "questions": [{"question": "Continue?", "header": "Next"}]
                },
                "interaction_status": status,
                "timestamp": "2026-07-29T14:00:00Z",
                "cwd": "/work/demo",
            }
        ),
        encoding="utf-8",
    )


def test_pending_side_file_emits_interaction_update(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo-project" / "session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    _write_side_file(
        pending_directory,
        session_id="session-1",
        transcript_path=transcript,
        status="pending",
    )

    records = extract_claude_pending_interaction_updates(claude_root)

    key = "claude_code:projects/demo-project/session-1.jsonl:toolu-question"
    assert set(records) == {key}
    assert records[key] == {
        "metadata_type": "conversation_interaction",
        "tool": "claude_code",
        "relative_path": "projects/demo-project/session-1.jsonl",
        "interaction_id": "toolu-question",
        "interaction_status": "pending",
        "question_tool": "AskUserQuestion",
        "interaction_input": {
            "questions": [{"question": "Continue?", "header": "Next"}]
        },
        "timestamp": "2026-07-29T14:00:00Z",
    }


def test_answered_side_file_emits_answered_update(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo-project" / "session-2.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    _write_side_file(
        pending_directory,
        session_id="session-2",
        transcript_path=transcript,
        status="answered",
    )

    record = next(
        iter(extract_claude_pending_interaction_updates(claude_root).values())
    )

    assert record["interaction_status"] == "answered"


def test_unresolvable_session_is_skipped(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    (claude_root / "projects").mkdir(parents=True)
    _write_side_file(
        pending_directory,
        session_id="missing-session",
        transcript_path=None,
        status="pending",
    )

    assert extract_claude_pending_interaction_updates(claude_root) == {}


def test_hook_writes_pending_side_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-hook",
        "transcript_path": "/home/test/.claude/projects/demo/session-hook.jsonl",
        "tool_name": "askuserquestion",
        "tool_use_id": "toolu-hook",
        "tool_input": {"questions": [{"question": "Pick one?"}]},
        "cwd": "/home/test/project",
    }
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "collector.claude_pending_hook"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    side_record = json.loads(
        (home / ".memento" / "claude-pending" / "session-hook.json").read_text(
            encoding="utf-8"
        )
    )
    assert side_record["interaction_id"] == "toolu-hook"
    assert side_record["interaction_status"] == "pending"
    assert side_record["interaction_input"] == payload["tool_input"]


def test_installer_preserves_settings_and_is_idempotent(tmp_path: Path) -> None:
    config_directory = tmp_path / ".claude"
    config_directory.mkdir()
    settings_path = config_directory / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model": "claude-opus",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "existing-hook"}],
                        },
                        {
                            "matcher": "AskUserQuestion",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "python "
                                        "C:\\Users\\test\\Downloads\\"
                                        "pending_question_hook.py"
                                    ),
                                }
                            ],
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["CLAUDE_CONFIG_DIR"] = str(config_directory)
    environment["HOME"] = str(tmp_path)
    environment["USERPROFILE"] = str(tmp_path)

    first = subprocess.run(
        [sys.executable, "-m", "collector.claude_pending_hook", "--install"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=10,
    )
    second = subprocess.run(
        [sys.executable, "-m", "collector.claude_pending_hook", "--install"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=10,
    )

    assert first.returncode == 0
    assert second.returncode == 0
    assert "Already configured" in second.stdout
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "claude-opus"
    for event_name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        memento_entries = [
            entry
            for entry in settings["hooks"][event_name]
            if entry.get("matcher") == "AskUserQuestion"
        ]
        assert len(memento_entries) == 1
        assert len(memento_entries[0]["hooks"]) == 1
        command = memento_entries[0]["hooks"][0]["command"]
        assert "-m collector.claude_pending_hook" in command
        assert "Downloads" not in command
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
