"""Read-only projection of Cursor's live composer state into conversation JSONL.

Recent Cursor releases write a sparse compatibility transcript under
``~/.cursor/projects``.  The authoritative ordered bubbles, model selection,
thinking blocks, tools, task progress, and interrupted status live in
``state.vscdb``.  This module projects only those conversation fields; opaque
composer state, encryption material, and unrelated editor data never leave the
machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .tools.base import Category, ContentType
from .tools.cursor import CursorTool

_MAX_TOOL_FIELD_CHARS = 262_144
_INTERRUPTED_STATES = {"aborted", "cancelled", "canceled", "interrupted"}
_TERMINAL_COMPOSER_STATES = _INTERRUPTED_STATES | {
    "complete",
    "completed",
    "done",
    "error",
    "failed",
}
_CURSOR_MODE_SWITCH_TOOLS = {"switch_mode", "switchmode"}
_TOOL_LABELS = {
    "await": "Await",
    "edit_file_v2": "Edit",
    "read_file_v2": "Read",
    "ripgrep_raw_search": "Ripgrep",
    "run_terminal_command_v2": "PowerShell",
    "search_replace": "Edit",
    "shell": "Shell",
}


@dataclass(frozen=True)
class CursorStateSnapshot:
    relative_path: str
    content: str
    content_hash: str
    metadata: dict[str, object]
    source_modified_at: float | None


@dataclass(frozen=True)
class _ComposerHeader:
    composer_id: str
    workspace_id: str
    created_at: object
    last_updated_at: object
    checkpoint_at: object
    is_subagent: bool
    value: object

    @property
    def revision(self) -> str:
        payload = json.dumps(
            [self.last_updated_at, self.checkpoint_at, _coerce_text(self.value)],
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _decode_json(value: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(_coerce_text(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _bounded_text(value: object, limit: int = _MAX_TOOL_FIELD_CHARS) -> str:
    text = _coerce_text(value).replace("\x00", "")
    if len(text) <= limit:
        return text
    marker = "\n\n[... truncated by Memento collector ...]\n\n"
    remaining = max(0, limit - len(marker))
    head = remaining * 3 // 4
    return text[:head] + marker + text[-(remaining - head):]


def _serialized_field(value: object) -> str:
    decoded = _decode_json(value)
    if isinstance(decoded, (dict, list)):
        return _bounded_text(
            json.dumps(decoded, ensure_ascii=False, indent=2, default=str)
        )
    return _bounded_text(value)


def _timestamp_seconds(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(
                _coerce_text(value).strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if not math.isfinite(numeric):
        return None
    # Cursor currently uses epoch milliseconds in composer headers and ISO
    # strings in bubble rows. Accept seconds as well, and safely normalize
    # micro/nanosecond values seen in older experimental builds.
    while abs(numeric) > 10_000_000_000:
        numeric /= 1000
    try:
        datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return numeric


def _iso_timestamp(value: object) -> str:
    seconds = _timestamp_seconds(value)
    if seconds is None:
        return ""
    parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if parsed.microsecond == 0:
        timespec = "seconds"
    elif parsed.microsecond % 1000 == 0:
        timespec = "milliseconds"
    else:
        timespec = "microseconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _compatibility_source_id(record: dict[str, object]) -> str:
    message = record.get("message")
    message_map = message if isinstance(message, dict) else {}
    return _coerce_text(
        record.get("id")
        or record.get("uuid")
        or record.get("bubbleId")
        or message_map.get("id")
        or message_map.get("uuid")
        or message_map.get("bubbleId")
    ).strip()


def _compatibility_timestamps(path: Path | None) -> dict[str, str]:
    """Return exact transcript timestamps keyed only by stable source IDs.

    Cursor's sparse compatibility transcript usually omits both fields. Some
    releases include a bubble ID and timestamp, though, and that exact pair is
    a safe fallback when the authoritative bubble projection lacks createdAt.
    Positional or content matching is intentionally forbidden: repeated tool
    calls and prose are valid distinct events.
    """
    if path is None:
        return {}
    timestamps: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return timestamps
    for line in lines:
        if not line.strip():
            continue
        record = _decode_json(line)
        if not isinstance(record, dict):
            continue
        source_id = _compatibility_source_id(record)
        if not source_id or source_id in timestamps:
            continue
        message = record.get("message")
        message_map = message if isinstance(message, dict) else {}
        for value in (
            record.get("createdAt"),
            record.get("timestamp"),
            record.get("updatedAt"),
            message_map.get("createdAt"),
            message_map.get("timestamp"),
            message_map.get("updatedAt"),
        ):
            timestamp = _iso_timestamp(value)
            if timestamp:
                timestamps[source_id] = timestamp
                break
    return timestamps


def _bubble_timestamp(
    bubble: dict[str, object],
    compatibility_timestamps: dict[str, str],
) -> str:
    """Resolve one bubble's source-backed timestamp in strict precedence."""
    for value in (
        bubble.get("createdAt"),
        bubble.get("timestamp"),
        bubble.get("updatedAt"),
    ):
        timestamp = _iso_timestamp(value)
        if timestamp:
            return timestamp
    bubble_id = _coerce_text(bubble.get("bubbleId")).strip()
    return compatibility_timestamps.get(bubble_id, "")


