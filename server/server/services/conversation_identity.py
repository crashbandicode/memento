"""Stable source identities for conversation files that can move on disk."""

from __future__ import annotations

import uuid
from datetime import datetime


CURSOR_SESSION_UNIQUE_INDEX = "uq_documents_cursor_machine_session"
CURSOR_SESSION_UNIQUE_INDEX_SQL = f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {CURSOR_SESSION_UNIQUE_INDEX}
    ON documents (machine_id, tool_id, (metadata->>'session_id'))
    WHERE category='conversation'
      AND tool_id='cursor'
      AND coalesce(metadata->>'session_id', '') <> ''
"""

CODEX_SESSION_UNIQUE_INDEX = "uq_documents_codex_machine_session"
CODEX_SESSION_UNIQUE_INDEX_SQL = f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {CODEX_SESSION_UNIQUE_INDEX}
    ON documents (machine_id, tool_id, (metadata->>'session_id'))
    WHERE category='conversation'
      AND tool_id='codex'
      AND coalesce(metadata->>'session_id', '') <> ''
      AND metadata->>'session_id' = metadata->>'thread_id'
"""


def _uuid_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def conversation_session_id(
    tool_id: str,
    category: str,
    metadata: object,
) -> str | None:
    """Return a verified stable UUID for a relocatable conversation source.

    Cursor exports one canonical ``session_id``. Codex exposes both the
    rollout ``session_id`` and ``thread_id``; requiring those fields to match
    avoids conflating a subagent rollout with its root-session lineage.
    """
    if category != "conversation" or not isinstance(metadata, dict):
        return None
    session_id = _uuid_string(metadata.get("session_id"))
    if session_id is None:
        return None
    if tool_id == "cursor":
        return session_id
    if tool_id == "codex":
        thread_id = _uuid_string(metadata.get("thread_id"))
        return session_id if thread_id == session_id else None
    return None


def cursor_session_id(
    tool_id: str,
    category: str,
    metadata: object,
) -> str | None:
    """Return the canonical Cursor UUID for a conversation upload.

    Limit identity-based upserts to the one source shape we have verified.
    Other tools reuse a ``session_id`` metadata key with different semantics,
    and non-conversation Cursor files can share filename stems.
    """
    if tool_id != "cursor":
        return None
    return conversation_session_id(tool_id, category, metadata)


def codex_session_id(
    tool_id: str,
    category: str,
    metadata: object,
) -> str | None:
    """Return a verified Codex rollout UUID when lineage fields agree."""
    if tool_id != "codex":
        return None
    return conversation_session_id(tool_id, category, metadata)


def cursor_path_rank(relative_path: str, session_id: str) -> tuple[int, int]:
    """Rank canonical Cursor locations above temporary or nested aliases."""
    normalized = (relative_path or "").replace("\\", "/").casefold()
    normalized_session = session_id.casefold()
    is_placeholder = (
        normalized.startswith("projects/empty-window/")
        or "/projects/empty-window/" in normalized
    )
    root_suffix = (
        f"/agent-transcripts/{normalized_session}/{normalized_session}.jsonl"
    )
    is_promoted_root = normalized.endswith(root_suffix)
    return (0 if is_placeholder else 1, 1 if is_promoted_root else 0)


