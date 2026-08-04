"""Read live prompt state written by the Claude Code hook."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .interaction_signals import _activity_record, _signal_record

if TYPE_CHECKING:
    from .tools.claude_code import ClaudeCodeTool


def _pending_directory() -> Path:
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        home = Path(profile) if profile else Path.home()
    else:
        home = Path.home()
    return home / ".memento" / "claude-pending"


def iter_claude_pending_side_files() -> list[Path]:
    """Return the current Claude pending-question side files."""
    directory = _pending_directory()
    try:
        return sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and path.suffix.casefold() == ".json"
            ),
            key=lambda path: path.name,
        )
    except OSError:
        return []


def _relative_transcript_path(
    root: Path,
    session_id: str,
    transcript_path: object,
) -> str | None:
    projects_root = root / "projects"
    expected_name = f"{session_id}.jsonl"

    if isinstance(transcript_path, str) and transcript_path.strip():
        candidate = Path(transcript_path).expanduser()
        if candidate.name == expected_name:
            try:
                relative_project_path = candidate.resolve(strict=False).relative_to(
                    projects_root.resolve(strict=False)
                )
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                return (Path("projects") / relative_project_path).as_posix()

    try:
        project_directories = sorted(
            (path for path in projects_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
    except OSError:
        return None

    for project_directory in project_directories:
        candidate = project_directory / expected_name
        try:
            if candidate.is_file():
                return candidate.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
    return None


def _read_side_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp_millis(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _session_state(root: Path, session_id: str) -> dict[str, Any]:
    sessions_directory = root / "sessions"
    try:
        paths = sorted(
            (
                path
                for path in sessions_directory.iterdir()
                if path.is_file() and path.suffix.casefold() == ".json"
            ),
            key=lambda path: path.name,
        )
    except OSError:
        return {}
    for path in paths:
        state = _read_side_file(path)
        if str(state.get("sessionId") or "").strip() == session_id:
            return state
    return {}


def _effective_status(
    root: Path,
    side_record: dict[str, Any],
    session_id: str,
    question_tool: str,
    status: str,
) -> str:
    """Close permission previews once Claude leaves its permission wait."""
    if status != "pending" or question_tool.casefold() != "permissionrequest":
        return status
    state = _session_state(root, session_id)
    if not state:
        return status
    interaction_at = _timestamp_millis(side_record.get("timestamp"))
    session_updated_at = _timestamp_millis(state.get("updatedAt"))
    if (
        interaction_at is not None
        and session_updated_at is not None
        and session_updated_at < interaction_at
    ):
        return status
    session_status = str(state.get("status") or "").casefold()
    waiting_for = str(state.get("waitingFor") or "").casefold()
    if session_status == "waiting" and "permission" in waiting_for:
        return status
    return "answered"


def extract_claude_pending_interaction_updates(
    tool: ClaudeCodeTool | Path,
) -> dict[str, dict[str, Any]]:
    """Resolve hook side files to lightweight conversation interaction updates."""
    root = tool if isinstance(tool, Path) else tool.root_path
    records: dict[str, dict[str, Any]] = {}

    for side_file in iter_claude_pending_side_files():
        side_record = _read_side_file(side_file)
        session_id = str(side_record.get("session_id") or "").strip()
        interaction_id = str(side_record.get("interaction_id") or "").strip()
        if (
            not session_id
            or not interaction_id
            or "/" in session_id
            or "\\" in session_id
        ):
            continue

        status = str(side_record.get("interaction_status") or "").casefold()
        if status not in {"pending", "answered", "cancelled"}:
            continue

        relative_path = _relative_transcript_path(
            root,
            session_id,
            side_record.get("transcript_path"),
        )
        if relative_path is None:
            continue

        question_tool = str(
            side_record.get("question_tool") or "AskUserQuestion"
        ).strip()
        if question_tool.casefold() not in {
            "askuserquestion",
            "permissionrequest",
            "elicitation",
            "notificationprompt",
        }:
            continue
        status = _effective_status(
            root,
            side_record,
            session_id,
            question_tool,
            status,
        )
        raw_input = side_record.get("interaction_input")
        if not isinstance(raw_input, dict):
            raw_input = {}

        signal = _signal_record(
            tool_name="claude_code",
            relative_path=relative_path,
            interaction_id=interaction_id,
            question_tool=question_tool,
            raw_input=raw_input,
            timestamp=side_record.get("timestamp"),
            status=status,
        )
        key = f"claude_code:{relative_path}:{interaction_id}"
        records[key] = signal

    return records


def extract_claude_live_activity_updates(
    tool: ClaudeCodeTool | Path,
) -> dict[str, dict[str, Any]]:
    """Resolve Claude hook side files to lightweight shell lifecycle updates."""
    root = tool if isinstance(tool, Path) else tool.root_path
    records: dict[str, dict[str, Any]] = {}
    for side_file in iter_claude_pending_side_files():
        side_record = _read_side_file(side_file)
        session_id = str(side_record.get("session_id") or "").strip()
        if (
            not session_id
            or "/" in session_id
            or "\\" in session_id
        ):
            continue
        relative_path = _relative_transcript_path(
            root,
            session_id,
            side_record.get("transcript_path"),
        )
        if relative_path is None:
            continue
        raw_activities = side_record.get("shell_activities")
        if not isinstance(raw_activities, dict):
            continue
        for activity_id, raw_activity in list(raw_activities.items())[-32:]:
            if not isinstance(raw_activity, dict):
                continue
            canonical_id = str(raw_activity.get("id") or activity_id).strip()
            status = str(raw_activity.get("status") or "").strip().casefold()
            command = str(raw_activity.get("command") or "").strip()
            if (
                not canonical_id
                or status not in {
                    "running",
                    "completed",
                    "failed",
                    "cancelled",
                }
                or not command
            ):
                continue
            signal = _activity_record(
                tool_name="claude_code",
                relative_path=relative_path,
                activity_id=canonical_id,
                activity_tool=raw_activity.get("tool_name"),
                command=command,
                timestamp=(
                    raw_activity.get("updated_at")
                    or raw_activity.get("started_at")
                ),
                status=status,
            )
            key = f"claude_code:{relative_path}:{canonical_id}"
            records[key] = signal
    return records
