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
_SUBAGENT_TRANSCRIPT_PATH = (
    "projects/fixture-parent/subagents/agent-fixture-subagent.jsonl"
)


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


def _claude_subagent_delta_frame() -> dict[str, object]:
    content = json.dumps(
        {
            "type": "user",
            "uuid": "fixture-subagent-user",
            "timestamp": "2026-08-27T12:00:00Z",
            "message": {
                "role": "user",
                "content": "Inspect the raw subagent transcript.",
            },
        }
    )
    return {
        "tool_id": "claude_code",
        "category": "conversation",
        "content_type": "jsonl",
        "relative_path": _SUBAGENT_TRANSCRIPT_PATH,
        "content": content,
        "content_hash": "b" * 64,
        "file_size": len(content.encode("utf-8")),
        "mode": "delta",
        "offset": _BASE_OFFSET + len(content.encode("utf-8")),
        "metadata": {"session_id": "fixture-subagent"},
        "timestamp": _OBSERVED_AT.timestamp(),
        "machine_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "33333333-3333-3333-3333-333333333333",
        "base_hash": _BASE_HASH,
        "base_offset": _BASE_OFFSET,
    }


def _claude_state() -> WriterState:
    state = _state(_existing_row(role="assistant"))
    metadata = {"session_id": "fixture-subagent"}
    state.document["document_metadata"] = metadata
    state.delivery["metadata"] = dict(metadata)
    return state


@pytest.mark.asyncio
async def test_subagent_transcript_raw_chain_remains_legacy_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings
    from server.services import realtime_raw_writer as raw_module

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", False)
    frame = _claude_subagent_delta_frame()

    with pytest.raises(
        RawWriterUnsupported,
        match="Claude transcript/sidecar pairing needs the legacy reducer",
    ):
        await raw_module.ingest_conversation_raw_chain(frames=[frame])

    with pytest.raises(
        RawWriterUnsupported,
        match="Claude transcript/sidecar pairing needs the legacy reducer",
    ):
        await raw_module.ingest_conversation_raw(
            tool_id=str(frame["tool_id"]),
            category=str(frame["category"]),
            content_type=str(frame["content_type"]),
            relative_path=str(frame["relative_path"]),
            content=str(frame["content"]),
            content_hash=str(frame["content_hash"]),
            file_size=int(frame["file_size"]),
            mode=str(frame["mode"]),
            offset=int(frame["offset"]),
            metadata=dict(frame["metadata"]),
            timestamp=float(frame["timestamp"]),
            machine_id=str(frame["machine_id"]),
            user_id=str(frame["user_id"]),
            base_hash=str(frame["base_hash"]),
            base_offset=int(frame["base_offset"]),
        )


@pytest.mark.asyncio
async def test_subagent_transcript_raw_chain_commits_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings
    from server.services import realtime_raw_writer as raw_module

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", True)
    frame = _claude_subagent_delta_frame()
    applied = []

    class FakeTransaction:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def start(self) -> None:
            self.calls.append("start")

        async def commit(self) -> None:
            self.calls.append("commit")

        async def rollback(self) -> None:
            self.calls.append("rollback")

    class FakeConnection:
        def __init__(self) -> None:
            self.transaction_ = FakeTransaction()

        def transaction(self) -> FakeTransaction:
            return self.transaction_

        async def execute(self, *_args) -> None:
            return None

    class FakeAcquire:
        def __init__(self, connection: FakeConnection) -> None:
            self.connection = connection

        async def __aenter__(self) -> FakeConnection:
            return self.connection

        async def __aexit__(self, *_args) -> bool:
            return False

    class FakePool:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    pool = FakePool()

    async def fake_pool(_database_url):
        return pool

    async def fake_load_state(*_args, **_kwargs) -> WriterState:
        return _claude_state()

    async def fake_apply(_connection, **kwargs):
        applied.append(kwargs["mutation"])
        return raw_module.RawDocument(_DOCUMENT_ID), None

    monkeypatch.setattr(raw_module, "_pool", fake_pool)
    monkeypatch.setattr(raw_module, "_load_state", fake_load_state)
    monkeypatch.setattr(raw_module, "_apply", fake_apply)

    document, event = await raw_module.ingest_conversation_raw_chain(frames=[frame])

    expected = reduce_writer_state(
        _claude_state(),
        tool_id="claude_code",
        category="conversation",
        content_type="jsonl",
        relative_path="projects/fixture-parent/main.jsonl",
        content=str(frame["content"]),
        content_hash=str(frame["content_hash"]),
        file_size=int(frame["file_size"]),
        mode="delta",
        offset=int(frame["offset"]),
        metadata=dict(frame["metadata"]),
        timestamp=float(frame["timestamp"]),
        machine_id=uuid.UUID(str(frame["machine_id"])),
        user_id=uuid.UUID(str(frame["user_id"])),
        base_hash=str(frame["base_hash"]),
        base_offset=int(frame["base_offset"]),
        authoritative_rebase=False,
        had_sensitive=False,
    )

    assert document.disposition == "committed"
    assert event is None
    assert pool.connection.transaction_.calls == ["start", "commit"]
    assert len(applied) == 1
    assert applied[0].messages == expected.messages


