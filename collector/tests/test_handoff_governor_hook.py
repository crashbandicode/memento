from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import claude_pending_hook as pending_hook  # noqa: E402
from collector import handoff_governor_hook as governor  # noqa: E402


def _write_complete_runner(directory: Path) -> Path:
    (directory / "_internal").mkdir(parents=True)
    runner = directory / "memento-hook-runner.exe"
    runner.write_bytes(b"runner")
    (directory / "_internal" / "python.dll").write_bytes(b"dll")
    return runner


def _runner_tree_snapshot(directory: Path) -> list[tuple[str, int, str]]:
    """Capture every runner file so live-cleanup tests detect partial deletion."""

    return sorted(
        (
            path.relative_to(directory).as_posix(),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in directory.rglob("*")
        if path.is_file()
    )


def _built_hook_runner_executable() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "tauri-collector"
        / "src-tauri"
        / "binaries"
        / "memento-hook-runner"
        / "memento-hook-runner.exe"
    )


def _assistant_record(
    *,
    cache_read: int,
    cache_creation: int,
    input_tokens: int,
    is_sidechain: bool = False,
) -> dict:
    return {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "message": {
            "usage": {
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "input_tokens": input_tokens,
            }
        },
    }


def _codex_token_record(*, input_tokens: int, context_window: int) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": max(0, input_tokens - 10),
                    "output_tokens": 10,
                    "total_tokens": input_tokens + 10,
                },
                "model_context_window": context_window,
            },
        },
    }


