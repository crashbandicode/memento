from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from sqlalchemy.exc import MissingGreenlet

from server.db import session as session_module
from server.db.models import Document
from server.services import embedding_service, graph_service, ingest_service


class _DocumentStub:
    def __init__(
        self,
        document_id,
        relative_path: str,
        content_hash: str = "current-revision",
    ) -> None:
        self.id = document_id
        self._relative_path = relative_path
        self.content_hash = content_hash
        self.expired = False

    @property
    def relative_path(self) -> str:
        if self.expired:
            raise MissingGreenlet("expired ORM attribute attempted implicit IO")
        return self._relative_path


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _SessionStub:
    def __init__(self, initial_doc, refreshed_doc) -> None:
        self.initial_doc = initial_doc
        self.refreshed_doc = refreshed_doc
        self.rollback_count = 0
        self.commit_count = 0
        self.get_calls: list[tuple] = []

    async def execute(self, _statement):
        return _ScalarResult(self.initial_doc)

    async def get(self, model, document_id, *, populate_existing=False):
        self.get_calls.append((model, document_id, populate_existing))
        return self.refreshed_doc

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def commit(self) -> None:
        self.commit_count += 1


class _SessionContext:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args) -> None:
        return None


def _install_session(monkeypatch, session) -> None:
    monkeypatch.setattr(
        session_module,
        "post_ingest_session_factory",
        lambda: _SessionContext(session),
    )


@pytest.mark.asyncio
async def test_post_ingest_rechecks_expected_revision_in_processing_session(
    monkeypatch,
) -> None:
    document_id = uuid4()
    current = _DocumentStub(
        document_id,
        "sessions/thread.jsonl",
        content_hash="new-revision",
    )
    session = _SessionStub(current, current)
    _install_session(monkeypatch, session)

    async def _unexpected(*_args) -> int:
        pytest.fail("superseded task reached an expensive post-ingest helper")

    monkeypatch.setattr(
        embedding_service,
        "generate_document_embeddings",
        _unexpected,
    )
    monkeypatch.setattr(
        graph_service,
        "extract_knowledge_from_document",
        _unexpected,
    )

    await ingest_service._run_post_ingest_inner(
        document_id,
        "codex",
        "conversation",
        expected_revision="old-revision",
    )

    assert session.commit_count == 0
    assert session.rollback_count == 0
    assert session.get_calls == []


@pytest.mark.asyncio
async def test_post_ingest_does_not_extract_newer_revision_after_embedding(
    monkeypatch,
) -> None:
    document_id = uuid4()
    initial = _DocumentStub(
        document_id,
        "sessions/thread.jsonl",
        content_hash="old-revision",
    )
    refreshed = _DocumentStub(
        document_id,
        "sessions/thread.jsonl",
        content_hash="new-revision",
    )
    session = _SessionStub(initial, refreshed)
    _install_session(monkeypatch, session)

    async def _embed(_db, _doc) -> int:
        return 0

    async def _unexpected(*_args) -> int:
        pytest.fail("old task extracted graph data from the newer revision")

    monkeypatch.setattr(embedding_service, "generate_document_embeddings", _embed)
    monkeypatch.setattr(
        graph_service,
        "extract_knowledge_from_document",
        _unexpected,
    )

    await ingest_service._run_post_ingest_inner(
        document_id,
        "codex",
        "conversation",
        expected_revision="old-revision",
    )

    assert session.get_calls == [(Document, document_id, True)]
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_post_ingest_reloads_after_embedding_internal_rollback(
    monkeypatch,
) -> None:
    document_id = uuid4()
    initial = _DocumentStub(document_id, "sessions/thread.jsonl")
    refreshed = _DocumentStub(document_id, "sessions/thread.jsonl")
    session = _SessionStub(initial, refreshed)
    _install_session(monkeypatch, session)
    graph_documents = []

    async def _embed(db, doc) -> int:
        doc.expired = True
        await db.rollback()
        return 0

    async def _extract(_db, doc) -> int:
        graph_documents.append(doc)
        return 0

    monkeypatch.setattr(embedding_service, "generate_document_embeddings", _embed)
    monkeypatch.setattr(graph_service, "extract_knowledge_from_document", _extract)

    await ingest_service._run_post_ingest_inner(
        document_id,
        "codex",
        "conversation",
    )

    assert graph_documents == [refreshed]
    assert session.get_calls == [(Document, document_id, True)]


@pytest.mark.asyncio
async def test_post_ingest_uses_scalar_label_when_embedding_raises_expired(
    monkeypatch,
    caplog,
) -> None:
    document_id = uuid4()
    initial = _DocumentStub(document_id, "sessions/thread.jsonl")
    refreshed = _DocumentStub(document_id, "sessions/thread.jsonl")
    session = _SessionStub(initial, refreshed)
    _install_session(monkeypatch, session)
    graph_documents = []

    async def _embed(_db, doc) -> int:
        doc.expired = True
        raise RuntimeError("embedding transaction failed")

    async def _extract(_db, doc) -> int:
        graph_documents.append(doc)
        return 0

    monkeypatch.setattr(embedding_service, "generate_document_embeddings", _embed)
    monkeypatch.setattr(graph_service, "extract_knowledge_from_document", _extract)
    caplog.set_level(logging.INFO, logger="post_ingest")

    await ingest_service._run_post_ingest_inner(
        document_id,
        "codex",
        "conversation",
    )

    assert graph_documents == [refreshed]
    assert session.rollback_count == 1
    assert "Embedding skipped for sessions/thread.jsonl" in caplog.text
    assert "greenlet_spawn has not been called" not in caplog.text
