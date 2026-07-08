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


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """The small subset of a conversation document needed for presentation."""

    document_id: Hashable
    tool_id: str | None
    relative_path: str | None
    metadata: Mapping[str, Any] | None
    title: str | None = None
    source_modified_at: datetime | None = None
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


def fold_codex_subagents(
    conversations: Iterable[ConversationRef],
) -> ConversationHierarchy:
    """Hide linked Codex children when a root is present.

    Descendants are grouped by ``root_session_id`` and counted by their own
    ``session_id``/``thread_id`` rather than by document rows.  If the root
    has not arrived yet, one deterministic child remains visible so the group
    never disappears from the UI.  Explicit legacy ``/subagents/`` paths are
    excluded; their established inline-message presentation is handled by the
    project conversations endpoint.
    """

    refs = list(conversations)
    visible_ids = {ref.document_id for ref in refs}
    canonical_document_ids = {
        ref.document_id: ref.document_id
        for ref in refs
    }
    roots_by_thread: dict[str, list[ConversationRef]] = {}
    children_by_root: dict[str, list[ConversationRef]] = {}

    for ref in refs:
        path = (ref.relative_path or "").replace("\\", "/")
        metadata = ref.metadata or {}
        thread_id = current_thread_id(metadata)
        is_metadata_child = (
            ref.tool_id == "codex"
            and str(metadata.get("thread_source") or "").strip().lower() == "subagent"
            and bool(metadata.get("root_session_id"))
            and "/subagents/" not in path
        )

        if is_metadata_child:
            root_id = str(metadata["root_session_id"])
            children_by_root.setdefault(root_id, []).append(ref)
        elif (
            ref.tool_id == "codex"
            and thread_id
            and "/subagents/" not in path
        ):
            roots_by_thread.setdefault(thread_id, []).append(ref)

    subagent_counts: dict[Hashable, int] = {}
    orphan_ids: set[Hashable] = set()
    subagent_document_ids: dict[Hashable, tuple[Hashable, ...]] = {}
    canonical_roots: dict[str, ConversationRef] = {}

    # The same Codex data can be uploaded by several machines.  Canonicalize
    # those root rows by logical thread UUID before applying child groups so a
    # multi-host sync still renders exactly one top-level card.
    for thread_id, roots in roots_by_thread.items():
        canonical = max(roots, key=_canonical_root_sort_key)
        canonical_roots[thread_id] = canonical
        visible_ids.difference_update(
            root.document_id
            for root in roots
            if root.document_id != canonical.document_id
        )
        for root in roots:
            canonical_document_ids[root.document_id] = canonical.document_id

    for root_thread_id, children in children_by_root.items():
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
        root = canonical_roots.get(root_thread_id)

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
            timestamp = child.source_modified_at or child.synced_at
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
            })
        if children:
            summaries[parent_id] = children
    return summaries


def _orphan_sort_key(ref: ConversationRef) -> tuple[int, float, int, str]:
    """Prefer the shallowest, newest child, then use its id as a stable tie-breaker."""

    metadata = ref.metadata or {}
    try:
        depth = int(metadata.get("agent_depth", 1))
    except (TypeError, ValueError):
        depth = 1
    timestamp = ref.source_modified_at or ref.synced_at
    epoch = timestamp.timestamp() if timestamp is not None else 0.0
    return depth, -epoch, -(ref.file_size_bytes or 0), str(ref.document_id)


def _canonical_root_sort_key(ref: ConversationRef) -> tuple[float, int, str]:
    """Choose the newest, largest root row with a stable document-id tie-breaker."""

    timestamp = ref.source_modified_at or ref.synced_at
    epoch = timestamp.timestamp() if timestamp is not None else 0.0
    return epoch, ref.file_size_bytes or 0, str(ref.document_id)
