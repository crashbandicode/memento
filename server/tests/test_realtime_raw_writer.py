"""Reducer coverage for raw-writer deferred-projection candidates."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from server.services.realtime_raw_writer import WriterState, reduce_writer_state


_DOCUMENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_BASE_HASH = "a" * 64
_BASE_OFFSET = 512
_SOURCE_ID = "cursor-source-row"
_OBSERVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)


def _existing_row(*, role: str, content: str = "old content") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="user",
        role=role,
        content=content,
        metadata_={"source_id": _SOURCE_ID},
        timestamp=_OBSERVED_AT,
    )


def _state(existing: SimpleNamespace) -> WriterState:
    metadata = {"source": "cursor_state_v1", "session_id": "raw-candidate"}
    return WriterState(
        document={
            "id": _DOCUMENT_ID,
            "content_hash": _BASE_HASH,
            "file_size_bytes": _BASE_OFFSET,
            "document_metadata": metadata,
            "title": "raw-candidate.jsonl",
            "project_id": None,
            "visibility": "private",
            "source_modified_at": _OBSERVED_AT,
            "activity_at": _OBSERVED_AT,
            "needs_review": False,
        },
        delivery={
            "revision_hash": _BASE_HASH,
            "file_size_bytes": _BASE_OFFSET,
            "metadata": metadata,
            "source_modified_at": _OBSERVED_AT,
            "activity_at": _OBSERVED_AT,
            "synced_at": _OBSERVED_AT,
        },
        sync={"last_hash": _BASE_HASH, "last_offset": _BASE_OFFSET},
        read_model=None,
        task_state=None,
        dashboard=None,
        tail=(existing,),
        cursor_sources=(existing,),
    )


def _reduce(existing: SimpleNamespace, content: str):
    row = json.dumps(
        {
            "type": "user",
            "role": "user",
            "id": _SOURCE_ID,
            "timestamp": "2026-08-27T12:00:00Z",
            "message": {"content": content},
        }
    )
    return reduce_writer_state(
        _state(existing),
        tool_id="cursor",
        category="conversation",
        content_type="jsonl",
        relative_path="phase4/raw-candidate.jsonl",
        content=row,
        content_hash="b" * 64,
        file_size=len(row.encode("utf-8")),
        mode="delta",
        offset=_BASE_OFFSET + len(row.encode("utf-8")),
        metadata={"source": "cursor_state_v1", "session_id": "raw-candidate"},
        timestamp=_OBSERVED_AT.timestamp(),
        machine_id=None,
        user_id=None,
        base_hash=_BASE_HASH,
        base_offset=_BASE_OFFSET,
        authoritative_rebase=False,
        had_sensitive=False,
    )


def test_cursor_user_replaced_by_directives_enqueues_canvas_and_search() -> None:
    mutation = _reduce(
        _existing_row(role="user", content="old indexed Canvas row"),
        "<additional_directives>system replacement</additional_directives>",
    )

    assert mutation.messages[0].operation == "update"
    assert mutation.messages[0].role == "system"
    assert mutation.canvas_candidate is True
    assert mutation.search_candidate is True


def test_cursor_system_replaced_by_directives_enqueues_neither_projection() -> None:
    mutation = _reduce(
        _existing_row(role="system"),
        "<additional_directives>still a system row</additional_directives>",
    )

    assert mutation.messages[0].role == "system"
    assert mutation.canvas_candidate is False
    assert mutation.search_candidate is False


def test_cursor_user_update_enqueues_search() -> None:
    mutation = _reduce(
        _existing_row(role="user"),
        "updated searchable user content",
    )

    assert mutation.messages[0].role == "user"
    assert mutation.search_candidate is True
