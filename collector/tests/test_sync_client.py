from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.queue import SOURCE_REPAIR_MAX_ATTEMPTS, QueueItem  # noqa: E402
from collector.outcomes import (  # noqa: E402
    SourceRepairAction,
    UploadOutcome,
    UploadOutcomeState,
)
from collector.sync_client import (  # noqa: E402
    CHUNK_RETRY_BASE_SECONDS,
    CHUNK_UPLOAD_MAX_ATTEMPTS,
    CHUNK_SIZE,
    MAX_CHUNKED_UPLOAD_BYTES,
    DeltaBaseConflict,
    SyncClient,
    _classify_http_response,
)


class _BoundedStream:
    def __init__(self, size: int) -> None:
        self.remaining = size
        self.largest_read = 0

    def read(self, size: int) -> bytes:
        self.largest_read = max(self.largest_read, size)
        amount = min(size, self.remaining)
        self.remaining -= amount
        return b"x" * amount


class _FakeQueue:
    def __init__(self, size: int) -> None:
        self.stream = _BoundedStream(size)
        self.renewals = 0
        self.delta_conflicts: list[QueueItem] = []
        self.delta_conflict_bases: list[tuple[str | None, int]] = []
        self.outcomes: list[UploadOutcome] = []

    @contextmanager
    def open_payload(self, _item: QueueItem):
        yield self.stream

    def renew_lease(self, _item: QueueItem, lease_seconds: int) -> bool:
        self.renewals += 1
        return lease_seconds == 300

    def read_payload_text(self, _item: QueueItem) -> str:
        return "payload"

    def mark_delta_conflict(
        self,
        item: QueueItem,
        *,
        expected_hash: str | None = None,
        expected_offset: int = 0,
    ) -> bool:
        self.delta_conflicts.append(item)
        self.delta_conflict_bases.append((expected_hash, expected_offset))
        return True

    def mark_upload_outcome(
        self,
        _item: QueueItem,
        outcome: UploadOutcome,
        **_kwargs,
    ) -> bool:
        self.outcomes.append(outcome)
        return True


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = "response"
        self.payload = payload or {}

    def json(self) -> dict:
        return self.payload


class _FakeHttpClient:
    def __init__(self) -> None:
        self.chunk_sizes: list[int] = []

    def post(self, _path: str, data: dict, files: dict) -> _Response:
        del data
        self.chunk_sizes.append(len(files["content"][1]))
        return _Response()


class _MetadataHttpClient:
    def __init__(self, status_code: int = 200) -> None:
        self.path = ""
        self.payload: dict = {}
        self.status_code = status_code

    def post(self, path: str, json: dict) -> _Response:
        self.path = path
        self.payload = json
        return _Response(self.status_code)