def _write_transcript(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _payload(transcript: Path, *, event: str, **extra: object) -> dict:
    return {
        "hook_event_name": event,
        "session_id": "session-governor-1",
        "transcript_path": str(transcript),
        "cwd": str(transcript.parent),
        **extra,
    }


def test_prefix_uses_latest_primary_assistant_and_all_usage_fields(
    tmp_path: Path,
) -> None:
    transcript = _write_transcript(
        tmp_path / "thread.jsonl",
        [
            _assistant_record(cache_read=10, cache_creation=10, input_tokens=10),
            _assistant_record(
                cache_read=999_999,
                cache_creation=999_999,
                input_tokens=999_999,
                is_sidechain=True,
            ),
            _assistant_record(cache_read=0, cache_creation=17, input_tokens=3),
        ],
    )

    assert governor.prefix_tokens_from_transcript(str(transcript)) == 20


def test_codex_rollout_usage_drives_window_aware_default_thresholds(
    tmp_path: Path,
) -> None:
    transcript = _write_transcript(
        tmp_path / "rollout.jsonl",
        [_codex_token_record(input_tokens=190_000, context_window=256_000)],
    )

    usage = governor._usage_from_transcript(str(transcript))

    assert usage == governor.ContextUsage(tokens=190_000, context_window=256_000)
    assert governor.configured_thresholds(usage.context_window) == (
        governor.GovernorThresholds(
            hygiene=192_000,
            handoff=230_400,
            reminder=243_200,
        )
    )


def test_yoga_primed_thread_can_finish_its_first_operator_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document-heavy priming turn must not immediately evict the operator."""

    transcript = _write_transcript(
        tmp_path / "rollout.jsonl",
        [_codex_token_record(input_tokens=223_806, context_window=258_400)],
    )
    monkeypatch.setattr(governor, "_governor_directory", lambda: tmp_path / "state")
    payload = _payload(transcript, event="PostToolUse", turn_id="turn-yoga-1")

    response = governor.process_hook_payload(payload, force_enabled=True)

    assert response is not None
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Context hygiene threshold reached" in context
    assert "milestone handoff" not in context

    _write_transcript(
        transcript,
        [_codex_token_record(input_tokens=235_000, context_window=258_400)],
    )
    response = governor.process_hook_payload(payload, force_enabled=True)

    assert response is not None
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Context handoff advisory" in context
    assert "explicitly authorizes a handoff" in context

    _write_transcript(
        transcript,
        [_codex_token_record(input_tokens=246_000, context_window=258_400)],
    )
    response = governor.process_hook_payload(payload, force_enabled=True)

    assert response is not None
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Operator-controlled handoff reminder" in context
    assert "every final response" in context
    assert "reminded later" in context
    assert "not to be reminded at all" in context


def test_explicit_thresholds_override_window_aware_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "101")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "202")
    monkeypatch.setenv("MEMENTO_GOVERNOR_BLOCK_TOKENS", "303")

    assert governor.configured_thresholds(256_000) == governor.GovernorThresholds(
        hygiene=101,
        handoff=202,
        reminder=303,
    )


def test_reminder_threshold_variable_supersedes_legacy_block_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMENTO_GOVERNOR_REMINDER_TOKENS", "304")
    monkeypatch.setenv("MEMENTO_GOVERNOR_BLOCK_TOKENS", "303")

    assert governor.configured_thresholds() is None

    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "101")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "202")
    assert governor.configured_thresholds() == governor.GovernorThresholds(
        hygiene=101,
        handoff=202,
        reminder=304,
    )


def test_cursor_composer_usage_is_read_by_conversation_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.vscdb"
    connection = governor.sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (
                "composerData:cursor-session-1",
                json.dumps(
                    {
                        "contextTokensUsed": 235_000,
                        "contextTokenLimit": 256_000,
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("MEMENTO_CURSOR_STATE_DB", str(database))
    monkeypatch.setattr(governor, "_governor_directory", lambda: tmp_path / "state")

    response = governor.process_hook_payload(
        {
            "hook_event_name": "PostToolUse",
            "conversation_id": "cursor-session-1",
            "cwd": str(tmp_path),
        },
        force_enabled=True,
    )

    assert response is not None
    assert "Context handoff advisory" in response["additional_context"]
    assert set(response) == {"additional_context"}


@pytest.mark.parametrize("database_contents", [None, b"not-a-sqlite-database"])
def test_missing_or_invalid_cursor_state_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_contents: bytes | None,
) -> None:
    database = tmp_path / "state.vscdb"
    if database_contents is not None:
        database.write_bytes(database_contents)
    monkeypatch.setenv("MEMENTO_CURSOR_STATE_DB", str(database))

    assert (
        governor.process_hook_payload(
            {
                "hook_event_name": "PostToolUse",
                "conversation_id": "cursor-session-1",
                "cwd": str(tmp_path),
            },
            force_enabled=True,
        )
        is None
    )


def test_post_tool_nudge_latches_each_threshold_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _write_transcript(
        tmp_path / "thread.jsonl",
        [_assistant_record(cache_read=0, cache_creation=20, input_tokens=0)],
    )
    monkeypatch.setattr(governor, "_governor_directory", lambda: tmp_path / "state")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "10")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "20")
    monkeypatch.setenv("MEMENTO_GOVERNOR_REMINDER_TOKENS", "30")
    payload = _payload(transcript, event="PostToolUse")

    first = governor.process_hook_payload(payload, force_enabled=True)
    second = governor.process_hook_payload(payload, force_enabled=True)

    assert first is not None
    assert (
        "Context hygiene threshold reached"
        in first["hookSpecificOutput"]["additionalContext"]
    )
    assert (
        "Context handoff advisory" in first["hookSpecificOutput"]["additionalContext"]
    )
    assert second is None
    state = json.loads(
        (tmp_path / "state" / "session-governor-1.json").read_text(encoding="utf-8")
    )
    assert state["notified_thresholds"] == [10, 20]


def test_former_cutoff_activates_recurring_operator_advice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _write_transcript(
        tmp_path / "thread.jsonl",
        [_assistant_record(cache_read=0, cache_creation=30, input_tokens=0)],
    )
    monkeypatch.setattr(governor, "_governor_directory", lambda: tmp_path / "state")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "10")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "20")
    monkeypatch.setenv("MEMENTO_GOVERNOR_REMINDER_TOKENS", "30")

    response = governor.process_hook_payload(
        _payload(transcript, event="PostToolUse"),
        force_enabled=True,
    )

    assert response is not None
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "Operator-controlled handoff reminder" in context
    assert "Continue executing the active task" in context
    assert "every final response" in context
    assert "not consent" in context
    assert "stop reminders" in context
    assert json.loads(
        (tmp_path / "state" / "session-governor-1.json").read_text(encoding="utf-8")
    )["notified_thresholds"] == [10, 20, 30]


def test_codex_post_tool_nudge_uses_only_strict_codex_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _write_transcript(
        tmp_path / "rollout.jsonl",
        [_codex_token_record(input_tokens=20, context_window=100)],
    )
    monkeypatch.setattr(governor, "_governor_directory", lambda: tmp_path / "state")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "10")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "20")
    monkeypatch.setenv("MEMENTO_GOVERNOR_REMINDER_TOKENS", "30")

    response = governor.process_hook_payload(
        _payload(transcript, event="PostToolUse", turn_id="turn-codex-1"),
        force_enabled=True,
    )

    assert response is not None
    assert set(response) == {"hookSpecificOutput"}
    assert response["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


@pytest.mark.parametrize("identity_field", ["session_id", "conversation_id"])
def test_stop_event_never_blocks_any_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_field: str,
) -> None:
    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "1")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "1")
    monkeypatch.setenv("MEMENTO_GOVERNOR_REMINDER_TOKENS", "1")

    assert (
        governor.process_hook_payload(
            {
                "hook_event_name": "Stop",
                identity_field: "session-governor-1",
                "cwd": str(tmp_path),
                "context_tokens": 999_999,
                "context_window_size": 1_000_000,
                "stop_hook_active": False,
                "loop_count": 0,
            },
            force_enabled=True,
        )
        is None
    )


def test_malformed_hook_input_emits_neutral_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(governor.sys, "stdin", io.StringIO("{malformed"))

    assert governor.main(["--enabled"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_noop_hook_input_emits_neutral_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        governor.sys,
        "stdin",
        io.StringIO(json.dumps(_payload(tmp_path / "missing.jsonl", event="Stop"))),
    )

    assert governor.main(["--enabled"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_missing_transcript_noops_cleanly(tmp_path: Path) -> None:
    response = governor.process_hook_payload(
        _payload(tmp_path / "missing.jsonl", event="PostToolUse"),
        force_enabled=True,
    )

    assert response is None


def test_registration_adds_governor_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / ".claude"
    config.mkdir()
    settings_path = config / "settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("MEMENTO_GOVERNOR_ENABLED", "1")

    _settings_path, changed = pending_hook.install_claude_pending_hooks()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    post_entries = [
        entry
        for entry in settings["hooks"]["PostToolUse"]
        if entry.get("matcher") == "*"
    ]
    stop_entries = [
        entry
        for entry in settings["hooks"].get("Stop", [])
        if entry.get("matcher") == "*"
    ]

    assert changed is True
    assert len(post_entries) == 1
    assert stop_entries == []
    command = post_entries[0]["hooks"][0]["command"]
    assert command.startswith('"')
    assert "-m collector.handoff_governor_hook --enabled" in command
    assert post_entries[0]["hooks"][0]["timeout"] == 10

    monkeypatch.delenv("MEMENTO_GOVERNOR_ENABLED")
    _settings_path, changed = pending_hook.install_claude_pending_hooks()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    assert changed is True
    assert not any(
        "handoff_governor_hook" in hook.get("command", "")
        for entries in settings["hooks"].values()
        for entry in entries
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    )
    assert "Stop" not in settings["hooks"]


@pytest.mark.parametrize(
    ("executable", "codex_windows", "expected"),
    [
        (
            r"C:\Users\intpa\AppData\Local\Memento\hooks\0.0.60\memento-hook-runner.exe",
            True,
            r"C:\Users\intpa\AppData\Local\Memento\hooks\0.0.60\memento-hook-runner.exe",
        ),
        (
            r"C:\Users\Example User\memento-hook-runner.exe",
            True,
            r'"C:\Users\Example User\memento-hook-runner.exe"',
        ),
        (
            r"C:\Users\intpa\memento-hook-runner.exe",
            False,
            r'"C:\Users\intpa\memento-hook-runner.exe"',
        ),
    ],
)
def test_hook_executable_token_avoids_unnecessary_codex_windows_quotes(
    executable: str,
    codex_windows: bool,
    expected: str,
) -> None:
    assert (
        pending_hook._hook_executable_token(
            executable,
            codex_windows=codex_windows,
        )
        == expected
    )


def test_codex_registration_preserves_user_hooks_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": "user-stop-hook"},
                                {
                                    "type": "command",
                                    "command": (
                                        r"C:\old\memento-hook-runner.exe "
                                        "claude-governor-hook --enabled"
                                    ),
                                },
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = tmp_path / "memento-hook-runner.exe"
    runner.write_bytes(b"runner")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("MEMENTO_GOVERNOR_ENABLED", "1")
    monkeypatch.setattr(pending_hook, "_install_hook_runner", lambda: runner)
    monkeypatch.setattr(
        pending_hook, "_maintain_hook_runner_versions", lambda *args, **kwargs: None
    )

    installed_path, changed = pending_hook.install_codex_governor_hooks()
    _installed_path, changed_again = pending_hook.install_codex_governor_hooks()
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert installed_path == hooks_path
    assert changed is True
    assert changed_again is False
    stop_commands = [
        hook["command"]
        for entry in settings["hooks"]["Stop"]
        for hook in entry["hooks"]
    ]
    assert "user-stop-hook" in stop_commands
    assert not any(
        "claude-governor-hook --enabled" in command for command in stop_commands
    )
    post_commands = [
        hook["command"]
        for entry in settings["hooks"]["PostToolUse"]
        for hook in entry["hooks"]
    ]
    assert len(post_commands) == 1
    assert str(runner.resolve()) in post_commands[0]
    if os.name == "nt":
        assert post_commands[0].startswith(str(runner.resolve()))

    monkeypatch.delenv("MEMENTO_GOVERNOR_ENABLED")
    _removed_path, removed = pending_hook.install_codex_governor_hooks()
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert removed is True
    assert settings["hooks"] == {
        "Stop": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": "user-stop-hook"}],
            }
        ],
    }


def test_cursor_native_registration_preserves_user_hooks_and_removes_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text("{}\n", encoding="utf-8")
    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    hooks_path = cursor_home / "hooks.json"
    stale_command = r"C:\old\memento-hook-runner.exe claude-governor-hook --enabled"
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {"command": "user-post-hook", "matcher": "Read"},
                        {"command": stale_command},
                    ],
                    "stop": [
                        {"command": "user-stop-hook"},
                        {"command": stale_command},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = tmp_path / "memento-hook-runner.exe"
    runner.write_bytes(b"runner")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("MEMENTO_CURSOR_HOOKS_PATH", str(hooks_path))
    monkeypatch.setenv("MEMENTO_GOVERNOR_ENABLED", "1")
    monkeypatch.setattr(pending_hook, "_install_hook_runner", lambda: runner)
    monkeypatch.setattr(
        pending_hook, "_maintain_hook_runner_versions", lambda *args, **kwargs: None
    )

    installed_path, changed = pending_hook.install_cursor_governor_hooks()
    _installed_path, changed_again = pending_hook.install_cursor_governor_hooks()
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert installed_path == hooks_path
    assert changed is True
    assert changed_again is False
    assert settings["version"] == 1
    assert settings["hooks"]["stop"] == [{"command": "user-stop-hook"}]
    assert {entry["command"] for entry in settings["hooks"]["postToolUse"]} == {
        "user-post-hook",
        pending_hook._governor_hook_command(
            runner,
            codex_windows=os.name == "nt",
        ),
    }

    monkeypatch.delenv("MEMENTO_GOVERNOR_ENABLED")
    _removed_path, removed = pending_hook.install_cursor_governor_hooks()
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert removed is True
    assert settings == {
        "version": 1,
        "hooks": {
            "postToolUse": [{"command": "user-post-hook", "matcher": "Read"}],
            "stop": [{"command": "user-stop-hook"}],
        },
    }


def test_cursor_uses_imported_claude_governor_without_native_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    claude_command = (
        r'"C:\managed\memento-hook-runner.exe" '
        "claude-governor-hook --enabled"
    )
    (claude_home / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": claude_command,
                                    "timeout": 10,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    hooks_path = cursor_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {"command": "user-post-hook", "matcher": "Read"},
                        {
                            "command": (
                                r"C:\old\memento-hook-runner.exe "
                                "claude-governor-hook --enabled"
                            )
                        },
                    ],
                    "stop": [{"command": "user-stop-hook"}],
                },
            }
        ),
        encoding="utf-8",
    )
    runner = tmp_path / "memento-hook-runner.exe"
    runner.write_bytes(b"runner")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("MEMENTO_CURSOR_HOOKS_PATH", str(hooks_path))
    monkeypatch.setenv("MEMENTO_GOVERNOR_ENABLED", "1")
    monkeypatch.setattr(pending_hook, "_install_hook_runner", lambda: runner)
    monkeypatch.setattr(
        pending_hook, "_maintain_hook_runner_versions", lambda *args, **kwargs: None
    )

    installed_path, changed = pending_hook.install_cursor_governor_hooks()
    _installed_path, changed_again = pending_hook.install_cursor_governor_hooks()
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert installed_path == hooks_path
    assert changed is True
    assert changed_again is False
    assert settings == {
        "version": 1,
        "hooks": {
            "postToolUse": [{"command": "user-post-hook", "matcher": "Read"}],
            "stop": [{"command": "user-stop-hook"}],
        },
    }


def test_frozen_tauri_reconciler_migrates_old_hooks_to_versioned_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old onefile commands are replaced only after an onedir copy succeeds."""

    config = tmp_path / ".claude"
    config.mkdir()
    settings_path = config / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"C:\\\\old\\\\memento-collector-sidecar.exe" claude-hook',
                                }
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        '"C:\\\\old\\\\memento-collector-sidecar.exe" '
                                        "claude-governor-hook --enabled"
                                    ),
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    app_directory = tmp_path / "Tauri app with spaces"
    sidecar = app_directory / "memento-collector-sidecar.exe"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"sidecar")
    bundled_runner = app_directory / "binaries" / "memento-hook-runner"
    _write_complete_runner(bundled_runner)
    local_app_data = tmp_path / "local-app-data"
    retired_runner = local_app_data / "Memento" / "hooks" / "0.0.54"
    _write_complete_runner(retired_runner)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("MEMENTO_GOVERNOR_ENABLED", "1")
    monkeypatch.setattr(pending_hook.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pending_hook.sys, "executable", str(sidecar))

    _settings_path, changed = pending_hook.install_claude_pending_hooks()

    installed_runner = (
        local_app_data / "Memento" / "hooks" / "0.0.60" / "memento-hook-runner.exe"
    )
    assert changed is True
    assert installed_runner.read_bytes() == b"runner"
    assert (installed_runner.parent / "_internal" / "python.dll").read_bytes() == b"dll"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    managed_hooks = [
        hook
        for entries in settings["hooks"].values()
        for entry in entries
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if (
            pending_hook._is_memento_hook(hook)
            or pending_hook._is_memento_governor_hook(hook)
        )
    ]
    assert managed_hooks
    assert all(hook["timeout"] == 10 for hook in managed_hooks)
    assert all(str(installed_runner) in hook["command"] for hook in managed_hooks)
    assert not any(
        "memento-collector-sidecar" in hook["command"] for hook in managed_hooks
    )
    retirement = json.loads(
        pending_hook._hook_runner_retirement_marker(retired_runner).read_text(
            encoding="utf-8"
        )
    )
    assert retirement["retiring_collector_version"] == "0.0.54"
    assert isinstance(retirement["retired_at"], str)

    _settings_path, changed = pending_hook.install_claude_pending_hooks()
    assert changed is False


