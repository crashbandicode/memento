import logging
from types import SimpleNamespace

import httpx
import pytest
from concurrent_log_handler import ConcurrentRotatingFileHandler

import collector.main as collector_main
from collector.main import (
    _COLLECTOR_LOG_HANDLER_MARKER,
    _CanvasPollSchedule,
    _log_queue_heartbeat,
    _poll_commands,
    _setup_logging,
)


class Response:
    def __init__(self, status_code: int, body=None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def collector_logging_cleanup():
    root = logging.getLogger()
    original_level = root.level
    dependency_levels = {
        name: logging.getLogger(name).level for name in ("httpx", "httpcore")
    }
    yield
    for handler in list(root.handlers):
        if getattr(handler, _COLLECTOR_LOG_HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(original_level)
    for name, level in dependency_levels.items():
        logging.getLogger(name).setLevel(level)


def _collector_file_handlers() -> list[ConcurrentRotatingFileHandler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, ConcurrentRotatingFileHandler)
        and getattr(handler, _COLLECTOR_LOG_HANDLER_MARKER, False)
    ]


def test_logging_rotates_with_bounded_retention(
    tmp_path,
    monkeypatch,
    collector_logging_cleanup,
):
    monkeypatch.setattr(collector_main, "COLLECTOR_LOG_MAX_BYTES", 512)
    monkeypatch.setattr(collector_main, "COLLECTOR_LOG_BACKUP_COUNT", 2)

    _setup_logging(SimpleNamespace(log_dir=tmp_path))

    handlers = _collector_file_handlers()
    assert len(handlers) == 1
    handler = handlers[0]
    assert handler.maxBytes == 512
    assert handler.backupCount == 2
    assert handler.encoding.lower().replace("-", "") == "utf8"

    logger = logging.getLogger("collector.rotation-test")
    for index in range(20):
        logger.info("rotation record %02d %s", index, "x" * 100)
    handler.flush()

    assert (tmp_path / "collector.log.1").exists()
    assert not (tmp_path / "collector.log.3").exists()


def test_logging_suppresses_routine_http_dependency_info(
    tmp_path,
    collector_logging_cleanup,
):
    _setup_logging(SimpleNamespace(log_dir=tmp_path))

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("collector").getEffectiveLevel() == logging.INFO


def test_repeated_logging_setup_replaces_and_closes_managed_handlers(
    tmp_path,
    collector_logging_cleanup,
):
    config = SimpleNamespace(log_dir=tmp_path)
    _setup_logging(config)
    first = _collector_file_handlers()[0]
    logging.getLogger("collector.setup-test").info("open the first handler")
    first_stream = first.stream

    _setup_logging(config)

    second = _collector_file_handlers()
    assert len(second) == 1
    assert second[0] is not first
    assert first not in logging.getLogger().handlers
    assert first.stream is None or first.stream.closed
    if first_stream is not None:
        assert first_stream.closed


def test_logging_writes_utf8(
    tmp_path,
    collector_logging_cleanup,
):
    _setup_logging(SimpleNamespace(log_dir=tmp_path))
    handler = _collector_file_handlers()[0]

    logging.getLogger("collector.utf8-test").info("同步标题 → complete")
    handler.flush()
    handler.close()

    text = (tmp_path / "collector.log").read_bytes().decode("utf-8")
    assert "同步标题 → complete" in text


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
