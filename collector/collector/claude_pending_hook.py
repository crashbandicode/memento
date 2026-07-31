"""Capture Claude prompts before their transcript records are flushed."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HOOK_SPECS = {
    "PreToolUse": ("AskUserQuestion",),
    "PostToolUse": ("AskUserQuestion",),
    "PostToolUseFailure": ("AskUserQuestion",),
    "PermissionRequest": (".*",),
    "Elicitation": (".*",),
    "ElicitationResult": (".*",),
    "Notification": ("agent_needs_input", "agent_completed"),
}
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


def _normalized_tool_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().casefold())


def _question_input(value: object) -> dict[str, Any] | None:
    """Return a narrowly validated AskUserQuestion input mapping."""
    if not isinstance(value, dict):
        return None
    questions = value.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    if not any(
        isinstance(question, dict)
        and bool(str(
            question.get("prompt") or question.get("question") or ""
        ).strip())
        for question in questions
    ):
        return None
    return value


def _existing_question_input(record: dict[str, Any]) -> dict[str, Any] | None:
    raw_input = record.get("interaction_input")
    if _normalized_tool_name(record.get("question_tool")) == "askuserquestion":
        return _question_input(raw_input)
    if (
        _normalized_tool_name(record.get("question_tool")) == "permissionrequest"
        and isinstance(raw_input, dict)
        and _normalized_tool_name(
            raw_input.get("requested_tool") or raw_input.get("tool_name")
        ) == "askuserquestion"
    ):
        return _question_input(raw_input.get("tool_input"))
    return None


def _same_question(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def identity(value: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "id": str(
                    question.get("id")
                    or question.get("header")
                    or question.get("prompt")
                    or question.get("question")
                    or ""
                ).strip(),
                "header": str(question.get("header") or "").strip(),
                "prompt": str(
                    question.get("prompt") or question.get("question") or ""
                ).strip(),
            }
            for question in value.get("questions", [])
            if isinstance(question, dict)
        ]

    return identity(left) == identity(right)


def _richer_question_input(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    existing_size = len(json.dumps(existing, ensure_ascii=False, default=str))
    incoming_size = len(json.dumps(incoming, ensure_ascii=False, default=str))
    return incoming if incoming_size > existing_size else existing


def _interaction_id(payload: dict[str, Any]) -> str:
    for field in ("tool_use_id", "toolUseId", "tool_call_id", "id"):
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return ""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_timestamp(payload: dict[str, Any]) -> str:
    value = str(payload.get("timestamp") or "").strip()
    return value[:128] if value else _timestamp()


def _synthetic_interaction_id(
    kind: str,
    session_id: str,
    *parts: object,
) -> str:
    serialized = json.dumps(
        [kind, session_id, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
    return f"memento-{kind}-{digest}"


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
    """Update one pending-interaction side file, ignoring malformed payloads."""
    if not isinstance(payload, dict):
        return

    event_name = _event_name(payload)
    tool_name = _tool_name(payload)
    notification_type = str(payload.get("notification_type") or "").casefold()
    is_question_tool = _normalized_tool_name(tool_name) == "askuserquestion"
    is_question_event = (
        is_question_tool
        and event_name in {"pretooluse", "posttooluse", "posttoolusefailure"}
    )
    is_wrapped_question = (
        event_name == "permissionrequest"
        and is_question_tool
        and _question_input(payload.get("tool_input")) is not None
    )
    is_question = is_question_event or is_wrapped_question
    is_permission = (
        event_name == "permissionrequest"
        and bool(tool_name)
        and not is_wrapped_question
        and not is_question_tool
    )
    is_elicitation = event_name in {"elicitation", "elicitationresult"}
    is_agent_notification = (
        event_name == "notification"
        and notification_type in {"agent_needs_input", "agent_completed"}
    )
    if not (
        is_question
        or is_permission
        or is_elicitation
        or is_agent_notification
    ):
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
    existing_id = str(existing.get("interaction_id") or "").strip()

    if is_question:
        incoming_id = _interaction_id(payload)
        incoming_input = _question_input(payload.get("tool_input"))
        existing_input = _existing_question_input(existing)
        same_as_existing = (
            incoming_input is not None
            and existing_input is not None
            and _same_question(existing_input, incoming_input)
        )
        existing_aliases = existing.get("interaction_alias_ids")
        aliases = {
            str(value).strip()
            for value in existing_aliases
            if str(value).strip()
        } if isinstance(existing_aliases, list) else set()

        if event_name in {"posttooluse", "posttoolusefailure"}:
            if not existing_id:
                return
            if (
                incoming_id
                and incoming_id != existing_id
                and incoming_id not in aliases
                and not same_as_existing
            ):
                return
            interaction_id = existing_id
        elif existing_id and existing_input is not None and same_as_existing:
            interaction_id = existing_id
        else:
            interaction_id = incoming_id
            if (
                not interaction_id
                and is_wrapped_question
                and incoming_input is not None
            ):
                interaction_id = _synthetic_interaction_id(
                    "question",
                    session_id,
                    incoming_input.get("questions"),
                )
        if not interaction_id or (incoming_input is None and existing_input is None):
            return
        if incoming_id and incoming_id != interaction_id:
            aliases.add(incoming_id)
        if same_as_existing:
            raw_input = _richer_question_input(existing_input, incoming_input)
        elif event_name in {"posttooluse", "posttoolusefailure"}:
            raw_input = existing_input
        else:
            raw_input = incoming_input
        if raw_input is None:
            return
        interaction_alias_ids = sorted(aliases)[:16]
        question_tool = "AskUserQuestion"
        status = {
            "pretooluse": "pending",
            "posttooluse": "answered",
            "posttoolusefailure": "cancelled",
            "permissionrequest": "pending",
        }[event_name]
    elif is_permission:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        interaction_id = _interaction_id(payload) or _synthetic_interaction_id(
            "permission",
            session_id,
            tool_name,
            tool_input,
        )
        raw_input = {
            "interaction_type": "permission_request",
            "requested_tool": tool_name,
            "tool_input": tool_input,
            "permission_mode": payload.get("permission_mode"),
            "permission_suggestions": payload.get("permission_suggestions"),
        }
        question_tool = "PermissionRequest"
        status = "pending"
        interaction_alias_ids = []
    elif is_elicitation:
        interaction_id = (
            str(payload.get("elicitation_id") or "").strip()
            or existing_id
        )
        if event_name == "elicitation":
            interaction_id = interaction_id or _synthetic_interaction_id(
                "elicitation",
                session_id,
                payload.get("mcp_server_name"),
                payload.get("message"),
                payload.get("requested_schema"),
            )
            raw_input = {
                "interaction_type": "elicitation",
                "mcp_server_name": payload.get("mcp_server_name"),
                "message": payload.get("message"),
                "mode": payload.get("mode"),
                "url": payload.get("url"),
                "requested_schema": payload.get("requested_schema"),
            }
            status = "pending"
        else:
            if not interaction_id or (
                existing_id and interaction_id != existing_id
            ):
                return
            raw_input = existing.get("interaction_input")
            if not isinstance(raw_input, dict):
                raw_input = {}
            action = str(payload.get("action") or "").casefold()
            status = "answered" if action == "accept" else "cancelled"
        question_tool = "Elicitation"
        interaction_alias_ids = []
    else:
        if notification_type == "agent_needs_input":
            raw_input = {
                "interaction_type": "agent_needs_input",
                "title": payload.get("title"),
                "message": payload.get("message"),
            }
            interaction_id = _synthetic_interaction_id(
                "agent-input",
                session_id,
                raw_input,
            )
            question_tool = "NotificationPrompt"
            status = "pending"
        else:
            existing_input = existing.get("interaction_input")
            if (
                not existing_id
                or not isinstance(existing_input, dict)
                or existing_input.get("interaction_type") != "agent_needs_input"
            ):
                return
            interaction_id = existing_id
            raw_input = existing_input
            question_tool = str(
                existing.get("question_tool") or "NotificationPrompt"
            )
            status = "answered"
        interaction_alias_ids = []

    record = {
        "session_id": session_id,
        "transcript_path": str(
            payload.get("transcript_path") or existing.get("transcript_path") or ""
        ),
        "interaction_id": interaction_id,
        "question_tool": question_tool,
        "interaction_input": raw_input,
        "interaction_status": status,
        "timestamp": _event_timestamp(payload),
        "cwd": str(payload.get("cwd") or existing.get("cwd") or ""),
    }
    if interaction_alias_ids:
        record["interaction_alias_ids"] = interaction_alias_ids
    _write_atomic(path, record)


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


def _merge_event_hooks(
    hooks: dict[str, Any],
    event_name: str,
    matchers: tuple[str, ...],
    command: str,
) -> bool:
    entries = hooks.setdefault(event_name, [])
    if not isinstance(entries, list):
        raise TypeError(f"hooks.{event_name} must be a list")
    before = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)

    cleaned_entries: list[object] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            cleaned_entries.append(entry)
            continue
        entry_hooks = [
            hook for hook in entry["hooks"] if not _is_memento_hook(hook)
        ]
        if entry_hooks:
            entry["hooks"] = entry_hooks
            cleaned_entries.append(entry)
    entries[:] = cleaned_entries

    for matcher in matchers:
        target = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict)
                and str(entry.get("matcher") or "").casefold()
                == matcher.casefold()
                and isinstance(entry.get("hooks"), list)
            ),
            None,
        )
        if target is None:
            target = {"matcher": matcher, "hooks": []}
            entries.append(target)
        target["hooks"].append({"type": "command", "command": command})

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
    for event_name, matchers in _HOOK_SPECS.items():
        if _merge_event_hooks(hooks, event_name, matchers, command):
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
