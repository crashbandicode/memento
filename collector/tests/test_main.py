from types import SimpleNamespace

import httpx

from collector.main import _CanvasPollSchedule, _log_queue_heartbeat, _poll_commands


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


def test_canvas_polling_backs_off_when_idle_and_resets_after_upload():
    schedule = _CanvasPollSchedule(minimum=5, maximum=20)
    empty = {"requested": 0, "failed": 0}

    generation = schedule.claim_due(0)
    assert generation == 0
    schedule.complete(generation, empty, now=0)
    assert schedule.claim_due(9.9) is None

    generation = schedule.claim_due(10)
    assert generation == 0
    schedule.complete(generation, empty, now=10)
    assert schedule.claim_due(29.9) is None

    schedule.notify_upload(SimpleNamespace(category="conversation"), now=11)
    assert schedule.claim_due(12.9) is None
    assert schedule.claim_due(13) == 1


def test_unchanged_heartbeat_does_not_query_queue_again():
    class Queue:
        def __init__(self):
            self.token = 7
            self.pending_calls = 0

        def change_token(self):
            return self.token

        def pending_count(self):
            self.pending_calls += 1
            return 0

    queue = Queue()
    logger = SimpleNamespace(info=lambda *_args: None)

    token = _log_queue_heartbeat(queue, logger, -1)
    assert _log_queue_heartbeat(queue, logger, token) == token
    assert queue.pending_calls == 1
