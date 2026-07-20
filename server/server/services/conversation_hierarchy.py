"""Presentation helpers for Codex root threads and their subagents.

Codex stores every fork as its own conversation document.  The collector
preserves those documents because each child is independently useful, but
list surfaces should present the root once and describe how many descendants
belong to it.  This module deliberately only decides visibility/counts; it
never combines transcripts because a child can contain a cloned copy of its
parent's history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Hashable, Iterable, Mapping

from sqlalchemy import and_, false, or_

from .conversation_activity import effective_conversation_activity


FOLDABLE_CONVERSATION_TOOLS = frozenset({
    "codex",
    "claude_code",
    "cursor",
})
_PATH_LINKED_SUBAGENT_TOOLS = frozenset({"claude_code", "cursor"})


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


def is_conversation_subagent(
    tool_id: str | None,
    relative_path: str | None,
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Recognize native subagent records without inspecting transcript text."""
    if tool_id not in FOLDABLE_CONVERSATION_TOOLS:
        return False
    values = metadata or {}
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
            bool(values.get("is_subagent"))
            or "/subagents/" in (relative_path or "").replace("\\", "/")
        )
    )


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
            nickname = metadata.get("agent_nickname")
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
            children.append({
                "id": str(child.document_id),
                "session_id": thread_id,
                "title": (
                    agent_path_label
                    or (str(nickname) if nickname else None)
                    or child.title
                    or (f"Subagent {thread_id[:8]}" if thread_id else "Subagent")
                ),
                "agent_nickname": str(nickname) if nickname else None,
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
    by_thread = {
        str(summary.get("session_id")): summary
        for summary in merged
        if summary.get("session_id")
    }
    for item in events:
        thread_id = str(item.get("agent_thread_id") or "").strip()
        agent_path = str(item.get("agent_path") or "").strip()
        if not thread_id or not agent_path:
            continue
        kind = str(item.get("kind") or "updated").strip().casefold()
        status = {
            "started": "running",
            "updated": "running",
            "completed": "completed",
            "interrupted": "interrupted",
            "failed": "failed",
        }.get(kind, "unknown")
        existing = by_thread.get(thread_id)
        if existing is None:
            label = str(item.get("label") or "").strip()
            if not label:
                label = " ".join(
                    agent_path.rstrip("/").rsplit("/", 1)[-1]
                    .replace("_", " ")
                    .replace("-", " ")
                    .split()
                ) or "Subagent"
            existing = {
                "id": None,
                "session_id": thread_id,
                "title": label,
                "agent_nickname": None,
                "agent_path": agent_path,
                "agent_depth": None,
                "parent_thread_id": None,
                "relative_path": None,
                "timestamp": item.get("timestamp"),
                "activity_at": item.get("timestamp"),
                "synced_at": None,
                "document_ready": False,
            }
            merged.append(existing)
            by_thread[thread_id] = existing
        else:
            existing["document_ready"] = bool(existing.get("id"))
        existing["status"] = status
        existing["last_event_at"] = item.get("timestamp")

    for summary in merged:
        summary.setdefault("document_ready", bool(summary.get("id")))
        summary.setdefault("status", "unknown")
        summary.setdefault("last_event_at", None)
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