@pytest.mark.parametrize(
    "layout",
    (
        "configured",
        "tauri_windows",
        "manual_fleet",
        "legacy_resources",
        "direct_build",
        "macos_resources",
    ),
)
def test_bundled_runner_discovery_supports_each_packaged_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    app_directory = tmp_path / "app bundle"
    sidecar = app_directory / "memento-collector-sidecar.exe"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"sidecar")
    local_app_data = tmp_path / "manual fleet local app data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("MEMENTO_HOOK_RUNNER_SOURCE", raising=False)
    monkeypatch.setattr(pending_hook.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pending_hook.sys, "executable", str(sidecar))

    locations = {
        "configured": tmp_path / "explicit source" / "memento-hook-runner",
        "tauri_windows": app_directory / "binaries" / "memento-hook-runner",
        "manual_fleet": (
            local_app_data / "Memento" / "binaries" / "memento-hook-runner"
        ),
        "legacy_resources": (
            app_directory / "resources" / "binaries" / "memento-hook-runner"
        ),
        "direct_build": app_directory / "memento-hook-runner",
        "macos_resources": (
            app_directory / "Resources" / "binaries" / "memento-hook-runner"
        ),
    }
    expected = locations[layout]
    _write_complete_runner(expected)
    if layout == "configured":
        monkeypatch.setenv("MEMENTO_HOOK_RUNNER_SOURCE", str(expected))

    assert pending_hook._bundled_hook_runner_directory() == expected