def _model_selection(config: object) -> tuple[str, str]:
    config_map = config if isinstance(config, dict) else {}
    model = _coerce_text(
        config_map.get("modelName")
        or config_map.get("modelId")
        or config_map.get("model")
    ).strip()
    effort = ""
    selected = config_map.get("selectedModels")
    if isinstance(selected, list):
        for item in selected:
            if not isinstance(item, dict):
                continue
            item_model = _coerce_text(
                item.get("modelId") or item.get("modelName")
            ).strip()
            if model and item_model and item_model != model:
                continue
            parameters = item.get("parameters")
            if isinstance(parameters, list):
                for parameter in parameters:
                    if not isinstance(parameter, dict):
                        continue
                    if _coerce_text(parameter.get("id")).lower() == "effort":
                        effort = _coerce_text(parameter.get("value")).strip()
                        break
            if effort:
                break
    return model, effort


def _workspace_folder_path(value: object) -> str:
    """Decode Cursor workspace URIs, including Windows-hosted WSL folders."""
    folder = _coerce_text(value).strip()
    try:
        parsed = urlsplit(folder)
    except ValueError:
        return ""

    scheme = parsed.scheme.casefold()
    host = parsed.netloc.casefold()
    path = unquote(parsed.path)
    if scheme == "file":
        if host in {"wsl.localhost", "wsl$"}:
            # file://wsl.localhost/<distro>/home/... is Cursor's Windows-side
            # URI for a WSL workspace. The distro is transport identity, not
            # part of the Linux filesystem path shown to the user.
            parts = path.lstrip("/").split("/", 1)
            return f"/{parts[1]}" if len(parts) == 2 and parts[1] else ""
        if host:
            return ""
        if re.match(r"^/[A-Za-z]:/", path):
            return path[1:]
        return path if path.startswith("/") else ""
    if scheme == "vscode-remote" and host.startswith("wsl+"):
        return path if path.startswith("/") else ""
    return ""


def _bubble_model(
    bubble: dict[str, object],
    fallback_model: str,
    fallback_effort: str,
) -> tuple[str, str]:
    info = bubble.get("modelInfo")
    info_map = info if isinstance(info, dict) else {}
    model = _coerce_text(
        info_map.get("modelName")
        or info_map.get("modelId")
        or info_map.get("model")
    ).strip()
    effort = _coerce_text(
        info_map.get("effort")
        or info_map.get("reasoningEffort")
        or info_map.get("thinkingLevel")
    ).strip()
    if model == fallback_model and not effort:
        effort = fallback_effort
    return model or fallback_model, effort or fallback_effort


