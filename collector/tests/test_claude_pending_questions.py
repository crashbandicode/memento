from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from collector import claude_pending_hook as hook_module
from collector import claude_pending_questions as pending_module
from collector.claude_pending_questions import (
    ClaudePendingPoller,
    extract_claude_live_activity_updates,
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


def test_change_poller_does_not_reread_unchanged_side_file(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    poller = ClaudePendingPoller()

    interactions, activities = poller.poll(claude_root)
    assert len(interactions) == 1
    assert activities == {}
    assert poller.needs_poll(claude_root) is False

    with monkeypatch.context() as context:
        context.setattr(
            pending_module,
            "_read_side_file",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("unchanged side file was reread")
            ),
        )
        assert poller.poll(claude_root) == ({}, {})

    _write_side_file(
        pending_directory,
        session_id="session-1",
        transcript_path=transcript,
        status="answered",
    )
    assert poller.needs_poll(claude_root) is True
    interactions, _activities = poller.poll(claude_root)
    assert next(iter(interactions.values()))["interaction_status"] == "answered"


def test_utf8_question_survives_windows_hook_pipe_and_collector(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo-project" / "session-utf8.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-utf8",
        "transcript_path": str(transcript),
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu-side-tail",
        "tool_input": {
            "questions": [
                {
                    "header": "Side-tail",
                    "question": (
                        "Proceed to eliminate the accept/switch side-tail and "
                        "source forwarding from JOB_START?"
                    ),
                    "options": [
                        {"label": "Yes — delete it, verify on real data first"},
                        {"label": "Not yet — keep side-tail"},
                    ],
                },
                {
                    "header": "Queue freshness",
                    "question": (
                        "JOB_SWITCH queue freshness once the side-tail is gone?"
                    ),
                    "options": [
                        {"label": "Accept STATUS2-cadence queue"},
                        {"label": "Keep switch-instant precision"},
                    ],
                },
            ]
        },
    }
    raw_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    windows_ansi_stream = io.TextIOWrapper(
        io.BytesIO(raw_payload),
        encoding="cp1252",
    )

    decoded = hook_module._read_stdin_payload(windows_ansi_stream)
    hook_module.process_payload(decoded)
    records = extract_claude_pending_interaction_updates(claude_root)

    record = records[
        "claude_code:projects/demo-project/session-utf8.jsonl:toolu-side-tail"
    ]
    questions = record["interaction_input"]["questions"]
    assert questions[0]["options"][0]["label"] == (
        "Yes — delete it, verify on real data first"
    )
    assert questions[0]["options"][1]["label"] == "Not yet — keep side-tail"
    assert len(questions) == 2


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


def test_permission_closes_when_transcript_continues_after_stale_wait_state(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = (
        claude_root / "projects" / "demo-project" / "session-continued.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-29T14:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Continuing work."}],
            },
        }) + "\n",
        encoding="utf-8",
    )
    sessions = claude_root / "sessions"
    sessions.mkdir()
    (sessions / "44.json").write_text(
        json.dumps({
            "sessionId": "session-continued",
            "status": "waiting",
            "waitingFor": "permission prompt",
            "updatedAt": 1785333630000,
        }),
        encoding="utf-8",
    )
    _write_side_file(
        pending_directory,
        session_id="session-continued",
        transcript_path=transcript,
        status="pending",
        question_tool="PermissionRequest",
        timestamp="2026-07-29T14:00:00Z",
    )

    record = next(
        iter(extract_claude_pending_interaction_updates(claude_root).values())
    )

    assert record["interaction_status"] == "answered"


