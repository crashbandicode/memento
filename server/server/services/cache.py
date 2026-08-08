"""Lightweight Redis-backed JSON cache for hot read endpoints.

Currently used by ``/api/daily`` (list-of-dates aggregate) where the cold-DB
query reads ~65 MB of conversation_messages blocks (2-3 s) but the answer
itself is only kilobytes and stable for tens of seconds at a time.

Failures (Redis down, serialisation glitch) degrade silently to a cache
miss — the caller still computes the live answer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from ..config import settings

logger = logging.getLogger("cache")

_client: aioredis.Redis | None = None
_PENDING_INVALIDATIONS_KEY = "memento_cache_namespace_invalidations"
_GENERATION_PREFIX = "cache:generation:"


def _get_client() -> aioredis.Redis | None:
    global _client
    if _client is not None:
        return _client
    try:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception as e:
        logger.warning("Redis cache disabled: %s", e)
        _client = None
    return _client


async def cache_get(key: str) -> Any | None:
    c = _get_client()
    if c is None:
        return None
    try:
        v = await c.get(key)
        if v is None:
            return None
        return json.loads(v)
    except Exception as e:
        logger.debug("cache_get(%s) failed: %s", key, e)
        return None


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    c = _get_client()
    if c is None:
        return
    try:
        await c.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl_seconds)
    except Exception as e:
        logger.debug("cache_set(%s) failed: %s", key, e)


def daily_cache_namespace(user_id: object) -> str:
    return f"daily:{user_id}"


def project_conversations_cache_namespace(
    user_id: object,
    project_id: object,
) -> str:
    return f"project-conversations:{user_id}:{project_id}"


async def namespaced_cache_key(namespace: str, suffix: str) -> str:
    """Return a cache key bound to the namespace's current generation."""
    c = _get_client()
    if c is None:
        generation = "0"
    else:
        try:
            generation = await c.get(f"{_GENERATION_PREFIX}{namespace}") or "0"
        except Exception as e:
            logger.debug("cache generation read (%s) failed: %s", namespace, e)
            generation = "0"
    return f"cache:data:{namespace}:g{generation}:{suffix}"


def stage_cache_invalidation(session: Any, *namespaces: str) -> None:
    """Deduplicate invalidations in transaction-local session state."""
    info = getattr(session, "info", None)
    if info is None:
        sync_session = getattr(session, "sync_session", None)
        info = getattr(sync_session, "info", None)
    if info is None:
        return
    pending = info.setdefault(_PENDING_INVALIDATIONS_KEY, set())
    pending.update(namespace for namespace in namespaces if namespace)


def discard_staged_cache_invalidations(session: Any) -> None:
    info = getattr(session, "info", None)
    if info is None:
        sync_session = getattr(session, "sync_session", None)
        info = getattr(sync_session, "info", None)
    if info is not None:
        info.pop(_PENDING_INVALIDATIONS_KEY, None)


async def publish_staged_cache_invalidations(session: Any) -> int:
    """Advance each changed namespace once, strictly after DB commit."""
    info = getattr(session, "info", None)
    if info is None:
        sync_session = getattr(session, "sync_session", None)
        info = getattr(sync_session, "info", None)
    if info is None:
        return 0
    namespaces = sorted(info.pop(_PENDING_INVALIDATIONS_KEY, set()))
    if not namespaces:
        return 0
    c = _get_client()
    if c is None:
        return 0
    published = 0
    try:
        for namespace in namespaces:
            await c.incr(f"{_GENERATION_PREFIX}{namespace}")
            published += 1
    except Exception as e:
        # The database commit already succeeded. Cache failures remain
        # best-effort and stale generations are still bounded by data-key TTLs.
        logger.debug("cache namespace publish failed: %s", e)
    return published
