from __future__ import annotations

import logging
from types import SimpleNamespace

from collector.canvas_sync import sync_pending_canvases


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, request: dict) -> None:
        self.request = request
        self.posts: list[tuple[str, dict | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, path: str) -> _Response:
        assert path == "/api/canvas-artifacts/pending"
        return _Response({"artifacts": [self.request]})

    def post(self, path: str, *, json=None, **_kwargs) -> _Response:
        self.posts.append((path, json))
        return _Response({"status": json.get("status") if json else "renderable"})


def _config():
    return SimpleNamespace(
        server=SimpleNamespace(url="https://memento.test", token="collector"),
        device_id="device",
        device_name="host",
        platform="windows",
    )


def test_unchanged_refresh_hash_skips_compiler_and_upload(monkeypatch) -> None:
    source_hash = "a" * 64
    request = {
        "path": r"C:\Users\me\.cursor\projects\work\canvases\live.canvas.tsx",
        "path_hash": "b" * 64,
        "reference_ids": ["11111111-1111-4111-8111-111111111111"],
        "current_source_hash": source_hash,
        "current_render_mode": "interactive",
    }
    client = _Client(request)
    monkeypatch.setattr("collector.canvas_sync.httpx.Client", lambda **_kwargs: client)
    monkeypatch.setattr(
        "collector.canvas_sync.probe_canvas_source",
        lambda _path: SimpleNamespace(source_hash=source_hash),
    )
    monkeypatch.setattr(
        "collector.canvas_sync.locate_canvas_toolchain",
        lambda: (_ for _ in ()).throw(AssertionError("toolchain inspected")),
    )
    monkeypatch.setattr(
        "collector.canvas_sync.capture_canvas",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compiled")),
    )

    counts = sync_pending_canvases(_config(), logging.getLogger("test"))

    assert counts["requested"] == 1
    assert counts["unchanged"] == 1
    assert counts["updated"] == 0
    assert client.posts == [
        (
            "/api/canvas-artifacts/outcome",
            {
                "reference_ids": request["reference_ids"],
                "path_hash": request["path_hash"],
                "status": "unchanged",
                "reason": "source_hash_match",
            },
        )
    ]


def test_static_refresh_retries_rendering_even_when_source_is_unchanged(
    monkeypatch,
) -> None:
    source_hash = "a" * 64
    request = {
        "path": r"C:\Users\me\.cursor\projects\work\canvases\live.canvas.tsx",
        "path_hash": "b" * 64,
        "reference_ids": ["11111111-1111-4111-8111-111111111111"],
        "current_source_hash": source_hash,
        "current_render_mode": "static",
    }
    client = _Client(request)
    captured = SimpleNamespace(
        name="live",
        source=b"source",
        source_hash=source_hash,
        compiled_hash="c" * 64,
        runtime_hash="d" * 64,
        render_mode="interactive",
        compiler_version="test",
        runtime_sdk_version="test",
        static_reason=None,
        compiled_javascript=b"compiled",
        runtime_javascript=b"runtime",
    )
    monkeypatch.setattr("collector.canvas_sync.httpx.Client", lambda **_kwargs: client)
    monkeypatch.setattr(
        "collector.canvas_sync.probe_canvas_source",
        lambda _path: SimpleNamespace(source_hash=source_hash),
    )
    monkeypatch.setattr("collector.canvas_sync.locate_canvas_toolchain", lambda: object())
    monkeypatch.setattr(
        "collector.canvas_sync.capture_canvas",
        lambda *_args, **_kwargs: captured,
    )

    counts = sync_pending_canvases(_config(), logging.getLogger("test"))

    assert counts["requested"] == 1
    assert counts["unchanged"] == 0
    assert counts["renderable"] == 1
    assert counts["updated"] == 1
    assert [path for path, _json in client.posts] == [
        "/api/canvas-artifacts/upload"
    ]