def test_new_question_closes_replaced_permission_without_session_state(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = (
        claude_root
        / "projects"
        / "demo-project"
        / "session-replaced-permission.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)

    hook_module.process_payload({
        "hook_event_name": "PermissionRequest",
        "session_id": "session-replaced-permission",
        "transcript_path": str(transcript),
        "tool_name": "PowerShell",
        "tool_input": {"command": "git push origin HEAD:main"},
        "timestamp": "2026-08-05T13:01:07Z",
    })
    side_path = pending_directory / "session-replaced-permission.json"
    permission_id = json.loads(
        side_path.read_text(encoding="utf-8")
    )["interaction_id"]

    hook_module.process_payload({
        "hook_event_name": "PreToolUse",
        "session_id": "session-replaced-permission",
        "transcript_path": str(transcript),
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu-next-question",
        "tool_input": {
            "questions": [{"question": "What should happen next?"}]
        },
        "timestamp": "2026-08-05T13:04:08Z",
    })

    side_record = json.loads(side_path.read_text(encoding="utf-8"))
    assert side_record["interaction_id"] == "toolu-next-question"
    assert side_record["interaction_status"] == "pending"
    assert side_record["resolved_interactions"] == [{
        "interaction_id": permission_id,
        "question_tool": "PermissionRequest",
        "interaction_input": {
            "interaction_type": "permission_request",
            "requested_tool": "PowerShell",
            "tool_input": {"command": "git push origin HEAD:main"},
            "permission_mode": None,
            "permission_suggestions": None,
        },
        "interaction_status": "answered",
        "timestamp": "2026-08-05T13:01:07Z",
        "resolved_at": "2026-08-05T13:04:08Z",
        "interaction_origin": {
            "version": 1,
            "kind": "hook_only",
            "record_uuid": "",
            "parent_uuid": "",
            "tool_use_id": "",
            "fingerprint": hook_module._permission_fingerprint(
                "PowerShell",
                {"command": "git push origin HEAD:main"},
            ),
            "agent_id": "",
            "is_sidechain": False,
        },
    }]

    records = extract_claude_pending_interaction_updates(claude_root)
    permission = records[
        "claude_code:projects/demo-project/"
        f"session-replaced-permission.jsonl:{permission_id}"
    ]
    question = records[
        "claude_code:projects/demo-project/"
        "session-replaced-permission.jsonl:toolu-next-question"
    ]
    assert permission["interaction_status"] == "answered"
    assert question["interaction_status"] == "pending"


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


def test_shell_hooks_emit_running_then_completed_activity(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo-project" / "session-shell.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    common = {
        "session_id": "session-shell",
        "transcript_path": str(transcript),
        "tool_name": "PowerShell",
        "tool_use_id": "toolu-shell-live",
        "tool_input": {"command": "Start-Sleep -Seconds 30"},
    }

    hook_module.process_payload({"hook_event_name": "PreToolUse", **common})
    running = extract_claude_live_activity_updates(claude_root)
    key = (
        "claude_code:projects/demo-project/session-shell.jsonl:"
        "toolu-shell-live"
    )
    assert running[key]["activity_status"] == "running"
    assert running[key]["activity_tool"] == "PowerShell"
    assert running[key]["command"] == "Start-Sleep -Seconds 30"

    hook_module.process_payload({"hook_event_name": "PostToolUse", **common})
    completed = extract_claude_live_activity_updates(claude_root)
    assert completed[key]["activity_status"] == "completed"
    assert completed[key]["command"] == "Start-Sleep -Seconds 30"


def test_shell_activity_does_not_refresh_pending_permission_timestamp(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = (
        claude_root / "projects" / "demo-project" / "session-permission-shell.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-08-05T14:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Permission resolved."}],
            },
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    common = {
        "session_id": "session-permission-shell",
        "transcript_path": str(transcript),
    }
    hook_module.process_payload({
        "hook_event_name": "PermissionRequest",
        **common,
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "timestamp": "2026-08-05T14:00:00Z",
    })
    hook_module.process_payload({
        "hook_event_name": "PreToolUse",
        **common,
        "tool_name": "PowerShell",
        "tool_use_id": "toolu-later-shell",
        "tool_input": {"command": "Get-Date"},
        "timestamp": "2026-08-05T14:02:00Z",
    })

    side_record = json.loads(
        (
            pending_directory / "session-permission-shell.json"
        ).read_text(encoding="utf-8")
    )
    assert side_record["timestamp"] == "2026-08-05T14:00:00Z"
    assert side_record["interaction_timestamp"] == "2026-08-05T14:00:00Z"
    interaction = next(
        iter(extract_claude_pending_interaction_updates(claude_root).values())
    )
    assert interaction["interaction_status"] == "answered"


def _claude_tool_record(
    *,
    uuid: str,
    parent_uuid: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict,
    **record_fields: object,
) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": tool_input,
            }],
        },
        **record_fields,
    }


