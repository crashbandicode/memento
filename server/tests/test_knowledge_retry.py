from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from server.tasks import knowledge_retry


@pytest.mark.asyncio
async def test_retry_scanner_does_not_query_without_provider(monkeypatch) -> None:
    def _unexpected_session():
        pytest.fail("knowledge retry opened a database session without a provider")

    monkeypatch.setattr(knowledge_retry, "knowledge_provider_configured", lambda: False)
    monkeypatch.setattr(knowledge_retry, "async_session_factory", _unexpected_session)

    assert await knowledge_retry._run() == {
        "scanned": 0,
        "retried": 0,
        "recovered": 0,
        "disabled": True,
    }


@pytest.mark.asyncio
async def test_retry_scanner_honors_durable_retry_timestamp(monkeypatch) -> None:
    statements = []

    class _Scalars:
        @staticmethod
        def all():
            return []

    class _Result:
        @staticmethod
        def scalars():
            return _Scalars()

    class _Session:
        async def execute(self, statement):
            statements.append(statement)
            return _Result()

    class _Context:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(knowledge_retry, "knowledge_provider_configured", lambda: True)
    monkeypatch.setattr(knowledge_retry, "async_session_factory", _Context)

    assert await knowledge_retry._run() == {
        "scanned": 0,
        "retried": 0,
        "recovered": 0,
    }
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "documents.knowledge_status" in sql
    assert "documents.knowledge_retry_at" in sql
    assert "documents.knowledge_attempts" in sql
    assert "permanent_failed" not in compiled.params.values()
