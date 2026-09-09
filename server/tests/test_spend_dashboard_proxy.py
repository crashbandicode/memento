from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from server.config import settings
from server.services.spend_dashboard_proxy import SpendDashboardProxy


def _snapshot(*, used: str = "$12.34") -> dict:
    return {
        "purpose": "Read-only dashboard view.",
        "ui": {},
        "spend": {"all": {"used": used}},
        "models": {},
        "tools": {},
        "projections": {},
        "history": {},
    }


class _Response:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("GET", "http://spend.test/api/snapshot")

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "upstream failure",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


def _install_client(
    monkeypatch,
    responder: Callable[[str, dict], Awaitable[_Response]],
) -> list[dict]:
    calls: list[dict] = []

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            calls.append({"timeout": timeout})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url: str, *, headers: dict) -> _Response:
            calls.append({"url": url, "headers": headers})
            return await responder(url, headers)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


@pytest.fixture(autouse=True)
def _settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "spend_dashboard_url", "http://spend.test")
    monkeypatch.setattr(settings, "spend_dashboard_access_token", "")
    monkeypatch.setattr(settings, "spend_dashboard_timeout_seconds", 9.0)
    monkeypatch.setattr(settings, "spend_dashboard_cache_ttl_seconds", 300)
    monkeypatch.setattr(settings, "spend_dashboard_max_stale_seconds", 3600)


def test_cached_snapshot_never_starts_an_upstream_fetch(monkeypatch) -> None:
    async def unexpected(_url: str, _headers: dict) -> _Response:
        raise AssertionError("cached-only lookup called upstream")

    calls = _install_client(monkeypatch, unexpected)
    proxy = SpendDashboardProxy()

    assert proxy.get_cached_snapshot() == {
        "available": False,
        "stale": False,
        "source": "spend-dashboard-mcp",
        "reason": "cache_empty",
        "snapshot": None,
    }
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_integration_never_calls_upstream(monkeypatch) -> None:
    monkeypatch.setattr(settings, "spend_dashboard_url", "")

    async def unexpected(_url: str, _headers: dict) -> _Response:
        raise AssertionError("disabled integration called upstream")

    calls = _install_client(monkeypatch, unexpected)
    result = await SpendDashboardProxy().get_snapshot()

    assert result == {
        "available": False,
        "stale": False,
        "source": "spend-dashboard-mcp",
        "reason": "not_configured",
        "snapshot": None,
    }
    assert calls == []


@pytest.mark.asyncio
async def test_initial_fetch_is_cached_and_sends_optional_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "spend_dashboard_access_token", "secret")

    async def respond(_url: str, _headers: dict) -> _Response:
        return _Response(_snapshot())

    calls = _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    first = await proxy.get_snapshot()
    second = await proxy.get_snapshot()

    assert first["available"] is True
    assert first["stale"] is False
    assert first["snapshot"]["spend"]["all"]["used"] == "$12.34"
    assert second["snapshot"] == first["snapshot"]
    assert calls == [
        {"timeout": 9.0},
        {
            "url": "http://spend.test/api/snapshot",
            "headers": {
                "cache-control": "no-store",
                "x-access-token": "secret",
            },
        },
    ]


@pytest.mark.asyncio
async def test_stale_hits_return_immediately_and_share_one_refresh(monkeypatch) -> None:
    monkeypatch.setattr(settings, "spend_dashboard_cache_ttl_seconds", 0)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    response_number = 0

    async def respond(_url: str, _headers: dict) -> _Response:
        nonlocal response_number
        response_number += 1
        if response_number == 1:
            return _Response(_snapshot(used="$1.00"))
        refresh_started.set()
        await release_refresh.wait()
        return _Response(_snapshot(used="$2.00"))

    calls = _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()
    await proxy.get_snapshot()
    await asyncio.sleep(0.001)

    stale_one = await proxy.get_snapshot()
    stale_two = await proxy.get_snapshot()
    await asyncio.wait_for(refresh_started.wait(), timeout=1)

    assert stale_one["stale"] is True
    assert stale_two["stale"] is True
    assert stale_one["snapshot"]["spend"]["all"]["used"] == "$1.00"
    assert len([call for call in calls if "url" in call]) == 2

    release_refresh.set()
    await asyncio.sleep(0.01)
    refreshed = await proxy.get_snapshot(force_refresh=True)
    assert refreshed["snapshot"]["spend"]["all"]["used"] == "$2.00"


@pytest.mark.asyncio
async def test_failed_forced_refresh_preserves_bounded_cache(monkeypatch) -> None:
    response_number = 0

    async def respond(_url: str, _headers: dict) -> _Response:
        nonlocal response_number
        response_number += 1
        if response_number == 1:
            return _Response(_snapshot())
        return _Response({}, status_code=503)

    _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()
    await proxy.get_snapshot()

    result = await proxy.get_snapshot(force_refresh=True)

    assert result["available"] is True
    assert result["stale"] is True
    assert result["error"] == "upstream_http_503"
    assert result["snapshot"]["purpose"] == "Read-only dashboard view."


@pytest.mark.asyncio
async def test_invalid_first_snapshot_fails_closed(monkeypatch) -> None:
    async def respond(_url: str, _headers: dict) -> _Response:
        return _Response({"spend": {}})

    _install_client(monkeypatch, respond)
    result = await SpendDashboardProxy().get_snapshot()

    assert result == {
        "available": False,
        "stale": False,
        "source": "spend-dashboard-mcp",
        "reason": "snapshot_missing_required_sections",
        "snapshot": None,
    }