def test_frozen_missing_runner_reconciles_with_legacy_timeout_and_removes_governor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / ".claude"
    config.mkdir()
    settings_path = config / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"C:\\\\old\\\\memento-collector-sidecar.exe" claude-hook',
                                }
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": "user-stop-hook"},
                                {
                                    "type": "command",
                                    "command": (
                                        '"C:\\\\old\\\\memento-collector-sidecar.exe" '
                                        "claude-governor-hook --enabled"
                                    ),
                                },
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / "manual sidecar" / "memento-collector-sidecar.exe"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"sidecar")
    local_app_data = tmp_path / "no-runner-local-app-data"
    old_version = local_app_data / "Memento" / "hooks" / "0.0.54"
    _write_complete_runner(old_version)
    old_version_before = _runner_tree_snapshot(old_version)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("MEMENTO_HOOK_RUNNER_SOURCE", raising=False)
    monkeypatch.delenv("MEMENTO_GOVERNOR_ENABLED", raising=False)
    monkeypatch.setattr(pending_hook.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pending_hook.sys, "executable", str(sidecar))

    _settings_path, changed = pending_hook.install_claude_pending_hooks()

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert changed is True
    pending_handlers = [
        hook
        for entries in settings["hooks"].values()
        for entry in entries
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if pending_hook._is_memento_hook(hook)
    ]
    assert pending_handlers
    assert all(str(sidecar.resolve()) in hook["command"] for hook in pending_handlers)
    assert all(hook["timeout"] == 10 for hook in pending_handlers)
    assert all(
        not pending_hook._is_memento_governor_hook(hook)
        for entry in settings["hooks"]["Stop"]
        for hook in entry["hooks"]
    )
    assert settings["hooks"]["Stop"][0]["hooks"] == [
        {"type": "command", "command": "user-stop-hook"},
    ]
    assert _runner_tree_snapshot(old_version) == old_version_before
    assert not pending_hook._hook_runner_retirement_marker(old_version).exists()


