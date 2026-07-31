from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from collector.claude_pending_questions import (
    extract_claude_pending_interaction_updates,
)

from collector import claude_pending_questions as pending_module


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
    question_tool: str = "AskUserQuestion",
    interaction_input: dict | None = None,
    timestamp: str = "2026-07-29T14:00:00Z",
) -> None:
    (directory / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path or ""),
                "interaction_id": "toolu-question",
                "question_tool": question_tool,
                "interaction_input": interaction_input or {
                    "questions": [{"question": "Continue?", "header": "Next"}]
                },
                "interaction_status": status,
                "timestamp": timestamp,
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


def test_permission_side_file_stays_pending_while_session_waits(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo-project" / "session-perm.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    sessions = claude_root / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(
        json.dumps({
            "sessionId": "session-perm",
            "status": "waiting",
            "waitingFor": "permission prompt",
            "updatedAt": 1785333660000,
        }),
        encoding="utf-8",
    )
    _write_side_file(
        pending_directory,
        session_id="session-perm",
        transcript_path=transcript,
        status="pending",
        question_tool="PermissionRequest",
        interaction_input={
            "interaction_type": "permission_request",
            "requested_tool": "PowerShell",
            "tool_input": {"command": "git push origin main"},
        },
        timestamp="2026-07-29T14:00:00Z",
    )

    record = next(
        iter(extract_claude_pending_interaction_updates(claude_root).values())
    )

    assert record["interaction_status"] == "pending"
    assert record["question_tool"] == "PermissionRequest"
    assert record["interaction_input"]["tool_input"]["command"] == (
        "git push origin main"
    )


def test_permission_side_file_closes_after_session_leaves_wait(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo-project" / "session-done.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    sessions = claude_root / "sessions"
    sessions.mkdir()
    (sessions / "43.json").write_text(
        json.dumps({
            "sessionId": "session-done",
            "status": "busy",
            "updatedAt": 1785333660000,
        }),
        encoding="utf-8",
    )
    _write_side_file(
        pending_directory,
        session_id="session-done",
        transcript_path=transcript,
        status="pending",
        question_tool="PermissionRequest",
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


def test_permission_wrapper_does_not_replace_ask_user_question(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    question_payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-question-permission",
        "transcript_path": (
            "/home/test/.claude/projects/demo/"
            "session-question-permission.jsonl"
        ),
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu-real-question",
        "tool_input": {
            "questions": [{
                "question": "How should I proceed?",
                "header": "Next step",
                "options": [
                    {"label": "Hold"},
                    {"label": "Continue"},
                ],
            }]
        },
    }
    permission_wrapper = {
        "hook_event_name": "PermissionRequest",
        "session_id": question_payload["session_id"],
        "transcript_path": question_payload["transcript_path"],
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [{
                **question_payload["tool_input"]["questions"][0],
                "options": [
                    {
                        "label": "Hold",
                        "description": "Pause at the current step.",
                    },
                    {
                        "label": "Continue",
                        "description": "Proceed with the rollout.",
                    },
                ],
            }]
        },
        "permission_mode": "acceptEdits",
    }
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)

    for payload in (question_payload, permission_wrapper):
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
        (
            home
            / ".memento"
            / "claude-pending"
            / "session-question-permission.json"
        ).read_text(encoding="utf-8")
    )
    assert side_record["interaction_id"] == "toolu-real-question"
    assert side_record["question_tool"] == "AskUserQuestion"
    assert side_record["interaction_status"] == "pending"
    assert side_record["interaction_input"] == permission_wrapper["tool_input"]