@pytest.mark.asyncio
async def test_subagent_transcript_full_remains_legacy_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings
    from server.services import realtime_raw_writer as raw_module

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", True)
    frame = _claude_subagent_delta_frame()

    with pytest.raises(
        RawWriterUnsupported,
        match="Claude transcript/sidecar pairing needs the legacy reducer",
    ):
        await raw_module.ingest_conversation_raw(
            tool_id=str(frame["tool_id"]),
            category=str(frame["category"]),
            content_type=str(frame["content_type"]),
            relative_path=str(frame["relative_path"]),
            content=str(frame["content"]),
            content_hash=str(frame["content_hash"]),
            file_size=int(frame["file_size"]),
            mode="full",
            offset=int(frame["offset"]),
            metadata=dict(frame["metadata"]),
            timestamp=float(frame["timestamp"]),
            machine_id=str(frame["machine_id"]),
            user_id=str(frame["user_id"]),
            base_hash=None,
            base_offset=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", (False, True))
async def test_subagent_sidecar_is_rejected_by_category_before_pairing_gate(
    monkeypatch: pytest.MonkeyPatch,
    flag: bool,
) -> None:
    from server.config import settings
    from server.services import realtime_raw_writer as raw_module

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", flag)
    frame = _claude_subagent_delta_frame()
    frame.update(
        category="state",
        content_type="json",
        relative_path=_SUBAGENT_TRANSCRIPT_PATH.replace(".jsonl", ".meta.json"),
    )

    with pytest.raises(
        RawWriterUnsupported,
        match="raw writer is limited to conversation JSONL",
    ):
        await raw_module.ingest_conversation_raw_chain(frames=[frame])


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
    history_timeline_rows: tuple[SimpleNamespace, ...] = (),
    tail: tuple[SimpleNamespace, ...] = (),
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
        tail=tail,
        recovered_history=(history_row,) if history_row is not None else (),
        ordinary_user_rows=ordinary_user_rows,
        history_timeline_rows=history_timeline_rows,
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
def test_changed_or_new_codex_history_falls_back_when_flag_off(
    history: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings

    monkeypatch.setattr(settings, "realtime_ingest_raw_codex_history", False)
    with pytest.raises(
        RawWriterUnsupported,
        match="authoritative rebuild/history needs legacy reducer",
    ):
        _reduce_codex_history(history=history)


def test_new_codex_history_commits_with_collision_free_positive_slots_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings

    monkeypatch.setattr(settings, "realtime_ingest_raw_codex_history", True)
    history_timestamp = datetime.fromtimestamp(1_785_672_000, tz=timezone.utc)
    assistant = SimpleNamespace(
        id=7,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="agent_message",
        role="assistant",
        content="Persisted assistant response.",
        metadata_={},
        timestamp=_OBSERVED_AT,
    )
    state = _codex_history_state(
        None,
        history_timeline_rows=(assistant,),
        tail=(assistant,),
    )

    mutation = _reduce_codex_history(
        history=[{"text": "Recovered earlier prompt.", "ts": 1_785_672_000}],
        state=state,
    )

    recovered = next(
        item
        for item in mutation.messages
        if item.message_type == "history_user_message"
    )
    moved = next(item for item in mutation.messages if item.existing_id == 7)
    source_append = next(
        item
        for item in mutation.messages
        if item.operation == "insert" and item.message_type == "agent_message"
    )
    assert recovered.line_number == 1
    assert recovered.timestamp == history_timestamp
    assert moved.line_number == 2
    assert source_append.line_number == 3
    assert len({recovered.line_number, moved.line_number, source_append.line_number}) == 3
    assert mutation.force_projection_rebuild is True


def test_flag_on_history_dedup_does_not_insert_a_second_recovered_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings

    monkeypatch.setattr(settings, "realtime_ingest_raw_codex_history", True)
    history_timestamp = datetime.fromtimestamp(1_785_672_000, tz=timezone.utc)
    ordinary = SimpleNamespace(
        id=8,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="user_message",
        role="user",
        content="Already represented prompt.",
        metadata_={},
        timestamp=history_timestamp,
    )
    state = _codex_history_state(
        None,
        ordinary_user_rows=(ordinary,),
        history_timeline_rows=(ordinary,),
        tail=(ordinary,),
    )

    mutation = _reduce_codex_history(
        history=[{"text": "Already represented prompt.", "ts": 1_785_672_000}],
        state=state,
    )

    assert not any(
        item.message_type == "history_user_message" for item in mutation.messages
    )
    assert mutation.force_projection_rebuild is False


def test_flag_on_history_respects_the_legacy_entry_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings
    from server.services.ingest_service import MAX_USER_HISTORY_ENTRIES

    monkeypatch.setattr(settings, "realtime_ingest_raw_codex_history", True)
    assistant = SimpleNamespace(
        id=9,
        document_id=_DOCUMENT_ID,
        line_number=1,
        message_type="agent_message",
        role="assistant",
        content="Existing response.",
        metadata_={},
        timestamp=_OBSERVED_AT,
    )
    state = _codex_history_state(
        None,
        history_timeline_rows=(assistant,),
        tail=(assistant,),
    )
    history = [
        {"text": f"Bounded prompt {index}", "ts": 0}
        for index in range(MAX_USER_HISTORY_ENTRIES + 1)
    ]

    mutation = _reduce_codex_history(history=history, state=state)

    recovered = [
        item
        for item in mutation.messages
        if item.message_type == "history_user_message"
    ]
    assert len(recovered) == MAX_USER_HISTORY_ENTRIES
    assert {item.metadata["source_id"] for item in recovered} == {
        f"codex-history:{index}" for index in range(MAX_USER_HISTORY_ENTRIES)
    }


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


def _reduce_existing_claude_delta(
    *,
    previous_title: str,
    metadata: dict[str, str],
):
    state = _state(_existing_row(role="assistant"))
    state.document["title"] = previous_title
    state.document["document_metadata"] = {"session_id": "claude-title-fixture"}
    state.delivery["metadata"] = {"session_id": "claude-title-fixture"}
    row = json.dumps(
        {
            "type": "assistant",
            "uuid": "claude-title-follow-up",
            "timestamp": "2026-08-27T12:00:01Z",
            "message": {"role": "assistant", "content": "Raw DELTA committed."},
        }
    )
    return reduce_writer_state(
        state,
        tool_id="claude_code",
        category="conversation",
        content_type="jsonl",
        relative_path="projects/fixture-parent/main.jsonl",
        content=row,
        content_hash="e" * 64,
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


def test_existing_friendly_claude_title_survives_untitled_raw_delta() -> None:
    mutation = _reduce_existing_claude_delta(
        previous_title="Inspect the raw writer pairing gate.",
        metadata={"session_id": "claude-title-fixture"},
    )

    assert mutation.document_values["title"] == "Inspect the raw writer pairing gate."
    assert mutation.title_changed is False


def test_existing_claude_delta_applies_unmarked_meaningful_title() -> None:
    # Legacy selects the real title at ingest_service.py:3526 and :3657, then
    # preserves it because the friendly pass only derives opaque/empty titles
    # (ingest_service.py:2293-2294). Match that net outcome in raw.
    mutation = _reduce_existing_claude_delta(
        previous_title="Earlier friendly Claude title",
        metadata={
            "session_id": "claude-title-fixture",
            "title": "Meaningful unmarked source title",
        },
    )

    assert mutation.document_values["title"] == "Meaningful unmarked source title"
    assert mutation.title_changed is True


def test_existing_claude_delta_applies_explicit_ai_title() -> None:
    mutation = _reduce_existing_claude_delta(
        previous_title="Earlier friendly Claude title",
        metadata={
            "session_id": "claude-title-fixture",
            "source_title_kind": "claude_ai_title",
            "title": "Explicit Claude AI title",
        },
    )

    assert mutation.document_values["title"] == "Explicit Claude AI title"
    assert mutation.title_changed is True
