"""Capture Claude prompts before their transcript records are flushed."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import orjson

from .handoff_governor_hook import governor_enabled

_HOOK_SPECS = {
    "PreToolUse": ("AskUserQuestion", "Bash", "PowerShell", "Shell"),
    "PostToolUse": ("AskUserQuestion", "Bash", "PowerShell", "Shell"),
    "PostToolUseFailure": (
        "AskUserQuestion",
        "Bash",
        "PowerShell",
        "Shell",
    ),
    "PermissionRequest": (".*",),
    "Elicitation": (".*",),
    "ElicitationResult": (".*",),
    "Notification": ("agent_needs_input", "agent_completed"),
    "SessionEnd": (".*",),
    "SessionStart": (".*",),
}
_SHELL_TOOLS = {"bash", "powershell", "shell"}
_RESOLVED_INTERACTION_LIMIT = 16
_TRANSCRIPT_TAIL_BYTES = 512 * 1024
_TRANSCRIPT_TAIL_RECORD_LIMIT = 2_048
_CANONICAL_MATCH_INPUT_BYTES = 64 * 1024
_ORIGIN_TRANSCRIPT_PATH_LIMIT = 2_048
_SESSION_LOCK_TIMEOUT_SECONDS = 2.0
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_MEMENTO_HOOK_MARKERS = (
    "pending_question_hook.py",
    "collector.claude_pending_hook",
    " claude-hook",
)
_GOVERNOR_HOOK_SPECS = {
    "PostToolUse": ("*",),
}
_GOVERNOR_HOOK_EVENTS = ("PostToolUse", "Stop")
_MEMENTO_GOVERNOR_HOOK_MARKERS = (
    "collector.handoff_governor_hook",
    " claude-governor-hook",
)
_HOOK_TIMEOUT_SECONDS = 10
_HOOK_RUNNER_NAME = "memento-hook-runner"
_HOOK_RUNNER_RETIREMENT_MARKER = "retired-at.json"
_HOOK_RUNNER_RETENTION_HOURS_ENV = "MEMENTO_HOOK_RUNNER_RETENTION_HOURS"
_WINDOWS_COMMAND_METACHARACTERS = frozenset('"&|<>^()%!')


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


def _shell_command(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("command", "cmd", "script"):
        command = value.get(key)
        if isinstance(command, list):
            text = " ".join(str(part) for part in command)
        elif command is not None:
            text = str(command)
        else:
            continue
        if text.strip():
            return text.strip()[:16_000]
    return ""


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


def _canonical_json(
    value: object,
    *,
    max_bytes: int | None = None,
) -> str | None:
    """Return bounded canonical JSON for an exact structured comparison."""
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        chunks: list[str] = []
        size = 0
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if max_bytes is not None and size > max_bytes:
                return None
            chunks.append(chunk)
        return "".join(chunks)
    except (TypeError, ValueError):
        return None


def _permission_fingerprint(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Hash the versioned, canonical request identity (never time or an anchor)."""
    # The prefix is part of the public v1 origin contract, so this digest can
    # never collide with a generic JSON hash or a later fingerprint format.
    prefix = b"memento.claude.permission-origin.v1\x00"
    digest = hashlib.sha256()
    digest.update(prefix)
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for chunk in encoder.iterencode({
            "tool_name": tool_name,
            "tool_input": tool_input,
        }):
            digest.update(chunk.encode("utf-8"))
    except (TypeError, ValueError):
        # A non-finite/non-JSON hook payload can never be an exact source
        # match. Keep the fallback deterministic without serializing it using
        # Python's non-standard NaN/Infinity tokens.
        return hashlib.sha256(prefix + b"invalid-canonical-json").hexdigest()
    return digest.hexdigest()


def _claude_root() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _safe_transcript_identity(
    transcript_path: object,
    session_id: str,
) -> tuple[Path, str, bool] | None:
    """Return only the owning main/session-nested subagent JSONL transcript."""
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return None
    try:
        candidate = Path(transcript_path).expanduser().resolve(strict=False)
        projects_root = (_claude_root() / "projects").resolve(strict=False)
        relative = candidate.relative_to(projects_root)
    except (OSError, RuntimeError, ValueError):
        return None
    parts = relative.parts
    if candidate.suffix.casefold() != ".jsonl" or len(parts) < 2:
        return None
    expected_name = f"{session_id}.jsonl"
    is_main_transcript = len(parts) == 2 and candidate.name == expected_name
    is_subagent_transcript = (
        len(parts) >= 4
        and parts[1] == session_id
        and _is_nested_subagent_path(parts[2:])
    )
    relative_path = (Path("projects") / relative).as_posix()
    if (
        not (is_main_transcript or is_subagent_transcript)
        or len(relative_path) > _ORIGIN_TRANSCRIPT_PATH_LIMIT
    ):
        return None
    return (
        candidate,
        relative_path,
        is_subagent_transcript,
    )


