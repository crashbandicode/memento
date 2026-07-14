from types import SimpleNamespace

import httpx

from collector.main import _poll_commands


class Response:
    def __init__(self, status_code: int, body=None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_command_is_not_executed_until_ack_succeeds(monkeypatch):
    command = {
        "id": 42,
        "action": "repair-conversations",
        "paths": [{"tool_name": "codex", "relative_path": "sessions/a.jsonl"}],
    }
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response(200, [command]))
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response(502))
    requested: list[tuple[str, str]] = []
    watcher = SimpleNamespace(
        request_relative_resync=lambda tool, path: requested.append((tool, path)) or True,
    )
    config = SimpleNamespace(
        server=SimpleNamespace(url="https://example.test", token="token"),
        device_id="device",
        auto_update_enabled=False,
    )
    logger = SimpleNamespace(warning=lambda *args: None)

    _poll_commands(config, SimpleNamespace(), watcher, SimpleNamespace(), logger)

    assert requested == []
