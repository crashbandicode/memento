"""Extract lightweight question state from the live end of transcripts.

Conversation deltas must remain strictly ordered, so a large in-flight delta can
temporarily block a newer tail.  Interactive questions cannot wait behind that
barrier: this module emits a tiny, independently revisioned metadata update that
the canonical transcript ingest later replaces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TAIL_BYTES = 512 * 1024
_MAX_SIGNALS = 64
_QUESTION_TOOLS = {
    "askquestion",
    "ask_question",
    "askuserquestion",
    "request_user_input",
}
_CURSOR_PLAN_MODE_TOOLS = {"switchmode", "switch_mode"}
_SHELL_TOOLS = {
    "bash",
    "execcommand",
    "powershell",
    "runterminalcommand",
    "runterminalcommandv2",
    "shell",
    "shellcommand",
    "terminal",
}
_PENDING_STATUSES = {"", "awaiting", "loading", "pending", "requested", "running"}
_CANCELLED_STATUSES = {
    "aborted",
    "cancelled",
    "canceled",
    "dismissed",
    "error",
    "failed",
    "rejected",
    "skipped",
    "timeout",
}


def _tail_lines(path: Path) -> list[bytes]:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - _TAIL_BYTES)
        stream.seek(start)
        payload = stream.read()
    lines = payload.splitlines()
    if start and lines:
        lines = lines[1:]
    return lines


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _is_cursor_plan_mode_request(tool_name: str, raw_input: object) -> bool:
    if tool_name.casefold() not in _CURSOR_PLAN_MODE_TOOLS:
        return False
    payload = _mapping(raw_input)
    target_mode = (
        payload.get("toModeId")
        or payload.get("to_mode_id")
        or payload.get("to_mode")
    )
    return str(target_mode or "").strip().casefold() == "plan"


def _cursor_state_interaction_status(record: dict[str, Any]) -> str:
    status = str(record.get("tool_status") or "").strip().casefold()
    if not status:
        content = str(record.get("content") or "")
        prefix = content.partition("\n")[0]
        if prefix.casefold().startswith("status:"):
            status = prefix.partition(":")[2].strip().casefold()
    if status in _PENDING_STATUSES:
        return "pending"
    if status in _CANCELLED_STATUSES:
        return "cancelled"
    return "answered"


def _message_parts(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def _human_text(record: dict[str, Any]) -> str:
    texts: list[str] = []
    for part in _message_parts(record):
        if part.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
    return " ".join(texts).strip()


def _signal_record(
    *,
    tool_name: str,
    relative_path: str,
    interaction_id: str,
    question_tool: str,
    raw_input: object,
    timestamp: object,
    status: str,
) -> dict[str, Any]:
    return {
        "metadata_type": "conversation_interaction",
        "tool": tool_name,
        "relative_path": relative_path,
        "interaction_id": interaction_id[:512],
        "interaction_status": status,
        "question_tool": question_tool[:256],
        "interaction_input": raw_input,
        "timestamp": str(timestamp or "")[:128],
    }


def extract_conversation_interaction_updates(
    path: Path,
    *,
    tool_name: str,
    relative_path: str,
) -> dict[str, dict[str, Any]]:
    """Return the latest question state visible in a bounded transcript tail."""
    if tool_name not in {"claude_code", "codex", "cursor"}:
        return {}

    signals: dict[str, dict[str, Any]] = {}
    open_cursor_ids: list[str] = []
    try:
        lines = _tail_lines(path)
    except OSError:
        return {}

    for raw_line in lines:
        try:
            record = json.loads(raw_line)
        except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue

        timestamp = record.get("timestamp")
        payload = record.get("payload")
        if isinstance(payload, dict):
            payload_type = str(payload.get("type") or "")
            call_id = str(payload.get("call_id") or payload.get("id") or "").strip()
            if (
                payload_type in {"function_call", "custom_tool_call"}
                and str(payload.get("name") or "").casefold() == "request_user_input"
                and call_id
            ):
                signals[call_id] = _signal_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    interaction_id=call_id,
                    question_tool="request_user_input",
                    raw_input=payload.get("arguments", payload.get("input", {})),
                    timestamp=timestamp,
                    status="pending",
                )
            elif (
                payload_type in {"function_call_output", "custom_tool_call_output"}
                and call_id
                and call_id in signals
            ):
                signals[call_id]["interaction_status"] = "answered"
                signals[call_id]["timestamp"] = str(timestamp or "")[:128]

        for part in _message_parts(record):
            part_type = str(part.get("type") or "")
            if part_type in {"tool_use", "toolCall"}:
                question_tool = str(part.get("name") or "")
                interaction_id = str(
                    part.get("id") or part.get("call_id") or ""
                ).strip()
                raw_input = (
                    part.get("input")
                    if "input" in part
                    else part.get("arguments", {})
                )
                is_question = question_tool.casefold() in _QUESTION_TOOLS
                is_plan_mode = (
                    tool_name == "cursor"
                    and _is_cursor_plan_mode_request(question_tool, raw_input)
                )
                if (is_question or is_plan_mode) and interaction_id:
                    signals[interaction_id] = _signal_record(
                        tool_name=tool_name,
                        relative_path=relative_path,
                        interaction_id=interaction_id,
                        question_tool=question_tool,
                        raw_input=raw_input,
                        timestamp=timestamp,
                        status="pending",
                    )
                    if tool_name == "cursor" and is_question:
                        open_cursor_ids.append(interaction_id)
            elif part_type in {"tool_result", "toolResult"}:
                interaction_id = str(
                    part.get("tool_use_id")
                    or part.get("toolCallId")
                    or part.get("call_id")
                    or ""
                ).strip()
                if interaction_id and interaction_id in signals:
                    output = part.get("content", part.get("output"))
                    status = "answered" if str(output or "").strip() else "cancelled"
                    signals[interaction_id]["interaction_status"] = status
                    signals[interaction_id]["timestamp"] = str(timestamp or "")[:128]

        # Cursor writes the answer as the next human row rather than a linked
        # tool-result row. The canonical parser uses the same pairing rule.
        role = str(record.get("role") or "").casefold()
        if tool_name == "cursor" and role == "user" and _human_text(record):
            for interaction_id in open_cursor_ids:
                if interaction_id in signals:
                    signals[interaction_id]["interaction_status"] = "answered"
                    signals[interaction_id]["timestamp"] = str(timestamp or "")[:128]
            open_cursor_ids.clear()

        # Synthetic Cursor state exports carry the question and status together.
        record_tool = str(record.get("tool_name") or "")
        record_input = _mapping(record.get("tool_input"))
        is_state_interaction = (
            record_tool.casefold() in _QUESTION_TOOLS
            or _is_cursor_plan_mode_request(record_tool, record_input)
        )
        if (
            str(record.get("type") or "") == "cursor_state_tool"
            and is_state_interaction
        ):
            interaction_id = str(
                record.get("tool_call_id") or record.get("id") or ""
            ).strip()
            if interaction_id:
                signals[interaction_id] = _signal_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    interaction_id=interaction_id,
                    question_tool=str(record.get("tool_name") or "AskQuestion"),
                    raw_input=record_input,
                    timestamp=timestamp,
                    status=_cursor_state_interaction_status(record),
                )

    latest = list(signals.items())[-_MAX_SIGNALS:]
    return {
        f"{tool_name}:{relative_path}:{interaction_id}": signal
        for interaction_id, signal in latest
    }


def _normalized_tool_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_shell_tool(value: object) -> bool:
    return _normalized_tool_name(value) in _SHELL_TOOLS


def _shell_command(value: object) -> str:
    payload = _mapping(value)
    for key in ("command", "cmd", "script"):
        command = payload.get(key)
        if isinstance(command, list):
            text = " ".join(str(part) for part in command)
        elif command is not None:
            text = str(command)
        else:
            continue
        if text.strip():
            return text.strip()[:16_000]
    if isinstance(value, str) and value.strip() and not payload:
        return value.strip()[:16_000]
    return ""


def _shell_activity_status(value: object) -> str:
    status = str(value or "").strip().casefold()
    if status in {"failed", "error"}:
        return "failed"
    if status in {
        "aborted",
        "cancelled",
        "canceled",
        "interrupted",
        "rejected",
        "skipped",
        "timeout",
    }:
        return "cancelled"
    if status in {"completed", "complete", "done", "success", "succeeded"}:
        return "completed"
    return "running"


def _activity_record(
    *,
    tool_name: str,
    relative_path: str,
    activity_id: str,
    activity_tool: object,
    command: object,
    timestamp: object,
    status: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    return {
        "metadata_type": "conversation_activity",
        "tool": tool_name,
        "relative_path": relative_path,
        "activity_id": activity_id[:512],
        "activity_status": status,
        "activity_tool": (
            str(activity_tool or "").strip()
            or str(previous.get("activity_tool") or "Shell")
        )[:256],
        "command": (
            str(command or "").strip()
            or str(previous.get("command") or "")
        )[:16_000],
        "timestamp": str(timestamp or "")[:128],
    }


def _extract_shell_activity_updates(
    lines: list[bytes | str],
    *,
    tool_name: str,
    relative_path: str,
) -> dict[str, dict[str, Any]]:
    activities: dict[str, dict[str, Any]] = {}
    for raw_line in lines:
        try:
            record = json.loads(raw_line)
        except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        timestamp = record.get("timestamp")
        payload = record.get("payload")
        if isinstance(payload, dict):
            payload_type = str(payload.get("type") or "")
            activity_id = str(
                payload.get("call_id") or payload.get("id") or ""
            ).strip()
            if (
                payload_type in {"function_call", "custom_tool_call"}
                and activity_id
                and _is_shell_tool(payload.get("name"))
            ):
                raw_input = payload.get(
                    "arguments",
                    payload.get("input", {}),
                )
                activities[activity_id] = _activity_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    activity_id=activity_id,
                    activity_tool=payload.get("name"),
                    command=_shell_command(raw_input),
                    timestamp=timestamp,
                    status="running",
                )
            elif (
                payload_type in {
                    "function_call_output",
                    "custom_tool_call_output",
                }
                and activity_id in activities
            ):
                activities[activity_id] = _activity_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    activity_id=activity_id,
                    activity_tool="",
                    command="",
                    timestamp=timestamp,
                    status="completed",
                    previous=activities[activity_id],
                )

        for part in _message_parts(record):
            part_type = str(part.get("type") or "")
            activity_id = str(
                part.get("id")
                or part.get("tool_use_id")
                or part.get("toolUseId")
                or part.get("call_id")
                or ""
            ).strip()
            if (
                part_type in {"tool_use", "toolCall"}
                and activity_id
                and _is_shell_tool(part.get("name"))
            ):
                raw_input = part.get(
                    "input",
                    part.get("arguments", {}),
                )
                activities[activity_id] = _activity_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    activity_id=activity_id,
                    activity_tool=part.get("name"),
                    command=_shell_command(raw_input),
                    timestamp=timestamp,
                    status="running",
                )
            elif (
                part_type in {"tool_result", "toolResult"}
                and activity_id in activities
            ):
                activities[activity_id] = _activity_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    activity_id=activity_id,
                    activity_tool="",
                    command="",
                    timestamp=timestamp,
                    status="completed",
                    previous=activities[activity_id],
                )

        if str(record.get("type") or "") == "cursor_state_tool":
            activity_id = str(
                record.get("tool_call_id") or record.get("id") or ""
            ).strip()
            activity_tool = record.get("tool_name")
            if activity_id and _is_shell_tool(activity_tool):
                activities[activity_id] = _activity_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    activity_id=activity_id,
                    activity_tool=activity_tool,
                    command=_shell_command(record.get("tool_input")),
                    timestamp=timestamp,
                    status=_shell_activity_status(record.get("tool_status")),
                    previous=activities.get(activity_id),
                )

    latest = list(activities.items())[-_MAX_SIGNALS:]
    return {
        f"{tool_name}:{relative_path}:{activity_id}": activity
        for activity_id, activity in latest
    }


def extract_conversation_activity_updates(
    path: Path,
    *,
    tool_name: str,
    relative_path: str,
) -> dict[str, dict[str, Any]]:
    """Return shell-command lifecycle state visible in a transcript tail."""
    if tool_name not in {"claude_code", "codex", "cursor"}:
        return {}
    try:
        lines = _tail_lines(path)
    except OSError:
        return {}
    return _extract_shell_activity_updates(
        lines,
        tool_name=tool_name,
        relative_path=relative_path,
    )


def extract_content_activity_updates(
    content: str,
    *,
    tool_name: str,
    relative_path: str,
) -> dict[str, dict[str, Any]]:
    """Return shell lifecycle state from a generated conversation snapshot."""
    return _extract_shell_activity_updates(
        content.splitlines(),
        tool_name=tool_name,
        relative_path=relative_path,
    )
