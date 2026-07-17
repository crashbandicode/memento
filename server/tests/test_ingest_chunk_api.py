from __future__ import annotations

import asyncio
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
from server.services.ingest_spool import (  # noqa: E402
    StagedChunk,
    stage_chunk as durable_stage_chunk,
)


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

    @staticmethod
    def _delta_payload() -> dict:
        return {
            "tool": "codex",
            "category": "conversation",
            "content_type": "jsonl",
            "relative_path": "sessions/thread.jsonl",
            "hash": "next-hash",
            "mode": "delta",
            "offset": 20,
            "file_size": 5,
            "base_hash": "base-hash",
            "base_offset": 10,
            "content": "tail\n",
        }

    def test_final_chunk_is_acknowledged_after_durable_stage_and_enqueued(self) -> None:
        headers = {
            "x-device-id": "device-1",
            "x-device-name": "Yoga",
            "x-device-platform": "Windows",
        }
        with (
            patch.object(ingest_api, "ensure_device", new_callable=AsyncMock),
            patch.object(
                ingest_api, "stage_chunk", side_effect=self._stage_in_test_spool
            ),
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

    def test_completion_receipt_retry_does_not_enqueue_missing_job(self) -> None:
        machine_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        headers = {
            "x-device-id": "device-1",
            "x-device-name": "Yoga",
            "x-device-platform": "Windows",
        }
        with (
            patch.object(
                ingest_api,
                "ensure_device",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(id=machine_id),
            ),
            patch.object(ingest_api, "has_completion_receipt", return_value=True),
            patch.object(
                ingest_api,
                "_completed_upload_needs_reprocessing",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                ingest_api,
                "stage_chunk",
                return_value=StagedChunk(
                    "a" * 64,
                    complete=True,
                    should_enqueue=False,
                ),
            ) as stage,
            patch(
                "server.tasks.ingest_spool.process_spooled_ingest.apply_async"
            ) as enqueue,
        ):
            response = self.client.post(
                "/api/ingest/file/chunk",
                data={"metadata": json.dumps(self._meta(0))},
                files={"content": ("chunk-0", b"first", "text/plain")},
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["document_id"].startswith("completed:"))
        self.assertFalse(stage.call_args.kwargs["force_reprocess"])
        enqueue.assert_not_called()

    def test_stale_completion_receipt_forces_reprocessing(self) -> None:
        machine_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        with (
            patch.object(
                ingest_api,
                "ensure_device",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(id=machine_id),
            ),
            patch.object(ingest_api, "has_completion_receipt", return_value=True),
            patch.object(
                ingest_api,
                "_completed_upload_needs_reprocessing",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                ingest_api,
                "stage_chunk",
                return_value=StagedChunk(
                    "a" * 64,
                    complete=False,
                    should_enqueue=False,
                ),
            ) as stage,
        ):
            response = self.client.post(
                "/api/ingest/file/chunk",
                data={"metadata": json.dumps(self._meta(0))},
                files={"content": ("chunk-0", b"first", "text/plain")},
                headers={"x-device-id": "device-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(stage.call_args.kwargs["force_reprocess"])

    def test_conversation_receipt_requires_current_stored_source_proof(self) -> None:
        machine_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        row = SimpleNamespace(content_hash="hash-1", metadata_={})
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
        )

        needs_reprocessing = asyncio.run(
            ingest_api._completed_upload_needs_reprocessing(
                db,
                machine_id=machine_id,
                meta=self._meta(0),
            )
        )
        self.assertTrue(needs_reprocessing)

        row.metadata_[ingest_api.STORED_SOURCE_REVISION_KEY] = "hash-1"
        needs_reprocessing = asyncio.run(
            ingest_api._completed_upload_needs_reprocessing(
                db,
                machine_id=machine_id,
                meta=self._meta(0),
            )
        )
        self.assertFalse(needs_reprocessing)

    def test_guarded_delta_mismatch_returns_resyncable_conflict(self) -> None:
        with (
            patch.object(ingest_api, "ensure_device", new_callable=AsyncMock),
            patch.object(
                ingest_api,
                "pending_source_revision_job_id",
                return_value=None,
            ),
            patch.object(
                ingest_api,
                "ingest_file",
                new_callable=AsyncMock,
                side_effect=ingest_api.DeltaBaseMismatch(
                    expected_hash="server-hash",
                    expected_offset=15,
                ),
            ) as ingest,
        ):
            response = self.client.post(
                "/api/ingest/file",
                json=self._delta_payload(),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "delta_base_mismatch",
                "expected_hash": "server-hash",
                "expected_offset": 15,
            },
        )
        self.assertEqual(ingest.await_count, 2)

    def test_delta_retries_when_pending_base_commits_during_lookup(self) -> None:
        machine_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        document_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        with (
            patch.object(
                ingest_api,
                "ensure_device",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(id=machine_id),
            ),
            patch.object(
                ingest_api,
                "pending_source_revision_job_id",
                return_value=None,
            ),
            patch.object(
                ingest_api,
                "ingest_file",
                new_callable=AsyncMock,
                side_effect=[
                    ingest_api.DeltaBaseMismatch(
                        expected_hash="server-hash",
                        expected_offset=5,
                    ),
                    SimpleNamespace(id=document_id),
                ],
            ) as ingest,
            patch.object(ingest_api, "stage_chunk") as stage,
        ):
            response = self.client.post(
                "/api/ingest/file",
                json=self._delta_payload(),
                headers={"x-device-id": "device-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["document_id"], str(document_id))
        self.assertEqual(ingest.await_count, 2)
        stage.assert_not_called()

    def test_guarded_delta_waits_behind_durable_pending_base(self) -> None:
        machine_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        headers = {
            "x-device-id": "device-1",
            "x-device-name": "Yoga",
            "x-device-platform": "Windows",
        }
        with (
            patch.object(
                ingest_api,
                "ensure_device",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(id=machine_id),
            ),
            patch.object(
                ingest_api,
                "ingest_file",
                new_callable=AsyncMock,
                side_effect=ingest_api.DeltaBaseMismatch(
                    expected_hash="server-hash",
                    expected_offset=5,
                ),
            ),
            patch.object(
                ingest_api,
                "pending_source_revision_job_id",
                return_value="b" * 64,
            ) as find_pending,
            patch.object(
                ingest_api,
                "stage_chunk",
                side_effect=self._stage_in_test_spool,
            ),
            patch(
                "server.tasks.ingest_spool.process_spooled_ingest.apply_async"
            ) as enqueue,
        ):
            response = self.client.post(
                "/api/ingest/file",
                json=self._delta_payload(),
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["document_id"].startswith("queued:"))
        self.assertIn("pending revision", response.json()["message"])
        find_pending.assert_called_once_with(
            user_id="11111111-1111-1111-1111-111111111111",
            device_id="device-1",
            tool="codex",
            relative_path="sessions/thread.jsonl",
            content_hash="base-hash",
            offset=10,
        )
        enqueue.assert_called_once()

    def test_multipart_guarded_delta_uses_same_pending_base_queue(self) -> None:
        machine_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        payload = self._delta_payload()
        content = payload.pop("content")
        with (
            patch.object(
                ingest_api,
                "ensure_device",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(id=machine_id),
            ),
            patch.object(
                ingest_api,
                "ingest_file",
                new_callable=AsyncMock,
                side_effect=ingest_api.DeltaBaseMismatch(
                    expected_hash="server-hash",
                    expected_offset=5,
                ),
            ),
            patch.object(
                ingest_api,
                "pending_source_revision_job_id",
                return_value="b" * 64,
            ),
            patch.object(
                ingest_api,
                "stage_chunk",
                side_effect=self._stage_in_test_spool,
            ),
            patch(
                "server.tasks.ingest_spool.process_spooled_ingest.apply_async"
            ) as enqueue,
        ):
            response = self.client.post(
                "/api/ingest/file/upload",
                data={"metadata": json.dumps(payload)},
                files={"content": ("delta", content.encode(), "text/plain")},
                headers={"x-device-id": "device-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["document_id"].startswith("queued:"))
        enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