def _is_nested_subagent_path(parts: tuple[str, ...]) -> bool:
    """Accept Claude's repeating ``subagents/<child>/`` transcript layout."""
    index = 0
    while index < len(parts):
        if parts[index] != "subagents":
            return False
        index += 1
        if index >= len(parts):
            return False
        child_or_transcript = parts[index]
        if index == len(parts) - 1:
            return Path(child_or_transcript).suffix.casefold() == ".jsonl"
        index += 1
    return False


def _session_end_marker(path: Path) -> Path:
    return path.with_name(f".{path.stem}.session-ended")


def _write_session_end_marker(path: Path, payload: dict[str, Any]) -> None:
    """Publish terminal intent before waiting for the side-file lock."""
    marker = _session_end_marker(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": _event_timestamp(payload),
        "reason": str(
            payload.get("reason") or payload.get("session_end_reason") or ""
        )[:256],
    }
    _write_atomic(marker, record)


def _read_session_end_marker(path: Path) -> dict[str, Any]:
    return _read_mapping(_session_end_marker(path))


def _clear_session_end_marker(path: Path) -> None:
    _session_end_marker(path).unlink(missing_ok=True)


def _mark_session_started(path: Path, payload: dict[str, Any]) -> None:
    """Advance the side-file generation without reopening a terminal prompt."""
    existing = _read_mapping(path)
    if not existing:
        return
    previous = existing.get("session_start_generation")
    try:
        generation = max(0, int(previous)) + 1
    except (TypeError, ValueError):
        generation = 1
    record = dict(existing)
    record["session_start_generation"] = generation
    record["session_start_timestamp"] = _event_timestamp(payload)
    _write_atomic(path, record)


