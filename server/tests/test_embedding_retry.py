from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest

from server.tasks import embedding_retry
from server.services import embedding_service


class _LockConnection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.commits = 0
        self.unlocks = 0

    async def scalar(self, *_args, **_kwargs) -> bool:
        return self.acquired

    async def execute(self, *_args, **_kwargs) -> None:
        self.unlocks += 1

    async def commit(self) -> None:
        self.commits += 1


class _ConnectionContext:
    def __init__(self, connection: _LockConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _LockConnection:
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


class _Engine:
    def __init__(self, connection: _LockConnection) -> None:
        self.connection = connection

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)


@pytest.mark.asyncio
async def test_overlapping_retry_sweep_exits_when_lock_is_held(monkeypatch) -> None:
    connection = _LockConnection(acquired=False)
    monkeypatch.setattr(embedding_retry, "engine", _Engine(connection))

    async def _unexpected():
        pytest.fail("locked retry sweep processed documents")

    monkeypatch.setattr(embedding_retry, "_run_locked", _unexpected)

    result = await embedding_retry._run()

    assert result == {
        "scanned": 0,
        "retried": 0,
        "recovered": 0,
        "locked": True,
    }
    assert connection.commits == 1
    assert connection.unlocks == 0


@pytest.mark.asyncio
async def test_retry_sweep_releases_session_lock(monkeypatch) -> None:
    connection = _LockConnection(acquired=True)
    monkeypatch.setattr(embedding_retry, "engine", _Engine(connection))

    async def _completed():
        return {"scanned": 1, "retried": 1, "recovered": 1}

    monkeypatch.setattr(embedding_retry, "_run_locked", _completed)

    result = await embedding_retry._run()

    assert result == {"scanned": 1, "retried": 1, "recovered": 1}
    assert connection.commits == 2
    assert connection.unlocks == 1


class _DocumentResult:
    def __init__(self, document) -> None:
        self.document = document

    def scalar_one_or_none(self):
        return self.document


class _RetryDB:
    def __init__(self, document) -> None:
        self.document = document

    async def execute(self, _statement) -> _DocumentResult:
        return _DocumentResult(self.document)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _RetrySessionContext:
    def __init__(self, db: _RetryDB) -> None:
        self.db = db

    async def __aenter__(self) -> _RetryDB:
        return self.db

    async def __aexit__(self, *_args) -> None:
        return None


class _RetrySessionFactory:
    def __init__(self, documents: list) -> None:
        self.documents = list(documents)
        self.opened = 0

    def __call__(self) -> _RetrySessionContext:
        self.opened += 1
        document = self.documents.pop(0)
        return _RetrySessionContext(_RetryDB(document))


@pytest.mark.asyncio
async def test_retry_sweep_refetches_each_document(monkeypatch) -> None:
    first = SimpleNamespace(id=uuid4(), relative_path="first.jsonl")
    second = SimpleNamespace(id=uuid4(), relative_path="second.jsonl")
    factory = _RetrySessionFactory([first, second, None])
    processed: list = []

    async def _generate(_db, document) -> int:
        processed.append(document.id)
        return 1

    monkeypatch.setattr(embedding_retry, "async_session_factory", factory)
    monkeypatch.setattr(embedding_retry, "generate_document_embeddings", _generate)
    monkeypatch.setattr(embedding_retry, "BATCH_SIZE", 3)

    result = await embedding_retry._run_locked()

    assert result == {"scanned": 2, "retried": 2, "recovered": 2}
    assert processed == [first.id, second.id]
    assert factory.opened == 3


def test_retry_task_handles_one_document_with_coherent_deadline() -> None:
    timeout = inspect.signature(
        embedding_service._call_embedding_server
    ).parameters["timeout"].default

    assert embedding_retry.BATCH_SIZE == 1
    assert embedding_retry.retry_failed_embeddings.time_limit >= timeout + 300
    schedule = embedding_retry.celery_app.conf.beat_schedule["embedding-retry"]
    assert len(schedule["schedule"].minute) == 60