def _permission_payload(
    *,
    session_id: str,
    transcript: Path,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
) -> dict:
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": session_id,
        "transcript_path": str(transcript),
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "git status"},
        "timestamp": "2026-08-14T12:00:00Z",
    }


def test_permission_fingerprint_v1_contract_fixture() -> None:
    assert hook_module._permission_fingerprint("Bash", {"command": "ls"}) == (
        "679eb83c897d20b481ff8e75961f8076d6721d232a7ad433cf4528bdeaf4099e"
    )


def test_permission_origin_resolves_exact_main_transcript_record(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-origin.jsonl"
    transcript.parent.mkdir(parents=True)
    requested_input = {"command": "git status", "timeout": 30}
    transcript.write_text(
        json.dumps(_claude_tool_record(
            uuid="record-main",
            parent_uuid="parent-main",
            tool_use_id="toolu-main",
            tool_name="Bash",
            tool_input={"timeout": 30, "command": "git status"},
        )) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-origin",
        transcript=transcript,
        tool_input=requested_input,
    ))

    side_record = json.loads(
        (pending_directory / "session-origin.json").read_text(encoding="utf-8")
    )
    origin = side_record["interaction_origin"]
    assert origin == {
        "version": 1,
        "kind": "claude_record",
        "record_uuid": "record-main",
        "parent_uuid": "parent-main",
        "tool_use_id": "toolu-main",
        "fingerprint": hook_module._permission_fingerprint("Bash", requested_input),
        "agent_id": "",
        "is_sidechain": False,
        "transcript_path": "projects/demo/session-origin.jsonl",
    }
    assert origin["fingerprint"] == hook_module._permission_fingerprint(
        "Bash",
        {"timeout": 30, "command": "git status"},
    )


