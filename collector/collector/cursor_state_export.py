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
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from .tools.base import Category, ContentType
from .tools.cursor import CursorTool

_MAX_TOOL_FIELD_CHARS = 262_144
_INTERRUPTED_STATES = {"aborted", "cancelled", "canceled", "interrupted"}
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
    last_updated_at: object
    checkpoint_at: object
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
    if value in (None, ""):
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
    if numeric > 10_000_000_000:
        numeric /= 1000
    return numeric


def _iso_timestamp(value: object) -> str:
    text = _coerce_text(value).strip()
    if "T" in text:
        return text
    seconds = _timestamp_seconds(value)
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


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
    raw_input = tool_data.get("rawArgs") or tool_data.get("params") or ""
    status = _coerce_text(tool_data.get("status")).strip().lower()
    result = tool_data.get("result")
    content = _serialized_field(result) if result not in (None, "") else ""
    if not content and status:
        content = f"Status: {status}"
    elif status and status not in {"completed", "success"}:
        content = f"Status: {status}\n\n{content}".strip()
    return _record(
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


def _project_records(
    composer: dict[str, object],
    bubbles: list[dict[str, object]],
    header: _ComposerHeader,
) -> list[dict[str, object]]:
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
            timestamp=_iso_timestamp(composer.get("createdAt")),
            model=active_model,
            reasoning_effort=active_effort,
            is_current=True,
        ))

    for bubble in bubbles:
        bubble_id = _coerce_text(bubble.get("bubbleId")).strip()
        if not bubble_id:
            continue
        timestamp = _iso_timestamp(bubble.get("createdAt"))
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
            timestamp=_iso_timestamp(header.last_updated_at),
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
            SELECT composerId, workspaceId, lastUpdatedAt, checkpointAt, value
            FROM composerHeaders
            WHERE COALESCE(isSubagent, 0)=0
            ORDER BY lastUpdatedAt DESC
            """
        )
        return [
            _ComposerHeader(
                composer_id=_coerce_text(row[0]),
                workspace_id=_coerce_text(row[1]),
                last_updated_at=row[2],
                checkpoint_at=row[3],
                value=row[4],
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
        ordered.extend(
            sorted(
                (bubble for bubble_id, bubble in by_id.items() if bubble_id not in seen),
                key=lambda item: (
                    _coerce_text(item.get("createdAt")),
                    _coerce_text(item.get("bubbleId")),
                ),
            )
        )

        records = _project_records(composer, ordered, header)
        if not records:
            return None
        content = "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            for record in records
        )
        metadata, relative_path = self._metadata_and_path(header, composer)
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
    ) -> tuple[dict[str, object], str]:
        transcript = self._transcript_path(header.composer_id)
        classification = (
            self.tool.classify_transcript_source(transcript) if transcript else None
        )
        metadata = dict(classification.metadata) if classification else {
            "session_id": header.composer_id,
            "is_subagent": False,
        }
        relative_path = classification.relative_path if classification else ""

        workspace = self._workspace_path(header.workspace_id)
        if workspace:
            metadata["project_path"] = workspace
            metadata["project_hash"] = re.split(r"[\\/]", workspace.rstrip("\\/"))[-1]
        if not relative_path:
            project_hash = self._project_hash(workspace) if workspace else "cursor-state"
            relative_path = (
                f"projects/{project_hash}/agent-transcripts/"
                f"{header.composer_id}/{header.composer_id}.jsonl"
            )

        header_value = _decode_json(header.value)
        header_map = header_value if isinstance(header_value, dict) else {}
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
            "is_subagent": False,
            "composer_status": _coerce_text(composer.get("status")),
        })
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
                    priority = ("subagents" in parts, len(parts), len(str(path)))
                    current = paths.get(path.stem)
                    if current is None:
                        paths[path.stem] = path
                        continue
                    current_priority = (
                        "subagents" in current.parts,
                        len(current.parts),
                        len(str(current)),
                    )
                    if priority < current_priority:
                        paths[path.stem] = path
            self._transcript_paths = paths
        return self._transcript_paths.get(session_id)

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
        if folder.startswith("file:///"):
            return unquote(folder[8:])
        return ""

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
