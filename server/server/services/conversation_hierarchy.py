"""Presentation helpers for Codex root threads and their subagents.

Codex stores every fork as its own conversation document.  The collector
preserves those documents because each child is independently useful, but
list surfaces should present the root once and describe how many descendants
belong to it.  This module deliberately only decides visibility/counts; it
never combines transcripts because a child can contain a cloned copy of its
parent's history.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Hashable, Iterable, Mapping

from sqlalchemy import and_, false, or_

from .conversation_activity import effective_conversation_activity
from .subagent_lifecycle import (
    SUBAGENT_TERMINAL_STATUSES,
    normalized_subagent_runtime,
    normalized_subagent_status,
    persisted_child_lifecycle,
    subagent_runtime_from_metadata,
)


FOLDABLE_CONVERSATION_TOOLS = frozenset({
    "codex",
    "claude_code",
    "cursor",
})
_PATH_LINKED_SUBAGENT_TOOLS = frozenset({"claude_code", "cursor"})
_BRIEFING_SESSION_ID = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
HANDOFF_MARKER_PREFIX = "MEMENTO-HANDOFF-FROM:"
TANGENT_MARKER_PREFIX = "MEMENTO-TANGENT-FROM:"
DELEGATE_MARKER_PREFIX = "MEMENTO-DELEGATE-FROM:"
_HANDOFF_MARKER_RE = re.compile(
    rf"\A{re.escape(HANDOFF_MARKER_PREFIX)}\s*(?P<session_id>"
    rf"{_BRIEFING_SESSION_ID})(?=\s|\Z)"
)
_TANGENT_MARKER_RE = re.compile(
    rf"\A{re.escape(TANGENT_MARKER_PREFIX)}\s*(?P<session_id>"
    rf"{_BRIEFING_SESSION_ID})(?=\s|\Z)"
)
_DELEGATE_MARKER_RE = re.compile(
    rf"\A{re.escape(DELEGATE_MARKER_PREFIX)}\s*(?P<session_id>"
    rf"{_BRIEFING_SESSION_ID})(?=\s|\Z)"
)
_BRIEFING_MARKER_PATTERNS = (
    ("handoff", _HANDOFF_MARKER_RE),
    ("tangent", _TANGENT_MARKER_RE),
    ("delegate", _DELEGATE_MARKER_RE),
)


def _metadata_flag_is_true(value: object) -> bool:
    """Accept JSON booleans and their serialized equivalents without truthiness."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    return False


def conversation_briefing_kind(content: object) -> str | None:
    """Classify a first-user-message protocol marker, if one is present."""
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    if text.startswith(HANDOFF_MARKER_PREFIX):
        return "handoff"
    if text.startswith(TANGENT_MARKER_PREFIX):
        return "tangent"
    if text.startswith(DELEGATE_MARKER_PREFIX):
        return "delegate"
    return None


def conversation_briefing_session_id(content: object) -> str | None:
    """Return the UUID carried by a first-user-message protocol marker."""
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    for _kind, pattern in _BRIEFING_MARKER_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        try:
            return str(uuid.UUID(match.group("session_id")))
        except (ValueError, AttributeError):
            return None
    return None


def conversation_is_chain_primary(
    metadata: Mapping[str, Any] | None,
    *,
    first_user_content: str | None = None,
) -> bool:
    """Return whether this thread is a handoff successor or tangent primary."""
    content = first_user_content
    if content is None:
        content = str((metadata or {}).get("first_user_message") or "")
    return conversation_briefing_kind(content) in {"handoff", "tangent"}


def conversation_message_user_origin(
    role: str | None,
    metadata: Mapping[str, Any] | None,
    thread_user_role_origin: str | None,
) -> str | None:
    """Prefer a persisted per-message origin over thread-level classification."""
    if (role or "") != "user":
        return None
    stored = str((metadata or {}).get("message_origin") or "").strip()
    if stored in {"human", "parent_agent"}:
        return stored
    return thread_user_role_origin


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """The small subset of a conversation document needed for presentation."""

    document_id: Hashable
    tool_id: str | None
    relative_path: str | None
    metadata: Mapping[str, Any] | None
    title: str | None = None
    source_modified_at: datetime | None = None
    activity_at: datetime | None = None
    synced_at: datetime | None = None
    file_size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class ConversationHierarchy:
    """Visibility and annotation decisions keyed by document id."""

    visible_document_ids: frozenset[Hashable]
    subagent_counts: Mapping[Hashable, int]
    orphan_document_ids: frozenset[Hashable]
    subagent_document_ids: Mapping[Hashable, tuple[Hashable, ...]]
    canonical_document_ids: Mapping[Hashable, Hashable]