def test_permission_origin_chooses_latest_exact_duplicate(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-latest.jsonl"
    transcript.parent.mkdir(parents=True)
    requested_input = {"command": "git status"}
    transcript.write_text(
        "\n".join(json.dumps(record) for record in [
            _claude_tool_record(
                uuid="record-earlier",
                parent_uuid="parent-1",
                tool_use_id="toolu-earlier",
                tool_name="Bash",
                tool_input=requested_input,
            ),
            _claude_tool_record(
                uuid="record-latest",
                parent_uuid="parent-2",
                tool_use_id="toolu-latest",
                tool_name="Bash",
                tool_input=requested_input,
            ),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-latest",
        transcript=transcript,
        tool_input=requested_input,
    ))

    origin = json.loads(
        (pending_directory / "session-latest.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert origin["record_uuid"] == "record-latest"
    assert origin["tool_use_id"] == "toolu-latest"


def test_permission_origin_never_fuzzy_matches_and_handles_large_malformed_tail(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-hook-only.jsonl"
    transcript.parent.mkdir(parents=True)
    requested_input = {"command": "git status"}
    # This prefix is larger than the resolver's tail and has no newline, so a
    # full-file scan would be observable while the bounded tail still finds the
    # valid final row after skipping the partial oversized line.
    transcript.write_bytes(
        b"x" * (hook_module._TRANSCRIPT_TAIL_BYTES + 1)
        + b"\nnot-json\n"
        + json.dumps(_claude_tool_record(
            uuid="record-different-input",
            parent_uuid="parent-different-input",
            tool_use_id="toolu-different-input",
            tool_name="Bash",
            tool_input={"command": "git status --short"},
        )).encode("utf-8")
        + b"\n"
    )
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-hook-only",
        transcript=transcript,
        tool_input=requested_input,
    ))

    origin = json.loads(
        (pending_directory / "session-hook-only.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert origin["kind"] == "hook_only"
    assert origin["record_uuid"] == ""
    assert origin["tool_use_id"] == ""
    assert origin["fingerprint"] == hook_module._permission_fingerprint(
        "Bash", requested_input
    )


def test_permission_origin_classifies_subagent_transcript(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = (
        claude_root
        / "projects"
        / "demo"
        / "session-parent"
        / "subagents"
        / "agent-42.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(_claude_tool_record(
            uuid="record-agent",
            parent_uuid="parent-agent",
            tool_use_id="toolu-agent",
            tool_name="PowerShell",
            tool_input={"command": "Get-Date"},
            isSidechain=True,
            agentId="agent-42",
        )) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-parent",
        transcript=transcript,
        tool_name="PowerShell",
        tool_input={"command": "Get-Date"},
    ))

    origin = json.loads(
        (pending_directory / "session-parent.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert origin["kind"] == "claude_subagent_record"
    assert origin["agent_id"] == "agent-42"
    assert origin["is_sidechain"] is True
    assert origin["transcript_path"] == (
        "projects/demo/session-parent/subagents/agent-42.jsonl"
    )


def test_permission_origin_survives_resolved_history_and_signal_extraction(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-history.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(_claude_tool_record(
            uuid="record-history",
            parent_uuid="parent-history",
            tool_use_id="toolu-history",
            tool_name="Bash",
            tool_input={"command": "git status"},
        )) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)
    hook_module.process_payload(_permission_payload(
        session_id="session-history",
        transcript=transcript,
    ))
    side_path = pending_directory / "session-history.json"
    permission_id = json.loads(side_path.read_text(encoding="utf-8"))["interaction_id"]

    hook_module.process_payload({
        "hook_event_name": "PreToolUse",
        "session_id": "session-history",
        "transcript_path": str(transcript),
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu-next",
        "tool_input": {"questions": [{"question": "Continue?"}]},
    })

    side_record = json.loads(side_path.read_text(encoding="utf-8"))
    history_origin = side_record["resolved_interactions"][0]["interaction_origin"]
    assert history_origin["record_uuid"] == "record-history"
    records = extract_claude_pending_interaction_updates(claude_root)
    signal = records[
        "claude_code:projects/demo/session-history.jsonl:" + permission_id
    ]
    assert signal["interaction_origin"] == history_origin


def test_session_end_cancels_only_pending_hook_only_permission(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-end.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)
    hook_module.process_payload(_permission_payload(
        session_id="session-end",
        transcript=transcript,
    ))
    side_path = pending_directory / "session-end.json"

    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-end",
        "timestamp": "2026-08-14T12:01:00Z",
    })

    cancelled = json.loads(side_path.read_text(encoding="utf-8"))
    assert cancelled["interaction_status"] == "cancelled"
    assert cancelled["session_end_timestamp"] == "2026-08-14T12:01:00Z"

    answered_history = [{
        "interaction_id": "memento-permission-prior",
        "question_tool": "PermissionRequest",
        "interaction_input": {"interaction_type": "permission_request"},
        "interaction_status": "answered",
        "interaction_origin": cancelled["interaction_origin"],
    }]
    preserved = {
        **cancelled,
        "interaction_id": "toolu-current-question",
        "question_tool": "AskUserQuestion",
        "interaction_input": {"questions": [{"question": "Keep history?"}]},
        "interaction_status": "pending",
        "resolved_interactions": answered_history,
    }
    side_path.write_text(json.dumps(preserved), encoding="utf-8")
    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-end",
        "timestamp": "2026-08-14T12:02:00Z",
    })
    assert json.loads(side_path.read_text(encoding="utf-8")) == preserved


@pytest.mark.parametrize("origin_kind", [
    "claude_record",
    "claude_subagent_record",
    "hook_only",
])
def test_session_end_cancels_every_pending_permission_origin(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin_kind: str,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-terminal.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    side_path = pending_directory / "session-terminal.json"
    side_path.write_text(json.dumps({
        "session_id": "session-terminal",
        "transcript_path": str(transcript),
        "interaction_id": "memento-permission-terminal",
        "question_tool": "PermissionRequest",
        "interaction_input": {"interaction_type": "permission_request"},
        "interaction_status": "pending",
        "interaction_origin": {
            "version": 1,
            "kind": origin_kind,
            "record_uuid": "record-terminal" if origin_kind != "hook_only" else "",
            "parent_uuid": "parent-terminal" if origin_kind != "hook_only" else "",
            "tool_use_id": "toolu-terminal" if origin_kind != "hook_only" else "",
            "fingerprint": "f" * 64,
            "agent_id": "agent-terminal" if origin_kind == "claude_subagent_record" else "",
            "is_sidechain": origin_kind == "claude_subagent_record",
        },
    }), encoding="utf-8")

    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-terminal",
        "reason": "logout",
        "timestamp": "2026-08-14T12:03:00Z",
    })

    terminal = json.loads(side_path.read_text(encoding="utf-8"))
    assert terminal["interaction_status"] == "cancelled"
    assert terminal["session_end_reason"] == "logout"