class _ScriptedHttpClient:
    def __init__(self, outcomes: list[_Response | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, path: str, data: dict, files: dict) -> _Response:
        if not self.outcomes:
            raise AssertionError("unexpected HTTP call")
        filename, content, content_type = files["content"]
        self.calls.append(
            {
                "path": path,
                "metadata_text": data["metadata"],
                "metadata": json.loads(data["metadata"]),
                "filename": filename,
                "content": content,
                "content_type": content_type,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _CommitHttpClient:
    def __init__(self, statuses: list[dict]) -> None:
        self.statuses = list(statuses)
        self.chunk_calls = 0
        self.status_calls = 0
        self.job_id = "a" * 64

    def post(self, path: str, *, data=None, files=None, json=None) -> _Response:
        if path == "/api/ingest/file/chunk":
            del data, files, json
            self.chunk_calls += 1
            return _Response(
                payload={"document_id": f"queued:{self.job_id}"},
            )
        if path == "/api/ingest/file/chunk/status":
            del data, files
            self.status_calls += 1
            if not self.statuses:
                raise AssertionError("unexpected status poll")
            payload = self.statuses.pop(0)
            self.last_status_request = json
            return _Response(payload={"job_id": self.job_id, **payload})
        raise AssertionError(f"unexpected path {path}")


class _ConcurrentQueue:
    def __init__(self, items: list[QueueItem]) -> None:
        self.items = list(items)
        self.synced: list[int] = []
        self.failed: list[int] = []
        self.claim_calls = 0

    def claim_batch(self, **_kwargs) -> list[QueueItem]:
        self.claim_calls += 1
        return [self.items.pop(0)] if self.items else []

    def mark_synced(self, item: QueueItem) -> bool:
        self.synced.append(item.id)
        return True

    def mark_failed(self, item: QueueItem, _error: str) -> None:
        self.failed.append(item.id)

    def cleanup_synced(self) -> None:
        return None


class _SignaledQueue(_ConcurrentQueue):
    def __init__(self) -> None:
        super().__init__([])
        self._token = 0
        self._condition = threading.Condition()
        self.waiting = threading.Event()

    def change_token(self) -> int:
        with self._condition:
            return self._token

    def wait_for_change(self, token: int, timeout: float | None = None) -> int:
        with self._condition:
            self.waiting.set()
            if self._token == token:
                self._condition.wait(timeout)
            return self._token

    def next_deferred_delay(self) -> None:
        return None

    def add(self, item: QueueItem) -> None:
        with self._condition:
            self.items.append(item)
            self._token += 1
            self._condition.notify_all()

    def mark_synced(self, item: QueueItem) -> bool:
        result = super().mark_synced(item)
        with self._condition:
            self._token += 1
            self._condition.notify_all()
        return result

    def wake_waiters(self) -> None:
        with self._condition:
            self._token += 1
            self._condition.notify_all()


class _AcceptedReceiptQueue(_SignaledQueue):
    def __init__(self, item: QueueItem) -> None:
        super().__init__()
        self.item = item
        self.accepted = True

    def accepted_receipt_items(self) -> list[QueueItem]:
        return [self.item] if self.accepted else []

    def is_accepted_receipt(self, item: QueueItem) -> bool:
        return self.accepted and item.id == self.item.id

    def mark_synced(self, item: QueueItem) -> bool:
        self.accepted = False
        return super().mark_synced(item)


class SyncClientStreamingTests(unittest.TestCase):
    @staticmethod
    def _item(
        total_size: int,
        source_modified_at: float | None = None,
    ) -> QueueItem:
        return QueueItem(
            id=1,
            tool_name="codex",
            category="conversation",
            content_type="jsonl",
            relative_path="thread.jsonl",
            content=None,
            content_hash="hash",
            file_size=total_size,
            sync_strategy="full",
            is_partial=False,
            offset=0,
            metadata={},
            created_at=1.0,
            source_modified_at=source_modified_at,
            payload_bytes=total_size,
            lease_token="lease",
        )

    @staticmethod
    def _payload() -> dict:
        return {
            "tool": "codex",
            "relative_path": "thread.jsonl",
            "hash": "hash",
        }

    @staticmethod
    def _client(queue: _FakeQueue, http_client) -> SyncClient:
        client = object.__new__(SyncClient)
        client._queue = queue
        client._config = SimpleNamespace(
            queue_lease_seconds=300,
            large_file_threshold=64 * 1024,
        )
        client._running = True
        client._pause_requested = threading.Event()
        client._client = http_client
        client._full_resync_callback = None
        client._delta_catchup_callback = None
        return client

    def test_all_upload_routes_use_source_mtime_and_legacy_fallback(self) -> None:
        timestamp_cases = (
            (1_700_000_123.5, 1_700_000_123.5),
            (None, 1.0),
        )
        size_cases = (
            (1, "json"),
            (64 * 1024 + 1, "multipart"),
            (CHUNK_SIZE + 1, "chunked"),
        )
        for source_modified_at, expected in timestamp_cases:
            for size, expected_route in size_cases:
                queue = _FakeQueue(size)
                client = self._client(queue, _FakeHttpClient())
                payloads: list[tuple[str, dict]] = []
                client._upload_json = lambda payload: (
                    payloads.append(("json", payload)) or True
                )
                client._upload_multipart = lambda payload, _stream: (
                    payloads.append(("multipart", payload)) or True
                )
                client._upload_chunked = lambda payload, _item: (
                    payloads.append(("chunked", payload)) or True
                )

                with self.subTest(
                    source_modified_at=source_modified_at,
                    route=expected_route,
                ):
                    self.assertTrue(
                        client._upload(
                            self._item(size, source_modified_at=source_modified_at),
                        )
                    )
                    self.assertEqual(payloads[0][0], expected_route)
                    self.assertEqual(payloads[0][1]["timestamp"], expected)

    def test_scheduler_claims_live_work_while_large_upload_is_running(self) -> None:
        archive = self._item(150 * 1024 * 1024)
        archive.id = 1
        archive.relative_path = "archived/large.jsonl"
        live = self._item(100)
        live.id = 2
        live.relative_path = "sessions/active.jsonl"
        live.sync_strategy = "delta"
        live.is_partial = True
        queue = _ConcurrentQueue([archive, live])
        client = object.__new__(SyncClient)
        client._queue = queue
        client._config = SimpleNamespace(
            batch_size=20,
            max_concurrent_uploads=2,
            max_in_flight_bytes=64 * 1024 * 1024,
            max_delta_upload_bytes=16 * 1024 * 1024,
            queue_lease_seconds=300,
            sync_interval=0.01,
        )
        client._running = True
        client._pause_requested = threading.Event()
        client._idle = threading.Event()
        client._pool = ThreadPoolExecutor(max_workers=2)
        client._delta_catchup_callback = None
        archive_started = threading.Event()
        release_archive = threading.Event()
        live_started = threading.Event()

        def upload(item: QueueItem) -> bool:
            if item.id == archive.id:
                archive_started.set()
                return release_archive.wait(timeout=2)
            live_started.set()
            return True

        client._upload = upload
        worker = threading.Thread(target=client._run)
        worker.start()
        try:
            self.assertTrue(archive_started.wait(timeout=1))
            self.assertTrue(live_started.wait(timeout=1))
            self.assertFalse(release_archive.is_set())
        finally:
            release_archive.set()
            deadline = time.monotonic() + 1
            while len(queue.synced) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            client._running = False
            worker.join(timeout=2)
            client._pool.shutdown(wait=True)

        self.assertEqual(sorted(queue.synced), [1, 2])

    def test_idle_scheduler_waits_for_change_and_new_work_is_prompt(self) -> None:
        queue = _SignaledQueue()
        client = object.__new__(SyncClient)
        client._queue = queue
        client._config = SimpleNamespace(
            batch_size=20,
            max_concurrent_uploads=1,
            max_in_flight_bytes=64 * 1024 * 1024,
            max_delta_upload_bytes=16 * 1024 * 1024,
            queue_lease_seconds=300,
            sync_interval=0.01,
        )
        client._running = True
        client._pause_requested = threading.Event()
        client._idle = threading.Event()
        client._pool = ThreadPoolExecutor(max_workers=1)
        client._delta_catchup_callback = None
        client._upload_synced_callback = None
        uploaded = threading.Event()
        client._upload = lambda _item: uploaded.set() or True

        worker = threading.Thread(target=client._run)
        worker.start()
        try:
            self.assertTrue(queue.waiting.wait(timeout=1))
            time.sleep(0.05)
            self.assertEqual(queue.claim_calls, 1)

            queue.add(self._item(100))
            self.assertTrue(uploaded.wait(timeout=0.5))
        finally:
            client._running = False
            queue.wake_waiters()
            worker.join(timeout=2)
            client._pool.shutdown(wait=True)

        self.assertFalse(worker.is_alive())

    def test_resume_rescans_durable_accepted_receipts(self) -> None:
        item = self._item(100)
        item.sync_strategy = "delta"
        item.is_partial = True
        item.receipt_id = "a" * 64
        queue = _AcceptedReceiptQueue(item)
        client = object.__new__(SyncClient)
        client._queue = queue
        client._config = SimpleNamespace(
            batch_size=20,
            max_concurrent_uploads=1,
            max_in_flight_bytes=64 * 1024 * 1024,
            max_delta_upload_bytes=16 * 1024 * 1024,
            queue_lease_seconds=300,
            sync_interval=0.01,
        )
        client._running = True
        client._pause_requested = threading.Event()
        client._pause_requested.set()
        client._idle = threading.Event()
        client._pool = ThreadPoolExecutor(max_workers=1)
        client._receipt_pool = ThreadPoolExecutor(max_workers=1)
        client._delta_catchup_callback = None
        client._upload_synced_callback = None
        polled = threading.Event()
        client._wait_for_admission_commit = (
            lambda _item: polled.set() or UploadOutcome.success()
        )

        worker = threading.Thread(target=client._run)
        worker.start()
        try:
            self.assertTrue(client._idle.wait(timeout=1))
            self.assertFalse(polled.is_set())
            client.resume()
            self.assertTrue(polled.wait(timeout=1))
            deadline = time.monotonic() + 1
            while queue.accepted and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            client._running = False
            queue.wake_waiters()
            worker.join(timeout=2)
            client._pool.shutdown(wait=True)
            client._receipt_pool.shutdown(wait=True)

        self.assertFalse(worker.is_alive())
        self.assertFalse(queue.accepted)
        self.assertEqual(queue.synced, [item.id])

    def test_successful_delta_base_schedules_the_next_bounded_window(self) -> None:
        item = self._item(100)
        item.sync_strategy = "delta"
        item.is_partial = False
        item.source_path = "/tmp/thread.jsonl"
        queue = _ConcurrentQueue([])
        requested: list[str] = []
        client = object.__new__(SyncClient)
        client._queue = queue
        client._delta_catchup_callback = requested.append
        completed: Future[bool] = Future()
        completed.set_result(True)
        futures = {completed: item}

        client._reap_completed(futures)

        self.assertEqual(futures, {})
        self.assertEqual(queue.synced, [item.id])
        self.assertEqual(requested, [item.source_path])

    def test_legacy_oversized_delta_snapshot_is_rebuilt_in_windows(self) -> None:
        size = MAX_CHUNKED_UPLOAD_BYTES + 1
        queue = _FakeQueue(size)
        client = self._client(queue, _ScriptedHttpClient([]))
        client._full_resync_callback = lambda _path: None
        item = self._item(size)
        item.sync_strategy = "delta"
        item.source_path = "/tmp/thread.jsonl"

        outcome = client._upload(item)
        self.assertEqual(
            outcome.state,
            UploadOutcomeState.SOURCE_REPAIR_REQUIRED,
        )
        self.assertEqual(
            outcome.repair_action,
            SourceRepairAction.REBUILD_BOUNDED_DELTA,
        )
        self.assertEqual(queue.stream.largest_read, 0)

    def test_chunked_upload_reads_only_one_chunk_at_a_time(self) -> None:
        total_size = CHUNK_SIZE * 2 + 123
        queue = _FakeQueue(total_size)
        http_client = _FakeHttpClient()
        client = self._client(queue, http_client)

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))
        self.assertEqual(http_client.chunk_sizes, [CHUNK_SIZE, CHUNK_SIZE, 123])
        self.assertEqual(queue.stream.largest_read, CHUNK_SIZE)
        self.assertEqual(queue.renewals, 3)

    def test_repair_snapshot_gets_fresh_upload_id_without_leaking_queue_state(
        self,
    ) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient([_Response(), _Response()])
        client = self._client(queue, http_client)
        item = self._item(total_size)
        item.metadata = {
            "session_id": "thread",
            "_queue_force_reprocess_nonce": "repair-token",
        }

        self.assertTrue(client._upload(item))
        self.assertEqual(len(http_client.calls), 2)
        for call in http_client.calls:
            self.assertEqual(
                call["metadata"]["upload_id"],
                "codex/thread.jsonl/hash/repair-repair-token",
            )
            self.assertEqual(call["metadata"]["metadata"], {"session_id": "thread"})
            self.assertTrue(call["metadata"]["authoritative_rebase"])

    def test_chunked_upload_waits_for_database_commit_before_success(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _CommitHttpClient(
            [
                {"status": "pending"},
                {"status": "completed"},
            ]
        )
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))
        self.assertEqual(http_client.chunk_calls, 2)
        self.assertEqual(http_client.status_calls, 2)
        self.assertEqual(
            http_client.last_status_request,
            {
                "upload_id": "codex/thread.jsonl/hash",
                "hash": "hash",
            },
        )
        self.assertEqual(len(delays), 1)

    def test_failed_chunked_delta_becomes_a_resyncable_conflict(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _CommitHttpClient(
            [
                {"status": "failed", "error_type": "DeltaBaseMismatch"},
            ]
        )
        client = self._client(queue, http_client)
        payload = self._payload()
        payload["mode"] = "delta"

        with self.assertRaises(DeltaBaseConflict):
            client._upload_chunked(payload, self._item(total_size))

    def test_terminal_chunk_conflict_reads_server_committed_revision(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        client = self._client(
            queue,
            _ScriptedHttpClient(
                [
                    _Response(
                        409,
                        {
                            "detail": {
                                "code": "delta_base_mismatch",
                                "expected_hash": "d2:" + ("d" * 61),
                                "expected_offset": 456,
                            }
                        },
                    )
                ]
            ),
        )
        payload = self._payload()
        payload["mode"] = "delta"

        with self.assertRaises(DeltaBaseConflict) as raised:
            client._upload_chunked(payload, self._item(total_size))

        self.assertEqual(raised.exception.expected_hash, "d2:" + ("d" * 61))
        self.assertEqual(raised.exception.expected_offset, 456)

    def test_guarded_delta_uses_synchronous_multipart_and_sends_base(self) -> None:
        size = CHUNK_SIZE + 123
        queue = _FakeQueue(size)
        client = self._client(queue, _FakeHttpClient())
        routes: list[tuple[str, dict]] = []
        client._upload_multipart = lambda payload, _stream: (
            routes.append(("multipart", payload)) or True
        )
        client._upload_chunked = lambda payload, _item: (
            routes.append(("chunked", payload)) or True
        )
        item = self._item(size)
        item.sync_strategy = "delta"
        item.is_partial = True
        item.offset = 300
        item.base_hash = "base-hash"
        item.base_offset = 100

        self.assertTrue(client._upload(item))
        self.assertEqual(routes[0][0], "multipart")
        self.assertEqual(routes[0][1]["mode"], "delta")
        self.assertEqual(routes[0][1]["base_hash"], "base-hash")
        self.assertEqual(routes[0][1]["base_offset"], 100)

    def test_delta_base_conflict_retires_chain_and_requests_full_resync(self) -> None:
        queue = _FakeQueue(100)
        client = self._client(queue, _FakeHttpClient())
        requested: list[str] = []
        client._full_resync_callback = requested.append
        captured: list[dict] = []

        def reject(payload: dict) -> bool:
            captured.append(payload)
            raise DeltaBaseConflict(payload["relative_path"])

        client._upload_json = reject
        item = self._item(100)
        item.sync_strategy = "delta"
        item.is_partial = True
        item.offset = 200
        item.base_hash = "base-hash"
        item.base_offset = 100
        item.source_path = "/tmp/thread.jsonl"

        outcome = client._upload(item)
        self.assertEqual(
            outcome.repair_action,
            SourceRepairAction.DELTA_BASE_CONFLICT,
        )
        completed: Future[UploadOutcome] = Future()
        completed.set_result(outcome)
        client._reap_completed({completed: item})
        self.assertEqual(captured[0]["base_hash"], "base-hash")
        self.assertEqual(queue.delta_conflicts, [item])
        self.assertEqual(requested, ["/tmp/thread.jsonl"])

    def test_generic_source_repair_cap_quarantines_and_requests_full_resync(
        self,
    ) -> None:
        queue = _FakeQueue(100)
        client = self._client(queue, _FakeHttpClient())
        requested: list[str] = []
        client._full_resync_callback = requested.append
        item = self._item(100)
        item.sync_strategy = "delta"
        item.is_partial = True
        item.source_path = "/tmp/thread.jsonl"
        item.retry_count = SOURCE_REPAIR_MAX_ATTEMPTS - 1
        completed: Future[UploadOutcome] = Future()
        completed.set_result(
            UploadOutcome.source_repair(
                "HTTP 400 stale chunk metadata",
                diagnostic_code="http_400",
                http_status=400,
            )
        )

        client._reap_completed({completed: item})

        self.assertEqual(len(queue.outcomes), 1)
        self.assertEqual(
            queue.outcomes[0].state,
            UploadOutcomeState.PERMANENT_QUARANTINE,
        )
        self.assertEqual(
            queue.outcomes[0].diagnostic_code,
            "source_repair_attempt_cap",
        )
        self.assertEqual(requested, ["/tmp/thread.jsonl"])

    def test_delta_conflict_reads_server_committed_revision(self) -> None:
        response = _Response(
            409,
            {
                "detail": {
                    "code": "delta_base_mismatch",
                    "expected_hash": "d2:" + ("b" * 61),
                    "expected_offset": 125,
                }
            },
        )

        with self.assertRaises(DeltaBaseConflict) as raised:
            SyncClient._raise_delta_conflict(
                response,
                {"mode": "delta", "relative_path": "thread.jsonl"},
            )

        self.assertEqual(raised.exception.expected_hash, "d2:" + ("b" * 61))
        self.assertEqual(raised.exception.expected_offset, 125)

    def test_delta_base_conflict_resumes_reproducible_server_prefix(self) -> None:
        queue = _FakeQueue(100)
        client = self._client(queue, _FakeHttpClient())
        resumed: list[str] = []
        rebuilt: list[str] = []
        client._delta_catchup_callback = resumed.append
        client._full_resync_callback = rebuilt.append

        def reject(payload: dict) -> bool:
            raise DeltaBaseConflict(
                payload["relative_path"],
                expected_hash="d2:" + ("a" * 61),
                expected_offset=150,
            )

        client._upload_json = reject
        item = self._item(100)
        item.sync_strategy = "delta"
        item.is_partial = True
        item.offset = 200
        item.base_hash = "stale-base"
        item.base_offset = 100
        item.source_path = "/tmp/thread.jsonl"

        outcome = client._upload(item)
        completed: Future[UploadOutcome] = Future()
        completed.set_result(outcome)
        client._reap_completed({completed: item})
        self.assertEqual(
            queue.delta_conflict_bases,
            [("d2:" + ("a" * 61), 150)],
        )
        self.assertEqual(resumed, [item.source_path])
        self.assertEqual(rebuilt, [])

    def test_response_lost_retries_same_accepted_chunk_then_continues(self) -> None:
        total_size = CHUNK_SIZE + 123
        queue = _FakeQueue(total_size)
        response_lost = httpx.ReadError(
            "response lost after acceptance",
            request=httpx.Request("POST", "https://example.test/api/ingest/file/chunk"),
        )
        http_client = _ScriptedHttpClient(
            [
                response_lost,
                _Response(200),
                _Response(200),
            ]
        )
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))

        self.assertEqual(
            [call["metadata"]["chunk_index"] for call in http_client.calls],
            [0, 0, 1],
        )
        self.assertEqual(
            http_client.calls[0]["metadata_text"], http_client.calls[1]["metadata_text"]
        )
        self.assertEqual(
            http_client.calls[0]["content"], http_client.calls[1]["content"]
        )
        self.assertEqual(
            http_client.calls[0]["filename"], http_client.calls[1]["filename"]
        )
        self.assertEqual(delays, [CHUNK_RETRY_BASE_SECONDS])
        self.assertEqual(queue.renewals, 3)

    def test_transient_502_retries_current_chunk(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient(
            [
                _Response(502),
                _Response(200),
                _Response(200),
            ]
        )
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))
        self.assertEqual(
            [call["metadata"]["chunk_index"] for call in http_client.calls],
            [0, 0, 1],
        )
        self.assertEqual(delays, [CHUNK_RETRY_BASE_SECONDS])

    def test_cloudflare_edge_failures_retry_current_chunk(self) -> None:
        for status in (520, 523):
            total_size = CHUNK_SIZE + 1
            queue = _FakeQueue(total_size)
            http_client = _ScriptedHttpClient(
                [
                    _Response(status),
                    _Response(200),
                    _Response(200),
                ]
            )
            client = self._client(queue, http_client)
            delays: list[float] = []
            client._sleep_interruptibly = delays.append

            with self.subTest(status=status):
                self.assertTrue(
                    client._upload_chunked(
                        self._payload(),
                        self._item(total_size),
                    )
                )
                self.assertEqual(
                    [call["metadata"]["chunk_index"] for call in http_client.calls],
                    [0, 0, 1],
                )
                self.assertEqual(delays, [CHUNK_RETRY_BASE_SECONDS])

    def test_permanent_4xx_fails_without_retrying(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient([_Response(400)])
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertFalse(
            client._upload_chunked(self._payload(), self._item(total_size))
        )
        self.assertEqual(len(http_client.calls), 1)
        self.assertEqual(http_client.calls[0]["metadata"]["chunk_index"], 0)
        self.assertEqual(delays, [])
        self.assertEqual(queue.renewals, 1)

    def test_transient_failures_stop_after_bounded_attempts(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient(
            [_Response(502) for _ in range(CHUNK_UPLOAD_MAX_ATTEMPTS)],
        )
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertFalse(
            client._upload_chunked(self._payload(), self._item(total_size))
        )
        self.assertEqual(len(http_client.calls), CHUNK_UPLOAD_MAX_ATTEMPTS)
        self.assertEqual(
            [call["metadata"]["chunk_index"] for call in http_client.calls],
            [0] * CHUNK_UPLOAD_MAX_ATTEMPTS,
        )
        self.assertEqual(len(delays), CHUNK_UPLOAD_MAX_ATTEMPTS - 1)

    def test_pause_interrupts_chunk_retry_backoff(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient([_Response(502), _Response(200)])
        client = self._client(queue, http_client)

        def pause_during_backoff(_delay: float) -> None:
            client._pause_requested.set()

        client._sleep_interruptibly = pause_during_backoff

        self.assertFalse(
            client._upload_chunked(self._payload(), self._item(total_size))
        )
        self.assertEqual(len(http_client.calls), 1)

    def test_metadata_upload_is_lightweight_and_hides_queue_state(self) -> None:
        queue = _FakeQueue(0)
        http_client = _MetadataHttpClient()
        client = self._client(queue, http_client)
        item = self._item(0)
        item.sync_strategy = "metadata"
        item.metadata = {
            "metadata_type": "codex_thread_title",
            "tool": "codex",
            "thread_id": "019f144c-82d6-70d0-95e8-e01e7b813e98",
            "title": "Renamed",
            "revision": 200,
            "_queue_state_namespace": "codex_thread_titles",
            "_queue_state_key": "private-key",
            "_queue_state_value": "Renamed",
        }

        self.assertTrue(client._upload(item))
        self.assertEqual(http_client.path, "/api/ingest/metadata")
        self.assertEqual(http_client.payload["title"], "Renamed")
        self.assertFalse(any(key.startswith("_queue_") for key in http_client.payload))
        self.assertEqual(queue.stream.largest_read, 0)

    def test_missing_transcript_metadata_response_remains_retryable(self) -> None:
        queue = _FakeQueue(0)
        client = self._client(queue, _MetadataHttpClient(status_code=404))
        item = self._item(0)
        item.sync_strategy = "metadata"
        item.metadata = {
            "metadata_type": "codex_thread_title",
            "tool": "codex",
            "thread_id": "019f144c-82d6-70d0-95e8-e01e7b813e98",
            "title": "Rename before transcript arrives",
            "revision": 200,
        }

        self.assertFalse(client._upload(item))

    def test_every_upload_endpoint_uses_the_same_typed_status_policy(self) -> None:
        endpoints = (
            "/api/ingest/metadata",
            "/api/ingest/file",
            "/api/ingest/file/upload",
            "/api/ingest/file/chunk",
            "/api/ingest/file/chunk/status",
        )
        cases = {
            UploadOutcomeState.SUCCESS: (200, 201, 202, 204),
            UploadOutcomeState.TRANSIENT_RETRY: (
                408,
                425,
                429,
                500,
                502,
                503,
                504,
                520,
                521,
                522,
                523,
                524,
                525,
                526,
                527,
            ),
            UploadOutcomeState.AUTHENTICATION_BLOCKED: (401, 403),
            UploadOutcomeState.SOURCE_REPAIR_REQUIRED: (400, 413, 422),
            UploadOutcomeState.PERMANENT_QUARANTINE: (
                301,
                404,
                409,
                410,
                418,
                501,
                505,
            ),
        }
        for endpoint in endpoints:
            for expected, statuses in cases.items():
                for status in statuses:
                    with self.subTest(
                        endpoint=endpoint,
                        status=status,
                        expected=expected.value,
                    ):
                        outcome = _classify_http_response(
                            _Response(status),
                            endpoint=endpoint,
                        )
                        self.assertEqual(outcome.state, expected)
                        if status >= 300:
                            self.assertEqual(outcome.http_status, status)

        metadata_404 = _classify_http_response(
            _Response(404),
            endpoint="/api/ingest/metadata",
            retry_not_found=True,
        )
        self.assertEqual(
            metadata_404.state,
            UploadOutcomeState.TRANSIENT_RETRY,
        )

    def test_chunk_authentication_failure_is_not_retried(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient([_Response(401)])
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        outcome = client._upload_chunked(self._payload(), self._item(total_size))

        self.assertEqual(
            outcome.state,
            UploadOutcomeState.AUTHENTICATION_BLOCKED,
        )
        self.assertEqual(len(http_client.calls), 1)
        self.assertEqual(delays, [])

    def test_invalid_local_metadata_requires_source_repair(self) -> None:
        size = CHUNK_SIZE
        queue = _FakeQueue(size)
        client = self._client(queue, _ScriptedHttpClient([]))
        item = self._item(size)
        item.metadata = {"invalid": object()}

        outcome = client._upload(item)

        self.assertEqual(
            outcome.state,
            UploadOutcomeState.SOURCE_REPAIR_REQUIRED,
        )
        self.assertEqual(outcome.diagnostic_code, "invalid_metadata")

    def test_terminal_chunk_commit_states_are_typed(self) -> None:
        total_size = CHUNK_SIZE + 1
        cases = (
            (
                {"status": "failed", "error_type": "ValueError"},
                UploadOutcomeState.PERMANENT_QUARANTINE,
                "commit_failed",
            ),
            (
                {"status": "blocked"},
                UploadOutcomeState.PERMANENT_QUARANTINE,
                "commit_blocked",
            ),
            (
                {"status": "missing"},
                UploadOutcomeState.SOURCE_REPAIR_REQUIRED,
                "commit_missing",
            ),
        )
        for status_payload, expected_state, expected_code in cases:
            with self.subTest(status=status_payload["status"]):
                queue = _FakeQueue(total_size)
                client = self._client(
                    queue,
                    _CommitHttpClient([status_payload]),
                )
                outcome = client._upload_chunked(
                    self._payload(),
                    self._item(total_size),
                )
                self.assertEqual(outcome.state, expected_state)
                self.assertEqual(outcome.diagnostic_code, expected_code)


if __name__ == "__main__":
    unittest.main()
