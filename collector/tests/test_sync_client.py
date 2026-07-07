from __future__ import annotations

import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.queue import QueueItem  # noqa: E402
from collector.sync_client import CHUNK_SIZE, SyncClient  # noqa: E402


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
    status_code = 200


class _FakeHttpClient:
    def __init__(self) -> None:
        self.chunk_sizes: list[int] = []

    def post(self, _path: str, data: dict, files: dict) -> _Response:
        del data
        self.chunk_sizes.append(len(files["content"][1]))
        return _Response()


class SyncClientStreamingTests(unittest.TestCase):
    def test_chunked_upload_reads_only_one_chunk_at_a_time(self) -> None:
        total_size = CHUNK_SIZE * 2 + 123
        queue = _FakeQueue(total_size)
        client = object.__new__(SyncClient)
        client._queue = queue
        client._config = SimpleNamespace(queue_lease_seconds=300)
        client._running = True
        client._pause_requested = threading.Event()
        client._client = _FakeHttpClient()
        item = QueueItem(
            id=1, tool_name="codex", category="conversation",
            content_type="jsonl", relative_path="thread.jsonl", content=None,
            content_hash="hash", file_size=total_size, sync_strategy="full",
            is_partial=False, offset=0, metadata={}, created_at=1.0,
            payload_bytes=total_size, lease_token="lease",
        )
        payload = {
            "tool": "codex", "relative_path": "thread.jsonl", "hash": "hash",
        }

        self.assertTrue(client._upload_chunked(payload, item))
        self.assertEqual(client._client.chunk_sizes, [CHUNK_SIZE, CHUNK_SIZE, 123])
        self.assertEqual(queue.stream.largest_read, CHUNK_SIZE)
        self.assertEqual(queue.renewals, 3)


if __name__ == "__main__":
    unittest.main()
