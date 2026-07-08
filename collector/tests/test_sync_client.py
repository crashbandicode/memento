from __future__ import annotations

import json
import sys
import threading
import unittest
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

    @contextmanager
    def open_payload(self, _item: QueueItem):
        yield self.stream

    def renew_lease(self, _item: QueueItem, lease_seconds: int) -> bool:
        self.renewals += 1
        return lease_seconds == 300


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


class SyncClientStreamingTests(unittest.TestCase):
    @staticmethod
    def _item(total_size: int) -> QueueItem:
        return QueueItem(
            id=1, tool_name="codex", category="conversation",
            content_type="jsonl", relative_path="thread.jsonl", content=None,
            content_hash="hash", file_size=total_size, sync_strategy="full",
            is_partial=False, offset=0, metadata={}, created_at=1.0,
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
        client._config = SimpleNamespace(queue_lease_seconds=300)
        client._running = True
        client._pause_requested = threading.Event()
        client._client = http_client
        return client

    def test_chunked_upload_reads_only_one_chunk_at_a_time(self) -> None:
        total_size = CHUNK_SIZE * 2 + 123
        queue = _FakeQueue(total_size)
        http_client = _FakeHttpClient()
        client = self._client(queue, http_client)

        self.assertTrue(client._upload_chunked(self._payload(), self._item(total_size)))
        self.assertEqual(http_client.chunk_sizes, [CHUNK_SIZE, CHUNK_SIZE, 123])
        self.assertEqual(queue.stream.largest_read, CHUNK_SIZE)
        self.assertEqual(queue.renewals, 3)

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
