from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.ingest_spool import (  # noqa: E402
    MAX_CHUNK_BYTES,
    MAX_CHUNKS,
    MAX_UPLOAD_BYTES,
    ChunkValidationError,
    assemble_job,
    cleanup_completion_receipts,
    cleanup_stale_incomplete_jobs,
    failed_job_ids,
    mark_job_complete,
    mark_job_failed,
    ready_job_ids,
    record_job_attempt,
    remove_job,
    stage_chunk,
)
import server.services.ingest_spool as ingest_spool  # noqa: E402


class IngestSpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "ingest-spool"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _meta(chunk_index: int, total_chunks: int, **overrides) -> dict:
        meta = {
            "upload_id": "codex/sessions/thread.jsonl/hash-1",
            "hash": "hash-1",
            "tool": "codex",
            "relative_path": "sessions/thread.jsonl",
            "category": "conversation",
            "content_type": "jsonl",
            "mode": "full",
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "file_size": 1,
            "metadata": {"title": "Thread title"},
        }
        meta.update(overrides)
        return meta

    def _stage(self, meta: dict, data: bytes) -> tuple[str, bool]:
        return stage_chunk(
            meta=meta,
            chunk_data=data,
            user_id="11111111-1111-1111-1111-111111111111",
            device_id="device-1",
            device_name="Yoga",
            device_platform="Windows",
            root=self.root,
        )

    def test_out_of_order_chunks_complete_only_after_gap_is_filled(self) -> None:
        job_id, complete = self._stage(self._meta(2, 3, file_size=16), b"third")
        self.assertFalse(complete)
        self.assertEqual(ready_job_ids(self.root), [])

        second_job_id, complete = self._stage(
            self._meta(0, 3, file_size=16), b"first",
        )
        self.assertEqual(second_job_id, job_id)
        self.assertFalse(complete)
        self.assertEqual(ready_job_ids(self.root), [])

        third_job_id, complete = self._stage(
            self._meta(1, 3, file_size=16), b"second",
        )
        self.assertEqual(third_job_id, job_id)
        self.assertTrue(complete)
        self.assertEqual(ready_job_ids(self.root), [job_id])

        _manifest, payload_path = assemble_job(job_id, self.root)
        self.assertEqual(payload_path.read_bytes(), b"firstsecondthird")

    def test_duplicate_chunk_is_idempotent(self) -> None:
        job_id, complete = self._stage(self._meta(0, 2, file_size=11), b"first")
        self.assertFalse(complete)

        duplicate_job_id, duplicate_complete = self._stage(
            self._meta(0, 2, file_size=11), b"first",
        )
        self.assertEqual(duplicate_job_id, job_id)
        self.assertFalse(duplicate_complete)
        self.assertEqual(
            (self.root / job_id / "chunk-000000.bin").read_bytes(),
            b"first",
        )
        with self.assertRaisesRegex(ChunkValidationError, "duplicate chunk content"):
            self._stage(self._meta(0, 2, file_size=11), b"other")
        self.assertEqual(
            (self.root / job_id / "chunk-000000.bin").read_bytes(),
            b"first",
        )

        _job_id, complete = self._stage(
            self._meta(1, 2, file_size=11), b"second",
        )
        self.assertTrue(complete)
        self.assertEqual(
            sorted(path.name for path in (self.root / job_id).glob("chunk-*.bin")),
            ["chunk-000000.bin", "chunk-000001.bin"],
        )

    def test_conflicting_metadata_is_rejected_without_mutating_job(self) -> None:
        job_id, _complete = self._stage(
            self._meta(0, 2, file_size=11), b"first",
        )
        manifest_path = self.root / job_id / "manifest.json"
        original_manifest = manifest_path.read_bytes()

        with self.assertRaisesRegex(ChunkValidationError, "conflicts"):
            self._stage(
                self._meta(1, 2, tool="claude_code", file_size=11), b"second",
            )
        with self.assertRaisesRegex(ChunkValidationError, "conflicts"):
            self._stage(self._meta(1, 3, file_size=11), b"second")

        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertFalse((self.root / job_id / "chunk-000001.bin").exists())

    def test_invalid_chunk_bounds_are_rejected(self) -> None:
        invalid_cases = [
            (self._meta(-1, 1), b"data"),
            (self._meta(1, 1), b"data"),
            (self._meta(0, 0), b"data"),
            (self._meta(0, MAX_CHUNKS + 1), b"data"),
            (self._meta("not-an-int", 1), b"data"),
            (self._meta(0, 1), b"x" * (MAX_CHUNK_BYTES + 1)),
            (self._meta(0, 1, file_size=0), b"data"),
            (self._meta(0, 1, file_size=MAX_UPLOAD_BYTES + 1), b"data"),
            (self._meta(0, 1, file_size=MAX_CHUNK_BYTES + 1), b"data"),
            (self._meta(0, 1, mode="append"), b"d"),
            (self._meta(0, 1, metadata=[]), b"d"),
            (self._meta(0, 1, timestamp="yesterday"), b"d"),
            (self._meta(0, 1, timestamp=float("nan")), b"d"),
            (self._meta(0, 1, offset=-1), b"d"),
            (self._meta(0, 1, category="x" * 51), b"d"),
            (self._meta(0, 1, hash="x" * 65), b"d"),
        ]

        for meta, data in invalid_cases:
            with self.subTest(meta=meta, size=len(data)):
                with self.assertRaises(ChunkValidationError):
                    self._stage(meta, data)

        self.assertFalse(self.root.exists())

    def test_assembly_preserves_manifest_and_is_repeatable(self) -> None:
        job_id, _complete = self._stage(
            self._meta(0, 2, file_size=11), b"alpha\n",
        )
        _job_id, complete = self._stage(
            self._meta(1, 2, file_size=11), b"beta\n",
        )
        self.assertTrue(complete)

        manifest, payload_path = assemble_job(job_id, self.root)
        self.assertEqual(payload_path.read_bytes(), b"alpha\nbeta\n")
        self.assertEqual(manifest["job_id"], job_id)
        self.assertEqual(manifest["user_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(manifest["device_id"], "device-1")
        self.assertEqual(manifest["total_chunks"], 2)
        self.assertEqual(manifest["meta"]["relative_path"], "sessions/thread.jsonl")

        repeated_manifest, repeated_payload_path = assemble_job(job_id, self.root)
        self.assertEqual(repeated_manifest, manifest)
        self.assertEqual(repeated_payload_path, payload_path)
        self.assertEqual(repeated_payload_path.read_bytes(), b"alpha\nbeta\n")

    def test_ready_discovery_and_safe_removal(self) -> None:
        first_job, complete = self._stage(
            self._meta(0, 1, upload_id="upload-a"), b"a",
        )
        self.assertTrue(complete)
        second_job, complete = self._stage(
            self._meta(0, 1, upload_id="upload-b"), b"b",
        )
        self.assertTrue(complete)
        incomplete_job, complete = self._stage(
            self._meta(0, 2, upload_id="upload-incomplete", file_size=14),
            b"partial",
        )
        self.assertFalse(complete)

        invalid_dir = self.root / "not-a-safe-job-id"
        invalid_dir.mkdir()
        (invalid_dir / "ready").write_text("ready\n", encoding="utf-8")
        (invalid_dir / "manifest.json").write_text(
            json.dumps({"total_chunks": 1}), encoding="utf-8",
        )
        no_manifest = self.root / ("f" * 64)
        no_manifest.mkdir()
        (no_manifest / "ready").write_text("ready\n", encoding="utf-8")

        self.assertEqual(
            ready_job_ids(self.root),
            sorted([first_job, second_job]),
        )
        self.assertNotIn(incomplete_job, ready_job_ids(self.root))

        remove_job(first_job, self.root)
        self.assertFalse((self.root / first_job).exists())
        self.assertEqual(ready_job_ids(self.root), [second_job])

        outside = self.root.parent / "outside-sentinel"
        outside.write_text("keep", encoding="utf-8")
        with self.assertRaises(ChunkValidationError):
            remove_job("../outside-sentinel", self.root)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_stale_cleanup_removes_only_incomplete_expired_jobs(self) -> None:
        stale_job, complete = self._stage(
            self._meta(0, 2, upload_id="stale", file_size=10), b"first",
        )
        self.assertFalse(complete)
        fresh_job, complete = self._stage(
            self._meta(0, 2, upload_id="fresh", file_size=10), b"first",
        )
        self.assertFalse(complete)
        ready_job, complete = self._stage(
            self._meta(0, 1, upload_id="ready", file_size=5), b"ready",
        )
        self.assertTrue(complete)

        old_timestamp = 1.0
        for job_id in (stale_job, ready_job):
            job_dir = self.root / job_id
            for entry in job_dir.iterdir():
                os.utime(entry, (old_timestamp, old_timestamp))
            os.utime(job_dir, (old_timestamp, old_timestamp))

        removed = cleanup_stale_incomplete_jobs(
            root=self.root,
            max_age_seconds=60,
        )

        self.assertEqual(removed, 1)
        self.assertFalse((self.root / stale_job).exists())
        self.assertTrue((self.root / fresh_job).is_dir())
        self.assertTrue((self.root / ready_job).is_dir())
        self.assertEqual(ready_job_ids(self.root), [ready_job])

    def test_exact_size_is_required_before_ready(self) -> None:
        job_id, complete = self._stage(
            self._meta(0, 2, upload_id="short", file_size=3), b"a",
        )
        self.assertFalse(complete)

        with self.assertRaisesRegex(ChunkValidationError, "declared file_size"):
            self._stage(
                self._meta(1, 2, upload_id="short", file_size=3), b"b",
            )

        self.assertFalse((self.root / job_id / "ready").exists())
        self.assertEqual(ready_job_ids(self.root), [])

    def test_completion_receipt_makes_lost_response_idempotent(self) -> None:
        meta = self._meta(0, 1, upload_id="completed", file_size=4)
        job_id, complete = self._stage(meta, b"done")
        self.assertTrue(complete)

        mark_job_complete(job_id, document_id="document-1", root=self.root)
        remove_job(job_id, self.root)
        self.assertFalse((self.root / job_id).exists())

        repeated_job_id, repeated_complete = self._stage(meta, b"done")
        self.assertEqual(repeated_job_id, job_id)
        self.assertTrue(repeated_complete)
        self.assertFalse((self.root / job_id).exists())

        receipt = self.root / "completed" / f"{job_id}.json"
        os.utime(receipt, (1.0, 1.0))
        self.assertEqual(
            cleanup_completion_receipts(root=self.root, max_age_seconds=60),
            1,
        )
        self.assertFalse(receipt.exists())

    def test_spool_quota_rejects_new_bytes_before_writing(self) -> None:
        with (
            patch.object(ingest_spool, "MAX_SPOOL_BYTES", 3),
            patch.object(ingest_spool, "MIN_FREE_BYTES", 0),
            self.assertRaisesRegex(ChunkValidationError, "capacity"),
        ):
            self._stage(
                self._meta(0, 1, upload_id="quota", file_size=4), b"data",
            )

        job_dirs = [
            path for path in self.root.iterdir()
            if path.is_dir() and path.name != "completed"
        ]
        self.assertEqual(len(job_dirs), 1)
        self.assertEqual(list(job_dirs[0].glob("chunk-*.bin")), [])

    def test_failed_job_is_quarantined_from_recovery(self) -> None:
        job_id, complete = self._stage(
            self._meta(0, 1, upload_id="poison", file_size=4), b"data",
        )
        self.assertTrue(complete)

        mark_job_failed(
            job_id,
            error_type="ValueError",
            attempts=1,
            root=self.root,
        )

        self.assertEqual(ready_job_ids(self.root), [])
        self.assertEqual(failed_job_ids(self.root), [job_id])
        failure = json.loads(
            (self.root / job_id / "failed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(failure["error_type"], "ValueError")
        self.assertEqual(failure["attempts"], 1)

    def test_attempt_counter_survives_worker_restarts(self) -> None:
        job_id, complete = self._stage(
            self._meta(0, 1, upload_id="attempts", file_size=4), b"data",
        )
        self.assertTrue(complete)

        self.assertEqual(record_job_attempt(job_id, root=self.root), 1)
        self.assertEqual(record_job_attempt(job_id, root=self.root), 2)
        persisted = json.loads(
            (self.root / job_id / "attempts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
