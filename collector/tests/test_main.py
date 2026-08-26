import logging
from types import SimpleNamespace

import pytest
from concurrent_log_handler import ConcurrentRotatingFileHandler

import collector.main as collector_main
from collector.main import (
    _COLLECTOR_LOG_HANDLER_MARKER,
    _CanvasPollSchedule,
    _log_queue_heartbeat,
    _setup_logging,
    execute_control_command,
)


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


def _executor_deps(**overrides):
    deps = {
        "config": SimpleNamespace(auto_update_enabled=False),
        "queue": SimpleNamespace(),
        "watcher": SimpleNamespace(),
        "sync_client": SimpleNamespace(),
        "logger": logging.getLogger("collector.test-executor"),
        "on_resync": None,
    }
    deps.update(overrides)
    return deps


def test_repair_command_reports_exact_queued_outcome():
    requested: list[tuple[str, str]] = []
    watcher = SimpleNamespace(
        request_relative_resync=lambda tool, path: requested.append((tool, path)) or True,
    )

    status, error_code, detail = execute_control_command(
        "conversation.repair",
        {
            "paths": [
                {"tool_name": "codex", "relative_path": "sessions/a.jsonl"},
                {"tool_name": "cursor", "relative_path": "b/b.jsonl"},
                {"tool_name": "codex", "relative_path": "ignored-over-batch.jsonl"},
            ]
        },
        **_executor_deps(watcher=watcher),
    )

    assert status == "completed"
    assert error_code is None
    assert detail == {"targets": 2, "queued": 2}
    assert requested == [
        ("codex", "sessions/a.jsonl"),
        ("cursor", "b/b.jsonl"),
    ]


def test_unknown_command_kind_fails_with_stable_capability_code():
    status, error_code, detail = execute_control_command(
        "agent.send_message", {}, **_executor_deps()
    )

    assert status == "failed"
    assert error_code == "capability.unsupported"
    assert detail == {"kind": "agent.send_message"}


def test_resync_failure_reports_outcome_and_reallows_scanning():
    calls: list[str] = []
    watcher = SimpleNamespace(
        cancel_scan=lambda timeout: calls.append("cancel") or False,
        allow_scan=lambda: calls.append("allow"),
    )

    status, error_code, detail = execute_control_command(
        "device.resync", {}, **_executor_deps(watcher=watcher)
    )

    assert status == "failed"
    assert error_code == "command.execution_failed"
    assert detail == {"reason": "scan_did_not_stop"}
    assert calls == ["cancel", "allow"]


def test_update_command_honors_disabled_auto_update():
    status, error_code, detail = execute_control_command(
        "collector.update", {}, **_executor_deps()
    )

    assert status == "completed"
    assert error_code is None
    assert detail == {"skipped": "auto_update_disabled"}


def test_canvas_polling_backs_off_when_idle_and_wakes_only_for_canvas_signals():
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

    schedule.notify_upload(
        SimpleNamespace(category="conversation", relative_path="sessions/a.jsonl"),
        now=11,
    )
    assert schedule.claim_due(29.9) is None

    schedule.notify_upload(
        SimpleNamespace(
            category="conversation",
            relative_path="canvases/live.canvas.tsx",
        ),
        now=11,
    )
    assert schedule.claim_due(12.9) is None
    assert schedule.claim_due(13) == 1


def test_canvas_sync_control_command_wakes_the_existing_poll_schedule():
    woke: list[bool] = []

    status, error_code, detail = execute_control_command(
        "canvas.sync",
        {},
        **_executor_deps(on_canvas_sync=lambda: woke.append(True)),
    )

    assert status == "completed"
    assert error_code is None
    assert detail == {"scheduled": True}
    assert woke == [True]


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
