"""Resource-bounded proxy for the external spend-dashboard snapshot.

The spend dashboard owns telemetry collection, pricing, projections, and
paint-ready chart data. Memento intentionally does not reproduce those
calculations. This service only protects dashboard page loads from a slow
upstream refresh with a small single-flight, stale-while-revalidate cache.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class InvalidSpendSnapshot(ValueError):
    """Raised when the configured service does not return the MCP contract."""


@dataclass(frozen=True)
class _CacheEntry:
    snapshot: dict[str, Any]
    cached_at: datetime
    cached_monotonic: float


@dataclass(frozen=True)
class _RefreshResult:
    entry: _CacheEntry | None
    error: str | None = None


def _snapshot_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/api/snapshot"):
        return normalized
    return f"{normalized}/api/snapshot"


def _validate_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidSpendSnapshot("snapshot_not_an_object")
    required_objects = ("ui", "spend", "models", "tools", "projections", "history")
    if any(not isinstance(payload.get(key), dict) for key in required_objects):
        raise InvalidSpendSnapshot("snapshot_missing_required_sections")
    if not isinstance(payload.get("purpose"), str):
        raise InvalidSpendSnapshot("snapshot_missing_purpose")
    return payload


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"upstream_http_{exc.response.status_code}"
    if isinstance(exc, InvalidSpendSnapshot):
        return str(exc)
    return "upstream_unavailable"


class SpendDashboardProxy:
    """Cache one canonical dashboard snapshot without adding a poller."""

    def __init__(self) -> None:
        self._entry: _CacheEntry | None = None
        self._last_error: str | None = None
        self._state_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[_RefreshResult] | None = None

    def reset(self) -> None:
        """Clear cache state. Intended for focused tests and config reloads."""
        task = self._refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._entry = None
        self._last_error = None
        self._refresh_task = None

    @staticmethod
    def _age(entry: _CacheEntry) -> float:
        return max(0.0, time.monotonic() - entry.cached_monotonic)

    @staticmethod
    def _response(
        entry: _CacheEntry,
        *,
        stale: bool,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "available": True,
            "stale": stale,
            "source": "spend-dashboard-mcp",
            "cached_at": entry.cached_at.isoformat(),
            "age_seconds": round(SpendDashboardProxy._age(entry), 3),
            "snapshot": entry.snapshot,
            "error": error,
        }

    async def _fetch_and_store(self) -> _RefreshResult:
        headers = {"cache-control": "no-store"}
        token = settings.spend_dashboard_access_token.strip()
        if token:
            headers["x-access-token"] = token

        try:
            async with httpx.AsyncClient(
                timeout=settings.spend_dashboard_timeout_seconds
            ) as client:
                response = await client.get(
                    _snapshot_url(settings.spend_dashboard_url),
                    headers=headers,
                )
                response.raise_for_status()
                snapshot = _validate_snapshot(response.json())
            entry = _CacheEntry(
                snapshot=snapshot,
                cached_at=datetime.now(timezone.utc),
                cached_monotonic=time.monotonic(),
            )
            self._entry = entry
            self._last_error = None
            return _RefreshResult(entry=entry)
        except Exception as exc:  # The endpoint degrades to cached/unavailable.
            error = _safe_error(exc)
            self._last_error = error
            logger.warning("Spend dashboard refresh failed: %s", error)
            return _RefreshResult(entry=None, error=error)
        finally:
            async with self._state_lock:
                if self._refresh_task is asyncio.current_task():
                    self._refresh_task = None

    async def _begin_refresh(self) -> asyncio.Task[_RefreshResult]:
        async with self._state_lock:
            task = self._refresh_task
            if task is None or task.done():
                task = asyncio.create_task(self._fetch_and_store())
                self._refresh_task = task
            return task

    async def get_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a fresh snapshot or a bounded stale result.

        Fresh cache hits never touch the upstream. Expired-but-usable cache
        entries return immediately and trigger one shared background refresh.
        An empty or too-old cache waits for the same bounded refresh task.
        """
        if not settings.spend_dashboard_url.strip():
            return {
                "available": False,
                "stale": False,
                "source": "spend-dashboard-mcp",
                "reason": "not_configured",
                "snapshot": None,
            }

        entry = self._entry
        ttl = max(0, settings.spend_dashboard_cache_ttl_seconds)
        max_stale = max(ttl, settings.spend_dashboard_max_stale_seconds)
        age = self._age(entry) if entry else None

        if entry is not None and not force_refresh and age is not None and age <= ttl:
            return self._response(entry, stale=False)

        if entry is not None and not force_refresh and age is not None and age <= max_stale:
            await self._begin_refresh()
            return self._response(entry, stale=True, error=self._last_error)

        result = await (await self._begin_refresh())
        if result.entry is not None:
            return self._response(result.entry, stale=False)

        fallback = self._entry
        if fallback is not None and self._age(fallback) <= max_stale:
            return self._response(fallback, stale=True, error=result.error)
        return {
            "available": False,
            "stale": False,
            "source": "spend-dashboard-mcp",
            "reason": result.error or "upstream_unavailable",
            "snapshot": None,
        }


spend_dashboard_proxy = SpendDashboardProxy()
