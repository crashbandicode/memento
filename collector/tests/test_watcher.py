"""Focused tests for filesystem event debouncing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from watchdog.events import (
    FileClosedNoWriteEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
)

from collector.outcomes import UploadOutcome
from collector.parsers.base import ParseResult
from collector.parsers.jsonl import JsonlParser
from collector.queue import SyncQueue
from collector.sanitizer import sanitize_jsonl
from collector.tools.base import (
    Category,
    ContentType,
    FileClassification,
    SyncStrategy,
)
from collector.watcher import (
    FULL_IDENTITY_VERSION,
    FileWatcher,
    _DebouncedHandler,
    _delta_hash_revision,
    _legacy_full_hash_revision,
)


def _modified(path: Path) -> FileModifiedEvent:
    return FileModifiedEvent(str(path))


def test_delta_revision_token_is_reproducible_for_an_append_prefix(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    first = b'{"message":"first"}\n'
    path.write_bytes(first)
    original = _delta_hash_revision(path, size=len(first))
    os.utime(path, (1_700_000_100, 1_700_000_100))

    assert _delta_hash_revision(path, size=len(first)) == original
    with path.open("ab") as stream:
        stream.write(b'{"message":"second"}\n')
    assert _delta_hash_revision(path, size=len(first)) == original
    assert _delta_hash_revision(path, size=path.stat().st_size) != original


def test_read_only_events_never_enter_debounce_queue(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    callbacks: list[Path] = []
    handler = _DebouncedHandler(callbacks.append, 0, [])

    try:
        handler.on_any_event(FileOpenedEvent(str(path)))
        handler.on_any_event(FileClosedNoWriteEvent(str(path)))
        handler.on_any_event(
            SimpleNamespace(
                is_directory=False,
                event_type="accessed",
                src_path=str(path),
            )
        )

        assert callbacks == []
        assert handler._pending == {}
        assert handler._worker is None
    finally:
        handler.stop()


def test_moved_event_routes_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "active.jsonl"
    destination = tmp_path / "archived.jsonl"
    destination.write_text("{}\n", encoding="utf-8")
    callbacks: list[Path] = []
    called = threading.Event()
    handler = _DebouncedHandler(
        lambda changed: (callbacks.append(changed), called.set()),
        0,
        [],
    )

    try:
        handler.on_any_event(FileMovedEvent(str(source), str(destination)))
        assert called.wait(2)
        assert callbacks == [destination]
    finally:
        handler.stop()


def test_relative_resync_resolves_only_files_below_the_tool_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    transcript = root / "sessions" / "thread.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    tool = SimpleNamespace(name="codex", root_path=root)
    watcher = object.__new__(FileWatcher)
    watcher._tools = [tool]
    requested: list[str] = []
    watcher.request_full_resync = requested.append

    assert watcher.request_relative_resync("codex", "sessions/thread.jsonl") is True
    assert requested == [str(transcript.resolve())]
    assert watcher.request_relative_resync("codex", "../outside.jsonl") is False
    assert watcher.request_relative_resync("unknown", "sessions/thread.jsonl") is False


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


def test_busy_path_does_not_starve_quiet_path(tmp_path: Path) -> None:
    """A busy sibling transcript must not hide an unanswered user prompt."""
    quiet = tmp_path / "waiting-for-user.jsonl"
    busy = tmp_path / "streaming-agent.jsonl"
    quiet.write_text("question\n", encoding="utf-8")
    busy.write_text("tools\n", encoding="utf-8")
    callbacks: list[Path] = []
    quiet_delivered = threading.Event()
    busy_delivered = threading.Event()

    def callback(path: Path) -> None:
        callbacks.append(path)
        if path == quiet:
            quiet_delivered.set()
        elif path == busy:
            busy_delivered.set()

    handler = _DebouncedHandler(callback, 0.08, [])
    handler.on_any_event(_modified(quiet))

    def keep_busy() -> None:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            handler.on_any_event(_modified(busy))
            time.sleep(0.02)

    busy_thread = threading.Thread(target=keep_busy)
    busy_thread.start()
    try:
        assert quiet_delivered.wait(0.3)
        assert busy_thread.is_alive()
        assert callbacks == [quiet]
        busy_thread.join(timeout=2)
        assert not busy_thread.is_alive()
        assert busy_delivered.wait(1)
    finally:
        handler.stop()
        busy_thread.join(timeout=2)

    assert callbacks == [quiet, busy]


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


def test_file_processing_enqueues_source_filesystem_mtime(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"role":"user","message":{"content":"hello"}}\n', encoding="utf-8")
    expected_mtime = 1_700_000_123.5
    os.utime(path, (expected_mtime, expected_mtime))

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return None, 0

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)
    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = []

    watcher._process_file_changed(path)

    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["source_modified_at"] == expected_mtime


def test_restarted_scan_skips_durable_unchanged_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        '{"role":"user","message":{"content":"hello"}}\n',
        encoding="utf-8",
    )
    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)
    database = tmp_path / "queue" / "sync.db"

    queue = SyncQueue(database)
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._process_file_changed(path, emit_live_signals=False)
    queue.close()

    queue = SyncQueue(database)
    watcher._queue = queue
    monkeypatch.setattr(
        JsonlParser,
        "parse_to_writer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durably unchanged source was reparsed")
        ),
    )
    try:
        watcher._process_file_changed(path, emit_live_signals=False)
    finally:
        queue.close()


def _legacy_full_watcher(
    tmp_path: Path,
    path: Path,
    *,
    legacy_hash: str,
    synced: bool = True,
) -> tuple[SyncQueue, FileWatcher]:
    database = tmp_path / "queue" / "sync.db"
    created = SyncQueue(database)
    created.close()
    with closing(sqlite3.connect(database)) as connection:
        # Exact v7 shape: the old database had neither the identity epoch nor
        # terminal payload-retention marker.
        connection.execute("ALTER TABLE file_state DROP COLUMN identity_version")
        connection.execute("ALTER TABLE queue DROP COLUMN payload_discarded_at")
        connection.execute("PRAGMA user_version=7")
        connection.execute(
            """INSERT INTO file_state (
                   tool_name, relative_path, last_hash, last_offset,
                   last_synced_at, observed_hash, observed_offset, observed_at,
                   synced_hash, synced_offset, synced_at,
                   source_size, source_mtime_ns
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "codex",
                "archived_sessions/rollout-2026-07-19.jsonl",
                legacy_hash,
                path.stat().st_size,
                100.0,
                legacy_hash,
                path.stat().st_size,
                100.0,
                legacy_hash if synced else None,
                path.stat().st_size if synced else 0,
                100.0 if synced else None,
                None,
                None,
            ),
        )
        connection.commit()

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="archived_sessions/rollout-2026-07-19.jsonl",
    )
    queue = SyncQueue(database, spool_threshold=64 * 1024)
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(path.parent): SimpleNamespace(
            classify_file=lambda _path: classification,
        )
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    return queue, watcher


