from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.session import TransactionalAsyncSession
from server.services import cache


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.increments: list[str] = []

    async def get(self, key: str):
        return self.values.get(key)

    async def incr(self, key: str):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        self.increments.append(key)
        return value

    async def scan_iter(self, **_kwargs):
        raise AssertionError("namespace invalidation must not scan Redis")

    async def delete(self, *_keys):
        raise AssertionError("namespace invalidation must not delete data keys")


@pytest.mark.asyncio
async def test_generation_invalidation_is_bounded_and_deduplicated(
    monkeypatch,
) -> None:
    redis = _Redis()
    monkeypatch.setattr(cache, "_client", redis)
    session = type("Session", (), {"info": {}})()

    cache.stage_cache_invalidation(session, "daily:user", "daily:user")
    cache.stage_cache_invalidation(session, "project:user:one")

    assert await cache.publish_staged_cache_invalidations(session) == 2
    assert sorted(redis.increments) == [
        "cache:generation:daily:user",
        "cache:generation:project:user:one",
    ]
    assert await cache.publish_staged_cache_invalidations(session) == 0


@pytest.mark.asyncio
async def test_commit_publishes_and_rollback_discards(monkeypatch) -> None:
    redis = _Redis()
    monkeypatch.setattr(cache, "_client", redis)
    monkeypatch.setattr(AsyncSession, "commit", AsyncMock())
    monkeypatch.setattr(AsyncSession, "rollback", AsyncMock())
    session = TransactionalAsyncSession()

    cache.stage_cache_invalidation(session, "daily:user")
    await session.commit()
    assert redis.increments == ["cache:generation:daily:user"]

    cache.stage_cache_invalidation(session, "project:user:one")
    await session.rollback()
    assert redis.increments == ["cache:generation:daily:user"]
    assert await cache.publish_staged_cache_invalidations(session) == 0


@pytest.mark.asyncio
async def test_generation_change_makes_old_data_key_unreachable(monkeypatch) -> None:
    redis = _Redis()
    monkeypatch.setattr(cache, "_client", redis)
    namespace = cache.daily_cache_namespace("user")

    before = await cache.namespaced_cache_key(namespace, "dates:30:0")
    session = type("Session", (), {"info": {}})()
    cache.stage_cache_invalidation(session, namespace)
    await cache.publish_staged_cache_invalidations(session)
    after = await cache.namespaced_cache_key(namespace, "dates:30:0")

    assert before.endswith(":g0:dates:30:0")
    assert after.endswith(":g1:dates:30:0")
