"""Authoritative task projection and hierarchical task-query service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..config import settings
from ..db.models import (
    ConversationMessage,
    ConversationTaskState,
    Document,
    Machine,
    User,
)
from .conversation_hierarchy import (
    build_conversation_companion_filter,
    conversation_root_thread_id,
    current_thread_id,
    is_conversation_subagent,
)
from .document_delivery import (
    delivery_activity_expression,
    delivery_metadata_expression,
    delivery_source_modified_expression,
    delivery_synced_expression,
)
from .user_filter import user_machine_ids

TASK_PROJECTION_VERSION = 1
CANONICAL_TASK_STATUSES = frozenset(
    {"pending", "in_progress", "blocked", "completed", "cancelled"}
)
OUTSTANDING_TASK_STATUSES = frozenset({"pending", "in_progress", "blocked"})
MAX_SELECTOR_SCAN = 500
MAX_TASK_CONTENT_CHARS = 1000
MAX_GLOBAL_HISTORY = 50
_TASK_DOCUMENT_COLUMNS = (
    Document.id,
    Document.machine_id,
    Document.tool_id,
    Document.relative_path,
    Document.category,
    Document.title,
    Document.metadata_,
    Document.activity_at,
    Document.source_modified_at,
    Document.synced_at,
    Document.file_size_bytes,
)


class TaskDocumentNotFound(LookupError):
    """An explicit document is absent or outside the authorized scope."""


class TaskSelectorAmbiguous(LookupError):
    """A logical selector resolves to several authorized root threads."""

    def __init__(self, candidates: list[dict[str, str]]) -> None:
        super().__init__("task selector is ambiguous")
        self.candidates = candidates[:10]


class TaskCursorError(ValueError):
    """A task cursor is malformed or belongs to another query."""


def _bounded(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_status(value: object) -> str:
    status = _bounded(value, 32).casefold().replace("-", "_").replace(" ", "_")
    return status if status in CANONICAL_TASK_STATUSES else "pending"


def canonical_task_state(value: object) -> dict[str, Any] | None:
    """Validate and bound a normalized task snapshot without parsing raw text."""
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return None
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_task in enumerate(value["tasks"][:200]):
        if not isinstance(raw_task, dict):
            continue
        task_id = _bounded(raw_task.get("id"), 256) or f"task-{index + 1}"
        content = _bounded(raw_task.get("content"), 16_000)
        if not content or task_id in seen:
            continue
        seen.add(task_id)
        tasks.append(
            {
                "id": task_id,
                "content": content,
                "status": _canonical_status(raw_task.get("status")),
                "active_form": _bounded(raw_task.get("active_form"), 16_000),
            }
        )
    try:
        revision = max(0, int(value.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    quality = _bounded(value.get("quality"), 32)
    if quality not in {"authoritative", "explicit_current", "partial"}:
        quality = (
            "explicit_current"
            if bool(value.get("is_current"))
            else "partial"
            if any(
                task["content"] == f"Task #{task['id']}"
                for task in tasks
            )
            else "authoritative"
        )
    source_ids = value.get("source_ids")
    return {
        "version": 1,
        "source": _bounded(value.get("source"), 64),
        "revision": revision,
        "is_current": bool(value.get("is_current")),
        "quality": quality,
        "source_ids": [
            source_id
            for item in (source_ids[-64:] if isinstance(source_ids, list) else [])
            if (source_id := _bounded(item, 256))
        ],
        "completed_count": sum(
            task["status"] == "completed" for task in tasks
        ),
        "total_count": len(tasks),
        "active_task_id": next(
            (
                task["id"]
                for task in tasks
                if task["status"] in {"in_progress", "pending"}
            ),
            "",
        ),
        "tasks": tasks,
    }


def task_state_counts(state: dict[str, Any]) -> dict[str, int]:
    counts = Counter(
        task["status"]
        for task in state.get("tasks", [])
        if isinstance(task, dict)
    )
    return {
        "pending": counts["pending"],
        "in_progress": counts["in_progress"],
        "blocked": counts["blocked"],
        "completed": counts["completed"],
        "cancelled": counts["cancelled"],
        "outstanding": sum(counts[status] for status in OUTSTANDING_TASK_STATUSES),
        "total": sum(counts.values()),
    }


def task_state_hash(state: dict[str, Any]) -> str:
    """Hash semantic current state, excluding transport revision/source IDs."""
    semantic = {
        "version": state.get("version", 1),
        "source": state.get("source", ""),
        "tasks": state.get("tasks", []),
    }
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _state_from_metadata(metadata: object) -> dict[str, Any] | None:
    values = metadata if isinstance(metadata, dict) else {}
    raw_state = values.get("task_state")
    state = canonical_task_state(raw_state)
    if state is None or not isinstance(raw_state, dict) or raw_state.get("quality"):
        return state
    compact_name = "".join(
        character
        for character in str(values.get("tool_name") or "").casefold()
        if character.isalnum()
    )
    if compact_name.endswith("taskupdate"):
        state["quality"] = "partial"
        state["is_current"] = False
    return state


def _document_identity(document: Document) -> dict[str, Any]:
    metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
    thread_id = current_thread_id(metadata) or str(document.id)
    root_id = (
        conversation_root_thread_id(
            document.tool_id,
            document.relative_path,
            metadata,
        )
        or thread_id
    )
    parent_id = _bounded(metadata.get("parent_thread_id"), 512) or None
    agent_id = _bounded(metadata.get("agent_id"), 512) or thread_id
    try:
        depth = max(0, int(metadata.get("agent_depth") or 0))
    except (TypeError, ValueError):
        depth = 0
    if is_conversation_subagent(
        document.tool_id,
        document.relative_path,
        metadata,
    ):
        depth = max(1, depth)
    return {
        "thread_id": _bounded(thread_id, 512),
        "root_thread_id": _bounded(root_id, 512),
        "parent_thread_id": parent_id,
        "agent_id": agent_id,
        "agent_path": _bounded(metadata.get("agent_path"), 4096) or None,
        "agent_depth": depth,
    }


async def current_projected_task_state(
    db: AsyncSession,
    document_id: UUID,
) -> dict[str, Any] | None:
    value = (
        await db.execute(
            select(ConversationTaskState.state).where(
                ConversationTaskState.document_id == document_id
            )
        )
    ).scalar_one_or_none()
    return canonical_task_state(value)


def _candidate_key(row: Any) -> tuple[int, int, int]:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    state = _state_from_metadata(metadata)
    explicit = int(
        state is not None
        and state["is_current"]
        and state["quality"] != "partial"
    )
    return explicit, int(row.line_number or 0), int(row.id or 0)


async def refresh_task_projection(
    db: AsyncSession,
    document: Document,
    *,
    candidate_rows: Iterable[Any] | None = None,
    replace: bool = True,
) -> ConversationTaskState | None:
    """Refresh one projection from supplied ingest rows or legacy history."""
    if candidate_rows is None:
        rows = (
            (
                await db.execute(
                    select(
                        ConversationMessage.id,
                        ConversationMessage.line_number,
                        ConversationMessage.metadata_.label("metadata_"),
                        ConversationMessage.timestamp,
                    )
                    .where(
                        ConversationMessage.document_id == document.id,
                        ConversationMessage.metadata_.op("?")("task_state"),
                    )
                    .order_by(
                        (
                            (
                                func.jsonb_extract_path_text(
                                    ConversationMessage.metadata_,
                                    "task_state",
                                    "is_current",
                                )
                                == "true"
                            )
                            & (
                                func.coalesce(
                                    func.jsonb_extract_path_text(
                                        ConversationMessage.metadata_,
                                        "task_state",
                                        "quality",
                                    ),
                                    "",
                                )
                                != "partial"
                            )
                        ).desc(),
                        ConversationMessage.line_number.desc(),
                        ConversationMessage.id.desc(),
                    )
                    .limit(20)
                )
            )
            .all()
        )
    else:
        rows = list(candidate_rows)
    candidates = [
        row
        for row in rows
        if _state_from_metadata(row.metadata_) is not None
    ]
    existing = await db.get(ConversationTaskState, document.id)
    if not candidates:
        if not replace:
            return existing
        if existing is not None:
            await db.delete(existing)
        elif candidate_rows is None:
            await db.execute(
                delete(ConversationTaskState).where(
                    ConversationTaskState.document_id == document.id
                )
            )
        return None

    source = max(candidates, key=_candidate_key)
    if existing is not None and not replace:
        existing_key = (
            int(existing.explicit_current),
            int(existing.source_line_number or 0),
            int(existing.source_message_id or 0),
        )
        # Mutable ingest rows (for example Cursor's current-task snapshot)
        # keep the same source row while their semantic state advances. An
        # equal source key must therefore flow through the hash comparison
        # below; only an older candidate is safe to ignore.
        if _candidate_key(source) < existing_key:
            return existing
    metadata = source.metadata_ if isinstance(source.metadata_, dict) else {}
    state = _state_from_metadata(metadata)
    assert state is not None
    source_ids = list(state["source_ids"])
    for value in (
        metadata.get("source_id"),
        metadata.get("tool_call_id"),
    ):
        bounded = _bounded(value, 256)
        if bounded and bounded not in source_ids:
            source_ids.append(bounded)
    state["source_ids"] = source_ids[-64:]
    state_hash = task_state_hash(state)
    counts = task_state_counts(state)
    identity = _document_identity(document)
    machine_user_id = (
        await db.execute(
            select(Machine.user_id).where(Machine.id == document.machine_id)
        )
    ).scalar_one_or_none()
    observed_at = source.timestamp or datetime.now(timezone.utc)
    explicit_current = bool(
        state["is_current"] and state["quality"] != "partial"
    )
    verified_at = observed_at if explicit_current else None

    values = {
        "machine_id": document.machine_id,
        "user_id": machine_user_id,
        "tool_id": document.tool_id,
        **identity,
        "source_message_id": source.id,
        "source_line_number": source.line_number,
        "source_ids": state["source_ids"],
        "revision": state["revision"],
        "state": state,
        "state_hash": state_hash,
        "explicit_current": explicit_current,
        "quality": state["quality"],
        "projection_version": TASK_PROJECTION_VERSION,
        "pending_count": counts["pending"],
        "in_progress_count": counts["in_progress"],
        "blocked_count": counts["blocked"],
        "completed_count": counts["completed"],
        "cancelled_count": counts["cancelled"],
        "outstanding_count": counts["outstanding"],
        "total_count": counts["total"],
        "observed_at": observed_at,
        "verified_at": verified_at,
    }
    if existing is None:
        existing = ConversationTaskState(document_id=document.id, **values)
        db.add(existing)
        await db.flush()
        return existing

    unchanged = (
        existing.state_hash == state_hash
        and existing.explicit_current == explicit_current
        and existing.quality == state["quality"]
        and existing.machine_id == document.machine_id
        and existing.tool_id == document.tool_id
        and existing.source_message_id == source.id
        and existing.source_line_number == source.line_number
        and existing.revision == state["revision"]
        and all(getattr(existing, key) == value for key, value in identity.items())
    )
    if unchanged:
        return existing
    for key, value in values.items():
        setattr(existing, key, value)
    await db.flush()
    return existing


def _query_fingerprint(values: dict[str, Any]) -> str:
    encoded = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def encode_task_cursor(offset: int, fingerprint: str) -> str:
    body = json.dumps(
        {"v": 1, "o": offset, "f": fingerprint},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()[:16]
    return base64.urlsafe_b64encode(body + signature).decode("ascii").rstrip("=")


def decode_task_cursor(value: str | None, fingerprint: str) -> int:
    if not value:
        return 0
    if len(value) > 512:
        raise TaskCursorError("cursor is too long")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        body, signature = raw[:-16], raw[-16:]
        expected = hmac.new(
            settings.secret_key.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()[:16]
        if not hmac.compare_digest(signature, expected):
            raise TaskCursorError("cursor signature is invalid")
        payload = json.loads(body)
        offset = int(payload["o"])
    except TaskCursorError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise TaskCursorError("cursor is invalid") from exc
    if (
        payload.get("v") != 1
        or payload.get("f") != fingerprint
        or not 0 <= offset <= 10_000
    ):
        raise TaskCursorError("cursor does not match this query")
    return offset


def _row_root_key(document: Document, projection: ConversationTaskState | None):
    metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
    root_id = (
        projection.root_thread_id
        if projection is not None
        else conversation_root_thread_id(
            document.tool_id,
            document.relative_path,
            metadata,
        )
    )
    return document.tool_id, str(root_id or current_thread_id(metadata) or document.id)


def _activity(document: Document) -> datetime:
    return (
        document.activity_at
        or document.source_modified_at
        or document.synced_at
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def _selector_candidates(
    groups: dict[tuple[str, str], list[tuple[Document, ConversationTaskState | None]]],
) -> list[dict[str, str]]:
    candidates = []
    for (tool_id, root_id), rows in list(groups.items())[:10]:
        document = max((row[0] for row in rows), key=_activity)
        candidates.append(
            {
                "tool": tool_id,
                "thread_id": root_id,
                "document_id": str(document.id),
                "title": _bounded(document.title, 200),
            }
        )
    return candidates


def _status_clause(status: str):
    if status == "all":
        return None
    if status == "outstanding":
        return ConversationTaskState.outstanding_count > 0
    return getattr(ConversationTaskState, f"{status}_count") > 0


def _selector_clause(name: str, value: str):
    metadata = delivery_metadata_expression()
    if name == "thread_id":
        return or_(
            ConversationTaskState.thread_id == value,
            ConversationTaskState.root_thread_id == value,
            metadata["session_id"].astext == value,
            metadata["thread_id"].astext == value,
            metadata["root_session_id"].astext == value,
        )
    if name == "agent_id":
        return or_(
            ConversationTaskState.agent_id == value,
            metadata["agent_id"].astext == value,
        )
    return and_(
        or_(
            ConversationTaskState.agent_id == value,
            ConversationTaskState.thread_id == value,
            metadata["agent_id"].astext == value,
            metadata["session_id"].astext == value,
            metadata["thread_id"].astext == value,
        ),
        or_(
            ConversationTaskState.agent_depth > 0,
            ConversationTaskState.parent_thread_id.is_not(None),
            metadata["parent_thread_id"].astext.is_not(None),
            metadata["is_subagent"].astext == "true",
        ),
    )


def _task_for_response(task: dict[str, Any]) -> dict[str, Any]:
    content = str(task.get("content") or "")
    active_form = str(task.get("active_form") or "")
    return {
        "id": _bounded(task.get("id"), 256),
        "content": content[:MAX_TASK_CONTENT_CHARS],
        "content_truncated": len(content) > MAX_TASK_CONTENT_CHARS,
        "status": _canonical_status(task.get("status")),
        "active_form": active_form[:MAX_TASK_CONTENT_CHARS],
        "active_form_truncated": len(active_form) > MAX_TASK_CONTENT_CHARS,
    }


def _take_task_budget(
    tasks: list[dict[str, Any]],
    budget: list[int],
) -> list[dict[str, Any]]:
    """Apply the shared response budget and record actual truncation."""
    if len(budget) == 1:
        budget.append(0)
    visible = tasks[: max(0, budget[0])]
    budget[0] -= len(visible)
    if len(visible) < len(tasks):
        budget[1] = 1
    return visible


def _state_for_response(
    projection: ConversationTaskState,
    *,
    status: str,
    budget: list[int],
) -> dict[str, Any]:
    state = canonical_task_state(projection.state) or {
        "tasks": [],
        "revision": 0,
        "source": projection.tool_id,
        "is_current": False,
        "quality": "partial",
        "source_ids": [],
    }
    statuses = (
        CANONICAL_TASK_STATUSES
        if status == "all"
        else OUTSTANDING_TASK_STATUSES
        if status == "outstanding"
        else {status}
    )
    matching = [
        task for task in state["tasks"] if task["status"] in statuses
    ]
    visible = _take_task_budget(matching, budget)
    counts = task_state_counts(state)
    return {
        "revision": state["revision"],
        "source_tool": state["source"],
        "explicit_current": projection.explicit_current,
        "explicit_empty": projection.total_count == 0,
        "quality": projection.quality,
        "projection_version": projection.projection_version,
        "state_hash": projection.state_hash,
        "observed_at": projection.observed_at.isoformat(),
        "verified_at": (
            projection.verified_at.isoformat()
            if projection.verified_at
            else None
        ),
        "staleness": _staleness(projection.observed_at),
        "summary": counts,
        "visible_count": len(visible),
        "tasks_truncated": len(visible) < len(matching),
        "tasks": [_task_for_response(task) for task in visible],
        "source": {
            "tool": state["source"],
            "message_id": projection.source_message_id,
            "line_number": projection.source_line_number,
            "source_ids": list(projection.source_ids or [])[:64],
        },
    }


def _staleness(observed_at: datetime) -> dict[str, Any]:
    value = observed_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    return {"age_seconds": seconds, "is_stale": seconds > 86_400}


def _node_identity(
    document: Document,
    projection: ConversationTaskState | None,
) -> dict[str, Any]:
    metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
    identity = _document_identity(document)
    model = (
        _bounded(metadata.get("_assistant_model"), 256)
        or _bounded(metadata.get("model"), 256)
        or None
    )
    effort = (
        _bounded(metadata.get("_assistant_reasoning_effort"), 128)
        or _bounded(metadata.get("reasoning_effort"), 128)
        or None
    )
    return {
        "document_id": str(document.id),
        "thread_id": (
            projection.thread_id if projection is not None else identity["thread_id"]
        ),
        "agent_id": (
            projection.agent_id if projection is not None else identity["agent_id"]
        ),
        "parent_thread_id": (
            projection.parent_thread_id
            if projection is not None
            else identity["parent_thread_id"]
        ),
        "depth": (
            projection.agent_depth
            if projection is not None
            else identity["agent_depth"]
        ),
        "path": (
            projection.agent_path if projection is not None else identity["agent_path"]
        ),
        "nickname": _bounded(metadata.get("agent_nickname"), 256) or None,
        "title": (
            _bounded(metadata.get("agent_launch_description"), 500)
            or _bounded(document.title, 500)
            or "Untitled agent"
        ),
        "model": model,
        "effort": effort,
        "activity_at": _activity(document).isoformat(),
        "navigation": {
            "document_id": str(document.id),
            "message_id": (
                projection.source_message_id if projection is not None else None
            ),
            "line_number": (
                projection.source_line_number if projection is not None else None
            ),
        },
    }


def _would_cycle(
    thread_id: str,
    parent_id: str,
    parents: dict[str, str | None],
) -> bool:
    seen = {thread_id}
    cursor: str | None = parent_id
    while cursor:
        if cursor in seen:
            return True
        seen.add(cursor)
        cursor = parents.get(cursor)
    return False


async def _task_history(
    db: AsyncSession,
    document_ids: list[UUID],
    per_document_limit: int,
) -> tuple[dict[UUID, list[dict[str, Any]]], bool]:
    if not document_ids or per_document_limit <= 0:
        return {}, False
    ranked = (
        select(
            ConversationMessage.document_id.label("document_id"),
            ConversationMessage.id.label("message_id"),
            ConversationMessage.line_number.label("line_number"),
            ConversationMessage.metadata_.label("metadata"),
            ConversationMessage.timestamp.label("timestamp"),
            func.row_number()
            .over(
                partition_by=ConversationMessage.document_id,
                order_by=(
                    ConversationMessage.timestamp.desc().nullslast(),
                    ConversationMessage.line_number.desc(),
                ),
            )
            .label("document_rank"),
        )
        .where(
            ConversationMessage.document_id.in_(document_ids),
            ConversationMessage.metadata_.op("?")("task_state"),
        )
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                ranked.c.document_id,
                ranked.c.message_id,
                ranked.c.line_number,
                ranked.c.metadata,
                ranked.c.timestamp,
            )
            .where(ranked.c.document_rank <= per_document_limit)
            .order_by(
                ranked.c.timestamp.desc().nullslast(),
                ranked.c.line_number.desc(),
            )
            .limit(MAX_GLOBAL_HISTORY + 1)
        )
    ).all()
    global_limit_truncated = len(rows) > MAX_GLOBAL_HISTORY
    rows = rows[:MAX_GLOBAL_HISTORY]
    history: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for document_id, message_id, line_number, metadata, timestamp in rows:
        if len(history[document_id]) >= per_document_limit:
            continue
        state = _state_from_metadata(metadata)
        if state is None:
            continue
        history[document_id].append(
            {
                "message_id": message_id,
                "line_number": line_number,
                "timestamp": timestamp.isoformat() if timestamp else None,
                "state": state,
            }
        )
    return history, global_limit_truncated


def _history_for_response(
    rows: list[dict[str, Any]],
    *,
    status: str,
    budget: list[int],
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        state = row["state"]
        statuses = (
            CANONICAL_TASK_STATUSES
            if status == "all"
            else OUTSTANDING_TASK_STATUSES
            if status == "outstanding"
            else {status}
        )
        matching = [
            task for task in state["tasks"] if task["status"] in statuses
        ]
        visible = _take_task_budget(matching, budget)
        result.append(
            {
                "message_id": row["message_id"],
                "line_number": row["line_number"],
                "timestamp": row["timestamp"],
                "revision": state["revision"],
                "explicit_current": state["is_current"],
                "quality": state["quality"],
                "summary": task_state_counts(state),
                "tasks_truncated": len(visible) < len(matching),
                "tasks": [_task_for_response(task) for task in visible],
            }
        )
    return result


def _build_root_response(
    root_key: tuple[str, str],
    rows: list[tuple[Document, ConversationTaskState | None]],
    *,
    status: str,
    budget: list[int],
    history: dict[UUID, list[dict[str, Any]]],
) -> dict[str, Any]:
    copies: dict[str, list[tuple[Document, ConversationTaskState | None]]] = (
        defaultdict(list)
    )
    for document, projection in rows:
        identity = _document_identity(document)
        thread_id = (
            projection.thread_id
            if projection is not None
            else identity["thread_id"]
        )
        copies[str(thread_id)].append((document, projection))
    canonical = {
        thread_id: max(
            items,
            key=lambda item: (
                item[1] is not None,
                _activity(item[0]),
                str(item[0].id),
            ),
        )
        for thread_id, items in copies.items()
    }
    parents = {
        thread_id: (
            item[1].parent_thread_id
            if item[1] is not None
            else _document_identity(item[0])["parent_thread_id"]
        )
        for thread_id, item in canonical.items()
    }
    nodes: dict[str, dict[str, Any]] = {}
    summary: Counter[str] = Counter()
    for thread_id, (document, projection) in canonical.items():
        node = _node_identity(document, projection)
        node["hierarchy_quality"] = "complete"
        node["task_state"] = (
            _state_for_response(projection, status=status, budget=budget)
            if projection is not None
            else None
        )
        if projection is not None:
            summary.update(
                task_state_counts(
                    canonical_task_state(projection.state) or {"tasks": []}
                )
            )
        node["history"] = _history_for_response(
            history.get(document.id, []),
            status=status,
            budget=budget,
        )
        node["subagents"] = []
        nodes[thread_id] = node

    top: list[dict[str, Any]] = []
    for thread_id, node in sorted(
        nodes.items(),
        key=lambda item: (item[1]["depth"], item[1]["activity_at"], item[0]),
    ):
        parent_id = parents.get(thread_id)
        if (
            not parent_id
            or parent_id == thread_id
            or parent_id not in nodes
            or _would_cycle(thread_id, parent_id, parents)
        ):
            if parent_id and parent_id not in nodes:
                node["hierarchy_quality"] = "missing_parent"
            elif parent_id:
                node["hierarchy_quality"] = "cycle"
            top.append(node)
        else:
            nodes[parent_id]["subagents"].append(node)
    for node in nodes.values():
        node["subagents"].sort(
            key=lambda child: (child["depth"], child["activity_at"], child["thread_id"])
        )
    return {
        "tool": root_key[0],
        "thread_id": root_key[1],
        "activity_at": max(node["activity_at"] for node in nodes.values()),
        "summary": {
            status_name: summary[status_name]
            for status_name in (
                "pending",
                "in_progress",
                "blocked",
                "completed",
                "cancelled",
                "outstanding",
                "total",
            )
        },
        "agents": top,
    }


async def query_conversation_tasks(
    db: AsyncSession,
    user: User,
    *,
    document_id: UUID | None = None,
    thread_id: str | None = None,
    agent_id: str | None = None,
    subagent_id: str | None = None,
    tool: str | None = None,
    status: str = "outstanding",
    include_history: bool = False,
    cursor: str | None = None,
    limit: int = 10,
    max_tasks: int = 100,
    history_limit: int = 0,
) -> dict[str, Any]:
    """Return authorized task roots and recursive agents from projections."""
    selectors = {
        "document_id": str(document_id) if document_id else None,
        "thread_id": thread_id,
        "agent_id": agent_id,
        "subagent_id": subagent_id,
        "tool": tool,
        "status": status,
        "include_history": include_history,
        "max_tasks": max_tasks,
        "history_limit": history_limit,
        "limit": limit,
    }
    fingerprint = _query_fingerprint(selectors)
    offset = decode_task_cursor(cursor, fingerprint)
    machine_ids = await user_machine_ids(db, user)
    if document_id is not None:
        authorization_criteria = [
            Document.id == document_id,
            Document.category == "conversation",
        ]
        if machine_ids is not None:
            authorization_criteria.append(Document.machine_id.in_(machine_ids))
        authorized_document = (
            await db.execute(
                select(Document.id).where(*authorization_criteria).limit(1)
            )
        ).scalar_one_or_none()
        if authorized_document is None:
            raise TaskDocumentNotFound
    criteria = [Document.category == "conversation"]
    if machine_ids is not None:
        criteria.append(Document.machine_id.in_(machine_ids))
    if document_id is not None:
        criteria.append(Document.id == document_id)
    if tool:
        criteria.append(Document.tool_id == tool)
    for name, value in (
        ("thread_id", thread_id),
        ("agent_id", agent_id),
        ("subagent_id", subagent_id),
    ):
        if value:
            criteria.append(_selector_clause(name, value))
    status_filter = _status_clause(status)
    if status_filter is not None:
        criteria.append(status_filter)

    matched = (
        await db.execute(
            select(Document, ConversationTaskState)
            .options(load_only(*_TASK_DOCUMENT_COLUMNS))
            .outerjoin(
                ConversationTaskState,
                ConversationTaskState.document_id == Document.id,
            )
            .where(*criteria)
            .order_by(
                func.coalesce(
                    delivery_activity_expression(),
                    delivery_source_modified_expression(),
                    delivery_synced_expression(),
                ).desc(),
                Document.id.desc(),
            )
            .limit(MAX_SELECTOR_SCAN + 1)
        )
    ).all()
    scan_truncated = len(matched) > MAX_SELECTOR_SCAN
    matched = matched[:MAX_SELECTOR_SCAN]
    groups: dict[
        tuple[str, str],
        list[tuple[Document, ConversationTaskState | None]],
    ] = defaultdict(list)
    for document, projection in matched:
        groups[_row_root_key(document, projection)].append((document, projection))
    if any((thread_id, agent_id, subagent_id)) and len(groups) > 1:
        raise TaskSelectorAmbiguous(_selector_candidates(groups))

    ordered_groups = sorted(
        groups,
        key=lambda key: (
            max(_activity(row[0]) for row in groups[key]),
            key,
        ),
        reverse=True,
    )
    page_keys = ordered_groups[offset : offset + limit]
    has_more = offset + limit < len(ordered_groups)
    next_cursor = (
        encode_task_cursor(offset + len(page_keys), fingerprint)
        if has_more and page_keys
        else None
    )

    if page_keys:
        roots_by_tool: dict[str, set[str]] = defaultdict(set)
        seed_ids: list[UUID] = []
        for key in page_keys:
            roots_by_tool[key[0]].add(key[1])
            seed_ids.extend(document.id for document, _projection in groups[key])
        companion = build_conversation_companion_filter(
            Document.tool_id,
            delivery_metadata_expression(),
            Document.relative_path,
            roots_by_tool,
        )
        companion_criteria = [
            Document.category == "conversation",
            or_(Document.id.in_(seed_ids), companion),
        ]
        if machine_ids is not None:
            companion_criteria.append(Document.machine_id.in_(machine_ids))
        expanded = (
            await db.execute(
                select(Document, ConversationTaskState)
                .options(load_only(*_TASK_DOCUMENT_COLUMNS))
                .outerjoin(
                    ConversationTaskState,
                    ConversationTaskState.document_id == Document.id,
                )
                .where(*companion_criteria)
            )
        ).all()
    else:
        expanded = []

    expanded_groups: dict[
        tuple[str, str],
        list[tuple[Document, ConversationTaskState | None]],
    ] = defaultdict(list)
    for document, projection in expanded:
        key = _row_root_key(document, projection)
        if key in page_keys:
            expanded_groups[key].append((document, projection))
    document_ids = [document.id for document, _projection in expanded]
    if include_history:
        history, history_global_limit_truncated = await _task_history(
            db,
            document_ids,
            history_limit,
        )
    else:
        history, history_global_limit_truncated = {}, False
    budget = [max_tasks, 0]
    roots = [
        _build_root_response(
            key,
            expanded_groups.get(key, groups[key]),
            status=status,
            budget=budget,
            history=history,
        )
        for key in page_keys
    ]
    return {
        "schema_version": 1,
        "query": selectors,
        "root_threads": roots,
        "pagination": {
            "limit": limit,
            "next_cursor": next_cursor,
            "has_more": has_more,
        },
        "truncated": {
            "selector_scan": scan_truncated,
            "tasks": bool(budget[1]),
            "history_global_limit": history_global_limit_truncated,
        },
    }


async def backfill_task_projections(
    db: AsyncSession,
    document_ids: Iterable[UUID] | None = None,
) -> dict[str, int]:
    """Idempotently project already-normalized task-state message rows."""
    statement = (
        select(Document)
        .options(load_only(*_TASK_DOCUMENT_COLUMNS))
        .join(ConversationMessage, ConversationMessage.document_id == Document.id)
        .where(
            Document.category == "conversation",
            ConversationMessage.metadata_.op("?")("task_state"),
        )
        .distinct()
    )
    if document_ids is not None:
        statement = statement.where(Document.id.in_(list(document_ids)))
    documents = (await db.execute(statement)).scalars().all()
    created_or_updated = 0
    for document in documents:
        before = await db.get(ConversationTaskState, document.id)
        before_signature = (
            (
                before.state_hash,
                before.source_message_id,
                before.source_line_number,
                before.source_ids,
                before.revision,
                before.explicit_current,
                before.quality,
                before.projection_version,
            )
            if before is not None
            else None
        )
        projection = await refresh_task_projection(db, document)
        after_signature = (
            (
                projection.state_hash,
                projection.source_message_id,
                projection.source_line_number,
                projection.source_ids,
                projection.revision,
                projection.explicit_current,
                projection.quality,
                projection.projection_version,
            )
            if projection is not None
            else None
        )
        if after_signature != before_signature:
            created_or_updated += 1
    return {
        "documents": len(documents),
        "created_or_updated": created_or_updated,
    }
