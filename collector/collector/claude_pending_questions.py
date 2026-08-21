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

_TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024


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
        try:
            relative_project_path = candidate.resolve(strict=False).relative_to(
                projects_root.resolve(strict=False)
            )
        except (OSError, RuntimeError, ValueError):
            pass
        else:
            parts = relative_project_path.parts
            is_main = (
                len(parts) == 2
                and candidate.name == expected_name
                and candidate.suffix.casefold() == ".jsonl"
            )
            is_subagent = (
                len(parts) >= 4
                and parts[1] == session_id
                and _is_nested_subagent_path(parts[2:])
            )
            if is_main or is_subagent:
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


def _is_nested_subagent_path(parts: tuple[str, ...]) -> bool:
    """Match Claude's repeating ``subagents/<child>/`` transcript layout."""
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


def _transcript_has_assistant_continuation(
    root: Path,
    side_record: dict[str, Any],
    session_id: str,
    interaction_at: int | None,
) -> bool:
    """Return whether Claude wrote a later main-thread assistant turn."""
    if interaction_at is None:
        return False
    relative_path = _relative_transcript_path(
        root,
        session_id,
        side_record.get("transcript_path"),
    )
    if relative_path is None:
        return False
    transcript = root / relative_path
    try:
        with transcript.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            payload = stream.read(_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return False
    if start:
        newline = payload.find(b"\n")
        if newline < 0:
            return False
        payload = payload[newline + 1:]

    for raw_line in payload.splitlines():
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("isSidechain") is True:
            continue
        message = record.get("message")
        message_role = (
            str(message.get("role") or "").casefold()
            if isinstance(message, dict)
            else ""
        )
        if (
            str(record.get("type") or "").casefold() != "assistant"
            and message_role != "assistant"
        ):
            continue
        if (_timestamp_millis(record.get("timestamp")) or -1) > interaction_at:
            return True
    return False


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
    interaction_at = _timestamp_millis(
        side_record.get("interaction_timestamp")
        or side_record.get("timestamp")
    )
    state = _session_state(root, session_id)
    if state:
        session_updated_at = _timestamp_millis(state.get("updatedAt"))
        state_is_current = (
            interaction_at is None
            or session_updated_at is None
            or session_updated_at >= interaction_at
        )
        if state_is_current:
            session_status = str(state.get("status") or "").casefold()
            waiting_for = str(state.get("waitingFor") or "").casefold()
            if not (
                session_status == "waiting"
                and "permission" in waiting_for
            ):
                return "answered"
    if _transcript_has_assistant_continuation(
        root,
        side_record,
        session_id,
        interaction_at,
    ):
        return "answered"
    return status


def _interaction_side_records(
    side_record: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return bounded terminal predecessors followed by the current prompt."""
    raw_resolved = side_record.get("resolved_interactions")
    resolved = [
        item
        for item in raw_resolved
        if isinstance(item, dict)
    ] if isinstance(raw_resolved, list) else []
    return [*resolved[-16:], side_record]


def extract_claude_pending_interaction_updates(
    tool: ClaudeCodeTool | Path,
    *,
    side_records: list[tuple[Path, dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve hook side files to lightweight conversation interaction updates."""
    root = tool if isinstance(tool, Path) else tool.root_path
    records: dict[str, dict[str, Any]] = {}

    source_records = side_records
    if source_records is None:
        source_records = [
            (side_file, _read_side_file(side_file))
            for side_file in iter_claude_pending_side_files()
        ]
    for _side_file, side_record in source_records:
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

        for interaction_record in _interaction_side_records(side_record):
            interaction_id = str(
                interaction_record.get("interaction_id") or ""
            ).strip()
            status = str(
                interaction_record.get("interaction_status") or ""
            ).casefold()
            if (
                not interaction_id
                or status not in {"pending", "answered", "cancelled"}
            ):
                continue

            question_tool = str(
                interaction_record.get("question_tool") or "AskUserQuestion"
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
                interaction_record,
                session_id,
                question_tool,
                status,
            )
            raw_input = interaction_record.get("interaction_input")
            if not isinstance(raw_input, dict):
                raw_input = {}

            signal = _signal_record(
                tool_name="claude_code",
                relative_path=relative_path,
                interaction_id=interaction_id,
                question_tool=question_tool,
                raw_input=raw_input,
                timestamp=(
                    interaction_record.get("interaction_timestamp")
                    or interaction_record.get("timestamp")
                ),
                status=status,
                interaction_origin=interaction_record.get("interaction_origin"),
                interaction_response=interaction_record.get(
                    "interaction_response"
                ),
            )
            key = f"claude_code:{relative_path}:{interaction_id}"
            records[key] = signal

    return records


def extract_claude_live_activity_updates(
    tool: ClaudeCodeTool | Path,
    *,
    side_records: list[tuple[Path, dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve Claude hook side files to lightweight shell lifecycle updates."""
    root = tool if isinstance(tool, Path) else tool.root_path
    records: dict[str, dict[str, Any]] = {}
    source_records = side_records
    if source_records is None:
        source_records = [
            (side_file, _read_side_file(side_file))
            for side_file in iter_claude_pending_side_files()
        ]
    for _side_file, side_record in source_records:
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


class ClaudePendingPoller:
    """Project only changed hook files and active permission dependencies."""

    def __init__(self) -> None:
        self._file_tokens: dict[Path, tuple[int, int, int]] = {}
        self._side_records: dict[Path, dict[str, Any]] = {}
        self._dependency_tokens: dict[Path, tuple[object, ...]] = {}
        self._interaction_values: dict[str, str] = {}
        self._activity_values: dict[str, str] = {}
        self._initialized = False

    @staticmethod
    def _file_token(path: Path) -> tuple[int, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _is_pending_permission(record: dict[str, Any]) -> bool:
        return (
            str(record.get("interaction_status") or "").casefold() == "pending"
            and str(record.get("question_tool") or "").casefold()
            == "permissionrequest"
        )

    def _dependency_token(
        self,
        root: Path,
        record: dict[str, Any],
    ) -> tuple[object, ...]:
        if not self._is_pending_permission(record):
            return ()
        session_id = str(record.get("session_id") or "").strip()
        relative_path = _relative_transcript_path(
            root,
            session_id,
            record.get("transcript_path"),
        )
        token: list[object] = [relative_path]
        if relative_path:
            transcript_token = self._file_token(root / relative_path)
            token.append(transcript_token)
        sessions_directory = root / "sessions"
        try:
            session_tokens = tuple(
                (path.name, self._file_token(path))
                for path in sorted(sessions_directory.glob("*.json"))
                if path.is_file()
            )
        except OSError:
            session_tokens = ()
        token.append(session_tokens)
        return tuple(token)

    def needs_poll(self, tool: ClaudeCodeTool | Path) -> bool:
        root = tool if isinstance(tool, Path) else tool.root_path
        paths = iter_claude_pending_side_files()
        if not self._initialized or set(paths) != set(self._file_tokens):
            return True
        for path in paths:
            if self._file_token(path) != self._file_tokens.get(path):
                return True
            record = self._side_records.get(path, {})
            if (
                self._is_pending_permission(record)
                and self._dependency_token(root, record)
                != self._dependency_tokens.get(path)
            ):
                return True
        return False

    def poll(
        self,
        tool: ClaudeCodeTool | Path,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        root = tool if isinstance(tool, Path) else tool.root_path
        paths = iter_claude_pending_side_files()
        current_paths = set(paths)
        for removed in set(self._file_tokens) - current_paths:
            self._file_tokens.pop(removed, None)
            self._side_records.pop(removed, None)
            self._dependency_tokens.pop(removed, None)

        changed_records: list[tuple[Path, dict[str, Any]]] = []
        for path in paths:
            file_token = self._file_token(path)
            record = self._side_records.get(path)
            changed = file_token != self._file_tokens.get(path)
            if changed or record is None:
                record = _read_side_file(path)
                self._side_records[path] = record
            dependency_token = self._dependency_token(root, record)
            if (
                changed
                or path not in self._dependency_tokens
                or dependency_token != self._dependency_tokens[path]
            ):
                changed_records.append((path, record))
            if file_token is not None:
                self._file_tokens[path] = file_token
            self._dependency_tokens[path] = dependency_token

        self._initialized = True
        if not changed_records:
            return {}, {}
        interactions = extract_claude_pending_interaction_updates(
            tool,
            side_records=changed_records,
        )
        activities = extract_claude_live_activity_updates(
            tool,
            side_records=changed_records,
        )
        return (
            self._changed_records(interactions, self._interaction_values),
            self._changed_records(activities, self._activity_values),
        )

    @staticmethod
    def _changed_records(
        records: dict[str, dict[str, Any]],
        observed: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        changed: dict[str, dict[str, Any]] = {}
        for key, record in records.items():
            value = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if observed.get(key) != value:
                changed[key] = record
            observed[key] = value
        return changed

    def invalidate(self) -> None:
        self._file_tokens.clear()
        self._side_records.clear()
        self._dependency_tokens.clear()
        self._interaction_values.clear()
        self._activity_values.clear()
        self._initialized = False
