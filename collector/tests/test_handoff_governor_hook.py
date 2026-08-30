from __future__ import annotations

import io
import json
import shutil
import sys
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
    monkeypatch.setenv("MEMENTO_GOVERNOR_BLOCK_TOKENS", "30")
    payload = _payload(transcript, event="PostToolUse")

    first = governor.process_hook_payload(payload, force_enabled=True)
    second = governor.process_hook_payload(payload, force_enabled=True)

    assert first is not None
    assert "Context hygiene threshold reached" in first["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "Author the milestone handoff NOW" in first["hookSpecificOutput"][
        "additionalContext"
    ]
    assert second is None
    state = json.loads(
        (tmp_path / "state" / "session-governor-1.json").read_text(encoding="utf-8")
    )
    assert state["notified_thresholds"] == [10, 20]


@pytest.mark.parametrize(
    ("tokens", "handoff_real", "stop_hook_active", "expects_block"),
    [
        (9, False, False, False),
        (9, True, False, False),
        (9, False, True, False),
        (9, True, True, False),
        (10, False, False, True),
        (10, True, False, False),
        (10, False, True, False),
        (10, True, True, False),
    ],
)
def test_stop_block_requires_threshold_and_missing_real_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokens: int,
    handoff_real: bool,
    stop_hook_active: bool,
    expects_block: bool,
) -> None:
    transcript = _write_transcript(
        tmp_path / "thread.jsonl",
        [_assistant_record(cache_read=0, cache_creation=tokens, input_tokens=0)],
    )
    handoff = tmp_path / "MEMENTO_HANDOFF.md"
    handoff.write_text(
        "# Handoff\n\n## Current session-governor-1\nReady to resume.\n"
        if handoff_real
        else "# Handoff\n\n## Previous session\nNo current handoff yet.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMENTO_GOVERNOR_HYGIENE_TOKENS", "10")
    monkeypatch.setenv("MEMENTO_GOVERNOR_HANDOFF_TOKENS", "10")
    monkeypatch.setenv("MEMENTO_GOVERNOR_BLOCK_TOKENS", "10")

    response = governor.process_hook_payload(
        _payload(
            transcript,
            event="Stop",
            stop_hook_active=stop_hook_active,
        ),
        force_enabled=True,
    )

    if expects_block:
        assert response is not None
        assert response["decision"] == "block"
        assert "session_id" in response["reason"]
        assert "cd-prefixed resume command" in response["reason"]
    else:
        assert response is None


def test_malformed_hook_input_exits_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(governor.sys, "stdin", io.StringIO("{malformed"))

    assert governor.main(["--enabled"]) == 0
    assert capsys.readouterr().out == ""


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
        entry for entry in settings["hooks"]["PostToolUse"] if entry.get("matcher") == "*"
    ]
    stop_entries = [
        entry for entry in settings["hooks"]["Stop"] if entry.get("matcher") == "*"
    ]

    assert changed is True
    assert len(post_entries) == len(stop_entries) == 1
    command = post_entries[0]["hooks"][0]["command"]
    assert command.startswith('"')
    assert "-m collector.handoff_governor_hook --enabled" in command
    assert post_entries[0]["hooks"][0]["timeout"] == 10
    assert stop_entries[0]["hooks"][0]["timeout"] == 10

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


def test_frozen_tauri_reconciler_migrates_old_hooks_to_versioned_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old onefile commands are replaced only after an onedir copy succeeds."""

    config = tmp_path / ".claude"
    config.mkdir()
    settings_path = config / "settings.json"
    settings_path.write_text(
        json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{
                        "type": "command",
                        "command": '"C:\\\\old\\\\memento-collector-sidecar.exe" claude-hook',
                    }],
                }],
                "Stop": [{
                    "matcher": "*",
                    "hooks": [{
                        "type": "command",
                        "command": (
                            '"C:\\\\old\\\\memento-collector-sidecar.exe" '
                            "claude-governor-hook --enabled"
                        ),
                    }],
                }],
            },
        }),
        encoding="utf-8",
    )
    app_directory = tmp_path / "Tauri app with spaces"
    sidecar = app_directory / "memento-collector-sidecar.exe"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"sidecar")
    bundled_runner = app_directory / "binaries" / "memento-hook-runner"
    _write_complete_runner(bundled_runner)
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("MEMENTO_GOVERNOR_ENABLED", "1")
    monkeypatch.setattr(pending_hook.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pending_hook.sys, "executable", str(sidecar))

    _settings_path, changed = pending_hook.install_claude_pending_hooks()

    installed_runner = (
        local_app_data
        / "Memento"
        / "hooks"
        / "0.0.55"
        / "memento-hook-runner.exe"
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
    assert not any("memento-collector-sidecar" in hook["command"] for hook in managed_hooks)

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
        json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{
                        "type": "command",
                        "command": '"C:\\\\old\\\\memento-collector-sidecar.exe" claude-hook',
                    }],
                }],
                "Stop": [{
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
                }],
            },
        }),
        encoding="utf-8",
    )
    sidecar = tmp_path / "manual sidecar" / "memento-collector-sidecar.exe"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"sidecar")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-runner-local-app-data"))
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


def test_runner_install_lost_windows_rename_race_uses_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source" / "memento-hook-runner"
    _write_complete_runner(source)
    local_app_data = tmp_path / "local-app-data"
    destination = local_app_data / "Memento" / "hooks" / "0.0.55"
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


def test_runner_retention_keeps_current_previous_and_skips_locked_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "hooks"
    old_deleted = root / "0.0.52"
    old_locked = root / "0.0.53"
    previous = root / "0.0.54"
    current = root / "0.0.55"
    for directory in (old_deleted, old_locked, previous, current):
        _write_complete_runner(directory)
    real_rmtree = shutil.rmtree

    def skip_locked(directory: Path, *args: object, **kwargs: object) -> None:
        if Path(directory) == old_locked:
            raise PermissionError(5, "in use", str(directory))
        real_rmtree(directory, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", skip_locked)

    pending_hook._cleanup_hook_runner_versions(root, current)

    assert not old_deleted.exists()
    assert old_locked.exists()
    assert previous.exists()
    assert current.exists()