def _timestamp_rank(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    try:
        return value.timestamp()
    except (ValueError, OSError, OverflowError):
        return float("-inf")


def cursor_document_preference(
    *,
    session_id: str,
    relative_path: str,
    source_modified_at: datetime | None,
    file_size_bytes: int,
    synced_at: datetime | None,
    document_id: object,
) -> tuple[tuple[int, int], float, int, float, str]:
    """Return a deterministic canonical-document ordering.

    Filesystem location is authoritative before revision clocks: a real
    project path must not flip back to ``empty-window``, and a promoted root
    transcript must not become a nested subagent again.  Within the same path
    class, prefer the newest/largest monotonic source revision.
    """
    return (
        cursor_path_rank(relative_path, session_id),
        _timestamp_rank(source_modified_at),
        int(file_size_bytes or 0),
        _timestamp_rank(synced_at),
        str(document_id),
    )


def codex_document_preference(
    *,
    relative_path: str,
    source_modified_at: datetime | None,
    file_size_bytes: int,
    synced_at: datetime | None,
    document_id: object,
) -> tuple[float, int, float, int, str]:
    """Prefer the newest complete Codex revision across active/archive moves."""
    normalized = (relative_path or "").replace("\\", "/").casefold()
    return (
        _timestamp_rank(source_modified_at),
        int(file_size_bytes or 0),
        _timestamp_rank(synced_at),
        1 if normalized.startswith("sessions/") else 0,
        str(document_id),
    )


def select_canonical_cursor_document(
    documents: list[object],
    session_id: str,
) -> object | None:
    """Select the shared canonical revision from model objects or row records."""
    if not documents:
        return None

    def field(document: object, name: str, default=None):
        if isinstance(document, dict):
            return document.get(name, default)
        try:
            return document[name]  # type: ignore[index]
        except (KeyError, TypeError):
            return getattr(document, name, default)

    return max(
        documents,
        key=lambda document: cursor_document_preference(
            session_id=session_id,
            relative_path=field(document, "relative_path", ""),
            source_modified_at=field(document, "source_modified_at"),
            file_size_bytes=field(document, "file_size_bytes", 0),
            synced_at=field(document, "synced_at"),
            document_id=field(document, "id", ""),
        ),
    )


def select_canonical_conversation_document(
    documents: list[object],
    *,
    tool_id: str,
    session_id: str,
) -> object | None:
    """Select one canonical revision for a verified movable conversation."""
    if tool_id == "cursor":
        return select_canonical_cursor_document(documents, session_id)
    if tool_id != "codex" or not documents:
        return None

    def field(document: object, name: str, default=None):
        if isinstance(document, dict):
            return document.get(name, default)
        try:
            return document[name]  # type: ignore[index]
        except (KeyError, TypeError):
            return getattr(document, name, default)

    return max(
        documents,
        key=lambda document: codex_document_preference(
            relative_path=field(document, "relative_path", ""),
            source_modified_at=field(document, "source_modified_at"),
            file_size_bytes=field(document, "file_size_bytes", 0),
            synced_at=field(document, "synced_at"),
            document_id=field(document, "id", ""),
        ),
    )


def should_relocate_cursor_document(
    *,
    session_id: str,
    current_path: str,
    incoming_path: str,
    current_modified_at: datetime | None,
    incoming_modified_at: datetime | None,
) -> bool:
    """Return whether an accepted upload should move the canonical path."""
    if current_path == incoming_path:
        return False
    current_rank = cursor_path_rank(current_path, session_id)
    incoming_rank = cursor_path_rank(incoming_path, session_id)
    if incoming_rank != current_rank:
        return incoming_rank > current_rank
    return _timestamp_rank(incoming_modified_at) > _timestamp_rank(
        current_modified_at
    )


def should_relocate_conversation_document(
    *,
    tool_id: str,
    session_id: str,
    current_path: str,
    incoming_path: str,
    current_modified_at: datetime | None,
    incoming_modified_at: datetime | None,
) -> bool:
    """Return whether the canonical document should follow an accepted path."""
    if tool_id == "cursor":
        return should_relocate_cursor_document(
            session_id=session_id,
            current_path=current_path,
            incoming_path=incoming_path,
            current_modified_at=current_modified_at,
            incoming_modified_at=incoming_modified_at,
        )
    if tool_id != "codex" or current_path == incoming_path:
        return False
    current_rank = _timestamp_rank(current_modified_at)
    incoming_rank = _timestamp_rank(incoming_modified_at)
    if incoming_rank != current_rank:
        return incoming_rank > current_rank
    current_active = current_path.replace("\\", "/").casefold().startswith("sessions/")
    incoming_active = incoming_path.replace("\\", "/").casefold().startswith("sessions/")
    return incoming_active and not current_active