def current_thread_id(metadata: Mapping[str, Any] | None) -> str | None:
    """Return the UUID identifying this document's thread, including legacy data."""

    values = metadata or {}
    value = (
        values.get("session_id")
        or values.get("thread_id")
        or values.get("cascade_id")
    )
    return str(value) if value else None


def explicit_subagent_parent_thread_id(relative_path: str | None) -> str | None:
    """Return the parent UUID encoded by Claude/Cursor subagent paths."""
    path = (relative_path or "").replace("\\", "/")
    if "/subagents/" not in path:
        return None
    parent_base = path.split("/subagents/", 1)[0].rstrip("/")
    parent_thread_id = parent_base.rsplit("/", 1)[-1]
    return parent_thread_id or None


def path_linked_subagent_identity(relative_path: str | None) -> dict[str, Any]:
    """Derive shared child-thread identity fields from a ``/subagents/`` path.

    Codex children carry these fields in session metadata. Claude Code and
    Cursor encode the same relationship in the transcript path, so normalize
    once here for collectors, ingest, and presentation.
    """
    path = (relative_path or "").replace("\\", "/")
    if "/subagents/" not in path:
        return {}
    root_base = path.split("/subagents/", 1)[0].rstrip("/")
    root_thread_id = root_base.rsplit("/", 1)[-1]
    parent_base = path.rsplit("/subagents/", 1)[0].rstrip("/")
    parent_thread_id = parent_base.rsplit("/", 1)[-1]
    if not root_thread_id or not parent_thread_id:
        return {}
    return {
        "is_subagent": True,
        "parent_thread_id": parent_thread_id,
        "root_session_id": root_thread_id,
        "agent_depth": path.count("/subagents/"),
    }


