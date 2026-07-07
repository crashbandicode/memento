from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api import ingest as ingest_api  # noqa: E402
from server.services.ingest_spool import stage_chunk as durable_stage_chunk  # noqa: E402


class ChunkIngestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.spool_root = Path(self._temporary.name) / "spool"
        app = FastAPI()
        app.include_router(ingest_api.router)

        async def collector_user():
            return SimpleNamespace(id=uuid.UUID("11111111-1111-1111-1111-111111111111"))

        async def no_throttle():
            yield

        class FakeDb:
            async def commit(self):
                return None

        async def fake_db():
            yield FakeDb()

        app.dependency_overrides[ingest_api.verify_collector_token] = collector_user
        app.dependency_overrides[ingest_api.throttle_ingest] = no_throttle
        app.dependency_overrides[ingest_api.get_db] = fake_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self._temporary.cleanup()

    @staticmethod
    def _meta(chunk_index: int) -> dict:
        return {
            "upload_id": "codex/sessions/thread.jsonl/hash-1",
            "hash": "hash-1",
            "tool": "codex",
            "relative_path": "sessions/thread.jsonl",
            "category": "conversation",
            "content_type": "jsonl",
            "mode": "full",
            "offset": 0,
            "file_size": 11,
            "metadata": {},
            "chunk_index": chunk_index,
            "total_chunks": 2,
        }

    def _stage_in_test_spool(self, **kwargs):
        return durable_stage_chunk(**kwargs, root=self.spool_root)

    def test_final_chunk_is_acknowledged_after_durable_stage_and_enqueued(self) -> None:
        headers = {
            "x-device-id": "device-1",
            "x-device-name": "Yoga",
            "x-device-platform": "Windows",
        }
        with (
            patch.object(ingest_api, "ensure_device", new_callable=AsyncMock),
            patch.object(ingest_api, "stage_chunk", side_effect=self._stage_in_test_spool),
            patch(
                "server.tasks.ingest_spool.process_spooled_ingest.apply_async"
            ) as enqueue,
        ):
            first = self.client.post(
                "/api/ingest/file/chunk",
                data={"metadata": json.dumps(self._meta(0))},
                files={"content": ("chunk-0", b"first", "text/plain")},
                headers=headers,
            )
            final = self.client.post(
                "/api/ingest/file/chunk",
                data={"metadata": json.dumps(self._meta(1))},
                files={"content": ("chunk-1", b"second", "text/plain")},
                headers=headers,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["document_id"], "pending")
        self.assertEqual(final.status_code, 200)
        self.assertTrue(final.json()["document_id"].startswith("queued:"))
        enqueue.assert_called_once()

    def test_invalid_metadata_returns_400_without_enqueuing(self) -> None:
        response = self.client.post(
            "/api/ingest/file/chunk",
            data={"metadata": "not-json"},
            files={"content": ("chunk", b"data", "text/plain")},
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
