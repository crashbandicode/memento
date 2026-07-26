"""Extract lightweight question state from the live end of transcripts.

Conversation deltas must remain strictly ordered, so a large in-flight delta can
temporarily block a newer tail.  Interactive questions cannot wait behind that
barrier: this module emits a tiny, independently revisioned metadata update that
the canonical transcript ingest later replaces.
"""

from __future__ import annotations

import json
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
            if part_type == "tool_use":
                question_tool = str(part.get("name") or "")
                interaction_id = str(part.get("id") or "").strip()
                if question_tool.casefold() in _QUESTION_TOOLS and interaction_id:
                    signals[interaction_id] = _signal_record(
                        tool_name=tool_name,
                        relative_path=relative_path,
                        interaction_id=interaction_id,
                        question_tool=question_tool,
                        raw_input=part.get("input", {}),
                        timestamp=timestamp,
                        status="pending",
                    )
                    if tool_name == "cursor":
                        open_cursor_ids.append(interaction_id)
            elif part_type == "tool_result":
                interaction_id = str(part.get("tool_use_id") or "").strip()
                if interaction_id and interaction_id in signals:
                    output = part.get("content")
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
        if (
            str(record.get("type") or "") == "cursor_state_tool"
            and str(record.get("tool_name") or "").casefold() in _QUESTION_TOOLS
        ):
            interaction_id = str(
                record.get("tool_call_id") or record.get("id") or ""
            ).strip()
            if interaction_id:
                content = str(record.get("content") or "").casefold()
                status = "pending" if "pending" in content else "answered"
                signals[interaction_id] = _signal_record(
                    tool_name=tool_name,
                    relative_path=relative_path,
                    interaction_id=interaction_id,
                    question_tool=str(record.get("tool_name") or "AskQuestion"),
                    raw_input=_mapping(record.get("tool_input")),
                    timestamp=timestamp,
                    status=status,
                )

    latest = list(signals.items())[-_MAX_SIGNALS:]
    return {
        f"{tool_name}:{relative_path}:{interaction_id}": signal
        for interaction_id, signal in latest
    }
