from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.ingest_spool import (  # noqa: E402
    MAX_CHUNK_BYTES,
    MAX_CHUNKS,
    MAX_UPLOAD_BYTES,
    ChunkValidationError,
    assemble_job,
    blocked_job_ids,
    cleanup_completion_receipts,
    cleanup_stale_incomplete_jobs,
    complete_and_remove_job,
    failed_job_ids,
    mark_job_complete,
    mark_job_blocked,
    mark_job_failed,
    ready_job_ids_in_recovery_order,
    ready_manifest,
    ready_manifest_metadata,
    ready_job_ids,
    record_job_attempt,
    remove_job,
    select_ready_source_head,
    source_identity,
    spool_source_lock,
    stage_chunk,
    superseding_ready_full_job_id,
)
import server.services.ingest_spool as ingest_spool  # noqa: E402
from server.tasks.celery_app import (  # noqa: E402
    INGEST_RECOVERY_EXPIRES_SECONDS,
    celery_app,
)
from server.tasks.ingest_spool import (  # noqa: E402
    _existing_full_supersedes,
    _preflight_full_supersedes,
    recover_spooled_ingests,
)


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

    def _stage(
        self,
        meta: dict,
        data: bytes,
        *,
        user_id: str = "11111111-1111-1111-1111-111111111111",
        device_id: str = "device-1",
    ) -> tuple[str, bool]:
        return stage_chunk(
            meta=meta,
            chunk_data=data,
            user_id=user_id,
            device_id=device_id,
            device_name="Yoga",
            device_platform="Windows",
            root=self.root,
        )

    def test_out_of_order_chunks_complete_only_after_gap_is_filled(self) -> None:
        job_id, complete = self._stage(self._meta(2, 3, file_size=16), b"third")
        self.assertFalse(complete)
        self.assertEqual(ready_job_ids(self.root), [])

        second_job_id, complete = self._stage(
            self._meta(0, 3, file_size=16),
            b"first",
        )
        self.assertEqual(second_job_id, job_id)
        self.assertFalse(complete)
        self.assertEqual(ready_job_ids(self.root), [])

        third_job_id, complete = self._stage(
            self._meta(1, 3, file_size=16),
            b"second",
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
            self._meta(0, 2, file_size=11),
            b"first",
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
            self._meta(1, 2, file_size=11),
            b"second",
        )
        self.assertTrue(complete)
        self.assertEqual(
            sorted(path.name for path in (self.root / job_id).glob("chunk-*.bin")),
            ["chunk-000000.bin", "chunk-000001.bin"],
        )

    def test_conflicting_metadata_is_rejected_without_mutating_job(self) -> None:
        job_id, _complete = self._stage(
            self._meta(0, 2, file_size=11),
            b"first",
        )
        manifest_path = self.root / job_id / "manifest.json"
        original_manifest = manifest_path.read_bytes()

        with self.assertRaisesRegex(ChunkValidationError, "conflicts"):
            self._stage(
                self._meta(1, 2, tool="claude_code", file_size=11),
                b"second",
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
            self._meta(0, 2, file_size=11),
            b"alpha\n",
        )
        _job_id, complete = self._stage(
            self._meta(1, 2, file_size=11),
            b"beta\n",
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
            self._meta(0, 1, upload_id="upload-a"),
            b"a",
        )
        self.assertTrue(complete)
        second_job, complete = self._stage(
            self._meta(0, 1, upload_id="upload-b"),
            b"b",
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
            json.dumps({"total_chunks": 1}),
            encoding="utf-8",
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

    def test_only_newer_full_snapshot_supersedes_same_owned_path(self) -> None:
        older, complete = self._stage(
            self._meta(
                0,
                1,
                upload_id="older",
                hash="older-hash",
                timestamp=100.0,
            ),
            b"a",
        )
        self.assertTrue(complete)
        newer, complete = self._stage(
            self._meta(
                0,
                1,
                upload_id="newer",
                hash="newer-hash",
                timestamp=200.0,
            ),
            b"b",
        )
        self.assertTrue(complete)

        head, cohort = select_ready_source_head(older, self.root)
        self.assertEqual(head, newer)
        self.assertEqual(cohort, (older,))
        self.assertEqual(
            superseding_ready_full_job_id(older, self.root),
            newer,
        )

        delta, complete = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta",
                hash="delta-hash",
                mode="delta",
                timestamp=300.0,
            ),
            b"c",
        )
        self.assertTrue(complete)

        head, cohort = select_ready_source_head(older, self.root)
        self.assertEqual(head, older)
        self.assertEqual(cohort, ())
        self.assertIsNone(superseding_ready_full_job_id(older, self.root))
        self.assertIsNone(superseding_ready_full_job_id(newer, self.root))
        self.assertIsNone(superseding_ready_full_job_id(delta, self.root))

        mark_job_failed(delta, error_type="test", attempts=1, root=self.root)
        mark_job_failed(newer, error_type="test", attempts=1, root=self.root)
        self.assertIsNone(superseding_ready_full_job_id(older, self.root))

    def test_committed_full_supersedes_only_same_or_strictly_older_source(self) -> None:
        now = datetime.now(timezone.utc)
        common = {
            "existing_hash": "existing",
            "existing_timestamp": now,
            "existing_offset": 100,
            "existing_size": 200,
            "incoming_hash": "incoming",
            "incoming_timestamp": now,
            "incoming_offset": 100,
            "incoming_size": 200,
        }

        self.assertFalse(_existing_full_supersedes(**common))
        self.assertTrue(
            _existing_full_supersedes(**{**common, "incoming_hash": "existing"})
        )
        self.assertTrue(
            _existing_full_supersedes(
                **{**common, "incoming_timestamp": now - timedelta(seconds=1)}
            )
        )
        self.assertTrue(_existing_full_supersedes(**{**common, "incoming_size": 199}))
        self.assertTrue(_existing_full_supersedes(**{**common, "incoming_offset": 99}))
        self.assertFalse(
            _existing_full_supersedes(
                **{**common, "incoming_timestamp": now + timedelta(seconds=1)}
            )
        )

    def test_equal_full_revisions_use_hash_as_persisted_tie_breaker(self) -> None:
        older, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="hash-a",
                hash="aaa",
                timestamp=100.0,
                offset=10,
            ),
            b"a",
        )
        newer, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="hash-z",
                hash="zzz",
                timestamp=100.0,
                offset=10,
            ),
            b"z",
        )

        head, cohort = select_ready_source_head(older, self.root)
        self.assertEqual(head, newer)
        self.assertEqual(cohort, (older,))

        now = datetime.fromtimestamp(100.0, tz=timezone.utc)
        self.assertTrue(
            _existing_full_supersedes(
                existing_hash="zzz",
                existing_timestamp=now,
                existing_offset=10,
                existing_size=1,
                incoming_hash="aaa",
                incoming_timestamp=now,
                incoming_offset=10,
                incoming_size=1,
            )
        )

    def test_same_hash_full_always_reaches_locked_pointer_and_timestamp_path(
        self,
    ) -> None:
        self.assertFalse(
            _preflight_full_supersedes(
                existing_hash="same",
                existing_timestamp=datetime.fromtimestamp(100, tz=timezone.utc),
                existing_offset=100,
                existing_size=100,
                incoming_hash="same",
                incoming_timestamp=300.0,
                incoming_offset=300,
                incoming_size=300,
            )
        )
        self.assertTrue(
            _preflight_full_supersedes(
                existing_hash="newer",
                existing_timestamp=datetime.fromtimestamp(300, tz=timezone.utc),
                existing_offset=300,
                existing_size=300,
                incoming_hash="older",
                incoming_timestamp=100.0,
                incoming_offset=100,
                incoming_size=100,
            )
        )

    def test_source_grouping_never_crosses_user_device_tool_or_path(self) -> None:
        target, _ = self._stage(
            self._meta(0, 1, upload_id="target", hash="target", timestamp=1.0),
            b"a",
        )
        variants = (
            (
                self._meta(
                    0,
                    1,
                    upload_id="other-device",
                    hash="other-device",
                    timestamp=9.0,
                ),
                {"device_id": "device-2"},
            ),
            (
                self._meta(
                    0,
                    1,
                    upload_id="other-user",
                    hash="other-user",
                    timestamp=9.0,
                ),
                {"user_id": "22222222-2222-2222-2222-222222222222"},
            ),
            (
                self._meta(
                    0,
                    1,
                    upload_id="other-tool",
                    hash="other-tool",
                    tool="claude_code",
                    timestamp=9.0,
                ),
                {},
            ),
            (
                self._meta(
                    0,
                    1,
                    upload_id="other-path",
                    hash="other-path",
                    relative_path="sessions/other.jsonl",
                    timestamp=9.0,
                ),
                {},
            ),
        )
        for meta, stage_kwargs in variants:
            self._stage(meta, b"b", **stage_kwargs)

        head, cohort = select_ready_source_head(target, self.root)
        self.assertEqual(head, target)
        self.assertEqual(cohort, ())

    def test_reverse_queued_full_and_deltas_keep_every_step_in_order(self) -> None:
        delta_two, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta-two",
                hash="delta-two",
                mode="delta",
                timestamp=3.0,
                offset=300,
            ),
            b"c",
        )
        delta_one, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta-one",
                hash="delta-one",
                mode="delta",
                timestamp=2.0,
                offset=200,
            ),
            b"b",
        )
        full, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="full",
                hash="full",
                timestamp=1.0,
                offset=100,
            ),
            b"a",
        )

        head, cohort = select_ready_source_head(delta_two, self.root)
        self.assertEqual(head, full)
        self.assertEqual(cohort, ())
        complete_and_remove_job(full, document_id="document-id", root=self.root)
        self.assertEqual(select_ready_source_head(delta_two, self.root)[0], delta_one)
        complete_and_remove_job(
            delta_one,
            document_id="document-id",
            root=self.root,
        )
        self.assertEqual(select_ready_source_head(delta_two, self.root)[0], delta_two)

    def test_failed_newest_full_leaves_older_snapshot_as_fallback(self) -> None:
        older, _ = self._stage(
            self._meta(0, 1, upload_id="older", hash="older", timestamp=1.0),
            b"a",
        )
        newer, _ = self._stage(
            self._meta(0, 1, upload_id="newer", hash="newer", timestamp=2.0),
            b"b",
        )
        mark_job_failed(newer, error_type="test", attempts=1, root=self.root)

        self.assertEqual(select_ready_source_head(older, self.root), (older, ()))

    def test_failed_delta_blocks_later_delta_until_full_rebase(self) -> None:
        full, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="base",
                hash="base",
                timestamp=1.0,
                offset=100,
            ),
            b"a",
        )
        delta_one, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta-one",
                hash="delta-one",
                mode="delta",
                timestamp=2.0,
                offset=200,
            ),
            b"b",
        )
        delta_two, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta-two",
                hash="delta-two",
                mode="delta",
                timestamp=3.0,
                offset=300,
            ),
            b"c",
        )
        mark_job_failed(delta_one, error_type="test", attempts=1, root=self.root)

        self.assertEqual(select_ready_source_head(delta_two, self.root), (full, ()))
        complete_and_remove_job(full, document_id="document-id", root=self.root)
        self.assertEqual(select_ready_source_head(delta_two, self.root), (None, ()))
        self.assertNotIn(delta_two, ready_job_ids_in_recovery_order(self.root))

        rebase, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="rebase",
                hash="rebase",
                timestamp=4.0,
                offset=400,
            ),
            b"d",
        )
        head, superseded = select_ready_source_head(delta_two, self.root)
        self.assertEqual(head, rebase)
        self.assertEqual(set(superseded), {delta_one, delta_two})
        for job_id in superseded:
            mark_job_blocked(
                job_id,
                superseding_job_id=rebase,
                document_id="document-id",
                root=self.root,
            )
        self.assertEqual(set(blocked_job_ids(self.root)), {delta_one, delta_two})
        self.assertTrue((self.root / delta_one / "failed.json").is_file())
        self.assertTrue((self.root / delta_one).is_dir())
        self.assertTrue((self.root / delta_two).is_dir())
        self.assertEqual(select_ready_source_head(rebase, self.root), (rebase, ()))

    def test_failed_base_full_blocks_delta_without_a_later_full(self) -> None:
        base, _ = self._stage(
            self._meta(0, 1, upload_id="base", hash="base", timestamp=1.0),
            b"a",
        )
        delta, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta",
                hash="delta",
                mode="delta",
                timestamp=2.0,
                offset=200,
            ),
            b"b",
        )
        mark_job_failed(base, error_type="test", attempts=1, root=self.root)

        self.assertEqual(select_ready_source_head(delta, self.root), (None, ()))
        self.assertNotIn(delta, ready_job_ids_in_recovery_order(self.root))

    def test_corrupt_delta_is_ordered_then_becomes_a_failed_barrier(self) -> None:
        base, _ = self._stage(
            self._meta(0, 1, upload_id="base", hash="base", timestamp=1.0),
            b"a",
        )
        corrupt_delta, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="corrupt-delta",
                hash="corrupt-delta",
                mode="delta",
                timestamp=2.0,
                offset=200,
            ),
            b"b",
        )
        later_delta, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="later-delta",
                hash="later-delta",
                mode="delta",
                timestamp=3.0,
                offset=300,
            ),
            b"c",
        )
        (self.root / corrupt_delta / "chunk-000000.bin").unlink()

        self.assertEqual(
            ready_manifest_metadata(corrupt_delta, self.root)["job_id"], corrupt_delta
        )
        with self.assertRaises(ChunkValidationError):
            ready_manifest(corrupt_delta, self.root)
        complete_and_remove_job(base, document_id="document-id", root=self.root)
        self.assertEqual(
            ready_job_ids_in_recovery_order(self.root),
            [corrupt_delta],
        )
        self.assertEqual(
            select_ready_source_head(later_delta, self.root)[0],
            corrupt_delta,
        )

        mark_job_failed(
            corrupt_delta,
            error_type="ChunkValidationError",
            attempts=1,
            root=self.root,
        )
        self.assertEqual(select_ready_source_head(later_delta, self.root), (None, ()))

    def test_corrupt_base_full_blocks_delta_after_quarantine(self) -> None:
        corrupt_base, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="corrupt-base",
                hash="corrupt-base",
                timestamp=1.0,
            ),
            b"a",
        )
        delta, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta",
                hash="delta",
                mode="delta",
                timestamp=2.0,
                offset=200,
            ),
            b"b",
        )
        (self.root / corrupt_base / "chunk-000000.bin").write_bytes(b"too-long")

        self.assertEqual(select_ready_source_head(delta, self.root)[0], corrupt_base)
        mark_job_failed(
            corrupt_base,
            error_type="ChunkValidationError",
            attempts=1,
            root=self.root,
        )
        self.assertEqual(select_ready_source_head(delta, self.root), (None, ()))

    def test_newer_full_rebase_unblocks_delta_after_failed_full(self) -> None:
        failed_full, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="failed-full",
                hash="failed-full",
                timestamp=2.0,
                offset=200,
            ),
            b"a",
        )
        rebase, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="rebase",
                hash="rebase",
                timestamp=3.0,
                offset=300,
            ),
            b"b",
        )
        mark_job_failed(
            failed_full,
            error_type="test",
            attempts=1,
            root=self.root,
        )

        self.assertEqual(
            select_ready_source_head(rebase, self.root),
            (rebase, (failed_full,)),
        )
        mark_job_blocked(
            failed_full,
            superseding_job_id=rebase,
            document_id="document-id",
            root=self.root,
        )
        complete_and_remove_job(rebase, document_id="document-id", root=self.root)

        delta, _ = self._stage(
            self._meta(
                0,
                1,
                upload_id="delta",
                hash="delta",
                mode="delta",
                timestamp=4.0,
                offset=400,
            ),
            b"c",
        )
        self.assertEqual(select_ready_source_head(delta, self.root), (delta, ()))

    def test_recovery_queues_one_head_per_source_and_malformed_jobs(self) -> None:
        older, _ = self._stage(
            self._meta(0, 1, upload_id="old", hash="old", timestamp=1.0),
            b"a",
        )
        newer, _ = self._stage(
            self._meta(0, 1, upload_id="new", hash="new", timestamp=2.0),
            b"b",
        )
        malformed, _ = self._stage(
            self._meta(0, 1, upload_id="bad", hash="bad", timestamp=3.0),
            b"c",
        )
        (self.root / malformed / "manifest.json").write_text("{}", encoding="utf-8")

        self.assertEqual(
            ready_job_ids_in_recovery_order(self.root),
            [malformed, newer],
        )
        self.assertIn(older, ready_job_ids(self.root))

    def test_periodic_recovery_expires_before_the_next_tick(self) -> None:
        options = celery_app.conf.beat_schedule["ingest-spool-recovery"]["options"]

        self.assertEqual(
            options,
            {
                "queue": "ingest",
                "expires": INGEST_RECOVERY_EXPIRES_SECONDS,
            },
        )
        self.assertGreater(INGEST_RECOVERY_EXPIRES_SECONDS, 0)
        self.assertLess(INGEST_RECOVERY_EXPIRES_SECONDS, 5 * 60)

    def test_recovery_children_expire_without_mutating_durable_work(self) -> None:
        job_id, _ = self._stage(
            self._meta(0, 1, upload_id="recover", hash="recover"),
            b"a",
        )

        with (
            patch(
                "server.tasks.ingest_spool.ready_job_ids_in_recovery_order",
                side_effect=lambda: ready_job_ids_in_recovery_order(self.root),
            ),
            patch(
                "server.tasks.ingest_spool.cleanup_stale_incomplete_jobs",
                return_value=0,
            ),
            patch(
                "server.tasks.ingest_spool.cleanup_completion_receipts",
                return_value=0,
            ),
            patch("server.tasks.ingest_spool.failed_job_ids", return_value=[]),
            patch("server.tasks.ingest_spool.blocked_job_ids", return_value=[]),
            patch(
                "server.tasks.ingest_spool.process_spooled_ingest.apply_async"
            ) as apply_async,
        ):
            first = recover_spooled_ingests.run()
            second = recover_spooled_ingests.run()

        expected_dispatch = call(
            args=[job_id],
            queue="ingest",
            expires=INGEST_RECOVERY_EXPIRES_SECONDS,
        )
        self.assertEqual(apply_async.call_args_list, [expected_dispatch] * 2)
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 1)
        self.assertEqual(ready_job_ids(self.root), [job_id])

    def test_source_lock_is_shared_by_every_job_for_one_path(self) -> None:
        job_id, _ = self._stage(
            self._meta(0, 1, upload_id="lock", hash="lock", timestamp=1.0),
            b"a",
        )
        identity = source_identity(ready_manifest(job_id, self.root))

        with spool_source_lock(identity, root=self.root):
            with spool_source_lock(
                identity,
                root=self.root,
                blocking=False,
            ) as acquired:
                self.assertFalse(acquired)

    def test_completed_full_cohort_receives_receipts_before_removal(self) -> None:
        first, _ = self._stage(
            self._meta(0, 1, upload_id="first", hash="first", timestamp=1.0),
            b"a",
        )
        second, _ = self._stage(
            self._meta(0, 1, upload_id="second", hash="second", timestamp=2.0),
            b"b",
        )

        self.assertTrue(
            complete_and_remove_job(
                first,
                document_id="document-id",
                root=self.root,
            )
        )
        self.assertFalse((self.root / first).exists())
        self.assertTrue((self.root / "completed" / f"{first}.json").is_file())
        self.assertTrue((self.root / second).is_dir())

    def test_stale_cleanup_removes_only_incomplete_expired_jobs(self) -> None:
        stale_job, complete = self._stage(
            self._meta(0, 2, upload_id="stale", file_size=10),
            b"first",
        )
        self.assertFalse(complete)
        fresh_job, complete = self._stage(
            self._meta(0, 2, upload_id="fresh", file_size=10),
            b"first",
        )
        self.assertFalse(complete)
        ready_job, complete = self._stage(
            self._meta(0, 1, upload_id="ready", file_size=5),
            b"ready",
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
            self._meta(0, 2, upload_id="short", file_size=3),
            b"a",
        )
        self.assertFalse(complete)

        with self.assertRaisesRegex(ChunkValidationError, "declared file_size"):
            self._stage(
                self._meta(1, 2, upload_id="short", file_size=3),
                b"b",
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
                self._meta(0, 1, upload_id="quota", file_size=4),
                b"data",
            )

        job_dirs = [
            path
            for path in self.root.iterdir()
            if path.is_dir() and path.name != "completed"
        ]
        self.assertEqual(len(job_dirs), 1)
        self.assertEqual(list(job_dirs[0].glob("chunk-*.bin")), [])

    def test_failed_job_is_quarantined_from_recovery(self) -> None:
        job_id, complete = self._stage(
            self._meta(0, 1, upload_id="poison", file_size=4),
            b"data",
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
            self._meta(0, 1, upload_id="attempts", file_size=4),
            b"data",
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