def _record(
    *,
    record_type: str,
    role: str,
    source_id: str,
    timestamp: str,
    model: str,
    reasoning_effort: str,
    **payload: object,
) -> dict[str, object]:
    record: dict[str, object] = {
        "type": record_type,
        "role": role,
        "id": source_id,
        "timestamp": timestamp,
        **payload,
    }
    if model:
        record["model"] = model
    if reasoning_effort:
        record["reasoning_effort"] = reasoning_effort
    return record


def _task_record(
    todos: list[object],
    *,
    source_id: str,
    timestamp: str,
    model: str,
    reasoning_effort: str,
    is_current: bool,
) -> dict[str, object]:
    tasks = [item for item in todos if isinstance(item, dict)]
    completed = sum(
        1
        for item in tasks
        if _coerce_text(item.get("status")).lower() in {"completed", "done"}
    )
    total = len(tasks)
    label = f"Task progress {completed}/{total}" if total else "Task progress"
    lines = [f"{completed} of {total} tasks complete"] if total else []
    for item in tasks:
        status = _coerce_text(item.get("status")).lower()
        marker = "✓" if status in {"completed", "done"} else "○"
        lines.append(f"{marker} {_coerce_text(item.get('content')).strip()}")
    return _record(
        record_type="cursor_state_task",
        role="tool",
        source_id=source_id,
        timestamp=timestamp,
        model=model,
        reasoning_effort=reasoning_effort,
        tool_name=label,
        tool_input=json.dumps(
            {"tasks": tasks, "is_current": is_current},
            ensure_ascii=False,
            indent=2,
        ),
        content="\n".join(lines),
    )


def _tool_record(
    tool_data: dict[str, object],
    *,
    source_id: str,
    timestamp: str,
    model: str,
    reasoning_effort: str,
) -> dict[str, object]:
    raw_name = _coerce_text(tool_data.get("name") or "Tool").strip() or "Tool"
    name = _TOOL_LABELS.get(raw_name.lower(), raw_name)
    normalized_name = raw_name.lower()
    # ``switch_mode`` stores its meaningful target and explanation in params
    # while rawArgs is the literal string ``{}``. Prefer the native params or
    # the projected interaction would lose everything shown to the user.
    if normalized_name in _CURSOR_MODE_SWITCH_TOOLS:
        raw_input = tool_data.get("params") or tool_data.get("rawArgs") or ""
    else:
        raw_input = tool_data.get("rawArgs") or tool_data.get("params") or ""
    status = _coerce_text(tool_data.get("status")).strip().lower()
    additional_data = tool_data.get("additionalData")
    if (
        raw_name.lower() in {"ask_question", "askquestion"}
        and isinstance(additional_data, dict)
    ):
        interaction_status = _coerce_text(
            additional_data.get("status")
        ).strip().lower()
        if interaction_status:
            status = interaction_status
    result = tool_data.get("result")
    content = _serialized_field(result) if result not in (None, "") else ""
    if not content and status:
        content = f"Status: {status}"
    elif status and status not in {"completed", "success"}:
        content = f"Status: {status}\n\n{content}".strip()
    record = _record(
        record_type="cursor_state_tool",
        role="tool",
        source_id=source_id,
        timestamp=timestamp,
        model=model,
        reasoning_effort=reasoning_effort,
        tool_name=name,
        tool_input=_serialized_field(raw_input),
        content=content or "(tool returned no textual output)",
        tool_call_id=_bounded_text(tool_data.get("toolCallId"), 512),
        tool_status=status,
    )
    if (
        normalized_name in _CURSOR_MODE_SWITCH_TOOLS
        and isinstance(additional_data, dict)
    ):
        status_reason = _bounded_text(
            additional_data.get("skipReason") or additional_data.get("reason"),
            512,
        ).strip()
        if status_reason:
            record["tool_status_reason"] = status_reason
    return record