@contextmanager
def _session_file_lock(
    path: Path,
    *,
    timeout: float | None = _SESSION_LOCK_TIMEOUT_SECONDS,
) -> Any:
    """Serialize side-file mutation across independent Claude hook processes."""
    lock_path = path.with_name(f".{path.stem}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + timeout if timeout is not None else None
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("Claude side-file lock timed out")
                    time.sleep(0.01)
            unlock = lambda: msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("Claude side-file lock timed out")
                    time.sleep(0.01)
            unlock = lambda: fcntl.flock(descriptor, fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()
    finally:
        os.close(descriptor)


def _origin_value(value: object, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _hook_only_origin(
    *,
    fingerprint: str,
    relative_path: str | None,
) -> dict[str, Any]:
    """Build the bounded v1 fallback when no exact transcript source exists."""
    origin: dict[str, Any] = {
        "version": 1,
        "kind": "hook_only",
        "record_uuid": "",
        "parent_uuid": "",
        "tool_use_id": "",
        "fingerprint": fingerprint,
        "agent_id": "",
        "is_sidechain": False,
    }
    if relative_path:
        origin["transcript_path"] = relative_path
    return origin


def _message_tool_uses(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        part
        for part in content
        if isinstance(part, dict)
        and str(part.get("type") or "").casefold() in {"tool_use", "toolcall"}
    ]


def _is_assistant_record(record: dict[str, Any]) -> bool:
    """Accept Claude assistant rows, never tool-shaped user/system rows."""
    record_type = str(record.get("type") or "").casefold()
    message = record.get("message")
    message_role = (
        str(message.get("role") or "").casefold()
        if isinstance(message, dict)
        else ""
    )
    return record_type == "assistant" or (
        not record_type and message_role == "assistant"
    )


def _permission_interaction_origin(
    *,
    transcript_path: object,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the latest exact Claude tool-use in a bounded transcript tail.

    A match requires both the literal requested tool name and canonical JSON of
    the structured input to agree.  Deliberately do not normalize, substring,
    or otherwise fuzzy-match either field: an incorrect UUID is worse than a
    hook-only origin.
    """
    fingerprint = _permission_fingerprint(tool_name, tool_input)
    identity = _safe_transcript_identity(transcript_path, session_id)
    if identity is None:
        return _hook_only_origin(
            fingerprint=fingerprint,
            relative_path=None,
        )
    transcript, relative_path, is_subagent_path = identity
    requested_input = _canonical_json(
        tool_input,
        max_bytes=_CANONICAL_MATCH_INPUT_BYTES,
    )
    if requested_input is None:
        return _hook_only_origin(
            fingerprint=fingerprint,
            relative_path=relative_path,
        )
    try:
        with transcript.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            payload = stream.read(_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return _hook_only_origin(
            fingerprint=fingerprint,
            relative_path=relative_path,
        )
    if start:
        newline = payload.find(b"\n")
        if newline < 0:
            return _hook_only_origin(
                fingerprint=fingerprint,
                relative_path=relative_path,
            )
        payload = payload[newline + 1:]

    # Reverse scan gives the latest causal record and the last matching part in
    # that record, without reading the unbounded transcript history.
    for raw_line in reversed(payload.splitlines()[-_TRANSCRIPT_TAIL_RECORD_LIMIT:]):
        try:
            record = orjson.loads(raw_line)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if not _is_assistant_record(record):
            continue
        for part in reversed(_message_tool_uses(record)):
            if str(part.get("name") or "") != tool_name:
                continue
            source_input = part.get("input")
            if not isinstance(source_input, dict):
                continue
            if _canonical_json(
                source_input,
                max_bytes=_CANONICAL_MATCH_INPUT_BYTES,
            ) != requested_input:
                continue
            agent_id = _origin_value(
                record.get("agentId") or record.get("agent_id")
            )
            is_sidechain = record.get("isSidechain") is True
            is_subagent = (
                is_subagent_path
                or transcript.name.casefold().startswith("agent-")
                or is_sidechain
                or bool(agent_id)
            )
            return {
                "version": 1,
                "kind": (
                    "claude_subagent_record"
                    if is_subagent
                    else "claude_record"
                ),
                "record_uuid": _origin_value(
                    record.get("uuid") or record.get("record_uuid")
                ),
                "parent_uuid": _origin_value(
                    record.get("parentUuid") or record.get("parent_uuid")
                ),
                "tool_use_id": _origin_value(
                    part.get("id") or part.get("tool_use_id")
                ),
                "fingerprint": fingerprint,
                "agent_id": agent_id,
                "is_sidechain": is_sidechain,
                "transcript_path": relative_path,
            }
    return _hook_only_origin(
        fingerprint=fingerprint,
        relative_path=relative_path,
    )


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = orjson.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, orjson.JSONDecodeError):
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


def _resolved_interactions(
    existing: dict[str, Any],
    replacing_interaction_id: str,
    resolved_at: str,
) -> list[dict[str, Any]]:
    """Preserve terminal updates when a newer prompt replaces the side file."""
    raw_resolved = existing.get("resolved_interactions")
    resolved = [
        dict(item)
        for item in raw_resolved
        if isinstance(item, dict)
    ] if isinstance(raw_resolved, list) else []

    existing_id = str(existing.get("interaction_id") or "").strip()
    existing_input = existing.get("interaction_input")
    existing_tool = str(existing.get("question_tool") or "").strip()
    if (
        existing_id
        and existing_id != replacing_interaction_id
        and isinstance(existing_input, dict)
        and existing_tool
    ):
        existing_status = str(
            existing.get("interaction_status") or ""
        ).strip().casefold()
        terminal_status = (
            existing_status
            if existing_status in {"answered", "cancelled"}
            else "answered"
        )
        resolved = [
            item
            for item in resolved
            if str(item.get("interaction_id") or "").strip() != existing_id
        ]
        resolved_record = {
            "interaction_id": existing_id,
            "question_tool": existing_tool,
            "interaction_input": existing_input,
            "interaction_status": terminal_status,
            "timestamp": str(
                existing.get("interaction_timestamp")
                or existing.get("timestamp")
                or ""
            ),
            "resolved_at": resolved_at,
        }
        existing_response = existing.get("interaction_response")
        if isinstance(existing_response, dict):
            resolved_record["interaction_response"] = existing_response
        existing_origin = existing.get("interaction_origin")
        if isinstance(existing_origin, dict):
            resolved_record["interaction_origin"] = existing_origin
        resolved.append(resolved_record)
    return resolved[-_RESOLVED_INTERACTION_LIMIT:]


def _read_stdin_payload(stream: object | None = None) -> object:
    """Decode Claude's hook pipe as UTF-8, independent of Windows ANSI locale."""
    input_stream = sys.stdin if stream is None else stream
    binary_stream = getattr(input_stream, "buffer", None)
    if binary_stream is not None:
        raw_payload = binary_stream.read()
        if isinstance(raw_payload, bytes):
            text = raw_payload.decode("utf-8-sig")
        else:
            text = str(raw_payload)
    else:
        reader = getattr(input_stream, "read", None)
        if not callable(reader):
            raise TypeError("Hook input stream is not readable")
        raw_payload = reader()
        text = (
            raw_payload.decode("utf-8-sig")
            if isinstance(raw_payload, bytes)
            else str(raw_payload)
        )
    return orjson.loads(text)


def _process_payload_unlocked(payload: object) -> None:
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
    is_session_end = event_name == "sessionend"
    is_shell_event = (
        event_name in {"pretooluse", "posttooluse", "posttoolusefailure"}
        and _normalized_tool_name(tool_name) in _SHELL_TOOLS
    )
    if not (
        is_question
        or is_permission
        or is_elicitation
        or is_agent_notification
        or is_shell_event
        or is_session_end
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

    if is_session_end:
        if (
            str(existing.get("interaction_status") or "").casefold()
            != "pending"
            or _normalized_tool_name(existing.get("question_tool"))
            != "permissionrequest"
        ):
            return
        record = dict(existing)
        record["interaction_status"] = "cancelled"
        marker = _read_session_end_marker(path)
        record["session_end_timestamp"] = str(
            marker.get("timestamp") or _event_timestamp(payload)
        )[:128]
        reason = str(marker.get("reason") or "")[:256]
        if reason:
            record["session_end_reason"] = reason
        _write_atomic(path, record)
        return

    if is_shell_event:
        activity_id = _interaction_id(payload)
        if not activity_id:
            return
        raw_activities = existing.get("shell_activities")
        activities = (
            {
                str(key): value
                for key, value in raw_activities.items()
                if isinstance(value, dict)
            }
            if isinstance(raw_activities, dict)
            else {}
        )
        previous = activities.get(activity_id)
        if event_name != "pretooluse" and not isinstance(previous, dict):
            return
        event_timestamp = _event_timestamp(payload)
        command = _shell_command(payload.get("tool_input"))
        if not command and isinstance(previous, dict):
            command = str(previous.get("command") or "")
        if event_name == "pretooluse" and not command:
            return
        status = {
            "pretooluse": "running",
            "posttooluse": "completed",
            "posttoolusefailure": "failed",
        }[event_name]
        activity = {
            "id": activity_id,
            "tool_name": tool_name,
            "command": command,
            "status": status,
            "started_at": (
                str(previous.get("started_at") or "")
                if isinstance(previous, dict)
                else ""
            ) or event_timestamp,
            "updated_at": event_timestamp,
        }
        activities.pop(activity_id, None)
        activities[activity_id] = activity
        record = dict(existing)
        record.update({
            "session_id": session_id,
            "transcript_path": str(
                payload.get("transcript_path")
                or existing.get("transcript_path")
                or ""
            ),
            "cwd": str(payload.get("cwd") or existing.get("cwd") or ""),
            "shell_activities": dict(list(activities.items())[-32:]),
        })
        existing_permission = existing.get("interaction_input")
        requested_tool = (
            str(existing_permission.get("requested_tool") or "")
            if isinstance(existing_permission, dict)
            else ""
        )
        requested_input = (
            existing_permission.get("tool_input")
            if isinstance(existing_permission, dict)
            else None
        )
        if (
            str(existing.get("interaction_status") or "").casefold()
            == "pending"
            and _normalized_tool_name(existing.get("question_tool"))
            == "permissionrequest"
            and requested_tool == tool_name
            and isinstance(requested_input, dict)
            and _canonical_json(
                requested_input,
                max_bytes=_CANONICAL_MATCH_INPUT_BYTES,
            )
            == _canonical_json(
                payload.get("tool_input"),
                max_bytes=_CANONICAL_MATCH_INPUT_BYTES,
            )
        ):
            interaction_id = str(existing.get("interaction_id") or "").strip()
            if interaction_id:
                record["interaction_status"] = "answered"
                record["interaction_response"] = {
                    "kind": "question_response",
                    "interaction_id": interaction_id,
                    "status": "answered",
                    "answers": [{
                        "question_id": "permission-decision",
                        "text": "Yes",
                        "selected_option_ids": ["allow"],
                    }],
                    "raw_text": "Yes",
                }
        _write_atomic(path, record)
        return

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
        session_generation = existing.get("session_start_generation")
        try:
            session_generation = max(0, int(session_generation))
        except (TypeError, ValueError):
            session_generation = 0
        interaction_id = _interaction_id(payload)
        if not interaction_id:
            interaction_id = _synthetic_interaction_id(
                "permission",
                session_id,
                tool_name,
                tool_input,
                *([session_generation] if session_generation else []),
            )
        elif (
            session_generation
            and interaction_id == existing_id
            and str(existing.get("interaction_status") or "").casefold()
            in {"answered", "cancelled"}
        ):
            interaction_id = _synthetic_interaction_id(
                "permission-resumed",
                session_id,
                interaction_id,
                tool_name,
                tool_input,
                session_generation,
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
        interaction_origin = _permission_interaction_origin(
            transcript_path=payload.get("transcript_path"),
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        existing_origin = existing.get("interaction_origin")
        if (
            interaction_origin.get("kind") == "hook_only"
            and existing_id == interaction_id
            and isinstance(existing_origin, dict)
            and existing_origin.get("version") == 1
            and existing_origin.get("kind")
            in {"claude_record", "claude_subagent_record"}
            and existing_origin.get("fingerprint")
            == interaction_origin.get("fingerprint")
        ):
            interaction_origin = existing_origin
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

    event_timestamp = _event_timestamp(payload)
    interaction_timestamp = event_timestamp
    if existing_id == interaction_id:
        interaction_timestamp = str(
            existing.get("interaction_timestamp")
            or existing.get("timestamp")
            or event_timestamp
        )
    record = {
        "session_id": session_id,
        "transcript_path": str(
            payload.get("transcript_path") or existing.get("transcript_path") or ""
        ),
        "interaction_id": interaction_id,
        "question_tool": question_tool,
        "interaction_input": raw_input,
        "interaction_status": status,
        "timestamp": interaction_timestamp,
        "interaction_timestamp": interaction_timestamp,
        "cwd": str(payload.get("cwd") or existing.get("cwd") or ""),
    }
    if is_permission:
        record["interaction_origin"] = interaction_origin
    session_generation = existing.get("session_start_generation")
    if isinstance(session_generation, int) and session_generation > 0:
        record["session_start_generation"] = session_generation
    resolved_interactions = _resolved_interactions(
        existing,
        interaction_id,
        event_timestamp,
    )
    if resolved_interactions:
        record["resolved_interactions"] = resolved_interactions
    if interaction_alias_ids:
        record["interaction_alias_ids"] = interaction_alias_ids
    if isinstance(existing.get("shell_activities"), dict):
        record["shell_activities"] = existing["shell_activities"]
    _write_atomic(path, record)


def process_payload(payload: object) -> None:
    """Serialize per-session hook updates and make SessionEnd terminal."""
    if not isinstance(payload, dict):
        return
    session_id = str(payload.get("session_id") or "").strip()
    if (
        not session_id
        or session_id in {".", ".."}
        or not _SAFE_SESSION_ID.fullmatch(session_id)
    ):
        return
    path = _pending_directory() / f"{session_id}.json"
    is_session_end = _event_name(payload) == "sessionend"
    is_session_start = _event_name(payload) == "sessionstart"
    try:
        if is_session_end:
            # Publish before the lock: a PermissionRequest that wins the lock
            # race still observes this marker before it can write pending.
            _write_session_end_marker(path, payload)
        with _session_file_lock(
            path,
            timeout=None if is_session_end else _SESSION_LOCK_TIMEOUT_SECONDS,
        ):
            if is_session_start:
                _clear_session_end_marker(path)
                _mark_session_started(path, payload)
                return
            if not is_session_end and _read_session_end_marker(path):
                return
            _process_payload_unlocked(payload)
    except (OSError, TimeoutError):
        # Hook failures must never block Claude. A SessionEnd marker remains
        # durable even if an individual side-file write cannot complete.
        return


def _settings_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return root / "settings.json"


def _hook_runner_filename() -> str:
    return f"{_HOOK_RUNNER_NAME}.exe" if os.name == "nt" else _HOOK_RUNNER_NAME


def _hook_runner_is_complete(directory: Path) -> bool:
    """Return whether an onedir hook-runner directory is executable."""

    return (
        (directory / _hook_runner_filename()).is_file()
        and (directory / "_internal").is_dir()
    )


def _bundled_hook_runner_directory() -> Path | None:
    """Locate the onedir runner packaged beside a frozen desktop sidecar."""

    configured = os.environ.get("MEMENTO_HOOK_RUNNER_SOURCE", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    if getattr(sys, "frozen", False):
        executable_directory = Path(sys.executable).resolve().parent
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if os.name == "nt":
            # Tauri's Windows resource directory is the app executable's
            # parent. The frozen collector sidecar is installed there too.
            candidates.append(
                executable_directory / "binaries" / _HOOK_RUNNER_NAME
            )
            # A fleet can hand-swap the sidecar under LocalAppData instead of
            # replacing a complete app bundle. Probe that known sidecar
            # location separately in case sys.executable still points at an
            # older app-bundle copy during a restart.
            if local_app_data:
                manual_sidecar = (
                    Path(local_app_data)
                    / "Memento"
                    / "memento-collector-sidecar.exe"
                )
                candidates.append(
                    manual_sidecar.parent
                    / "binaries"
                    / _HOOK_RUNNER_NAME
                )
        candidates.extend(
            (
                # Existing direct-build and non-Windows resource layouts.
                executable_directory
                / "resources"
                / "binaries"
                / _HOOK_RUNNER_NAME,
                # These fallbacks support direct PyInstaller builds and the
                # macOS resource layout without making the runtime depend on
                # Tauri internals.
                executable_directory / _HOOK_RUNNER_NAME,
                executable_directory
                / "Resources"
                / "binaries"
                / _HOOK_RUNNER_NAME,
            )
        )
    return next(
        (candidate for candidate in candidates if _hook_runner_is_complete(candidate)),
        None,
    )


def _hook_runner_install_directory() -> Path:
    """Return this immutable collector version's local hook-runner directory."""

    from ._version import __version__

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "Memento" / "hooks" / __version__


def _hook_runner_retirement_marker(directory: Path) -> Path:
    return directory / _HOOK_RUNNER_RETIREMENT_MARKER


def _hook_runner_retention_age() -> timedelta | None:
    raw_hours = os.environ.get(_HOOK_RUNNER_RETENTION_HOURS_ENV, "").strip()
    if not raw_hours:
        return None
    try:
        hours = float(raw_hours)
    except ValueError:
        return None
    if not math.isfinite(hours) or hours <= 0:
        return None
    try:
        return timedelta(hours=hours)
    except OverflowError:
        return None


def _write_hook_runner_retirement_marker(
    directory: Path,
    *,
    retired_at: datetime | None = None,
) -> bool:
    """Durably mark an unregistered version without touching its runner files."""

    marker = _hook_runner_retirement_marker(directory)
    if marker.exists():
        return True
    timestamp = retired_at or datetime.now(timezone.utc)
    payload = {
        "retired_at": timestamp.astimezone(timezone.utc).isoformat(),
        "retiring_collector_version": directory.name,
    }
    try:
        with marker.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        return True
    except OSError:
        return False
    return True


def _hook_runner_retirement_age(marker: Path) -> timedelta | None:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        retired_at = payload.get("retired_at") if isinstance(payload, dict) else None
        if not isinstance(retired_at, str):
            return None
        timestamp = datetime.fromisoformat(retired_at)
        if timestamp.tzinfo is None:
            return None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)


def _hook_runner_version_directories(root: Path) -> list[Path]:
    """Return installed or previously-retired runner version directories."""

    try:
        return [
            directory
            for directory in root.iterdir()
            if (
                directory.is_dir()
                and not directory.name.startswith(".")
                and (
                    (directory / _hook_runner_filename()).is_file()
                    or _hook_runner_retirement_marker(directory).is_file()
                )
            )
        ]
    except OSError:
        return []


def _mark_unregistered_hook_runner_versions(
    root: Path,
    current: Path,
    registered_directories: set[Path],
) -> None:
    """Mark all non-current, unregistered runner versions as retired."""

    protected = {current, *registered_directories}
    for directory in _hook_runner_version_directories(root):
        if directory not in protected:
            _write_hook_runner_retirement_marker(directory)


def _sweep_retired_hook_runner_versions(
    root: Path,
    current: Path,
    registered_directories: set[Path],
) -> None:
    """Delete aged retired versions only after removing their executable first."""

    protected = {current, *registered_directories}
    retention_age = _hook_runner_retention_age()
    for directory in _hook_runner_version_directories(root):
        if directory in protected:
            continue
        marker = _hook_runner_retirement_marker(directory)
        if not marker.is_file():
            _write_hook_runner_retirement_marker(directory)
            continue
        if retention_age is None:
            continue
        retired_for = _hook_runner_retirement_age(marker)
        if retired_for is None or retired_for < retention_age:
            continue

        executable = directory / _hook_runner_filename()
        if executable.exists():
            try:
                # This is deliberately the first mutation. Windows cannot
                # remove a live process image, so failure leaves every file in
                # the directory untouched for a later daemon startup.
                os.remove(executable)
            except OSError:
                continue
        try:
            import shutil

            shutil.rmtree(directory)
        except OSError:
            # The executable is already gone, so this directory cannot launch
            # again; residual files are safe to retry on a later daemon start.
            continue


def _maintain_hook_runner_versions(
    hook_runner: Path,
    *,
    sweep_retired: bool,
) -> None:
    """Mark obsolete versions; only the collector daemon may sweep them."""

    current = hook_runner.parent
    root = current.parent
    registered_directories = {current}
    _mark_unregistered_hook_runner_versions(
        root,
        current,
        registered_directories,
    )
    if sweep_retired:
        _sweep_retired_hook_runner_versions(
            root,
            current,
            registered_directories,
        )


def _is_lost_hook_runner_install_race(error: OSError) -> bool:
    """Recognize Windows' two observed directory-exists error forms."""

    return getattr(error, "winerror", None) in {5, 183} or (
        isinstance(error, (FileExistsError, PermissionError))
        and error.errno in {5, 17}
    )


def _install_hook_runner() -> Path | None:
    """Install a versioned runner without replacing files a live hook may lock.

    A desktop collector update carries a fresh resource directory. We copy it
    into a previously unused versioned destination and only then repoint
    managed registrations. Retention runs after that reconciliation, using an
    atomic rename probe so a live old hook is never partly deleted.
    """

    if not (os.name == "nt" and getattr(sys, "frozen", False)):
        return None
    destination = _hook_runner_install_directory()
    executable = destination / _hook_runner_filename()
    if _hook_runner_is_complete(destination):
        return executable

    source = _bundled_hook_runner_directory()
    if source is None:
        return None

    import shutil

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging)
        if not _hook_runner_is_complete(staging):
            return None
        try:
            os.replace(staging, destination)
        except OSError as error:
            # Another collector process won the race.  Its complete immutable
            # destination is equivalent, so use it rather than replacing a
            # directory that could already have live hook processes.
            if not (
                _is_lost_hook_runner_install_race(error)
                and _hook_runner_is_complete(destination)
            ):
                raise
    except OSError:
        return None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    if not _hook_runner_is_complete(destination):
        return None
    return executable


def _hook_command(hook_runner: Path | None = None) -> str:
    if hook_runner is not None:
        executable = str(hook_runner.resolve()).replace('"', '\\"')
        return f'"{executable}" claude-hook'
    executable = str(Path(sys.executable).resolve()).replace('"', '\\"')
    if getattr(sys, "frozen", False):
        return f'"{executable}" claude-hook'
    return f'"{executable}" -m collector.claude_pending_hook'


def _hook_executable_token(executable: str, *, codex_windows: bool) -> str:
    """Format one executable token for the hook host's command shell.

    Codex 0.152 invokes Windows command hooks through ``cmd.exe`` and exits 1
    when a command string begins with a quoted executable token, even when the
    executable path itself contains no whitespace. Prefer the safe unquoted
    form for that exact host; retain quoting for Claude and for paths that need
    shell protection.
    """

    if codex_windows and not any(
        character.isspace() or character in _WINDOWS_COMMAND_METACHARACTERS
        for character in executable
    ):
        return executable
    escaped = executable.replace('"', '\\"')
    return f'"{escaped}"'


def _governor_hook_command(
    hook_runner: Path | None = None,
    *,
    codex_windows: bool = False,
) -> str:
    if hook_runner is not None:
        executable = _hook_executable_token(
            str(hook_runner.resolve()),
            codex_windows=codex_windows,
        )
        return f"{executable} claude-governor-hook --enabled"
    executable = _hook_executable_token(
        str(Path(sys.executable).resolve()),
        codex_windows=codex_windows,
    )
    if getattr(sys, "frozen", False):
        return f"{executable} claude-governor-hook --enabled"
    return f"{executable} -m collector.handoff_governor_hook --enabled"


def _is_memento_hook(hook: object) -> bool:
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = str(hook.get("command") or "")
    return any(marker in command for marker in _MEMENTO_HOOK_MARKERS)


def _is_memento_governor_hook(hook: object) -> bool:
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = str(hook.get("command") or "")
    return any(marker in command for marker in _MEMENTO_GOVERNOR_HOOK_MARKERS)


def _claude_governor_hook_is_registered() -> bool:
    """Return whether Cursor can import the managed global Claude governor."""

    settings = _read_mapping(_settings_path())
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get("PostToolUse")
    if not isinstance(entries, list):
        return False
    return any(
        _is_memento_governor_hook(hook)
        for entry in entries
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
    )


def _merge_event_hooks(
    hooks: dict[str, Any],
    event_name: str,
    matchers: tuple[str, ...],
    command: str,
    *,
    managed_hook: Callable[[object], bool] = _is_memento_hook,
    timeout: int = _HOOK_TIMEOUT_SECONDS,
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
            hook for hook in entry["hooks"] if not managed_hook(hook)
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
        target["hooks"].append({
            "type": "command",
            "command": command,
            "timeout": timeout,
        })

    after = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)
    return before != after


def _remove_event_hooks(
    hooks: dict[str, Any],
    event_name: str,
    *,
    managed_hook: Callable[[object], bool],
) -> bool:
    entries = hooks.get(event_name)
    if entries is None:
        return False
    if not isinstance(entries, list):
        raise TypeError(f"hooks.{event_name} must be a list")
    before = json.dumps(entries, ensure_ascii=False, sort_keys=True, default=str)
    cleaned_entries: list[object] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            cleaned_entries.append(entry)
            continue
        retained_hooks = [
            hook for hook in entry["hooks"] if not managed_hook(hook)
        ]
        if retained_hooks:
            entry["hooks"] = retained_hooks
            cleaned_entries.append(entry)
    if cleaned_entries:
        entries[:] = cleaned_entries
    else:
        hooks.pop(event_name)
    after = json.dumps(
        hooks.get(event_name, []),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return before != after


def install_claude_pending_hooks(
    *,
    sweep_retired: bool = False,
) -> tuple[Path, bool]:
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

    before_hooks = json.dumps(
        hooks,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    hook_runner = _install_hook_runner()
    command = _hook_command(hook_runner)
    for event_name, matchers in _HOOK_SPECS.items():
        _merge_event_hooks(hooks, event_name, matchers, command)
    if governor_enabled():
        governor_command = _governor_hook_command(hook_runner)
        for event_name, matchers in _GOVERNOR_HOOK_SPECS.items():
            _merge_event_hooks(
                hooks,
                event_name,
                matchers,
                governor_command,
                managed_hook=_is_memento_governor_hook,
            )
        for event_name in set(_GOVERNOR_HOOK_EVENTS) - set(_GOVERNOR_HOOK_SPECS):
            _remove_event_hooks(
                hooks,
                event_name,
                managed_hook=_is_memento_governor_hook,
            )
    else:
        for event_name in _GOVERNOR_HOOK_EVENTS:
            _remove_event_hooks(
                hooks,
                event_name,
                managed_hook=_is_memento_governor_hook,
            )
    changed = before_hooks != json.dumps(
        hooks,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if not changed:
        if hook_runner is not None:
            _maintain_hook_runner_versions(
                hook_runner,
                sweep_retired=sweep_retired,
            )
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
    if hook_runner is not None:
        _maintain_hook_runner_versions(
            hook_runner,
            sweep_retired=sweep_retired,
        )
    return settings_path, True


def _codex_hooks_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    root = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return root / "hooks.json"


def install_codex_governor_hooks(
    *,
    sweep_retired: bool = False,
) -> tuple[Path, bool]:
    """Idempotently install the shared governor in Codex's native hook file."""

    hooks_path = _codex_hooks_path()
    if not hooks_path.parent.is_dir():
        return hooks_path, False
    settings = _read_mapping(hooks_path) if hooks_path.exists() else {}
    if hooks_path.exists() and not settings:
        try:
            decoded = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot merge invalid JSON in {hooks_path}") from exc
        if not isinstance(decoded, dict):
            raise TypeError(f"Cannot merge non-object settings in {hooks_path}")
        settings = decoded
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("Codex hooks must be an object")

    before_hooks = json.dumps(
        hooks,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    hook_runner = _install_hook_runner() if governor_enabled() else None
    if governor_enabled():
        governor_command = _governor_hook_command(
            hook_runner,
            codex_windows=os.name == "nt",
        )
        for event_name, matchers in _GOVERNOR_HOOK_SPECS.items():
            _merge_event_hooks(
                hooks,
                event_name,
                matchers,
                governor_command,
                managed_hook=_is_memento_governor_hook,
            )
        for event_name in set(_GOVERNOR_HOOK_EVENTS) - set(_GOVERNOR_HOOK_SPECS):
            _remove_event_hooks(
                hooks,
                event_name,
                managed_hook=_is_memento_governor_hook,
            )
    else:
        for event_name in _GOVERNOR_HOOK_EVENTS:
            _remove_event_hooks(
                hooks,
                event_name,
                managed_hook=_is_memento_governor_hook,
            )
    changed = before_hooks != json.dumps(
        hooks,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if not changed:
        if hook_runner is not None:
            _maintain_hook_runner_versions(
                hook_runner,
                sweep_retired=sweep_retired,
            )
        return hooks_path, False

    previous_mode = None
    if hooks_path.exists():
        previous_mode = stat.S_IMODE(hooks_path.stat().st_mode)
    temporary = hooks_path.with_name(
        f".{hooks_path.name}.{os.getpid()}.memento.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        os.replace(temporary, hooks_path)
    finally:
        temporary.unlink(missing_ok=True)
    if hook_runner is not None:
        _maintain_hook_runner_versions(
            hook_runner,
            sweep_retired=sweep_retired,
        )
    return hooks_path, True


def _cursor_hooks_path() -> Path:
    configured = os.environ.get("MEMENTO_CURSOR_HOOKS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cursor" / "hooks.json"


def _is_memento_cursor_governor_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = str(hook.get("command") or "")
    return any(marker in command for marker in _MEMENTO_GOVERNOR_HOOK_MARKERS)


def _remove_cursor_governor_hooks(hooks: dict[str, Any], event_name: str) -> None:
    entries = hooks.get(event_name)
    if entries is None:
        return
    if not isinstance(entries, list):
        raise TypeError(f"Cursor hooks {event_name} must be an array")
    retained = [
        entry for entry in entries if not _is_memento_cursor_governor_hook(entry)
    ]
    if retained:
        hooks[event_name] = retained
    else:
        hooks.pop(event_name, None)


def install_cursor_governor_hooks(
    *,
    sweep_retired: bool = False,
) -> tuple[Path, bool]:
    """Reconcile one Cursor advisory, preferring Cursor's Claude-hook import."""

    hooks_path = _cursor_hooks_path()
    if not hooks_path.parent.is_dir():
        return hooks_path, False
    settings = _read_mapping(hooks_path) if hooks_path.exists() else {}
    if hooks_path.exists() and not settings:
        try:
            decoded = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot merge invalid JSON in {hooks_path}") from exc
        if not isinstance(decoded, dict):
            raise TypeError(f"Cannot merge non-object settings in {hooks_path}")
        settings = decoded
    version = settings.setdefault("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported Cursor hooks version in {hooks_path}: {version}")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("Cursor hooks must be an object")

    before = json.dumps(settings, ensure_ascii=False, sort_keys=True, default=str)
    hook_runner = _install_hook_runner() if governor_enabled() else None
    _remove_cursor_governor_hooks(hooks, "postToolUse")
    _remove_cursor_governor_hooks(hooks, "stop")
    if governor_enabled() and not _claude_governor_hook_is_registered():
        entries = hooks.setdefault("postToolUse", [])
        if not isinstance(entries, list):
            raise TypeError("Cursor hooks postToolUse must be an array")
        entries.append(
            {
                "command": _governor_hook_command(
                    hook_runner,
                    codex_windows=os.name == "nt",
                )
            }
        )

    changed = before != json.dumps(
        settings,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if not changed:
        if hook_runner is not None:
            _maintain_hook_runner_versions(
                hook_runner,
                sweep_retired=sweep_retired,
            )
        return hooks_path, False

    previous_mode = None
    if hooks_path.exists():
        previous_mode = stat.S_IMODE(hooks_path.stat().st_mode)
    temporary = hooks_path.with_name(
        f".{hooks_path.name}.{os.getpid()}.memento.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        os.replace(temporary, hooks_path)
    finally:
        temporary.unlink(missing_ok=True)
    if hook_runner is not None:
        _maintain_hook_runner_versions(
            hook_runner,
            sweep_retired=sweep_retired,
        )
    return hooks_path, True


def _hook_main() -> int:
    try:
        try:
            payload = _read_stdin_payload()
        except (OSError, UnicodeError, TypeError, ValueError):
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