def test_runner_install_lost_windows_rename_race_uses_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source" / "memento-hook-runner"
    _write_complete_runner(source)
    local_app_data = tmp_path / "local-app-data"
    destination = local_app_data / "Memento" / "hooks" / "0.0.60"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("MEMENTO_HOOK_RUNNER_SOURCE", str(source))
    monkeypatch.setattr(pending_hook.sys, "frozen", True, raising=False)

    def lose_race(staging: Path, target: Path) -> None:
        shutil.copytree(staging, target)
        raise PermissionError(5, "Access is denied", str(target))

    monkeypatch.setattr(pending_hook.os, "replace", lose_race)

    assert pending_hook._install_hook_runner() == (
        destination / "memento-hook-runner.exe"
    )
    assert pending_hook._hook_runner_is_complete(destination)


def test_runner_install_recognizes_generic_windows_already_exists_error() -> None:
    class WindowsAlreadyExistsError(OSError):
        @property
        def winerror(self) -> int:
            return 183

    assert pending_hook._is_lost_hook_runner_install_race(
        WindowsAlreadyExistsError(1, "already exists")
    )


def _expired_retirement_time() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=30)


def test_runner_retention_is_opt_in_and_tunable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMENTO_HOOK_RUNNER_RETENTION_HOURS", raising=False)
    assert pending_hook._hook_runner_retention_age() is None

    monkeypatch.setenv("MEMENTO_HOOK_RUNNER_RETENTION_HOURS", "0.5")
    assert pending_hook._hook_runner_retention_age() == timedelta(minutes=30)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "invalid"])