def test_legacy_archived_full_adopts_only_acknowledged_unchanged_source(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archived_sessions"
    archive_dir.mkdir()
    path = archive_dir / "rollout-2026-07-19.jsonl"
    path.write_text(
        json.dumps({"message": "x" * 70_000}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    stat = path.stat()
    legacy_hash = _legacy_full_hash_revision(
        path,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    queue, watcher = _legacy_full_watcher(
        tmp_path,
        path,
        legacy_hash=legacy_hash,
    )

    try:
        canonical_content = sanitize_jsonl(
            path.read_text(encoding="utf-8")
        ).content
        canonical_hash = hashlib.sha256(
            canonical_content.encode("utf-8")
        ).hexdigest()
        queue.enqueue(
            tool_name="codex",
            category="conversation",
            content_type="jsonl",
            relative_path="archived_sessions/rollout-2026-07-19.jsonl",
            content=canonical_content,
            content_hash=canonical_hash,
            file_size=len(canonical_content.encode("utf-8")),
            sync_strategy="full",
            offset=path.stat().st_size,
            source_path=str(path),
            source_size=path.stat().st_size,
            source_mtime_ns=path.stat().st_mtime_ns,
        )
        assert queue.pending_count() == 1
        assert len(list((tmp_path / "queue" / "spool").glob("*.payload"))) == 1

        watcher._process_file_changed(path, emit_live_signals=False)

        assert queue.pending_count() == 0
        assert queue.get_file_state(
            "codex",
            "archived_sessions/rollout-2026-07-19.jsonl",
        ) == (canonical_hash, path.stat().st_size)
        assert queue.get_source_revision(
            "codex",
            "archived_sessions/rollout-2026-07-19.jsonl",
            identity_version=FULL_IDENTITY_VERSION,
        ) == (path.stat().st_size, path.stat().st_mtime_ns)
        assert list((tmp_path / "queue" / "spool").glob("*.payload")) == []
        with queue._lock:
            assert queue._conn.execute(
                "SELECT status FROM queue ORDER BY id DESC LIMIT 1"
            ).fetchone() == ("superseded",)
    finally:
        queue.close()


def test_changed_or_unacknowledged_legacy_full_is_not_adopted(
    tmp_path: Path,
) -> None:
    for synced, changed in ((True, True), (False, False)):
        case = tmp_path / f"{synced}-{changed}"
        archive_dir = case / "archived_sessions"
        archive_dir.mkdir(parents=True)
        path = archive_dir / "rollout-2026-07-19.jsonl"
        path.write_text('{"message":"original"}\n', encoding="utf-8")
        stat = path.stat()
        legacy_hash = _legacy_full_hash_revision(
            path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        queue, watcher = _legacy_full_watcher(
            case,
            path,
            legacy_hash=legacy_hash,
            synced=synced,
        )
        try:
            if changed:
                path.write_text('{"message":"changed"}\n', encoding="utf-8")
                changed_at = path.stat().st_mtime + 10
                os.utime(path, (changed_at, changed_at))
            watcher._process_file_changed(path, emit_live_signals=False)

            item = queue.claim_batch()[0]
            assert item.content_hash != legacy_hash
            assert queue.read_payload_text(item) == sanitize_jsonl(
                path.read_text(encoding="utf-8")
            ).content
        finally:
            queue.close()


def test_unacknowledged_interrupted_full_row_recovers_after_migration(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archived_sessions"
    archive_dir.mkdir()
    path = archive_dir / "rollout-2026-07-19.jsonl"
    path.write_text('{"message":"not-yet-acknowledged"}\n', encoding="utf-8")
    stat = path.stat()
    legacy_hash = _legacy_full_hash_revision(
        path,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    queue, watcher = _legacy_full_watcher(
        tmp_path,
        path,
        legacy_hash=legacy_hash,
        synced=False,
    )
    canonical = sanitize_jsonl(path.read_text(encoding="utf-8")).content
    canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    item_id = queue.enqueue(
        tool_name="codex",
        category="conversation",
        content_type="jsonl",
        relative_path="archived_sessions/rollout-2026-07-19.jsonl",
        content=canonical,
        content_hash=canonical_hash,
        file_size=len(canonical.encode("utf-8")),
        sync_strategy="full",
        offset=stat.st_size,
        source_path=str(path),
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
    )
    interrupted = queue.claim_batch(lease_seconds=0)[0]
    assert interrupted.id == item_id
    queue.close()

    queue = SyncQueue(tmp_path / "queue" / "sync.db")
    watcher._queue = queue
    try:
        watcher._process_file_changed(path, emit_live_signals=False)

        recovered = queue.claim_batch()[0]
        assert recovered.id == item_id
        assert recovered.content_hash == canonical_hash
        assert queue.read_payload_text(recovered) == canonical
    finally:
        queue.close()


def test_pruned_terminal_payload_rebuilds_from_exact_source(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archived_sessions"
    archive_dir.mkdir()
    path = archive_dir / "large.jsonl"
    path.write_text(
        json.dumps({"message": "x" * 70_000}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    stat = path.stat()
    queue = SyncQueue(
        tmp_path / "queue" / "sync.db",
        spool_threshold=64 * 1024,
        terminal_spool_max_bytes=1,
    )
    item_id = queue.enqueue(
        tool_name="codex",
        category="conversation",
        content_type="jsonl",
        relative_path="archived_sessions/large.jsonl",
        content=sanitize_jsonl(path.read_text(encoding="utf-8")).content,
        content_hash="terminal-hash",
        file_size=70_000,
        sync_strategy="full",
        offset=stat.st_size,
        source_path=str(path),
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        identity_version=FULL_IDENTITY_VERSION,
    )
    terminal = queue.claim_batch(max_bytes=100_000)[0]
    assert queue.mark_upload_outcome(
        terminal,
        UploadOutcome.quarantine(
            "unsupported payload",
            diagnostic_code="unsupported_payload",
        ),
    )
    rebuild = queue.terminal_rebuild_source(item_id)
    assert rebuild is not None

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="archived_sessions/large.jsonl",
    )
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(archive_dir): SimpleNamespace(
            classify_file=lambda _path: classification,
        )
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._stop_event = threading.Event()
    watcher._processing_lock = threading.Lock()
    try:
        source_path, tool_name, relative_path = rebuild
        assert watcher.rebuild_terminal_source(
            source_path,
            tool_name=tool_name,
            relative_path=relative_path,
        )

        rebuilt = queue.claim_batch(max_bytes=100_000)[0]
        assert rebuilt.id == item_id
        assert rebuilt.metadata["_queue_force_reprocess_nonce"]
        assert queue.read_payload_text(rebuilt) == sanitize_jsonl(
            path.read_text(encoding="utf-8")
        ).content
    finally:
        queue.close()


def test_full_identity_epoch_rechecks_sanitizer_visible_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"message":"visible"}\n', encoding="utf-8")
    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    queue = SyncQueue(tmp_path / "queue" / "sync.db")
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(tmp_path): SimpleNamespace(classify_file=lambda _path: classification)
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    try:
        watcher._process_file_changed(path, emit_live_signals=False)
        assert queue.mark_synced(queue.claim_batch()[0])
        with queue._lock:
            queue._conn.execute(
                """UPDATE file_state
                   SET last_hash=?, observed_hash=?, synced_hash=?,
                       identity_version='sanitized-payload-v0'
                   WHERE tool_name='cursor' AND relative_path='projects/session.jsonl'""",
                ("f" * 64, "f" * 64, "f" * 64),
            )
            queue._conn.commit()

        watcher._process_file_changed(path, emit_live_signals=False)

        repaired = queue.claim_batch()[0]
        assert repaired.content_hash != "f" * 64
        assert queue.read_payload_text(repaired) == '{"message":"visible"}'
    finally:
        queue.close()


def test_unchanged_touch_updates_observation_without_requeue(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"message":"unchanged"}\n', encoding="utf-8")
    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    queue = SyncQueue(tmp_path / "queue" / "sync.db")
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(tmp_path): SimpleNamespace(classify_file=lambda _path: classification)
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]

    try:
        watcher._process_file_changed(path, emit_live_signals=False)
        original = queue.claim_batch()[0]
        assert queue.mark_synced(original)

        touched_at = path.stat().st_mtime + 10
        os.utime(path, (touched_at, touched_at))
        watcher._process_file_changed(path, emit_live_signals=False)

        assert queue.pending_count() == 0
        assert queue.get_file_state("cursor", "projects/session.jsonl")[0] == (
            original.content_hash
        )
        assert queue.get_source_revision(
            "cursor", "projects/session.jsonl"
        ) == (path.stat().st_size, path.stat().st_mtime_ns)
        with queue._lock:
            assert queue._conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 1
    finally:
        queue.close()


def test_same_size_same_prefix_tail_change_gets_new_canonical_hash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    prefix = "x" * (256 * 1024 + 32)
    first = json.dumps({"prefix": prefix, "tail": "AAAA"}, separators=(",", ":"))
    second = json.dumps({"prefix": prefix, "tail": "BBBB"}, separators=(",", ":"))
    assert len(first) == len(second)
    path.write_text(first + "\n", encoding="utf-8")
    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    queue = SyncQueue(tmp_path / "queue" / "sync.db", spool_threshold=64 * 1024)
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(tmp_path): SimpleNamespace(classify_file=lambda _path: classification)
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]

    try:
        watcher._process_file_changed(path, emit_live_signals=False)
        original = queue.claim_batch(max_bytes=1024 * 1024)[0]
        assert queue.mark_synced(original)

        path.write_text(second + "\n", encoding="utf-8")
        changed_at = path.stat().st_mtime + 10
        os.utime(path, (changed_at, changed_at))
        watcher._process_file_changed(path, emit_live_signals=False)

        changed = queue.claim_batch(max_bytes=1024 * 1024)[0]
        assert changed.content_hash != original.content_hash
        assert '"tail":"BBBB"' in queue.read_payload_text(changed)
    finally:
        queue.close()


def test_redacted_secret_changes_keep_sanitized_identity(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    first_key = "sk-" + ("a" * 32)
    second_key = "sk-" + ("b" * 32)
    path.write_text(json.dumps({"message": first_key}) + "\n", encoding="utf-8")
    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    queue = SyncQueue(tmp_path / "queue" / "sync.db")
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(tmp_path): SimpleNamespace(classify_file=lambda _path: classification)
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]

    try:
        watcher._process_file_changed(path, emit_live_signals=False)
        original = queue.claim_batch()[0]
        sanitized = queue.read_payload_text(original)
        assert first_key not in sanitized
        assert "[API_KEY_REDACTED]" in sanitized
        assert queue.mark_synced(original)

        path.write_text(json.dumps({"message": second_key}) + "\n", encoding="utf-8")
        changed_at = path.stat().st_mtime + 10
        os.utime(path, (changed_at, changed_at))
        watcher._process_file_changed(path, emit_live_signals=False)

        assert queue.pending_count() == 0
        assert queue.get_file_state("cursor", "projects/session.jsonl")[0] == (
            original.content_hash
        )
    finally:
        queue.close()


def test_delta_processing_uses_guarded_base_and_force_full_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    first = '{"role":"user","message":{"content":"first"}}\n'
    second = '{"role":"assistant","message":{"content":"second"}}\n'
    path.write_text(first + second, encoding="utf-8")
    base_offset = len(first.encode("utf-8"))

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return "observed-hash", path.stat().st_size

        def get_delta_base(self, _tool_name: str, _relative_path: str):
            return "base-hash", base_offset

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)
    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._config = SimpleNamespace(max_delta_upload_bytes=16 * 1024 * 1024)

    watcher._process_file_changed(path)
    watcher._process_file_changed(path, force_full=True)

    incremental, complete = queue.enqueued
    assert "second" in incremental["content"]
    assert "first" not in incremental["content"]
    assert incremental["is_partial"] is True
    assert incremental["base_hash"] == "base-hash"
    assert incremental["base_offset"] == base_offset
    assert incremental["source_path"] == str(path)
    assert "_queue_force_reprocess_nonce" not in incremental["metadata"]
    assert "first" in complete["content"]
    assert "second" in complete["content"]
    assert complete["is_partial"] is False
    assert complete["base_hash"] is None
    assert complete["base_offset"] == 0
    repair_nonce = complete["metadata"].get("_queue_force_reprocess_nonce")
    assert isinstance(repair_nonce, str)
    assert len(repair_nonce) == 32


def test_delta_processing_rebases_when_reproducible_prefix_changed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    first = '{"role":"user","message":{"content":"first"}}\n'
    replacement = '{"role":"user","message":{"content":"other"}}\n'
    second = '{"role":"assistant","message":{"content":"second"}}\n'
    path.write_text(first + second, encoding="utf-8")
    base_offset = len(first.encode("utf-8"))
    committed_hash = _delta_hash_revision(path, size=base_offset)
    path.write_text(replacement + second, encoding="utf-8")

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return committed_hash, base_offset

        def get_delta_base(self, _tool_name: str, _relative_path: str):
            return committed_hash, base_offset

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/thread.jsonl",
        metadata={},
    )
    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(tmp_path): SimpleNamespace(classify_file=lambda _path: classification)
    }
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._config = SimpleNamespace(max_delta_upload_bytes=16 * 1024 * 1024)

    watcher._process_file_changed(path)

    rebased = queue.enqueued[0]
    assert rebased["is_partial"] is False
    assert rebased["base_hash"] is None
    assert rebased["base_offset"] == 0
    assert "other" in rebased["content"]
    assert "second" in rebased["content"]
    assert len(rebased["metadata"]["_queue_force_reprocess_nonce"]) == 32


def test_force_full_captures_append_only_prefix_then_queues_guarded_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active-session.jsonl"
    first = '{"type":"event_msg","payload":{"type":"user_message","message":"first"}}\n'
    second = (
        '{"type":"event_msg","payload":{"type":"user_message","message":"second"}}\n'
    )
    path.write_text(first, encoding="utf-8")
    prefix_size = path.stat().st_size

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/active-session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)
    queue = SyncQueue(tmp_path / "queue" / "sync.db")

    class AppendingParser(JsonlParser):
        def parse(self, changed_path, offset=0, *, end_offset=None):
            result = super().parse(
                changed_path,
                offset=offset,
                end_offset=end_offset,
            )
            with changed_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(second)
            return result

    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [AppendingParser()]
    watcher._config = SimpleNamespace(max_delta_upload_bytes=16 * 1024 * 1024)

    try:
        watcher._process_file_changed(path, force_full=True)
        complete = queue.claim_batch(max_bytes=1024 * 1024)[0]
        assert complete.is_partial is False
        assert complete.offset == prefix_size
        assert "first" in queue.read_payload_text(complete)
        assert "second" not in queue.read_payload_text(complete)

        # The appended second record must not become a speculative tail while
        # the authoritative base is merely leased. Its receipt callback will
        # capture the tail after the base is committed.
        watcher._parsers = [JsonlParser()]
        watcher._process_file_changed(path)
        assert queue.pending_count() == 1
        assert queue.mark_synced(complete)

        watcher._process_file_changed(path)
        tail = queue.claim_batch(max_bytes=1024 * 1024)[0]
        assert tail.is_partial is True
        assert tail.base_hash == complete.content_hash
        assert tail.base_offset == prefix_size
        assert "second" in queue.read_payload_text(tail)
        assert "first" not in queue.read_payload_text(tail)
    finally:
        queue.close()


def test_large_jsonl_initial_sync_and_backlog_use_bounded_windows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large-session.jsonl"
    record = json.dumps(
        {
            "type": "event_msg",
            "payload": {"text": "x" * 1000},
        },
        separators=(",", ":"),
    )
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for _ in range(18_000):
            stream.write(record + "\n")
    base_source_size = path.stat().st_size
    assert base_source_size > 16 * 1024 * 1024

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/large-session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)
    queue = SyncQueue(tmp_path / "queue" / "sync.db", spool_threshold=64 * 1024)
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._config = SimpleNamespace(max_delta_upload_bytes=16 * 1024 * 1024)

    try:
        watcher._process_file_changed(path)
        complete = queue.claim_batch(max_bytes=32 * 1024 * 1024)[0]
        assert complete.is_partial is False
        assert complete.payload_bytes <= 17 * 1024 * 1024
        assert 16 * 1024 * 1024 <= complete.offset < base_source_size
        first_window_offset = complete.offset
        assert queue.mark_synced(complete)

        watcher._process_file_changed(path)
        tail = queue.claim_batch(max_bytes=32 * 1024 * 1024)[0]
        assert tail.is_partial is True
        assert tail.base_hash == complete.content_hash
        assert tail.base_offset == first_window_offset
        assert tail.offset == base_source_size
        assert tail.payload_bytes < 4 * 1024 * 1024
    finally:
        queue.close()


def test_large_force_full_repair_bootstraps_a_bounded_base(tmp_path: Path) -> None:
    path = tmp_path / "large-repair.jsonl"
    record = (
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"text": "x" * 1000},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    path.write_text(record * 40, encoding="utf-8")
    max_delta_bytes = len(record.encode("utf-8")) * 8 - 10

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/large-repair.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return None, 0

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._config = SimpleNamespace(max_delta_upload_bytes=max_delta_bytes)

    watcher._process_file_changed(path, force_full=True)

    base = queue.enqueued[0]
    assert base["is_partial"] is False
    assert base["base_hash"] is None
    assert base["base_offset"] == 0
    assert len(base["content"].splitlines()) == 8
    assert base["offset"] < path.stat().st_size
    assert len(base["metadata"]["_queue_force_reprocess_nonce"]) == 32


def test_large_delta_backlog_is_captured_in_bounded_windows(tmp_path: Path) -> None:
    path = tmp_path / "active-large-session.jsonl"
    first = json.dumps({"type": "event_msg", "payload": {"text": "first"}}) + "\n"
    record = (
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"text": "x" * 1000},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    path.write_text(first + (record * 40), encoding="utf-8")
    base_offset = len(first.encode("utf-8"))
    max_delta_bytes = len(record.encode("utf-8")) * 8 - 10

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/active-large-session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return "observed-hash", base_offset

        def get_delta_base(self, _tool_name: str, _relative_path: str):
            return "base-hash", base_offset

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [JsonlParser()]
    watcher._config = SimpleNamespace(max_delta_upload_bytes=max_delta_bytes)

    watcher._process_file_changed(path)
    tail = queue.enqueued[0]
    assert tail["is_partial"] is True
    assert tail["base_hash"] == "base-hash"
    assert tail["base_offset"] == base_offset
    assert tail["offset"] > base_offset + max_delta_bytes
    assert tail["offset"] <= base_offset + max_delta_bytes + len(record.encode("utf-8"))
    assert tail["offset"] < path.stat().st_size
    assert len(tail["content"].splitlines()) == 8
    assert len(tail["content"].encode("utf-8")) < path.stat().st_size - base_offset


def test_bounded_delta_accepts_append_after_captured_window(tmp_path: Path) -> None:
    path = tmp_path / "actively-growing-session.jsonl"
    first = json.dumps({"type": "event_msg", "payload": {"text": "first"}}) + "\n"
    record = json.dumps({"type": "event_msg", "payload": {"text": "x" * 1000}}) + "\n"
    appended = json.dumps({"type": "event_msg", "payload": {"text": "later"}}) + "\n"
    path.write_text(first + (record * 32), encoding="utf-8")
    base_offset = len(first.encode("utf-8"))

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return "observed-hash", base_offset

        def get_delta_base(self, _tool_name: str, _relative_path: str):
            return "base-hash", base_offset

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    class AppendingParser(JsonlParser):
        def parse(
            self, changed_path: Path, offset: int = 0, end_offset: int | None = None
        ):
            result = super().parse(changed_path, offset=offset, end_offset=end_offset)
            with changed_path.open("a", encoding="utf-8") as stream:
                stream.write(appended)
            return result

    classification = FileClassification(
        tool_name="codex",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.DELTA,
        relative_path="sessions/actively-growing-session.jsonl",
    )
    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {
        str(tmp_path): SimpleNamespace(classify_file=lambda _path: classification)
    }
    watcher._queue = queue
    watcher._parsers = [AppendingParser()]
    watcher._config = SimpleNamespace(
        max_delta_upload_bytes=len(record.encode("utf-8")) * 8
    )

    watcher._process_file_changed(path)

    assert len(queue.enqueued) == 1
    tail = queue.enqueued[0]
    assert tail["is_partial"] is True
    assert tail["base_hash"] == "base-hash"
    assert tail["base_offset"] == base_offset
    assert tail["offset"] < path.stat().st_size
    assert "later" not in tail["content"]


def test_mutation_during_read_defers_without_advancing_source_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    original = '{"role":"user","message":{"content":"old"}}\n'
    replacement = '{"role":"user","message":{"content":"new"}}\n'
    path.write_text(original, encoding="utf-8")
    original_mtime = 1_700_000_123.5
    replacement_mtime = 1_700_000_456.75
    os.utime(path, (original_mtime, original_mtime))

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued: list[dict] = []

        def get_file_state(self, _tool_name: str, _relative_path: str):
            return None, 0

        def enqueue(self, **kwargs) -> int:
            self.enqueued.append(kwargs)
            return 1

    class MutatingParser:
        def can_parse(self, _path: Path) -> bool:
            return True

        def parse(self, changed_path: Path, offset: int = 0) -> ParseResult:
            del offset
            content = changed_path.read_text(encoding="utf-8")
            changed_path.write_text(replacement, encoding="utf-8")
            os.utime(changed_path, (replacement_mtime, replacement_mtime))
            return ParseResult(content=content, offset=len(content))

    classification = FileClassification(
        tool_name="cursor",
        category=Category.CONVERSATION,
        content_type=ContentType.JSONL,
        sync_strategy=SyncStrategy.FULL,
        relative_path="projects/session.jsonl",
    )
    tool = SimpleNamespace(classify_file=lambda _path: classification)
    queue = RecordingQueue()
    watcher = object.__new__(FileWatcher)
    watcher._tool_map = {str(tmp_path): tool}
    watcher._queue = queue
    watcher._parsers = [MutatingParser()]

    watcher._process_file_changed(path)

    assert queue.enqueued == []

    # The unstable read did not advance file_state; a later event/scan can
    # process the complete replacement revision normally.
    watcher._parsers = []
    watcher._process_file_changed(path)

    assert len(queue.enqueued) == 1
    assert json.loads(queue.enqueued[0]["content"])["message"]["content"] == "new"
    assert queue.enqueued[0]["source_modified_at"] == replacement_mtime