@pytest.mark.parametrize("status", ["answered", "cancelled"])
def test_session_end_preserves_existing_terminal_permission(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    side_path = pending_directory / "session-already-terminal.json"
    existing = {
        "session_id": "session-already-terminal",
        "interaction_id": "memento-permission-terminal",
        "question_tool": "PermissionRequest",
        "interaction_input": {"interaction_type": "permission_request"},
        "interaction_status": status,
        "interaction_origin": {"kind": "hook_only", "version": 1},
    }
    side_path.write_text(json.dumps(existing), encoding="utf-8")

    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-already-terminal",
        "reason": "clear",
        "timestamp": "2026-08-14T12:04:00Z",
    })

    assert json.loads(side_path.read_text(encoding="utf-8")) == existing


def test_session_end_marker_prevents_late_permission_resurrection(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-race.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    # SessionEnd publishes its durable marker before obtaining the side-file
    # lock. A PermissionRequest that arrives after that point cannot recreate
    # a pending side record, even if it would otherwise be valid.
    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-race",
        "reason": "user_exit",
    })
    hook_module.process_payload(_permission_payload(
        session_id="session-race",
        transcript=transcript,
    ))

    assert not (pending_directory / "session-race.json").exists()
    marker = json.loads(
        (pending_directory / ".session-race.session-ended").read_text(
            encoding="utf-8"
        )
    )
    assert marker["reason"] == "user_exit"


def test_permission_then_session_end_cancels_race_winner(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-race-winner.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-race-winner",
        transcript=transcript,
    ))
    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-race-winner",
        "reason": "clear",
    })

    terminal = json.loads(
        (pending_directory / "session-race-winner.json").read_text(
            encoding="utf-8"
        )
    )
    assert terminal["interaction_status"] == "cancelled"
    assert terminal["session_end_reason"] == "clear"


def test_session_start_clears_terminal_marker_without_reviving_permission(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-resume.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)
    payload = _permission_payload(
        session_id="session-resume",
        transcript=transcript,
    )
    hook_module.process_payload(payload)
    side_path = pending_directory / "session-resume.json"
    cancelled_id = json.loads(side_path.read_text(encoding="utf-8"))["interaction_id"]
    hook_module.process_payload({
        "hook_event_name": "SessionEnd",
        "session_id": "session-resume",
        "reason": "compact",
    })
    hook_module.process_payload({
        "hook_event_name": "SessionStart",
        "session_id": "session-resume",
        "reason": "resume",
    })
    assert not (pending_directory / ".session-resume.session-ended").exists()

    hook_module.process_payload(payload)

    resumed = json.loads(side_path.read_text(encoding="utf-8"))
    assert resumed["interaction_status"] == "pending"
    assert resumed["interaction_id"] != cancelled_id
    assert resumed["session_start_generation"] == 1
    assert resumed["resolved_interactions"][0]["interaction_id"] == cancelled_id
    assert resumed["resolved_interactions"][0]["interaction_status"] == "cancelled"


