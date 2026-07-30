from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from server.db.models import ConversationTaskState, Document
from server.services import conversation_tasks
from server.services.conversation_tasks import (
    TaskDocumentNotFound,
    TaskSelectorAmbiguous,
    backfill_task_projections,
    canonical_task_state,
    query_conversation_tasks,
    task_state_counts,
    task_state_hash,
)


class _Result:
    def __init__(
        self,
        *,
        rows=None,
        scalar=None,
        scalars=None,
    ) -> None:
        self._rows = list(rows or [])
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)


class _DB:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.statements = []
        self.get_values = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    async def get(self, _model, _identity):
        return self.get_values.pop(0) if self.get_values else None


def _document(thread_id: str, machine_id, *, title: str = "Agent") -> Document:
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    return Document(
        id=uuid4(),
        machine_id=machine_id,
        tool_id="codex",
        relative_path=f"sessions/{thread_id}.jsonl",
        category="conversation",
        content_type="jsonl",
        title=title,
        content_hash="hash",
        file_size_bytes=10,
        metadata_={
            "session_id": thread_id,
            "thread_id": thread_id,
            "root_session_id": thread_id,
            "agent_id": "shared-agent",
        },
        activity_at=now,
        source_modified_at=now,
        synced_at=now,
    )


def _projection(document: Document) -> ConversationTaskState:
    state = canonical_task_state(
        {
            "version": 1,
            "source": "codex",
            "revision": 1,
            "is_current": False,
            "quality": "authoritative",
            "source_ids": [],
            "tasks": [
                {
                    "id": "1",
                    "content": document.title,
                    "status": "pending",
                    "active_form": "",
                }
            ],
        }
    )
    assert state is not None
    counts = task_state_counts(state)
    return ConversationTaskState(
        document_id=document.id,
        machine_id=document.machine_id,
        user_id=uuid4(),
        tool_id=document.tool_id,
        thread_id=document.metadata_["thread_id"],
        root_thread_id=document.metadata_["root_session_id"],
        parent_thread_id=None,
        agent_id=document.metadata_["agent_id"],
        agent_path=None,
        agent_depth=0,
        source_message_id=1,
        source_line_number=1,
        source_ids=[],
        revision=1,
        state=state,
        state_hash=task_state_hash(state),
        explicit_current=False,
        quality="authoritative",
        projection_version=1,
        pending_count=1,
        in_progress_count=0,
        blocked_count=0,
        completed_count=0,
        cancelled_count=0,
        outstanding_count=counts["outstanding"],
        total_count=1,
        observed_at=datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
        verified_at=None,
    )


@pytest.mark.asyncio
async def test_explicit_unauthorized_document_is_indistinguishable_from_absent(
    monkeypatch,
) -> None:
    machine_id = uuid4()

    async def _machine_ids(_db, _user):
        return [machine_id]

    monkeypatch.setattr(conversation_tasks, "user_machine_ids", _machine_ids)
    db = _DB([_Result(scalar=None)])

    with pytest.raises(TaskDocumentNotFound):
        await query_conversation_tasks(
            db,
            SimpleNamespace(id=uuid4(), role="viewer"),
            document_id=uuid4(),
        )

    sql = str(db.statements[0].compile())
    assert "documents.machine_id IN" in sql


@pytest.mark.asyncio
async def test_cross_thread_agent_collision_returns_bounded_ambiguity(
    monkeypatch,
) -> None:
    machine_id = uuid4()
    first = _document("root-one", machine_id, title="One")
    second = _document("root-two", machine_id, title="Two")

    async def _machine_ids(_db, _user):
        return [machine_id]

    monkeypatch.setattr(conversation_tasks, "user_machine_ids", _machine_ids)
    db = _DB(
        [
            _Result(
                rows=[
                    (first, _projection(first)),
                    (second, _projection(second)),
                ]
            )
        ]
    )

    with pytest.raises(TaskSelectorAmbiguous) as error:
        await query_conversation_tasks(
            db,
            SimpleNamespace(id=uuid4(), role="viewer"),
            agent_id="shared-agent",
            status="all",
        )

    assert len(error.value.candidates) == 2
    assert {item["title"] for item in error.value.candidates} == {"One", "Two"}


@pytest.mark.asyncio
async def test_root_pagination_is_cursor_bound_and_output_is_capped(
    monkeypatch,
) -> None:
    machine_id = uuid4()
    documents = [
        _document(f"root-{index}", machine_id, title=f"Agent {index}")
        for index in range(3)
    ]
    matched = [(document, _projection(document)) for document in documents]

    async def _machine_ids(_db, _user):
        return [machine_id]

    monkeypatch.setattr(conversation_tasks, "user_machine_ids", _machine_ids)
    db = _DB([_Result(rows=matched), _Result(rows=matched)])

    response = await query_conversation_tasks(
        db,
        SimpleNamespace(id=uuid4(), role="viewer"),
        status="all",
        limit=2,
        max_tasks=1,
    )

    assert len(response["root_threads"]) == 2
    assert response["pagination"]["has_more"] is True
    assert response["pagination"]["next_cursor"]
    assert response["truncated"]["tasks"] is True


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_uses_normalized_metadata(
    monkeypatch,
) -> None:
    document = _document("root", uuid4())
    projected = _projection(document)

    async def _refresh(_db, _document):
        return projected

    monkeypatch.setattr(conversation_tasks, "refresh_task_projection", _refresh)
    first_db = _DB([_Result(scalars=[document])])
    first_db.get_values = [None]
    second_db = _DB([_Result(scalars=[document])])
    second_db.get_values = [projected]

    first = await backfill_task_projections(first_db)
    second = await backfill_task_projections(second_db)

    assert first == {"documents": 1, "created_or_updated": 1}
    assert second == {"documents": 1, "created_or_updated": 0}
    compiled = first_db.statements[0].compile()
    assert "conversation_messages" in str(compiled)
    assert "documents.content," not in str(compiled)
    assert "task_state" in compiled.params.values()


def test_projection_model_declares_selector_and_status_indexes() -> None:
    table = ConversationTaskState.__table__
    names = {index.name for index in table.indexes}

    assert {
        "idx_task_state_thread",
        "idx_task_state_root",
        "idx_task_state_parent",
        "idx_task_state_agent",
        "idx_task_state_outstanding",
    } <= names
    assert table.c.document_id.primary_key
    assert table.c.source_message_id.foreign_keys
    assert table.c.state.type.__class__.__name__ == "JSONB"