def _project_records(
    composer: dict[str, object],
    bubbles: list[dict[str, object]],
    header: _ComposerHeader,
    *,
    compatibility_timestamps: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    compatibility_timestamps = compatibility_timestamps or {}
    fallback_model, fallback_effort = _model_selection(composer.get("modelConfig"))
    active_model = fallback_model
    active_effort = fallback_effort
    records: list[dict[str, object]] = []
    previous_history_todos = ""

    # Keep the one mutable current-task snapshot at a stable prefix position.
    # New immutable bubbles can then travel as tiny append deltas; only a real
    # task transition requires a complete replacement.
    current_todos = composer.get("todos")
    if isinstance(current_todos, list) and current_todos:
        records.append(_task_record(
            current_todos,
            source_id=f"{header.composer_id}:tasks:current",
            timestamp=(
                _iso_timestamp(composer.get("createdAt"))
                or _iso_timestamp(header.created_at)
            ),
            model=active_model,
            reasoning_effort=active_effort,
            is_current=True,
        ))

    for bubble in bubbles:
        bubble_id = _coerce_text(bubble.get("bubbleId")).strip()
        if not bubble_id:
            continue
        timestamp = _bubble_timestamp(bubble, compatibility_timestamps)
        active_model, active_effort = _bubble_model(
            bubble,
            active_model or fallback_model,
            active_effort or fallback_effort,
        )
        bubble_type = bubble.get("type")
        text = _coerce_text(bubble.get("text")).strip()

        if bubble_type in (1, "1", "user") and text:
            records.append(_record(
                record_type="user",
                role="user",
                source_id=bubble_id,
                timestamp=timestamp,
                model=active_model,
                reasoning_effort=active_effort,
                message={"content": text},
            ))
        elif bubble_type in (2, "2", "assistant"):
            thinking = bubble.get("thinking")
            thinking_map = thinking if isinstance(thinking, dict) else {}
            thinking_text = _coerce_text(
                thinking_map.get("text") if thinking_map else thinking
            ).strip()
            if thinking_text:
                records.append(_record(
                    record_type="cursor_state_thinking",
                    role="assistant",
                    source_id=f"{bubble_id}:thinking",
                    timestamp=timestamp,
                    model=active_model,
                    reasoning_effort=active_effort,
                    message={
                        "content": [{"type": "thinking", "thinking": thinking_text}]
                    },
                    thinking_duration_ms=bubble.get("thinkingDurationMs"),
                ))
            if text:
                records.append(_record(
                    record_type="assistant",
                    role="assistant",
                    source_id=f"{bubble_id}:text" if thinking_text else bubble_id,
                    timestamp=timestamp,
                    model=active_model,
                    reasoning_effort=active_effort,
                    message={"content": text},
                ))
            tool_data = bubble.get("toolFormerData")
            if isinstance(tool_data, dict):
                records.append(_tool_record(
                    tool_data,
                    source_id=f"{bubble_id}:tool",
                    timestamp=timestamp,
                    model=active_model,
                    reasoning_effort=active_effort,
                ))

        todos = bubble.get("todos")
        if isinstance(todos, list) and todos:
            todo_key = json.dumps(todos, ensure_ascii=False, sort_keys=True, default=str)
            if todo_key != previous_history_todos:
                records.append(_task_record(
                    todos,
                    source_id=f"{bubble_id}:tasks",
                    timestamp=timestamp,
                    model=active_model,
                    reasoning_effort=active_effort,
                    is_current=False,
                ))
                previous_history_todos = todo_key

    status = _coerce_text(composer.get("status")).strip().lower()
    if status in _INTERRUPTED_STATES:
        records.append(_record(
            record_type="cursor_state_status",
            role="tool",
            source_id=f"{header.composer_id}:status:{status}",
            timestamp=(
                _iso_timestamp(header.last_updated_at)
                or _iso_timestamp(header.checkpoint_at)
            ),
            model=active_model,
            reasoning_effort=active_effort,
            tool_name="Turn interrupted",
            tool_input="",
            content="Cursor stopped this turn before completion.",
            tool_status=status,
        ))
    return records


class CursorStateExporter:
    """Incrementally discover and project changed normal Cursor composers."""

    def __init__(self, tool: CursorTool) -> None:
        self.tool = tool
        self._seen_revisions: dict[str, str] = {}
        self._transcript_paths: dict[str, Path] | None = None

    def invalidate(self) -> None:
        self._seen_revisions.clear()
        self._transcript_paths = None
        self.tool._state_session_ids_checked_at = 0.0

    def export_changed(self, *, limit: int = 8) -> list[CursorStateSnapshot]:
        database = self.tool.state_database_path
        if not database.is_file():
            return []
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=1,
            )
            connection.execute("PRAGMA query_only=ON")
            headers = self._composer_headers(connection)
            snapshots: list[CursorStateSnapshot] = []
            for header in headers:
                if self._seen_revisions.get(header.composer_id) == header.revision:
                    continue
                snapshot = self._snapshot(connection, header)
                # A stale header can outlive its composerData row. Mark that
                # exact revision observed so it cannot monopolize every poll;
                # any later native update changes the revision and retries it.
                self._seen_revisions[header.composer_id] = header.revision
                if snapshot is None:
                    continue
                snapshots.append(snapshot)
                if len(snapshots) >= limit:
                    break
            return snapshots
        except (OSError, sqlite3.Error):
            return []
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _composer_headers(connection: sqlite3.Connection) -> list[_ComposerHeader]:
        rows = connection.execute(
            """
            SELECT composerId, workspaceId, createdAt, lastUpdatedAt,
                   checkpointAt, COALESCE(isSubagent, 0), value
            FROM composerHeaders
            ORDER BY COALESCE(lastUpdatedAt, checkpointAt, createdAt) DESC
            """
        )
        return [
            _ComposerHeader(
                composer_id=_coerce_text(row[0]),
                workspace_id=_coerce_text(row[1]),
                created_at=row[2],
                last_updated_at=row[3],
                checkpoint_at=row[4],
                is_subagent=bool(row[5]),
                value=row[6],
            )
            for row in rows
            if row and row[0]
        ]

    def _snapshot(
        self,
        connection: sqlite3.Connection,
        header: _ComposerHeader,
    ) -> CursorStateSnapshot | None:
        row = connection.execute(
            "SELECT value FROM cursorDiskKV WHERE key=?",
            (f"composerData:{header.composer_id}",),
        ).fetchone()
        composer = _decode_json(row[0]) if row else None
        if not isinstance(composer, dict):
            return None

        prefix = f"bubbleId:{header.composer_id}:"
        bubble_rows = connection.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key>=? AND key<?",
            (prefix, prefix + "\uffff"),
        )
        by_id: dict[str, dict[str, object]] = {}
        for key, value in bubble_rows:
            bubble = _decode_json(value)
            if not isinstance(bubble, dict):
                continue
            bubble_id = _coerce_text(bubble.get("bubbleId")) or _coerce_text(key)[
                len(prefix):
            ]
            bubble["bubbleId"] = bubble_id
            by_id[bubble_id] = bubble

        ordered: list[dict[str, object]] = []
        seen: set[str] = set()
        headers = composer.get("fullConversationHeadersOnly")
        if isinstance(headers, list):
            for item in headers:
                if not isinstance(item, dict):
                    continue
                bubble_id = _coerce_text(item.get("bubbleId"))
                bubble = by_id.get(bubble_id)
                if bubble is not None and bubble_id not in seen:
                    ordered.append(bubble)
                    seen.add(bubble_id)
        composer_status = _coerce_text(composer.get("status")).strip().casefold()
        # Cursor can retain hundreds of superseded checkpoint bubbles under a
        # completed composer. Their IDs are unique, so content dedupe cannot
        # distinguish them from legitimate repeated turns. Once the composer
        # is terminal, fullConversationHeadersOnly is the authoritative order;
        # retain unlisted bubbles only while a live composer may still be
        # publishing a bubble before its header entry.
        if not seen or composer_status not in _TERMINAL_COMPOSER_STATES:
            ordered.extend(
                sorted(
                    (
                        bubble
                        for bubble_id, bubble in by_id.items()
                        if bubble_id not in seen
                    ),
                    key=lambda item: (
                        _coerce_text(item.get("createdAt")),
                        _coerce_text(item.get("bubbleId")),
                    ),
                )
            )

        transcript = self._transcript_path(header.composer_id)
        records = _project_records(
            composer,
            ordered,
            header,
            compatibility_timestamps=_compatibility_timestamps(transcript),
        )
        if not records:
            return None
        content = "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            for record in records
        )
        metadata, relative_path = self._metadata_and_path(
            header,
            composer,
            transcript=transcript,
        )
        return CursorStateSnapshot(
            relative_path=relative_path,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            metadata=metadata,
            # This is a newly generated authoritative snapshot, not the mtime
            # of Cursor's older sparse compatibility file.  Observation time
            # therefore orders it after that file while the records retain
            # their exact native activity timestamps.
            source_modified_at=time.time(),
        )

    def _metadata_and_path(
        self,
        header: _ComposerHeader,
        composer: dict[str, object],
        *,
        transcript: Path | None = None,
    ) -> tuple[dict[str, object], str]:
        classification = (
            self.tool.classify_transcript_source(transcript) if transcript else None
        )
        metadata = dict(classification.metadata) if classification else {
            "session_id": header.composer_id,
            "is_subagent": header.is_subagent,
        }
        relative_path = classification.relative_path if classification else ""

        header_value = _decode_json(header.value)
        header_map = header_value if isinstance(header_value, dict) else {}
        subagent_info = header_map.get("subagentInfo")
        subagent_map = subagent_info if isinstance(subagent_info, dict) else {}
        is_subagent = bool(
            header.is_subagent
            or metadata.get("is_subagent")
            or subagent_map
        )

        workspace = self._workspace_path(header.workspace_id)
        if workspace:
            metadata["project_path"] = workspace
            metadata["project_hash"] = re.split(r"[\\/]", workspace.rstrip("\\/"))[-1]
        if not relative_path:
            project_hash = self._project_hash(workspace) if workspace else "cursor-state"
            root_id = _coerce_text(
                subagent_map.get("rootParentConversationId")
                or subagent_map.get("parentComposerId")
            ).strip()
            if is_subagent and root_id:
                relative_path = (
                    f"projects/{project_hash}/agent-transcripts/{root_id}/"
                    f"subagents/{header.composer_id}.jsonl"
                )
            else:
                relative_path = (
                    f"projects/{project_hash}/agent-transcripts/"
                    f"{header.composer_id}/{header.composer_id}.jsonl"
                )

        title = _coerce_text(
            composer.get("name")
            or header_map.get("name")
            or header_map.get("title")
        ).strip()
        if title:
            metadata["title"] = title
        metadata.update({
            "source": "cursor_state_v1",
            "doc_type": "full_conversation",
            "session_id": header.composer_id,
            "is_subagent": is_subagent,
            "composer_status": _coerce_text(composer.get("status")),
        })
        if is_subagent:
            parent_id = _coerce_text(subagent_map.get("parentComposerId")).strip()
            root_id = _coerce_text(
                subagent_map.get("rootParentConversationId") or parent_id
            ).strip()
            if parent_id:
                metadata.setdefault("parent_thread_id", parent_id)
            if root_id:
                metadata.setdefault("root_session_id", root_id)
            metadata.setdefault("agent_depth", 1)
        model, effort = _model_selection(composer.get("modelConfig"))
        if model:
            metadata["model"] = model
        if effort:
            metadata["reasoning_effort"] = effort
        return metadata, relative_path

    def _transcript_path(self, session_id: str) -> Path | None:
        if self._transcript_paths is None:
            paths: dict[str, Path] = {}
            root = self.tool.root_path / "projects"
            if root.is_dir():
                for path in root.glob("**/*.jsonl"):
                    parts = path.parts
                    # Cursor can mirror a child transcript at a top-level path.
                    # The nested path carries its authoritative parent identity.
                    priority = ("subagents" not in parts, len(parts), len(str(path)))
                    current = paths.get(path.stem)
                    if current is None:
                        paths[path.stem] = path
                        continue
                    current_priority = (
                        "subagents" not in current.parts,
                        len(current.parts),
                        len(str(current)),
                    )
                    if priority < current_priority:
                        paths[path.stem] = path
            self._transcript_paths = paths
        transcript = self._transcript_paths.get(session_id)
        if transcript is not None:
            return transcript

        # The cache is initialized before some live subagents create their
        # compatibility file. Refresh just the missing session to retain the
        # native hierarchy path without rescanning every transcript.
        root = self.tool.root_path / "projects"
        if root.is_dir():
            candidates = list(root.glob(f"**/{session_id}.jsonl"))
            if candidates:
                transcript = min(
                    candidates,
                    key=lambda path: (
                        "subagents" not in path.parts,
                        len(path.parts),
                        len(str(path)),
                    ),
                )
                self._transcript_paths[session_id] = transcript
        return transcript

    def _workspace_path(self, workspace_id: str) -> str:
        if not workspace_id:
            return ""
        workspace_file = (
            self.tool.state_database_path.parent.parent
            / "workspaceStorage"
            / workspace_id
            / "workspace.json"
        )
        try:
            data = json.loads(workspace_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        folder = _coerce_text(data.get("folder")) if isinstance(data, dict) else ""
        return _workspace_folder_path(folder)

    @staticmethod
    def _project_hash(workspace: str) -> str:
        normalized = workspace.replace(":", "").replace("\\", "-").replace("/", "-")
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-")
        if re.match(r"^[A-Z]-", normalized):
            normalized = normalized[0].lower() + normalized[1:]
        return normalized or "cursor-state"


def enqueue_cursor_state_snapshots(
    exporter: CursorStateExporter,
    queue,
    *,
    limit: int = 8,
) -> int:
    """Project changed composers and enqueue complete coalescible snapshots."""
    queued = 0
    for snapshot in exporter.export_changed(limit=limit):
        snapshot_bytes = snapshot.content.encode("utf-8")
        base_hash, base_offset = queue.get_delta_base(
            "cursor", snapshot.relative_path
        )
        is_append = False
        payload = snapshot.content
        if (
            base_hash
            and 0 < base_offset < len(snapshot_bytes)
            and snapshot_bytes[base_offset:base_offset + 1] == b"\n"
            and hashlib.sha256(snapshot_bytes[:base_offset]).hexdigest() == base_hash
        ):
            payload = snapshot_bytes[base_offset + 1:].decode("utf-8")
            is_append = bool(payload)
        queue.enqueue(
            tool_name="cursor",
            category=Category.CONVERSATION.value,
            content_type=ContentType.JSONL.value,
            relative_path=snapshot.relative_path,
            content=payload,
            content_hash=snapshot.content_hash,
            file_size=len(snapshot_bytes),
            sync_strategy="delta" if is_append else "full",
            is_partial=is_append,
            offset=len(snapshot_bytes),
            metadata=snapshot.metadata,
            source_modified_at=snapshot.source_modified_at,
            base_hash=base_hash if is_append else None,
            base_offset=base_offset if is_append else 0,
            source_path=str(exporter.tool.state_database_path),
        )
        queued += 1
    return queued