def test_permission_origin_rejects_unrelated_and_nonassistant_records(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    unrelated = claude_root / "projects" / "demo" / "other-session.jsonl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(json.dumps(_claude_tool_record(
        uuid="record-unrelated",
        parent_uuid="parent-unrelated",
        tool_use_id="toolu-unrelated",
        tool_name="Bash",
        tool_input={"command": "git status"},
    )) + "\n", encoding="utf-8")
    main = claude_root / "projects" / "demo" / "session-assistant.jsonl"
    main.write_text(json.dumps({
        **_claude_tool_record(
            uuid="record-user",
            parent_uuid="parent-user",
            tool_use_id="toolu-user",
            tool_name="Bash",
            tool_input={"command": "git status"},
        ),
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_use",
                "id": "toolu-user",
                "name": "Bash",
                "input": {"command": "git status"},
            }],
        },
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-owner",
        transcript=unrelated,
    ))
    unrelated_origin = json.loads(
        (pending_directory / "session-owner.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert unrelated_origin["kind"] == "hook_only"

    hook_module.process_payload(_permission_payload(
        session_id="session-assistant",
        transcript=main,
    ))
    nonassistant_origin = json.loads(
        (pending_directory / "session-assistant.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert nonassistant_origin["kind"] == "hook_only"


def test_permission_origin_accepts_nested_subagent_topology(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = (
        claude_root
        / "projects"
        / "demo"
        / "session-root"
        / "subagents"
        / "child-agent"
        / "subagents"
        / "grandchild-agent.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(_claude_tool_record(
        uuid="record-grandchild",
        parent_uuid="parent-grandchild",
        tool_use_id="toolu-grandchild",
        tool_name="Bash",
        tool_input={"command": "git status"},
    )) + "\n", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-root",
        transcript=transcript,
    ))

    origin = json.loads(
        (pending_directory / "session-root.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert origin["kind"] == "claude_subagent_record"
    assert origin["transcript_path"] == (
        "projects/demo/session-root/subagents/child-agent/subagents/"
        "grandchild-agent.jsonl"
    )


def test_permission_duplicate_keeps_prior_exact_origin_after_fallback(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-retain.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(_claude_tool_record(
        uuid="record-retain",
        parent_uuid="parent-retain",
        tool_use_id="toolu-retain",
        tool_name="Bash",
        tool_input={"command": "git status"},
    )) + "\n", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)
    payload = _permission_payload(
        session_id="session-retain",
        transcript=transcript,
    )
    hook_module.process_payload(payload)
    side_path = pending_directory / "session-retain.json"
    exact_origin = json.loads(side_path.read_text(encoding="utf-8"))["interaction_origin"]

    transcript.unlink()
    hook_module.process_payload(payload)

    origin = json.loads(side_path.read_text(encoding="utf-8"))["interaction_origin"]
    assert origin == exact_origin


def test_signal_drops_corrupt_legacy_origin_transcript_path(
    tmp_path: Path,
    pending_directory: Path,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-origin-path.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    _write_side_file(
        pending_directory,
        session_id="session-origin-path",
        transcript_path=transcript,
        status="pending",
        question_tool="PermissionRequest",
    )
    side_path = pending_directory / "session-origin-path.json"
    side_record = json.loads(side_path.read_text(encoding="utf-8"))
    side_record["interaction_origin"] = {
        "version": 1,
        "kind": "claude_record",
        "record_uuid": "record-safe",
        "parent_uuid": "parent-safe",
        "tool_use_id": "toolu-safe",
        "fingerprint": "f" * 64,
        "agent_id": "",
        "is_sidechain": False,
        "transcript_path": "../../outside.jsonl",
    }
    side_path.write_text(json.dumps(side_record), encoding="utf-8")

    signal = next(
        iter(extract_claude_pending_interaction_updates(claude_root).values())
    )

    assert signal["interaction_origin"]["record_uuid"] == "record-safe"
    assert "transcript_path" not in signal["interaction_origin"]


def test_nonfinite_permission_input_never_matches_or_crashes(
    tmp_path: Path,
    pending_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / ".claude"
    transcript = claude_root / "projects" / "demo" / "session-nonfinite.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook_module, "_pending_directory", lambda: pending_directory)
    monkeypatch.setattr(hook_module, "_claude_root", lambda: claude_root)

    hook_module.process_payload(_permission_payload(
        session_id="session-nonfinite",
        transcript=transcript,
        tool_input={"timeout": float("nan")},
    ))

    origin = json.loads(
        (pending_directory / "session-nonfinite.json").read_text(encoding="utf-8")
    )["interaction_origin"]
    assert origin["kind"] == "hook_only"
    assert origin["record_uuid"] == ""


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
    assert settings["hooks"]["SessionEnd"][0]["matcher"] == ".*"
    assert settings["hooks"]["SessionStart"][0]["matcher"] == ".*"
    assert {
        entry["matcher"] for entry in settings["hooks"]["Notification"]
    } == {"agent_needs_input", "agent_completed"}
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    for event_name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        assert {
            entry["matcher"]
            for entry in settings["hooks"][event_name]
            if any(
                hook_module._is_memento_hook(hook)
                for hook in entry.get("hooks", [])
            )
        } >= {"AskUserQuestion", "Bash", "PowerShell", "Shell"}