def is_conversation_subagent(
    tool_id: str | None,
    relative_path: str | None,
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Recognize native subagent records without inspecting transcript text."""
    if tool_id not in FOLDABLE_CONVERSATION_TOOLS:
        return False
    values = metadata or {}
    if values.get("orchestration_parent_document_id"):
        return True
    if (
        str(values.get("orchestration") or "").strip() == "claw"
        and _metadata_flag_is_true(values.get("is_subagent"))
    ):
        return True
    if (
        tool_id == "codex"
        and str(values.get("thread_source") or "").strip().lower()
        == "subagent"
        and bool(values.get("root_session_id"))
    ):
        return True
    return (
        tool_id in _PATH_LINKED_SUBAGENT_TOOLS
        and explicit_subagent_parent_thread_id(relative_path) is not None
        and (
            _metadata_flag_is_true(values.get("is_subagent"))
            or "/subagents/" in (relative_path or "").replace("\\", "/")
        )
    )


def conversation_display_title(
    tool_id: str | None,
    relative_path: str | None,
    metadata: Mapping[str, Any] | None,
    source_title: str | None,
) -> str | None:
    """Return a presentation title without changing the source document title."""
    values = metadata or {}
    if (
        tool_id == "claude_code"
        and (
            is_conversation_subagent(tool_id, relative_path, values)
            or (
                _metadata_flag_is_true(values.get("is_subagent"))
                and bool(
                    values.get("parent_thread_id")
                    or values.get("root_session_id")
                )
            )
        )
    ):
        launch_description = str(
            values.get("agent_launch_description") or ""
        ).strip()
        if launch_description:
            return launch_description
    return source_title


def conversation_user_role_origin(
    tool_id: str | None,
    relative_path: str | None,
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Identify child-thread user turns that were dispatched by a parent agent."""
    values = metadata or {}
    if conversation_is_chain_primary(values):
        return None
    if str(values.get("orchestration") or "").strip() == "claw":
        return "parent_agent"
    if values.get("orchestration_parent_document_id"):
        return "parent_agent"
    if (
        tool_id in {"claude_code", "cursor"}
        and (
            is_conversation_subagent(tool_id, relative_path, metadata)
            or (
                _metadata_flag_is_true(values.get("is_subagent"))
                and bool(
                    values.get("parent_thread_id")
                    or values.get("root_session_id")
                )
            )
        )
    ):
        return "parent_agent"
    return None


def conversation_root_thread_id(
    tool_id: str | None,
    relative_path: str | None,
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return the logical root ID shared by a root and all its children."""
    if tool_id not in FOLDABLE_CONVERSATION_TOOLS:
        return None
    values = metadata or {}
    if is_conversation_subagent(tool_id, relative_path, values):
        if values.get("root_session_id"):
            return str(values["root_session_id"])
        return explicit_subagent_parent_thread_id(relative_path)
    return current_thread_id(values)


def group_conversation_root_thread_ids(
    conversations: Iterable[ConversationRef],
    *,
    path_children_only: bool = False,
) -> dict[str, set[str]]:
    """Group represented logical roots by tool for companion queries."""
    roots: dict[str, set[str]] = {}
    for ref in conversations:
        if (
            path_children_only
            and ref.tool_id in _PATH_LINKED_SUBAGENT_TOOLS
            and not is_conversation_subagent(
                ref.tool_id,
                ref.relative_path,
                ref.metadata,
            )
        ):
            continue
        root_thread_id = conversation_root_thread_id(
            ref.tool_id,
            ref.relative_path,
            ref.metadata,
        )
        if ref.tool_id and root_thread_id:
            roots.setdefault(ref.tool_id, set()).add(root_thread_id)
    return roots


def build_conversation_companion_filter(
    tool_column,
    metadata_column,
    relative_path_column,
    roots_by_tool: Mapping[str, Iterable[str]],
):
    """Build one reusable SQL predicate for roots, copies, and children."""
    tool_scopes = []
    for tool_id, root_values in roots_by_tool.items():
        root_ids = sorted({str(value) for value in root_values if value})
        if tool_id not in FOLDABLE_CONVERSATION_TOOLS or not root_ids:
            continue
        companion_clauses = [
            metadata_column["session_id"].astext.in_(root_ids),
            metadata_column["thread_id"].astext.in_(root_ids),
            metadata_column["root_session_id"].astext.in_(root_ids),
        ]
        if tool_id in _PATH_LINKED_SUBAGENT_TOOLS:
            companion_clauses.extend(
                relative_path_column.like(f"%/{root_id}/subagents/%")
                for root_id in root_ids
            )
        tool_scopes.append(
            and_(tool_column == tool_id, or_(*companion_clauses))
        )
    return or_(*tool_scopes) if tool_scopes else false()


def fold_conversation_subagents(
    conversations: Iterable[ConversationRef],
) -> ConversationHierarchy:
    """Hide linked Codex, Claude Code, and Cursor children under their root.

    Codex links children through metadata. Claude Code and Cursor encode the
    parent session in their native ``/subagents/`` path. Descendants are
    counted by their own session/thread ID rather than document rows. If the
    root has not arrived yet, one deterministic child remains visible so the
    group never disappears from the UI.
    """

    refs = list(conversations)
    visible_ids = {ref.document_id for ref in refs}
    canonical_document_ids = {
        ref.document_id: ref.document_id
        for ref in refs
    }
    roots_by_thread: dict[tuple[str, str], list[ConversationRef]] = {}
    children_by_root: dict[tuple[str, str], list[ConversationRef]] = {}

    for ref in refs:
        root_thread_id = conversation_root_thread_id(
            ref.tool_id,
            ref.relative_path,
            ref.metadata,
        )
        if not ref.tool_id or not root_thread_id:
            continue
        root_key = (ref.tool_id, root_thread_id)
        if is_conversation_subagent(
            ref.tool_id,
            ref.relative_path,
            ref.metadata,
        ):
            children_by_root.setdefault(root_key, []).append(ref)
        else:
            roots_by_thread.setdefault(root_key, []).append(ref)

    subagent_counts: dict[Hashable, int] = {}
    orphan_ids: set[Hashable] = set()
    subagent_document_ids: dict[Hashable, tuple[Hashable, ...]] = {}
    canonical_roots: dict[tuple[str, str], ConversationRef] = {}

    # The same Codex data can be uploaded by several machines.  Canonicalize
    # those root rows by logical thread UUID before applying child groups so a
    # multi-host sync still renders exactly one top-level card.
    for root_key, roots in roots_by_thread.items():
        canonical = max(roots, key=_canonical_root_sort_key)
        canonical_roots[root_key] = canonical
        visible_ids.difference_update(
            root.document_id
            for root in roots
            if root.document_id != canonical.document_id
        )
        for root in roots:
            canonical_document_ids[root.document_id] = canonical.document_id

    for root_key, children in children_by_root.items():
        # A thread UUID identifies the logical child.  Fall back to the
        # document UUID for malformed/older metadata so it is still counted.
        children_by_thread: dict[str, list[ConversationRef]] = {}
        for child in children:
            child_thread_id = (
                current_thread_id(child.metadata) or str(child.document_id)
            )
            children_by_thread.setdefault(child_thread_id, []).append(child)
        canonical_children = [
            max(copies, key=_canonical_root_sort_key)
            for copies in children_by_thread.values()
        ]
        canonical_children.sort(key=_orphan_sort_key)
        count = len(canonical_children)
        root = canonical_roots.get(root_key)

        if root is not None:
            visible_ids.difference_update(child.document_id for child in children)
            for child in children:
                canonical_document_ids[child.document_id] = root.document_id
            subagent_counts[root.document_id] = count
            subagent_document_ids[root.document_id] = tuple(
                child.document_id for child in canonical_children
            )
            continue

        representative = canonical_children[0]
        visible_ids.difference_update(
            child.document_id
            for child in children
            if child.document_id != representative.document_id
        )
        for child in children:
            canonical_document_ids[child.document_id] = representative.document_id
        orphan_ids.add(representative.document_id)
        subagent_counts[representative.document_id] = count
        subagent_document_ids[representative.document_id] = tuple(
            child.document_id for child in canonical_children
        )

    # Cross-tool orchestrators cannot share a native thread/root ID. Their
    # normalized relation uses exact document UUIDs and is applied only when
    # both documents are present in this read set. A child whose normalized
    # relation is known to be resolved can still be suppressed in a tool-scoped
    # list where its cross-tool parent is intentionally outside the query. A
    # delayed or malformed relation remains visible and is marked orphaned.
    refs_by_string_id = {str(ref.document_id): ref for ref in refs}
    for child in refs:
        parent_id = str(
            (child.metadata or {}).get("orchestration_parent_document_id") or ""
        ).strip()
        if not parent_id:
            continue
        parent = refs_by_string_id.get(parent_id)
        if parent is None or parent.document_id == child.document_id:
            if (
                parent is None
                and (child.metadata or {}).get(
                    "orchestration_relation_resolved"
                ) is True
            ):
                visible_ids.discard(child.document_id)
                canonical_document_ids[child.document_id] = parent_id
                continue
            orphan_ids.add(child.document_id)
            continue
        canonical_parent_id = canonical_document_ids.get(
            parent.document_id,
            parent.document_id,
        )
        visible_ids.discard(child.document_id)
        canonical_document_ids[child.document_id] = canonical_parent_id
        existing_children = list(subagent_document_ids.get(canonical_parent_id, ()))
        if child.document_id not in existing_children:
            existing_children.append(child.document_id)
        subagent_document_ids[canonical_parent_id] = tuple(existing_children)
        subagent_counts[canonical_parent_id] = len(existing_children)

    for child in refs:
        values = child.metadata or {}
        if str(values.get("orchestration") or "").strip() != "claw":
            continue
        if conversation_is_chain_primary(values):
            continue
        if str(values.get("orchestration_parent_document_id") or "").strip():
            continue
        if not _metadata_flag_is_true(values.get("is_subagent")):
            continue
        orphan_ids.add(child.document_id)

    return ConversationHierarchy(
        visible_document_ids=frozenset(visible_ids),
        subagent_counts=subagent_counts,
        orphan_document_ids=frozenset(orphan_ids),
        subagent_document_ids=subagent_document_ids,
        canonical_document_ids=canonical_document_ids,
    )


def fold_codex_subagents(
    conversations: Iterable[ConversationRef],
) -> ConversationHierarchy:
    """Compatibility alias for the now cross-tool folding implementation."""
    return fold_conversation_subagents(conversations)


def build_subagent_summaries(
    hierarchy: ConversationHierarchy,
    conversations: Iterable[ConversationRef],
) -> dict[Hashable, list[dict[str, Any]]]:
    """Build navigable child-thread metadata for each visible root card."""

    refs_by_id = {ref.document_id: ref for ref in conversations}
    summaries: dict[Hashable, list[dict[str, Any]]] = {}
    for parent_id, child_ids in hierarchy.subagent_document_ids.items():
        children: list[dict[str, Any]] = []
        for child_id in child_ids:
            child = refs_by_id.get(child_id)
            if child is None:
                continue
            metadata = child.metadata or {}
            thread_id = current_thread_id(metadata)
            orchestration = str(metadata.get("orchestration") or "").strip() or None
            orchestration_name = (
                str(metadata.get("orchestration_agent_name") or "").strip()
                or None
            )
            orchestration_codename = (
                str(metadata.get("orchestration_agent_codename") or "").strip()
                or None
            )
            agent_id = str(metadata.get("agent_id") or "").strip() or None
            agent_tool_use_id = (
                str(metadata.get("agent_tool_use_id") or "").strip() or None
            )
            nickname = orchestration_codename or metadata.get("agent_nickname")
            launch_description = (
                str(metadata.get("agent_launch_description") or "").strip()
                if child.tool_id == "claude_code"
                else ""
            ) or None
            agent_path = metadata.get("agent_path")
            agent_path_label = (
                str(agent_path).strip("/").rsplit("/", 1)[-1]
                if agent_path
                else None
            )
            if agent_path_label:
                agent_path_label = " ".join(
                    agent_path_label.replace("_", " ").replace("-", " ").split()
                )
            try:
                agent_depth = int(metadata["agent_depth"])
            except (KeyError, TypeError, ValueError):
                agent_depth = None
            parent_thread_id = metadata.get("parent_thread_id")
            timestamp = effective_conversation_timestamp(child)
            runtime = subagent_runtime_from_metadata(metadata)
            lifecycle = persisted_child_lifecycle(metadata)
            lifecycle_status = (
                lifecycle.get("status") if lifecycle is not None else None
            )
            lifecycle_at = (
                lifecycle.get("timestamp") if lifecycle is not None else None
            )
            children.append({
                "id": str(child.document_id),
                "session_id": thread_id,
                "agent_id": agent_id,
                "agent_tool_use_id": agent_tool_use_id,
                "title": (
                    orchestration_name
                    or orchestration_codename
                    or launch_description
                    or agent_path_label
                    or (str(nickname) if nickname else None)
                    or child.title
                    or (f"Subagent {thread_id[:8]}" if thread_id else "Subagent")
                ),
                "agent_nickname": str(nickname) if nickname else None,
                "orchestration": orchestration,
                "orchestration_run_id": metadata.get("orchestration_run_id"),
                "orchestration_run_kind": metadata.get("orchestration_run_kind"),
                "orchestration_agent_key": metadata.get("orchestration_agent_key"),
                "tool_id": child.tool_id,
                "agent_path": str(agent_path) if agent_path else None,
                "agent_depth": agent_depth,
                "parent_thread_id": (
                    str(parent_thread_id) if parent_thread_id else None
                ),
                "relative_path": child.relative_path,
                "timestamp": timestamp.isoformat() if timestamp else None,
                "activity_at": timestamp.isoformat() if timestamp else None,
                "synced_at": (
                    child.synced_at.isoformat() if child.synced_at else None
                ),
                "user_role_origin": conversation_user_role_origin(
                    child.tool_id,
                    child.relative_path,
                    metadata,
                ),
                "model": runtime.get("model"),
                "model_family": runtime.get("model_family"),
                "reasoning_effort": runtime.get("reasoning_effort"),
                "status": lifecycle_status or "unknown",
                "status_source": (
                    lifecycle.get("source") if lifecycle is not None else None
                ),
                "last_event_at": lifecycle_at,
                "started_at": None,
                "completed_at": (
                    lifecycle_at
                    if lifecycle_status in SUBAGENT_TERMINAL_STATUSES
                    else None
                ),
            })
        if children:
            summaries[parent_id] = children
    return summaries


def merge_subagent_event_summaries(
    summaries: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge persisted lifecycle events into navigable child summaries.

    A Codex parent records ``sub_agent_activity`` immediately, while a newly
    forked child can take minutes to upload and normalize because its rollout
    contains inherited history.  Returning a pending summary from the parent
    event keeps the task visible during that window.  Once the child document
    arrives, its real title/nickname/navigation target wins and the lifecycle
    fields are overlaid without producing a duplicate card.
    """

    merged = [dict(summary) for summary in summaries]
    by_thread: dict[str, dict[str, Any]] = {}
    by_tool_use: dict[str, dict[str, Any]] = {}

    def register(summary: dict[str, Any]) -> None:
        for value in (summary.get("session_id"), summary.get("agent_id")):
            identity = str(value or "").strip()
            if identity:
                by_thread[identity] = summary
        tool_use_id = str(summary.get("agent_tool_use_id") or "").strip()
        if tool_use_id:
            by_tool_use[tool_use_id] = summary

    for summary in merged:
        register(summary)

    terminal_statuses = set(SUBAGENT_TERMINAL_STATUSES)
    for item in events:
        thread_id = str(item.get("agent_thread_id") or "").strip()
        tool_use_id = str(item.get("agent_tool_use_id") or "").strip()
        agent_path = str(item.get("agent_path") or "").strip()
        if not thread_id and not tool_use_id:
            continue
        kind = str(item.get("kind") or "updated").strip().casefold()
        status = normalized_subagent_status(item.get("resolved_status"))
        raw_status = normalized_subagent_status(item.get("status"))
        if status is None and kind == "interrupted" and raw_status == "cancelled":
            status = "cancelled"
        status = status or {
            "started": "running",
            "updated": "running",
            "completed": "completed",
            "interrupted": "interrupted",
            "failed": "failed",
        }.get(kind, "unknown")
        event_timestamp = (
            item.get("status_updated_at")
            if item.get("resolved_status")
            else None
        ) or item.get("timestamp")
        event_runtime = normalized_subagent_runtime(
            model=item.get("model"),
            reasoning_effort=item.get("reasoning_effort"),
        )
        started_at = item.get("started_at") or (
            event_timestamp if kind == "started" else None
        )
        completed_at = item.get("completed_at") or (
            event_timestamp
            if status in terminal_statuses
            else None
        )
        existing = (
            by_tool_use.get(tool_use_id)
            if tool_use_id
            else None
        ) or (
            by_thread.get(thread_id)
            if thread_id
            else None
        )
        if existing is None:
            label = str(item.get("label") or "").strip()
            if not label:
                label = (
                    " ".join(
                        agent_path.rstrip("/").rsplit("/", 1)[-1]
                        .replace("_", " ")
                        .replace("-", " ")
                        .split()
                    )
                    if agent_path
                    else ""
                ) or "Subagent"
            existing = {
                "id": None,
                "session_id": thread_id or None,
                "agent_id": thread_id or None,
                "agent_tool_use_id": tool_use_id or None,
                "title": label,
                "agent_nickname": None,
                "agent_path": agent_path or None,
                "agent_depth": item.get("agent_depth"),
                "parent_thread_id": item.get("parent_thread_id"),
                "relative_path": None,
                "timestamp": item.get("timestamp"),
                "activity_at": item.get("timestamp"),
                "synced_at": None,
                "document_ready": False,
                "user_role_origin": item.get("user_role_origin"),
                "model": event_runtime.get("model"),
                "model_family": event_runtime.get("model_family"),
                "reasoning_effort": event_runtime.get("reasoning_effort"),
                "status_source": item.get("status_source"),
                "started_at": started_at,
                "completed_at": completed_at,
            }
            merged.append(existing)
            register(existing)
        else:
            existing["document_ready"] = bool(existing.get("id"))
            # Path-linked children often arrive with a first-prompt title and no
            # agent_path. Overlay the shared lifecycle identity when missing so
            # Cursor/Claude cards match Codex presentation quality.
            filled_agent_path = bool(agent_path and not existing.get("agent_path"))
            if filled_agent_path:
                existing["agent_path"] = agent_path
            label = str(item.get("label") or "").strip()
            if (
                label
                and label != "Subagent"
                and (
                    tool_use_id
                    or filled_agent_path
                )
            ):
                existing["title"] = label
            if thread_id and not existing.get("agent_id"):
                existing["agent_id"] = thread_id
            if thread_id and not existing.get("session_id"):
                existing["session_id"] = thread_id
            if tool_use_id and not existing.get("agent_tool_use_id"):
                existing["agent_tool_use_id"] = tool_use_id
            if item.get("parent_thread_id") and not existing.get("parent_thread_id"):
                existing["parent_thread_id"] = item.get("parent_thread_id")
            if item.get("agent_depth") is not None and existing.get("agent_depth") is None:
                existing["agent_depth"] = item.get("agent_depth")
            register(existing)
        if event_runtime.get("model") and not existing.get("model"):
            existing["model"] = event_runtime["model"]
            existing["model_family"] = event_runtime.get("model_family")
        if (
            event_runtime.get("reasoning_effort")
            and not existing.get("reasoning_effort")
        ):
            existing["reasoning_effort"] = event_runtime["reasoning_effort"]
        if started_at and not existing.get("started_at"):
            existing["started_at"] = started_at
        if completed_at:
            existing["completed_at"] = completed_at
        previous_status = str(existing.get("status") or "unknown")
        # A terminal source event is sticky for a unique launch tool-use ID.
        # Replayed/out-of-order launch rows must never turn a finished child green.
        if not (
            previous_status in terminal_statuses | {"disconnected"}
            and status == "running"
        ):
            existing["status"] = status
            existing["last_event_at"] = event_timestamp
            if item.get("status_source"):
                existing["status_source"] = item.get("status_source")

    for summary in merged:
        summary.setdefault("document_ready", bool(summary.get("id")))
        summary.setdefault("status", "unknown")
        summary.setdefault("status_source", None)
        summary.setdefault("last_event_at", None)
        summary.setdefault("agent_id", None)
        summary.setdefault("agent_tool_use_id", None)
        summary.setdefault("user_role_origin", None)
        summary.setdefault("model", None)
        summary.setdefault("model_family", None)
        summary.setdefault("reasoning_effort", None)
        summary.setdefault("started_at", None)
        summary.setdefault("completed_at", None)
    return merged


def merge_authoritative_subagent_summaries(
    summaries: Iterable[dict[str, Any]],
    authoritative: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay durable orchestrator identity/status and retain native details."""
    merged = [dict(summary) for summary in summaries]
    by_tool_use = {
        str(summary.get("agent_tool_use_id")): index
        for index, summary in enumerate(merged)
        if summary.get("agent_tool_use_id")
    }
    for overlay in authoritative:
        projected = dict(overlay)
        tool_use_id = str(projected.get("agent_tool_use_id") or "")
        index = by_tool_use.get(tool_use_id) if tool_use_id else None
        if index is None:
            merged.append(projected)
            if tool_use_id:
                by_tool_use[tool_use_id] = len(merged) - 1
            continue
        combined = dict(merged[index])
        combined.update(
            {
                key: value
                for key, value in projected.items()
                if value is not None
            }
        )
        combined["document_ready"] = bool(combined.get("id"))
        merged[index] = combined
    return merged


def build_logical_activity_map(
    hierarchy: ConversationHierarchy,
    conversations: Iterable[ConversationRef],
) -> dict[Hashable, datetime]:
    """Return outward effective activity for each visible logical thread.

    Every copy and metadata-linked subagent is already mapped to its visible
    root by ``canonical_document_ids``.  Persisted ``activity_at`` remains a
    real user/assistant message time only.  Legacy transcripts without such a
    timestamp fall back here to source modification time and finally sync time
    so presentation remains chronological without writing import time into the
    activity column.
    """
    activity: dict[Hashable, datetime] = {}
    for ref in conversations:
        effective_activity = effective_conversation_timestamp(ref)
        if effective_activity is None:
            continue
        canonical_id = hierarchy.canonical_document_ids.get(
            ref.document_id,
            ref.document_id,
        )
        if canonical_id not in hierarchy.visible_document_ids:
            continue
        previous = activity.get(canonical_id)
        if previous is None or effective_activity > previous:
            activity[canonical_id] = effective_activity
    return activity


def effective_conversation_timestamp(ref: ConversationRef) -> datetime | None:
    """Prefer transcript activity while retaining legacy ordering fallback."""
    return effective_conversation_activity(
        ref.activity_at,
        ref.source_modified_at,
        ref.synced_at,
    )


def _orphan_sort_key(ref: ConversationRef) -> tuple[int, float, int, str]:
    """Prefer the shallowest, newest child, then use its id as a stable tie-breaker."""

    metadata = ref.metadata or {}
    try:
        depth = int(metadata.get("agent_depth", 1))
    except (TypeError, ValueError):
        depth = 1
    timestamp = effective_conversation_timestamp(ref)
    epoch = timestamp.timestamp() if timestamp is not None else 0.0
    return depth, -epoch, -(ref.file_size_bytes or 0), str(ref.document_id)


def _canonical_root_sort_key(ref: ConversationRef) -> tuple[float, int, str]:
    """Choose the newest, largest root row with a stable document-id tie-breaker."""

    timestamp = effective_conversation_timestamp(ref)
    epoch = timestamp.timestamp() if timestamp is not None else 0.0
    return epoch, ref.file_size_bytes or 0, str(ref.document_id)
