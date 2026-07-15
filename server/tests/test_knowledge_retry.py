from __future__ import annotations

import pytest

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
