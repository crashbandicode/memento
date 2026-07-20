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

from collector.queue import QueueItem  # noqa: E402
from collector.sync_client import (  # noqa: E402
    CHUNK_RETRY_BASE_SECONDS,
    CHUNK_UPLOAD_MAX_ATTEMPTS,
    CHUNK_SIZE,
    MAX_CHUNKED_UPLOAD_BYTES,
    DeltaBaseConflict,
    SyncClient,
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

    @contextmanager
    def open_payload(self, _item: QueueItem):
        yield self.stream

    def renew_lease(self, _item: QueueItem, lease_seconds: int) -> bool:
        self.renewals += 1
        return lease_seconds == 300

    def read_payload_text(self, _item: QueueItem) -> str:
        return "payload"

    def mark_delta_conflict(self, item: QueueItem) -> bool:
        self.delta_conflicts.append(item)
        return True


class _Response:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "response"


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
        self.calls.append({
            "path": path,
            "metadata_text": data["metadata"],
            "metadata": json.loads(data["metadata"]),
            "filename": filename,
            "content": content,
            "content_type": content_type,
        })
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


class SyncClientStreamingTests(unittest.TestCase):
    @staticmethod
    def _item(
        total_size: int,
        source_modified_at: float | None = None,
    ) -> QueueItem:
        return QueueItem(
            id=1, tool_name="codex", category="conversation",
            content_type="jsonl", relative_path="thread.jsonl", content=None,
            content_hash="hash", file_size=total_size, sync_strategy="full",
            is_partial=False, offset=0, metadata={}, created_at=1.0,
            source_modified_at=source_modified_at,
            payload_bytes=total_size, lease_token="lease",
        )

    @staticmethod
    def _payload() -> dict:
        return {
            "tool": "codex", "relative_path": "thread.jsonl", "hash": "hash",
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
                client._upload_json = (
                    lambda payload: payloads.append(("json", payload)) or True
                )
                client._upload_multipart = (
                    lambda payload, _stream:
                    payloads.append(("multipart", payload)) or True
                )
                client._upload_chunked = (
                    lambda payload, _item:
                    payloads.append(("chunked", payload)) or True
                )

                with self.subTest(
                    source_modified_at=source_modified_at,
                    route=expected_route,
                ):
                    self.assertTrue(client._upload(
                        self._item(size, source_modified_at=source_modified_at),
                    ))
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
        requested: list[str] = []
        client._full_resync_callback = requested.append
        item = self._item(size)
        item.sync_strategy = "delta"
        item.source_path = "/tmp/thread.jsonl"

        self.assertFalse(client._upload(item))
        self.assertEqual(requested, [item.source_path])
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

    def test_guarded_delta_uses_synchronous_multipart_and_sends_base(self) -> None:
        size = CHUNK_SIZE + 123
        queue = _FakeQueue(size)
        client = self._client(queue, _FakeHttpClient())
        routes: list[tuple[str, dict]] = []
        client._upload_multipart = (
            lambda payload, _stream: routes.append(("multipart", payload)) or True
        )
        client._upload_chunked = (
            lambda payload, _item: routes.append(("chunked", payload)) or True
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

        self.assertTrue(client._upload(item))
        self.assertEqual(captured[0]["base_hash"], "base-hash")
        self.assertEqual(queue.delta_conflicts, [item])
        self.assertEqual(requested, ["/tmp/thread.jsonl"])

    def test_response_lost_retries_same_accepted_chunk_then_continues(self) -> None:
        total_size = CHUNK_SIZE + 123
        queue = _FakeQueue(total_size)
        response_lost = httpx.ReadError(
            "response lost after acceptance",
            request=httpx.Request("POST", "https://example.test/api/ingest/file/chunk"),
        )
        http_client = _ScriptedHttpClient([
            response_lost,
            _Response(200),
            _Response(200),
        ])
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))

        self.assertEqual(
            [call["metadata"]["chunk_index"] for call in http_client.calls],
            [0, 0, 1],
        )
        self.assertEqual(http_client.calls[0]["metadata_text"], http_client.calls[1]["metadata_text"])
        self.assertEqual(http_client.calls[0]["content"], http_client.calls[1]["content"])
        self.assertEqual(http_client.calls[0]["filename"], http_client.calls[1]["filename"])
        self.assertEqual(delays, [CHUNK_RETRY_BASE_SECONDS])
        self.assertEqual(queue.renewals, 3)

    def test_transient_502_retries_current_chunk(self) -> None:
        total_size = CHUNK_SIZE + 1
        queue = _FakeQueue(total_size)
        http_client = _ScriptedHttpClient([
            _Response(502),
            _Response(200),
            _Response(200),
        ])
        client = self._client(queue, http_client)
        delays: list[float] = []
        client._sleep_interruptibly = delays.append

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))
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

        self.assertFalse(client._upload_chunked(self._payload(), self._item(total_size)))
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

        self.assertFalse(client._upload_chunked(self._payload(), self._item(total_size)))
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

        self.assertFalse(client._upload_chunked(self._payload(), self._item(total_size)))
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
        self.assertFalse(any(
            key.startswith("_queue_") for key in http_client.payload
        ))
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


if __name__ == "__main__":
    unittest.main()
