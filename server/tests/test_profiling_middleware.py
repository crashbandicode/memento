"""On-demand profiling middleware: inert by default, gated by flag + token."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from server.middleware import profiling
from server.middleware.profiling import ProfilingMiddleware


def _client() -> TestClient:
    async def hello(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/hello", hello)])
    app.add_middleware(ProfilingMiddleware)
    return TestClient(app)


@pytest.fixture
def enable(monkeypatch):
    def _set(enabled: bool, token: str) -> None:
        monkeypatch.setattr(profiling.settings, "profiling_enabled", enabled)
        monkeypatch.setattr(profiling.settings, "profiling_token", token)

    return _set


def test_disabled_by_default_passthrough(enable):
    enable(False, "")
    r = _client().get("/hello?profile=1", headers={"x-memento-profile-token": "anything"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_enabled_but_no_profile_flag_passthrough(enable):
    enable(True, "sekret")
    r = _client().get("/hello")
    assert r.json() == {"ok": True}


def test_enabled_wrong_token_is_invisible(enable):
    enable(True, "sekret")
    # Asking to profile without the right token behaves exactly like a normal
    # request — no flame graph, no hint the feature exists.
    r = _client().get("/hello?profile=1", headers={"x-memento-profile-token": "wrong"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_enabled_correct_token_returns_flamegraph(enable):
    enable(True, "sekret")
    r = _client().get("/hello?profile=1", headers={"x-memento-profile-token": "sekret"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # pyinstrument HTML output carries its own marker.
    assert "pyinstrument" in r.text.lower()


def test_token_via_query_param_also_works(enable):
    enable(True, "sekret")
    r = _client().get("/hello?profile=1&profile_token=sekret")
    assert "text/html" in r.headers["content-type"]
