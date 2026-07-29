"""Capture Claude AskUserQuestion before its transcript record is flushed."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
_MATCHER = "AskUserQuestion"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_MEMENTO_HOOK_MARKERS = (
    "pending_question_hook.py",
    "collector.claude_pending_hook",
    " claude-hook",
)


def _pending_directory() -> Path:
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        home = Path(profile) if profile else Path.home()
    else:
        home = Path.home()
    return home / ".memento" / "claude-pending"


def _event_name(payload: dict[str, Any]) -> str:
    value = payload.get("hook_event_name", payload.get("hook_event", ""))
    return re.sub(r"[^a-z]", "", str(value).casefold())


def _tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name", payload.get("toolName", ""))).strip()


def _interaction_id(payload: dict[str, Any]) -> str:
    for field in ("tool_use_id", "toolUseId", "tool_call_id", "id"):
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return ""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_atomic(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_payload(payload: object) -> None:
    """Update one pending-question side file, ignoring malformed payloads."""
    if not isinstance(payload, dict):
        return
    if _tool_name(payload).casefold() != "askuserquestion":
        return

    event_name = _event_name(payload)
    if event_name not in {"pretooluse", "posttooluse", "posttoolusefailure"}:
        return

    session_id = str(payload.get("session_id") or "").strip()
    if (
        not session_id
        or session_id in {".", ".."}
        or not _SAFE_SESSION_ID.fullmatch(session_id)
    ):
        return

    path = _pending_directory() / f"{session_id}.json"
    existing = _read_mapping(path)
    interaction_id = _interaction_id(payload)
    existing_id = str(existing.get("interaction_id") or "").strip()

    if (
        event_name != "pretooluse"
        and existing_id
        and interaction_id
        and existing_id != interaction_id
    ):
        return
    if not interaction_id:
        interaction_id = existing_id
    if not interaction_id:
        return

    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, dict):
        raw_input = existing.get("interaction_input")
    if not isinstance(raw_input, dict):
        raw_input = {}

    status = {
        "pretooluse": "pending",
        "posttooluse": "answered",
        "posttoolusefailure": "cancelled",
    }[event_name]
    _write_atomic(
        path,
        {
            "session_id": session_id,
            "transcript_path": str(
                payload.get("transcript_path") or existing.get("transcript_path") or ""
            ),
            "interaction_id": interaction_id,
            "question_tool": "AskUserQuestion",
            "interaction_input": raw_input,
            "interaction_status": status,
            "timestamp": _timestamp(),
            "cwd": str(payload.get("cwd") or existing.get("cwd") or ""),
        },
    )


def _settings_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return root / "settings.json"


def _hook_command() -> str:
    executable = str(Path(sys.executable).resolve()).replace('"', '\\"')
    if getattr(sys, "frozen", False):
        return f'"{executable}" claude-hook'
    return f'"{executable}" -m collector.claude_pending_hook'


def _is_memento_hook(hook: object) -> bool:
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = str(hook.get("command") or "")
    return any(marker in command for marker in _MEMENTO_HOOK_MARKERS)


def _merge_event_hook(
    hooks: dict[str, Any],
    event_name: str,
    command: str,
) -> bool:
    entries = hooks.setdefault(event_name, [])
    if not isinstance(entries, list):
        raise TypeError(f"hooks.{event_name} must be a list")
    before = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)
    matching = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and str(entry.get("matcher") or "").casefold() == _MATCHER.casefold()
        and isinstance(entry.get("hooks"), list)
    ]
    if not matching:
        entries.append(
            {
                "matcher": _MATCHER,
                "hooks": [{"type": "command", "command": command}],
            }
        )
    else:
        primary = matching[0]
        merged_hooks: list[object] = []
        has_current = False
        for entry in matching:
            for hook in entry["hooks"]:
                if not _is_memento_hook(hook):
                    merged_hooks.append(hook)
                elif hook.get("command") == command and not has_current:
                    merged_hooks.append(hook)
                    has_current = True
        if not has_current:
            merged_hooks.append({"type": "command", "command": command})
        primary["hooks"] = merged_hooks
        entries[:] = [
            entry for entry in entries if entry is primary or entry not in matching
        ]
    after = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)
    return before != after


def install_claude_pending_hooks() -> tuple[Path, bool]:
    """Idempotently install hooks that call back into this collector package."""
    settings_path = _settings_path()
    if not settings_path.parent.is_dir():
        return settings_path, False
    settings = _read_mapping(settings_path) if settings_path.exists() else {}
    if settings_path.exists() and not settings:
        try:
            decoded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot merge invalid JSON in {settings_path}") from exc
        if not isinstance(decoded, dict):
            raise TypeError(f"Cannot merge non-object settings in {settings_path}")
        settings = decoded
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("settings hooks must be an object")

    command = _hook_command()
    changed = False
    for event_name in _EVENTS:
        if _merge_event_hook(hooks, event_name, command):
            changed = True
    if not changed:
        return settings_path, False

    previous_mode = None
    if settings_path.exists():
        previous_mode = stat.S_IMODE(settings_path.stat().st_mode)
    temporary = settings_path.with_name(
        f".{settings_path.name}.{os.getpid()}.memento.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        os.replace(temporary, settings_path)
    finally:
        temporary.unlink(missing_ok=True)
    return settings_path, True


def _hook_main() -> int:
    try:
        try:
            payload = json.load(sys.stdin)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        process_payload(payload)
    except Exception:  # noqa: BLE001, S110 -- hooks must never block Claude
        pass
    finally:
        print("{}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--install"]:
        try:
            path, changed = install_claude_pending_hooks()
        except (OSError, TypeError, ValueError) as exc:
            print(f"Memento Claude hook installation failed: {exc}", file=sys.stderr)
            return 1
        action = "Updated" if changed else "Already configured"
        print(f"{action}: {path}")
        return 0
    return _hook_main()


if __name__ == "__main__":
    raise SystemExit(main())
