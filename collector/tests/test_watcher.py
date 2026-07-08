"""Focused tests for filesystem event debouncing."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileModifiedEvent

from collector.watcher import _DebouncedHandler


def _modified(path: Path) -> FileModifiedEvent:
    return FileModifiedEvent(str(path))


def test_event_storm_uses_one_worker_and_coalesces_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    callbacks: list[Path] = []
    called = threading.Event()
    started_threads: list[threading.Thread] = []
    original_start = threading.Thread.start

    class CountingCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.notify_count = 0

        def notify(self, n: int = 1) -> None:
            self.notify_count += 1
            super().notify(n)

    def record_start(thread: threading.Thread) -> None:
        started_threads.append(thread)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", record_start)
    handler = _DebouncedHandler(
        callback=lambda changed: (callbacks.append(changed), called.set()),
        debounce_seconds=0.1,
        excluded_patterns=[],
    )
    condition = CountingCondition()
    handler._condition = condition

    try:
        for _ in range(1_000):
            handler.on_any_event(_modified(path))

        assert called.wait(2)
        assert callbacks == [path]
        assert len(started_threads) == 1
        assert started_threads[0].name.startswith("memento-debouncer-")
        assert condition.notify_count == 1
    finally:
        handler.stop()

    assert not started_threads[0].is_alive()


def test_debounce_is_trailing_edge_and_batches_unique_paths(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    callbacks: list[tuple[Path, float]] = []
    batch_complete = threading.Event()

    def callback(path: Path) -> None:
        callbacks.append((path, time.monotonic()))
        if len(callbacks) == 2:
            batch_complete.set()

    handler = _DebouncedHandler(callback, 0.25, [])
    try:
        handler.on_any_event(_modified(first))
        time.sleep(0.05)
        last_event_at = time.monotonic()
        handler.on_any_event(_modified(first))
        handler.on_any_event(_modified(second))

        assert not batch_complete.wait(0.12)
        assert batch_complete.wait(2)
    finally:
        handler.stop()

    assert [path for path, _called_at in callbacks] == [first, second]
    assert callbacks[0][1] >= last_event_at + 0.20


def test_callback_exception_does_not_drop_remaining_paths(
    caplog,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    callbacks: list[Path] = []
    batch_complete = threading.Event()

    def callback(path: Path) -> None:
        callbacks.append(path)
        if path == first:
            raise RuntimeError("expected callback failure")
        batch_complete.set()

    handler = _DebouncedHandler(callback, 0.01, [])
    try:
        with caplog.at_level(logging.ERROR, logger="collector.watcher"):
            handler.on_any_event(_modified(first))
            handler.on_any_event(_modified(second))
            assert batch_complete.wait(2)
    finally:
        handler.stop()

    assert callbacks == [first, second]
    assert "Error processing" in caplog.text


def test_stop_waits_for_active_callback_and_drops_rest(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    callbacks: list[Path] = []
    callback_started = threading.Event()
    release_callback = threading.Event()
    stop_complete = threading.Event()

    def callback(path: Path) -> None:
        callbacks.append(path)
        callback_started.set()
        assert release_callback.wait(2)

    handler = _DebouncedHandler(callback, 0.01, [])
    handler.on_any_event(_modified(first))
    handler.on_any_event(_modified(second))
    assert callback_started.wait(2)

    def stop_handler() -> None:
        handler.stop()
        stop_complete.set()

    stopper = threading.Thread(target=stop_handler)
    stopper.start()
    try:
        deadline = time.monotonic() + 2
        while not handler._stopped and time.monotonic() < deadline:
            time.sleep(0.001)
        assert handler._stopped
        assert not stop_complete.is_set()
    finally:
        release_callback.set()
        stopper.join(timeout=2)

    assert stop_complete.is_set()
    handler.on_any_event(_modified(second))
    time.sleep(0.05)
    assert callbacks == [first]
