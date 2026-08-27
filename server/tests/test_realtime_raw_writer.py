"""Reducer coverage for raw-writer deferred-projection candidates."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from server.services.ingest_service import _select_updated_document_title
from server.services.realtime_raw_writer import (
    MessageMutation,
    RawWriterUnsupported,
    WriterState,
    _history_metadata_is_already_committed,
    reduce_writer_state,
)


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


def _codex_history_row(
    *,
    text: str,
    timestamp: datetime,
    line_number: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=2,
        document_id=_DOCUMENT_ID,
        line_number=line_number,
        message_type="history_user_message",
        role="user",
        content=text,
        metadata_={"source_id": "codex-history:0"},
        timestamp=timestamp,
    )


def _codex_history_state(
    history_row: SimpleNamespace | None,
    *,
    ordinary_user_rows: tuple[SimpleNamespace, ...] = (),
) -> WriterState:
    metadata = {"session_id": "codex-history-state"}
    return WriterState(
        document={
            "id": _DOCUMENT_ID,
            "content_hash": _BASE_HASH,
            "file_size_bytes": _BASE_OFFSET,
            "document_metadata": metadata,
            "title": "history.jsonl",
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
        recovered_history=(history_row,) if history_row is not None else (),
        ordinary_user_rows=ordinary_user_rows,
    )


def _reduce_codex_history(
    *,
    history: list[dict[str, object]],
    state: WriterState | None = None,
    first_user_message: str = "Use Core staging.",
) -> object:
    timestamp = datetime.fromtimestamp(1_785_672_000, tz=timezone.utc)
    row = json.dumps(
        {
            "type": "event_msg",
            "timestamp": "2026-08-02T11:00:03Z",
            "payload": {"type": "agent_message", "message": "Already committed."},
        }
    )
    return reduce_writer_state(
        state or _codex_history_state(
            _codex_history_row(text="Use Core staging.", timestamp=timestamp)
        ),
        tool_id="codex",
        category="conversation",
        content_type="jsonl",
        relative_path="phase45/history.jsonl",
        content=row,
        content_hash="c" * 64,
        file_size=len(row.encode("utf-8")),
        mode="delta",
        offset=_BASE_OFFSET + len(row.encode("utf-8")),
        metadata={
            "session_id": "codex-history-state",
            "user_history": history,
            "first_user_message": first_user_message,
        },
        timestamp=_OBSERVED_AT.timestamp(),
        machine_id=None,
        user_id=None,
        base_hash=_BASE_HASH,
        base_offset=_BASE_OFFSET,
        authoritative_rebase=False,
        had_sensitive=False,
    )


def test_unchanged_codex_history_is_ignored_by_raw_reducer() -> None:
    mutation = _reduce_codex_history(
        history=[{"text": "Use Core staging.", "ts": 1_785_672_000}],
    )

    assert mutation.disposition == "committed"
    assert mutation.messages[0].role == "assistant"


@pytest.mark.parametrize(
    "history",
    (
        [{"text": "Changed prompt.", "ts": 1_785_672_000}],
        [
            {"text": "Use Core staging.", "ts": 1_785_672_000},
            {"text": "A genuinely new prompt.", "ts": 1_785_672_001},
        ],
    ),
)
def test_changed_or_new_codex_history_falls_back(history: list[dict[str, object]]) -> None:
    with pytest.raises(
        RawWriterUnsupported,
        match="authoritative rebuild/history needs legacy reducer",
    ):
        _reduce_codex_history(history=history)


def test_negative_recovered_history_row_falls_back_for_legacy_reconciliation() -> None:
    timestamp = datetime.fromtimestamp(1_785_672_000, tz=timezone.utc)
    state = _codex_history_state(
        _codex_history_row(
            text="Use Core staging.",
            timestamp=timestamp,
            line_number=-100,
        )
    )

    with pytest.raises(
        RawWriterUnsupported,
        match="authoritative rebuild/history needs legacy reducer",
    ):
        _reduce_codex_history(
            history=[{"text": "Use Core staging.", "ts": 1_785_672_000}],
            state=state,
        )


def test_history_represented_by_ordinary_user_commits_raw() -> None:
    timestamp = datetime.fromtimestamp(1_785_672_000, tz=timezone.utc)
    ordinary_user = SimpleNamespace(
        id=3,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="user_message",
        role="user",
        content="Use Core staging.",
        metadata_={},
        timestamp=timestamp,
    )
    state = _codex_history_state(
        None,
        ordinary_user_rows=(ordinary_user,),
    )

    mutation = _reduce_codex_history(
        history=[{"text": "Use Core staging.", "ts": 1_785_672_000}],
        state=state,
    )

    assert mutation.disposition == "committed"


def test_first_user_message_is_noop_when_ordinary_user_exists() -> None:
    ordinary_user = SimpleNamespace(
        id=3,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="user_message",
        role="user",
        content="A later ordinary user message.",
        metadata_={},
        timestamp=_OBSERVED_AT,
    )

    mutation = _reduce_codex_history(
        history=[],
        state=_codex_history_state(None, ordinary_user_rows=(ordinary_user,)),
    )

    assert mutation.disposition == "committed"


def test_first_user_message_without_user_row_requires_legacy_injection() -> None:
    state = _codex_history_state(None)

    assert _history_metadata_is_already_committed(
        state,
        tool_id="codex",
        history=[],
        first_user_message="Use Core staging.",
        prospective_mutations=[],
    ) is False
    with pytest.raises(
        RawWriterUnsupported,
        match="authoritative rebuild/history needs legacy reducer",
    ):
        _reduce_codex_history(history=[], state=state)


def test_non_user_codex_first_user_message_is_already_committed() -> None:
    assert _history_metadata_is_already_committed(
        _codex_history_state(None),
        tool_id="codex",
        history=[],
        first_user_message="# Context from my IDE setup:\n<context>",
        prospective_mutations=[],
    ) is True


def test_first_user_message_is_noop_when_current_frame_adds_user() -> None:
    prospective_user = MessageMutation(
        ordinal=0,
        operation="insert",
        line_number=1,
        message_type="user_message",
        role="user",
        content="Current-frame user message.",
        metadata={},
        timestamp=_OBSERVED_AT,
    )

    assert _history_metadata_is_already_committed(
        _codex_history_state(None),
        tool_id="codex",
        history=[],
        first_user_message="Use Core staging.",
        prospective_mutations=[prospective_user],
    ) is True


def test_first_user_message_requires_legacy_when_frame_flips_sole_user_row() -> None:
    ordinary_user = SimpleNamespace(
        id=3,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="user_message",
        role="user",
        content="A later ordinary user message.",
        metadata_={},
        timestamp=_OBSERVED_AT,
    )
    role_flip = MessageMutation(
        ordinal=0,
        operation="update",
        line_number=1,
        message_type="assistant_message",
        role="assistant",
        content="Rewritten as assistant transport.",
        metadata={},
        timestamp=_OBSERVED_AT,
        existing_id=3,
        previous_role="user",
    )

    # Legacy re-checks AFTER the frame applies: with the sole user row
    # rewritten to a non-user role, it would inject the fallback prompt.
    assert _history_metadata_is_already_committed(
        _codex_history_state(None, ordinary_user_rows=(ordinary_user,)),
        tool_id="codex",
        history=[],
        first_user_message="Use Core staging.",
        prospective_mutations=[role_flip],
    ) is False


def test_existing_first_user_message_is_already_committed() -> None:
    stored_first_user = SimpleNamespace(
        id=4,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="first_user_message",
        role="user",
        content="An earlier fallback prompt.",
        metadata_={},
        timestamp=_OBSERVED_AT,
    )

    mutation = _reduce_codex_history(
        history=[],
        state=_codex_history_state(stored_first_user),
    )

    assert mutation.disposition == "committed"


def test_existing_title_uses_legacy_selector_and_enqueues_search() -> None:
    existing = _existing_row(role="assistant")
    state = _state(existing)
    state.document["title"] = "Earlier Claude title"
    state.document["document_metadata"] = {
        "session_id": "raw-candidate",
        "memento_title_source": "claude_ai_title",
    }
    state.delivery["metadata"] = dict(state.document["document_metadata"])
    row = json.dumps(
        {
            "type": "assistant",
            "uuid": "claude-title-update",
            "timestamp": "2026-08-27T12:00:00Z",
            "message": {"role": "assistant", "content": "No-op body for title."},
        }
    )
    metadata = {
        "session_id": "raw-candidate",
        "source_title_kind": "claude_ai_title",
        "title": "Selected Claude title",
    }
    mutation = reduce_writer_state(
        state,
        tool_id="claude_code",
        category="conversation",
        content_type="jsonl",
        relative_path="phase45/title.jsonl",
        content=row,
        content_hash="d" * 64,
        file_size=len(row.encode("utf-8")),
        mode="delta",
        offset=_BASE_OFFSET + len(row.encode("utf-8")),
        metadata=metadata,
        timestamp=_OBSERVED_AT.timestamp(),
        machine_id=None,
        user_id=None,
        base_hash=_BASE_HASH,
        base_offset=_BASE_OFFSET,
        authoritative_rebase=False,
        had_sensitive=False,
    )

    expected = _select_updated_document_title(
        "Earlier Claude title",
        "Selected Claude title",
        category="conversation",
        tool_id="claude_code",
        metadata={"memento_title_source": "claude_ai_title"},
        incoming_title_is_explicit=True,
    )
    assert mutation.document_values["title"] == expected
    assert mutation.title_changed is True
    assert mutation.search_candidate is True