def test_runner_retention_rejects_non_positive_or_non_finite_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MEMENTO_HOOK_RUNNER_RETENTION_HOURS", value)

    assert pending_hook._hook_runner_retention_age() is None


def test_runner_sweep_keeps_expired_versions_when_retention_is_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMENTO_HOOK_RUNNER_RETENTION_HOURS", raising=False)
    root = tmp_path / "hooks"
    old = root / "0.0.53"
    current = root / "0.0.56"
    _write_complete_runner(old)
    _write_complete_runner(current)
    assert pending_hook._write_hook_runner_retirement_marker(
        old,
        retired_at=_expired_retirement_time(),
    )
    before_sweep = _runner_tree_snapshot(old)

    pending_hook._sweep_retired_hook_runner_versions(root, current, {current})

    assert _runner_tree_snapshot(old) == before_sweep


def test_runner_sweep_marks_missing_retirement_marker_without_deleting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hooks"
    old = root / "0.0.53"
    current = root / "0.0.56"
    _write_complete_runner(old)
    _write_complete_runner(current)
    before_marker = _runner_tree_snapshot(old)

    pending_hook._sweep_retired_hook_runner_versions(root, current, {current})

    marker = pending_hook._hook_runner_retirement_marker(old)
    assert old.exists()
    assert [
        entry
        for entry in _runner_tree_snapshot(old)
        if entry[0] != pending_hook._HOOK_RUNNER_RETIREMENT_MARKER
    ] == before_marker
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["retiring_collector_version"] == "0.0.53"
    assert isinstance(marker_payload["retired_at"], str)