def test_permission_wrapper_before_question_keeps_one_stable_id_through_answer(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    question_input = {
        "questions": [{
            "question": "How should I roll the remaining schedulers?",
            "header": "Fleet approach",
            "multiSelect": False,
            "options": [
                {
                    "label": "Fresh-venv sweep",
                    "description": "Recreate every environment in waves.",
                },
                {
                    "label": "Fix fleet_deploy first",
                    "description": "Add the reusable fresh-venv path first.",
                },
            ],
        }]
    }
    common = {
        "session_id": "session-wrapper-first",
        "transcript_path": (
            "/home/test/.claude/projects/demo/session-wrapper-first.jsonl"
        ),
        "tool_name": "AskUserQuestion",
        "tool_input": question_input,
    }
    payloads = [
        {"hook_event_name": "PermissionRequest", **common},
        {
            "hook_event_name": "PreToolUse",
            "tool_use_id": "toolu-wrapper-first",
            **common,
        },
        {
            "hook_event_name": "PostToolUse",
            "tool_use_id": "toolu-wrapper-first",
            **common,
        },
    ]
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    side_path = (
        home / ".memento" / "claude-pending" / "session-wrapper-first.json"
    )

    stable_id = ""
    for index, payload in enumerate(payloads):
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
        side_record = json.loads(side_path.read_text(encoding="utf-8"))
        if index == 0:
            stable_id = side_record["interaction_id"]
            assert stable_id.startswith("memento-question-")
        assert side_record["interaction_id"] == stable_id

    assert side_record["question_tool"] == "AskUserQuestion"
    assert side_record["interaction_status"] == "answered"
    assert side_record["interaction_alias_ids"] == ["toolu-wrapper-first"]
    assert side_record["interaction_input"] == question_input


def test_valid_permission_wrapper_alone_preserves_exact_question_input(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    question_input = {
        "questions": [{
            "id": "fleet",
            "prompt": "Choose a rollout.",
            "header": "Fleet approach",
            "allowMultiple": True,
            "options": [{
                "id": "wave",
                "label": "Wave rollout",
                "label_short": "Waves",
                "description": "Verify each wave.",
                "preview": "Do not replace the description.",
            }],
        }]
    }
    payload = {
        "hook_event_name": "PermissionRequest",
        "session_id": "session-wrapper-only",
        "transcript_path": (
            "/home/test/.claude/projects/demo/session-wrapper-only.jsonl"
        ),
        "tool_name": "ASK_User-Question",
        "tool_input": question_input,
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
    side_record = json.loads(
        (
            home
            / ".memento"
            / "claude-pending"
            / "session-wrapper-only.json"
        ).read_text(encoding="utf-8")
    )
    assert side_record["question_tool"] == "AskUserQuestion"
    assert side_record["interaction_status"] == "pending"
    assert side_record["interaction_input"] == question_input


def test_malformed_nested_ask_user_permission_wrapper_is_ignored(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "hook_event_name": "PermissionRequest",
        "session_id": "session-malformed-wrapper",
        "transcript_path": (
            "/home/test/.claude/projects/demo/session-malformed-wrapper.jsonl"
        ),
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "tool_input": {
                "questions": [{"question": "Do not trust nested input."}]
            }
        },
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
    assert not (
        home
        / ".memento"
        / "claude-pending"
        / "session-malformed-wrapper.json"
    ).exists()


def test_hook_writes_permission_request_with_stable_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "hook_event_name": "PermissionRequest",
        "session_id": "session-permission",
        "transcript_path": (
            "/home/test/.claude/projects/demo/session-permission.jsonl"
        ),
        "tool_name": "PowerShell",
        "tool_input": {
            "command": "git push fork main",
            "description": "Push the release",
        },
        "permission_mode": "default",
        "permission_suggestions": [{
            "type": "addRules",
            "rules": [{
                "toolName": "PowerShell",
                "ruleContent": "git push *",
            }],
            "behavior": "allow",
            "destination": "session",
        }],
        "cwd": "/home/test/project",
    }
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)

    first = subprocess.run(
        [sys.executable, "-m", "collector.claude_pending_hook"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=10,
    )
    second = subprocess.run(
        [sys.executable, "-m", "collector.claude_pending_hook"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=10,
    )

    assert first.returncode == 0
    assert second.returncode == 0
    side_record = json.loads(
        (
            home
            / ".memento"
            / "claude-pending"
            / "session-permission.json"
        ).read_text(encoding="utf-8")
    )
    assert side_record["interaction_id"].startswith("memento-permission-")
    assert side_record["question_tool"] == "PermissionRequest"
    assert side_record["interaction_status"] == "pending"
    assert side_record["interaction_input"]["requested_tool"] == "PowerShell"
    assert side_record["interaction_input"]["tool_input"]["command"] == (
        "git push fork main"
    )
    assert side_record["interaction_input"]["permission_suggestions"] == (
        payload["permission_suggestions"]
    )


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
    assert settings["hooks"]["PermissionRequest"][0]["matcher"] == ".*"
    assert settings["hooks"]["Elicitation"][0]["matcher"] == ".*"
    assert settings["hooks"]["ElicitationResult"][0]["matcher"] == ".*"
    assert {
        entry["matcher"] for entry in settings["hooks"]["Notification"]
    } == {"agent_needs_input", "agent_completed"}
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