def _snapshot_with_hop(document_id: str = "near-hop-document") -> dict:
    payload = _snapshot()
    payload["hygiene"] = {
        "hopThreads": {
            "items": [
                {
                    "documentId": document_id,
                    "source": "codex",
                    "ratio": 180.0,
                    "status": "near",
                    "hopLine": 200,
                    "hardLine": 400,
                }
            ]
        }
    }
    return payload


def test_prime_cache_is_a_noop_when_integration_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "spend_dashboard_url", "")

    async def unexpected(_url: str, _headers: dict) -> _Response:
        raise AssertionError("disabled integration triggered a warm")

    calls = _install_client(monkeypatch, unexpected)
    proxy = SpendDashboardProxy()

    proxy.prime_cache()

    assert calls == []
    assert proxy.get_cached_snapshot()["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_prime_cache_warms_empty_cache_without_blocking(monkeypatch) -> None:
    fetch_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def respond(_url: str, _headers: dict) -> _Response:
        fetch_started.set()
        await release_refresh.wait()
        return _Response(_snapshot(used="$7.50"))

    calls = _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    # The cache-only read path triggers a background warm and returns at once.
    proxy.prime_cache()
    assert proxy.get_cached_snapshot()["reason"] == "cache_empty"

    # The refresh is in flight but has NOT populated the cache yet: the request
    # path did not await the upstream fetch.
    await asyncio.wait_for(fetch_started.wait(), timeout=1)
    assert proxy.get_cached_snapshot()["reason"] == "cache_empty"

    release_refresh.set()
    await asyncio.wait_for(proxy._refresh_task, timeout=1)

    warmed = proxy.get_cached_snapshot()
    assert warmed["available"] is True
    assert warmed["stale"] is False
    assert warmed["snapshot"]["spend"]["all"]["used"] == "$7.50"
    assert len([call for call in calls if "url" in call]) == 1


@pytest.mark.asyncio
async def test_prime_cache_rapid_triggers_share_one_refresh(monkeypatch) -> None:
    fetch_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def respond(_url: str, _headers: dict) -> _Response:
        fetch_started.set()
        await release_refresh.wait()
        return _Response(_snapshot())

    calls = _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    # Rapid open-threads polls (~every 13 s) hitting an empty/stale cache must
    # not stack duplicate in-flight refreshes.
    proxy.prime_cache()
    proxy.prime_cache()
    proxy.prime_cache()
    await asyncio.wait_for(fetch_started.wait(), timeout=1)
    proxy.prime_cache()  # still in flight

    assert len([call for call in calls if "url" in call]) == 1

    release_refresh.set()
    await asyncio.wait_for(proxy._refresh_task, timeout=1)
    assert proxy.get_cached_snapshot()["available"] is True


@pytest.mark.asyncio
async def test_prime_cache_skips_refresh_when_cache_is_fresh(monkeypatch) -> None:
    async def respond(_url: str, _headers: dict) -> _Response:
        return _Response(_snapshot())

    calls = _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    await proxy.get_snapshot()  # one warm fetch
    url_calls = len([call for call in calls if "url" in call])
    assert url_calls == 1

    proxy.prime_cache()  # fresh cache → no new fetch scheduled
    await asyncio.sleep(0.01)

    assert proxy._refresh_task is None
    assert len([call for call in calls if "url" in call]) == url_calls


@pytest.mark.asyncio
async def test_prime_cache_refreshes_expired_cache(monkeypatch) -> None:
    monkeypatch.setattr(settings, "spend_dashboard_cache_ttl_seconds", 0)
    response_number = 0

    async def respond(_url: str, _headers: dict) -> _Response:
        nonlocal response_number
        response_number += 1
        return _Response(_snapshot(used=f"${response_number}.00"))

    calls = _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    await proxy.get_snapshot()  # first warm (ttl=0 → immediately stale)
    assert len([call for call in calls if "url" in call]) == 1

    proxy.prime_cache()  # stale/expired → background refresh
    await asyncio.wait_for(proxy._refresh_task, timeout=1)

    assert len([call for call in calls if "url" in call]) == 2
    assert proxy.get_cached_snapshot()["snapshot"]["spend"]["all"]["used"] == "$2.00"


@pytest.mark.asyncio
async def test_prime_cache_swallows_upstream_errors(monkeypatch) -> None:
    async def respond(_url: str, _headers: dict) -> _Response:
        return _Response({}, status_code=503)

    _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    proxy.prime_cache()
    task = proxy._refresh_task
    assert task is not None
    # A failed background warm resolves without raising to the caller.
    await asyncio.wait_for(task, timeout=1)

    result = proxy.get_cached_snapshot()
    assert result["available"] is False
    assert result["reason"] == "cache_empty"


@pytest.mark.asyncio
async def test_primed_warm_yields_health_for_near_hop_document(monkeypatch) -> None:
    dashboard = pytest.importorskip("server.api.dashboard")
    thread_health_by_document = dashboard._thread_health_by_document

    async def respond(_url: str, _headers: dict) -> _Response:
        return _Response(_snapshot_with_hop("near-hop-document"))

    _install_client(monkeypatch, respond)
    proxy = SpendDashboardProxy()

    proxy.prime_cache()
    task = proxy._refresh_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)

    health = thread_health_by_document(proxy.get_cached_snapshot())
    assert health["near-hop-document"] == {
        "available": True,
        "stale": False,
        "source": "codex",
        "ratio": 180.0,
        "status": "near",
        "hop_line": 200.0,
        "hard_line": 400.0,
    }