def test_runner_sweep_keeps_fresh_retirement_marker_untouched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hooks"
    old = root / "0.0.53"
    current = root / "0.0.56"
    _write_complete_runner(old)
    _write_complete_runner(current)
    assert pending_hook._write_hook_runner_retirement_marker(old)
    before_sweep = _runner_tree_snapshot(old)

    pending_hook._sweep_retired_hook_runner_versions(root, current, {current})

    assert _runner_tree_snapshot(old) == before_sweep


def test_runner_sweep_never_touches_current_or_registered_versions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "hooks"
    registered = root / "0.0.53"
    current = root / "0.0.56"
    for directory in (registered, current):
        _write_complete_runner(directory)
        assert pending_hook._write_hook_runner_retirement_marker(
            directory,
            retired_at=_expired_retirement_time(),
        )
    registered_before = _runner_tree_snapshot(registered)
    current_before = _runner_tree_snapshot(current)

    pending_hook._sweep_retired_hook_runner_versions(
        root,
        current,
        {current, registered},
    )

    assert _runner_tree_snapshot(registered) == registered_before
    assert _runner_tree_snapshot(current) == current_before


@pytest.mark.skipif(
    os.name != "nt" or not _built_hook_runner_executable().is_file(),
    reason="requires the locally built Windows onedir hook runner",
)
def test_runner_sweep_preserves_live_governor_straggler_then_reclaims_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executable gate must preserve a live advisory invocation."""

    monkeypatch.setenv("MEMENTO_HOOK_RUNNER_RETENTION_HOURS", "0.001")
    root = tmp_path / "hooks"
    old_live = root / "0.0.53"
    previous = root / "0.0.54"
    current = root / "0.0.56"
    project = tmp_path / "project"
    project.mkdir()
    transcript = _write_transcript(
        project / "thread.jsonl",
        [_assistant_record(cache_read=0, cache_creation=100, input_tokens=0)],
    )
    payload = _payload(transcript, event="PostToolUse")
    shutil.copytree(_built_hook_runner_executable().parent, old_live)
    _write_complete_runner(previous)
    _write_complete_runner(current)
    assert pending_hook._write_hook_runner_retirement_marker(
        old_live,
        retired_at=_expired_retirement_time(),
    )
    before_sweep = _runner_tree_snapshot(old_live)
    process: subprocess.Popen[str] | None = None
    try:
        environment = {
            **os.environ,
            "MEMENTO_GOVERNOR_HYGIENE_TOKENS": "1",
            "MEMENTO_GOVERNOR_HANDOFF_TOKENS": "2",
            "MEMENTO_GOVERNOR_REMINDER_TOKENS": "3",
            "USERPROFILE": str(tmp_path / "profile"),
        }
        process = subprocess.Popen(
            [
                str(old_live / "memento-hook-runner.exe"),
                "claude-governor-hook",
                "--enabled",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        # Hold stdin past warm startup so the executable is a real straggler.
        time.sleep(0.75)
        assert process.poll() is None

        pending_hook._sweep_retired_hook_runner_versions(root, current, {current})

        assert _runner_tree_snapshot(old_live) == before_sweep
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stdout is not None
        assert process.stderr is not None
        response = json.loads(process.stdout.read())
        assert set(response) == {"hookSpecificOutput"}
        assert (
            "Operator-controlled handoff reminder"
            in response["hookSpecificOutput"]["additionalContext"]
        )
        assert "decision" not in response
        assert process.stderr.read() == ""

        pending_hook._sweep_retired_hook_runner_versions(root, current, {current})

        assert not old_live.exists()
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
