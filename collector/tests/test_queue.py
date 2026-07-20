from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.queue import SyncQueue  # noqa: E402


class SyncQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db_path = self.root / "sync_queue.db"
        self.queue = SyncQueue(self.db_path, spool_threshold=64 * 1024)

    def tearDown(self) -> None:
        self.queue.close()
        self._temporary.cleanup()

    def _enqueue(self, path: str, content: str, content_hash: str,
                 strategy: str = "full", partial: bool = False,
                 offset: int = 0,
                 source_modified_at: float | None = None,
                 base_hash: str | None = None,
                 base_offset: int = 0,
                 source_path: str | None = None) -> int:
        return self.queue.enqueue(
            tool_name="codex",
            category="conversation",
            content_type="jsonl",
            relative_path=path,
            content=content,
            content_hash=content_hash,
            file_size=len(content),
            sync_strategy=strategy,
            is_partial=partial,
            offset=offset,
            metadata={"title": content_hash},
            source_modified_at=source_modified_at,
            base_hash=base_hash,
            base_offset=base_offset,
            source_path=source_path,
        )

    def _status_rows(self) -> list[tuple]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute(
                "SELECT id, content_hash, status, payload_path FROM queue ORDER BY id"
            ).fetchall()

    @staticmethod
    def _metadata_record(
        thread_id: str,
        title: str,
        revision: int,
        *,
        title_kind: str | None = None,
    ) -> dict[str, dict]:
        record = {
            "metadata_type": "codex_thread_title",
            "tool": "codex",
            "thread_id": thread_id,
            "title": title,
            "revision": revision,
        }
        if title_kind:
            record["title_kind"] = title_kind
        return {thread_id: record}

    def _make_retries_available(self) -> None:
        with self.queue._lock:
            self.queue._conn.execute(
                "UPDATE queue SET available_at=0 WHERE status='pending'"
            )
            self.queue._conn.commit()

    def test_pending_full_snapshots_coalesce_and_replace_spool(self) -> None:
        first_content = "a" * 70_000
        second_content = "b" * 75_000
        first_id = self._enqueue("sessions/thread.jsonl", first_content, "hash-1")
        first_row = self._status_rows()[0]
        first_spool = Path(first_row[3])
        with closing(sqlite3.connect(self.db_path)) as connection:
            first_created_at = connection.execute(
                "SELECT created_at FROM queue WHERE id=?", (first_id,)
            ).fetchone()[0]
        self.assertTrue(first_spool.exists())

        second_id = self._enqueue("sessions/thread.jsonl", second_content, "hash-2")

        self.assertEqual(first_id, second_id)
        self.assertFalse(first_spool.exists())
        self.assertEqual(self.queue.pending_count(), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            second_created_at = connection.execute(
                "SELECT created_at FROM queue WHERE id=?", (second_id,)
            ).fetchone()[0]
        self.assertEqual(second_created_at, first_created_at)
        item = self.queue.claim_batch(max_bytes=200_000)[0]
        self.assertIsNone(item.content)
        self.assertEqual(item.content_hash, "hash-2")
        self.assertEqual(self.queue.read_payload_text(item), second_content)

    def test_source_mtime_survives_spool_coalescing_and_retry(self) -> None:
        first_id = self._enqueue(
            "sessions/thread.jsonl",
            "a" * 70_000,
            "hash-1",
            source_modified_at=1_700_000_001.25,
        )
        second_id = self._enqueue(
            "sessions/thread.jsonl",
            "b" * 75_000,
            "hash-2",
            source_modified_at=1_700_000_099.75,
        )

        self.assertEqual(second_id, first_id)
        self.queue.close()
        self.queue = SyncQueue(self.db_path, spool_threshold=64 * 1024)
        first_claim = self.queue.claim_batch(max_bytes=200_000)[0]
        self.assertEqual(first_claim.source_modified_at, 1_700_000_099.75)
        self.assertTrue(self.queue.mark_failed(first_claim, "temporary outage"))
        self._make_retries_available()

        retry = self.queue.claim_batch(max_bytes=200_000)[0]
        self.assertEqual(retry.source_modified_at, 1_700_000_099.75)
        self.assertEqual(self.queue.read_payload_text(retry), "b" * 75_000)

    def test_uploading_full_is_immutable_and_newer_snapshot_waits(self) -> None:
        self._enqueue("sessions/thread.jsonl", "old", "hash-1")
        old = self.queue.claim_batch()[0]
        self._enqueue("sessions/thread.jsonl", "new", "hash-2")

        self.assertEqual(self.queue.claim_batch(), [])
        self.assertTrue(self.queue.mark_failed(old, "superseded"))
        newer = self.queue.claim_batch()[0]
        self.assertEqual(newer.content_hash, "hash-2")
        self.assertTrue(self.queue.mark_synced(newer))

        rows = self._status_rows()
        self.assertEqual([row[2] for row in rows], ["superseded", "synced"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            state = connection.execute(
                "SELECT observed_hash, synced_hash, last_synced_at FROM file_state"
            ).fetchone()
        self.assertEqual(state[0], "hash-2")
        self.assertEqual(state[1], "hash-2")
        self.assertIsNotNone(state[2])

    def test_clear_file_state_forces_identical_complete_snapshot_to_requeue(self) -> None:
        relative_path = "sessions/thread.jsonl"
        self._enqueue(relative_path, "complete", "hash-1", "delta")
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))
        self.assertEqual(
            self._enqueue(relative_path, "complete", "hash-1", "delta"),
            0,
        )

        self.queue.clear_file_state("codex", relative_path)
        item_id = self._enqueue(relative_path, "complete", "hash-1", "delta")

        self.assertGreater(item_id, 0)
        self.assertEqual(self.queue.pending_count(), 1)
        self.assertEqual(
            self.queue.read_payload_text(self.queue.claim_batch()[0]),
            "complete",
        )

    def test_server_requested_repair_moves_ahead_of_ordinary_backlog(self) -> None:
        self._enqueue("sessions/ordinary.jsonl", "ordinary", "hash-ordinary")
        self._enqueue("sessions/repair.jsonl", "repair", "hash-repair")

        self.assertEqual(
            self.queue.prioritize_file("codex", "sessions/repair.jsonl"),
            1,
        )

        claimed = self.queue.claim_batch(batch_size=1)[0]
        self.assertEqual(claimed.relative_path, "sessions/repair.jsonl")

    def test_delta_rows_remain_fifo_and_one_per_path(self) -> None:
        self._enqueue("history.jsonl", "first", "hash-1", "delta", True, 10)
        self._enqueue("history.jsonl", "second", "hash-2", "delta", True, 20)

        first = self.queue.claim_batch()[0]
        self.assertEqual(first.offset, 10)
        self.assertEqual(self.queue.claim_batch(), [])
        self.assertTrue(self.queue.mark_synced(first))
        second = self.queue.claim_batch()[0]
        self.assertEqual(second.offset, 20)

    def test_pending_guarded_delta_coalesces_from_earliest_base(self) -> None:
        self._enqueue("history.jsonl", "base", "hash-0", "full", False, 10)
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))
        self.assertEqual(
            self.queue.get_delta_base("codex", "history.jsonl"),
            ("hash-0", 10),
        )

        first_id = self._enqueue(
            "history.jsonl", "tail-1", "hash-1", "delta", True, 20,
            base_hash="hash-0", base_offset=10, source_path="/tmp/history.jsonl",
        )
        self.assertEqual(
            self.queue.get_delta_base("codex", "history.jsonl"),
            ("hash-0", 10),
        )
        second_id = self._enqueue(
            "history.jsonl", "tail-1\ntail-2", "hash-2", "delta", True, 30,
            base_hash="hash-0", base_offset=10, source_path="/tmp/history.jsonl",
        )

        self.assertEqual(second_id, first_id)
        self.assertEqual(self.queue.pending_count(), 1)
        item = self.queue.claim_batch()[0]
        self.assertEqual(item.base_hash, "hash-0")
        self.assertEqual(item.base_offset, 10)
        self.assertEqual(item.source_path, "/tmp/history.jsonl")
        self.assertEqual(self.queue.read_payload_text(item), "tail-1\ntail-2")

    def test_delta_conflict_discards_chain_and_resets_to_synced_base(self) -> None:
        self._enqueue("history.jsonl", "base", "hash-0", "full", False, 10)
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))
        self._enqueue(
            "history.jsonl", "tail-1", "hash-1", "delta", True, 20,
            base_hash="hash-0", base_offset=10, source_path="/tmp/history.jsonl",
        )
        active = self.queue.claim_batch()[0]
        self._enqueue(
            "history.jsonl", "tail-2", "hash-2", "delta", True, 30,
            base_hash="hash-1", base_offset=20, source_path="/tmp/history.jsonl",
        )

        self.assertTrue(self.queue.mark_delta_conflict(active))
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(
            self.queue.get_file_state("codex", "history.jsonl"),
            ("hash-0", 10),
        )
        self.assertEqual(
            [row[2] for row in self._status_rows()],
            ["synced", "superseded", "superseded"],
        )

    def test_mixed_strategies_cannot_claim_same_path_concurrently(self) -> None:
        self._enqueue("same.jsonl", "delta", "hash-delta", "delta", True, 10)
        self._enqueue("same.jsonl", "full", "hash-full", "full", False, 20)

        first_batch = self.queue.claim_batch(batch_size=10)
        self.assertEqual(len(first_batch), 1)
        self.assertEqual(first_batch[0].sync_strategy, "full")
        self.assertEqual(self.queue.claim_batch(batch_size=10), [])
        self.assertTrue(self.queue.mark_synced(first_batch[0]))
        self.assertEqual(
            [row[2] for row in self._status_rows()],
            ["superseded", "synced"],
        )

    def test_claim_is_metadata_only_and_enforces_global_byte_budget(self) -> None:
        self._enqueue("one.jsonl", "a" * 70_000, "hash-1")
        self._enqueue("two.jsonl", "b" * 70_000, "hash-2")

        first_batch = self.queue.claim_batch(batch_size=10, max_bytes=100_000)
        self.assertEqual(len(first_batch), 1)
        self.assertIsNone(first_batch[0].content)
        self.assertEqual(self.queue.claim_batch(batch_size=10, max_bytes=100_000), [])
        self.assertTrue(self.queue.mark_synced(first_batch[0]))
        self.assertEqual(len(self.queue.claim_batch(batch_size=10, max_bytes=100_000)), 1)

    def test_oversize_payload_is_claimed_alone(self) -> None:
        self._enqueue("large.jsonl", "a" * 150_000, "hash-large")
        self._enqueue("small.jsonl", "small", "hash-small")

        batch = self.queue.claim_batch(batch_size=10, max_bytes=100_000)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].content_hash, "hash-large")

    def test_repeated_large_full_updates_keep_one_spool_payload(self) -> None:
        for version in range(50):
            content = chr(65 + version % 26) * 70_000
            self._enqueue("active.jsonl", content, f"hash-{version}")

        self.assertEqual(self.queue.pending_count(), 1)
        self.assertEqual(len(list((self.root / "spool").glob("*.payload"))), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT COUNT(*), length(content), content_hash FROM queue"
            ).fetchone()
        self.assertEqual(row, (1, 0, "hash-49"))

    def test_stale_lease_cannot_ack_reclaimed_item(self) -> None:
        self._enqueue("thread.jsonl", "content", "hash-1")
        stale = self.queue.claim_batch(lease_seconds=0)[0]
        current = self.queue.claim_batch()[0]

        self.assertNotEqual(stale.lease_token, current.lease_token)
        self.assertFalse(self.queue.mark_synced(stale))
        self.assertTrue(self.queue.mark_synced(current))

    def test_restart_does_not_steal_a_live_lease(self) -> None:
        self._enqueue("thread.jsonl", "content", "hash-1")
        leased = self.queue.claim_batch(lease_seconds=300)[0]
        self.queue.close()
        self.queue = SyncQueue(self.db_path, spool_threshold=64 * 1024)

        self.assertEqual(self.queue.claim_batch(), [])
        self.assertTrue(self.queue.renew_lease(leased))

    def test_observation_does_not_claim_sync_success(self) -> None:
        self._enqueue("thread.jsonl", "content", "hash-1")
        with closing(sqlite3.connect(self.db_path)) as connection:
            before = connection.execute(
                "SELECT observed_hash, synced_hash, last_synced_at FROM file_state"
            ).fetchone()
        self.assertEqual(before, ("hash-1", None, None))

        item = self.queue.claim_batch()[0]
        self.assertTrue(self.queue.mark_synced(item))
        with closing(sqlite3.connect(self.db_path)) as connection:
            after = connection.execute(
                "SELECT observed_hash, synced_hash, last_synced_at FROM file_state"
            ).fetchone()
            retained_content = connection.execute(
                "SELECT length(content) FROM queue"
            ).fetchone()[0]
        self.assertEqual(after[0:2], ("hash-1", "hash-1"))
        self.assertIsNotNone(after[2])
        self.assertEqual(retained_content, 0)

    def test_full_retries_indefinitely_without_losing_payload(self) -> None:
        self._enqueue("broken.jsonl", "x" * 70_000, "hash-broken")
        for _attempt in range(12):
            item = self.queue.claim_batch()[0]
            self.assertTrue(self.queue.mark_failed(item, "server unavailable"))
            self._make_retries_available()

        row = self._status_rows()[0]
        self.assertEqual(row[2], "pending")
        self.assertIsNotNone(row[3])
        self.assertEqual(len(list((self.root / "spool").glob("*.payload"))), 1)
        self.assertEqual(
            self.queue.get_file_state("codex", "broken.jsonl"),
            ("hash-broken", 0),
        )

    def test_delta_failure_never_skips_a_missing_segment(self) -> None:
        self._enqueue("history.jsonl", "delta", "hash-delta", "delta", True, 5)
        for _attempt in range(12):
            item = self.queue.claim_batch()[0]
            self.assertTrue(self.queue.mark_failed(item, "temporary rejection"))
            self._make_retries_available()

        self.assertEqual(self._status_rows()[0][2], "pending")

    def test_metadata_transition_is_durable_and_acknowledged_separately(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        first = {
            thread_id: {
                "metadata_type": "codex_thread_title",
                "tool": "codex",
                "thread_id": thread_id,
                "title": "Original title",
                "revision": 1000,
            }
        }
        self.assertEqual(self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex", records=first,
        ), 1)
        self.assertEqual(self.queue.pending_count(), 1)
        first_item = self.queue.claim_batch()[0]
        self.assertEqual(first_item.metadata["title"], "Original title")
        with closing(sqlite3.connect(self.db_path)) as connection:
            first_state = connection.execute(
                """SELECT observed_value, synced_value FROM metadata_state
                   WHERE namespace='codex_thread_titles' AND item_key=?""",
                (thread_id,),
            ).fetchone()
        self.assertEqual(first_state, ("Original title", ""))
        self.assertTrue(self.queue.mark_synced(first_item))

        renamed = {thread_id: {**first[thread_id], "title": "Renamed", "revision": 2000}}
        self.assertEqual(self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex", records=renamed,
        ), 1)
        item = self.queue.claim_batch()[0]
        self.assertEqual(item.sync_strategy, "metadata")
        self.assertEqual(item.payload_bytes, 0)
        self.assertEqual(item.metadata["title"], "Renamed")

        with closing(sqlite3.connect(self.db_path)) as connection:
            before = connection.execute(
                """SELECT observed_value, synced_value FROM metadata_state
                   WHERE namespace='codex_thread_titles' AND item_key=?""",
                (thread_id,),
            ).fetchone()
        self.assertEqual(before, ("Renamed", "Original title"))
        self.assertTrue(self.queue.mark_synced(item))
        with closing(sqlite3.connect(self.db_path)) as connection:
            after = connection.execute(
                """SELECT observed_value, synced_value FROM metadata_state
                   WHERE namespace='codex_thread_titles' AND item_key=?""",
                (thread_id,),
            ).fetchone()
        self.assertEqual(after, ("Renamed", "Renamed"))

    def test_metadata_is_claimed_before_an_older_large_payload(self) -> None:
        self._enqueue("sessions/large.jsonl", "x" * 150_000, "large-hash")
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(thread_id, "Urgent rename", 100),
        )

        first = self.queue.claim_batch(batch_size=1, max_bytes=100_000)[0]
        self.assertEqual(first.sync_strategy, "metadata")
        self.assertEqual(first.metadata["title"], "Urgent rename")
        self.assertTrue(self.queue.mark_synced(first))
        self.assertEqual(
            self.queue.claim_batch(batch_size=1, max_bytes=100_000)[0].content_hash,
            "large-hash",
        )

    def test_inflight_oversize_payload_does_not_block_metadata(self) -> None:
        self._enqueue("sessions/large.jsonl", "x" * 150_000, "large-hash")
        large = self.queue.claim_batch(batch_size=1, max_bytes=100_000)[0]
        self.assertEqual(large.content_hash, "large-hash")

        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(thread_id, "Rename while busy", 101),
        )

        metadata = self.queue.claim_batch(batch_size=1, max_bytes=100_000)[0]
        self.assertEqual(metadata.sync_strategy, "metadata")
        self.assertEqual(metadata.payload_bytes, 0)

    def test_live_delta_leapfrogs_historical_backlog(self) -> None:
        self._enqueue("archived/old.jsonl", "old", "old-hash")
        self._enqueue(
            "sessions/active.jsonl", "tail", "tail-hash", "delta", True, 20,
            base_hash="base-hash", base_offset=10,
            source_path="/tmp/sessions/active.jsonl",
        )

        first = self.queue.claim_batch(batch_size=1)[0]

        self.assertEqual(first.relative_path, "sessions/active.jsonl")
        self.assertTrue(first.is_partial)

    def test_live_delta_uses_reserved_lane_during_oversize_upload(self) -> None:
        self._enqueue("archived/large.jsonl", "x" * 150_000, "large-hash")
        large = self.queue.claim_batch(batch_size=1, max_bytes=100_000)[0]
        self.assertEqual(large.relative_path, "archived/large.jsonl")
        self._enqueue(
            "sessions/active.jsonl", "tail", "tail-hash", "delta", True, 20,
            base_hash="base-hash", base_offset=10,
            source_path="/tmp/sessions/active.jsonl",
        )

        live = self.queue.claim_batch(
            batch_size=1,
            max_bytes=100_000,
            live_delta_reserve_bytes=10_000,
        )[0]

        self.assertEqual(live.relative_path, "sessions/active.jsonl")
        self.assertTrue(live.is_partial)

    def test_live_delta_reserve_remains_bounded(self) -> None:
        self._enqueue("archived/large.jsonl", "x" * 150_000, "large-hash")
        self.queue.claim_batch(batch_size=1, max_bytes=100_000)
        self._enqueue(
            "sessions/active.jsonl", "tail" * 3_000, "tail-hash", "delta", True, 20,
            base_hash="base-hash", base_offset=10,
            source_path="/tmp/sessions/active.jsonl",
        )

        self.assertEqual(
            self.queue.claim_batch(
                batch_size=1,
                max_bytes=100_000,
                live_delta_reserve_bytes=10_000,
            ),
            [],
        )

    def test_metadata_priority_preserves_fifo_and_same_path_barrier(self) -> None:
        first_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        second_id = "019f144c-82d6-70d0-95e8-e01e7b813e99"
        records = {}
        records.update(self._metadata_record(first_id, "First", 1))
        records.update(self._metadata_record(second_id, "Second", 1))
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=records,
        )

        first = self.queue.claim_batch(batch_size=1)[0]
        self.assertEqual(first.metadata["thread_id"], first_id)
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(first_id, "First renamed again", 2),
        )

        # The other metadata path remains eligible, but the successor for the
        # active first path must wait behind its lease.
        second = self.queue.claim_batch(batch_size=1)[0]
        self.assertEqual(second.metadata["thread_id"], second_id)
        self.assertEqual(self.queue.claim_batch(batch_size=1), [])
        self.assertTrue(self.queue.mark_synced(first))
        successor = self.queue.claim_batch(batch_size=1)[0]
        self.assertEqual(successor.metadata["title"], "First renamed again")

    def test_synced_custom_title_suppresses_later_first_prompt_fallback(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "Initial prompt", 1, title_kind="fallback",
            ),
        )
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "netbird setup", 2, title_kind="custom",
            ),
        )
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))

        queued = self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "Initial prompt", 3, title_kind="fallback",
            ),
        )

        self.assertEqual(queued, 0)
        self.assertEqual(self.queue.pending_count(), 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            observed, synced = connection.execute(
                """SELECT observed_value, synced_value FROM metadata_state
                   WHERE namespace='codex_thread_titles' AND item_key=?""",
                (thread_id,),
            ).fetchone()
        self.assertIn("netbird setup", observed)
        self.assertEqual(observed, synced)

    def test_pending_custom_title_is_not_coalesced_back_to_fallback(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "Initial prompt", 1, title_kind="fallback",
            ),
        )
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "netbird setup", 2, title_kind="custom",
            ),
        )

        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "Initial prompt", 3, title_kind="fallback",
            ),
        )

        pending = self.queue.claim_batch()[0]
        self.assertEqual(pending.metadata["title"], "netbird setup")
        self.assertEqual(pending.metadata["title_kind"], "custom")

    def test_upgrade_recovers_custom_title_from_legacy_queue_history(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        # Simulate the deployed pre-title-kind collector: both values were
        # acknowledged, but the queue still retains the custom event history.
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(thread_id, "netbird setup", 1),
        )
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(thread_id, "Initial prompt", 2),
        )
        self.assertTrue(self.queue.mark_synced(self.queue.claim_batch()[0]))

        queued = self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=self._metadata_record(
                thread_id, "Initial prompt", 3, title_kind="fallback",
            ),
        )

        self.assertEqual(queued, 1)
        recovery = self.queue.claim_batch()[0]
        self.assertEqual(recovery.metadata["title"], "netbird setup")
        self.assertEqual(recovery.metadata["title_kind"], "custom")

    def test_metadata_pending_update_coalesces_and_revert_cancels_it(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"

        def record(title: str, revision: int) -> dict[str, dict]:
            return {thread_id: {
                "metadata_type": "codex_thread_title",
                "tool": "codex",
                "thread_id": thread_id,
                "title": title,
                "revision": revision,
            }}

        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex",
            records=record("A", 1),
        )
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex",
            records=record("B", 2),
        )
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex",
            records=record("C", 3),
        )
        self.assertEqual(self.queue.pending_count(), 1)
        self.assertEqual(self.queue.claim_batch()[0].metadata["title"], "C")

        # Use a second thread for the no-in-flight revert case.
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e99"
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex",
            records=record("A", 1),
        )
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex",
            records=record("B", 2),
        )
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex",
            records=record("A", 3),
        )
        self.assertEqual(self.queue.pending_count(), 2)
        self.assertEqual(self.queue.claim_batch()[0].metadata["title"], "A")

    def test_force_resync_requeues_unacknowledged_metadata(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        baseline = {thread_id: {
            "metadata_type": "codex_thread_title",
            "tool": "codex",
            "thread_id": thread_id,
            "title": "A",
            "revision": 1,
        }}
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex", records=baseline,
        )
        changed = {thread_id: {**baseline[thread_id], "title": "B", "revision": 2}}
        self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex", records=changed,
        )
        self.queue.clear_all_state()
        self.assertEqual(self.queue.pending_count(), 0)

        self.assertEqual(self.queue.enqueue_metadata_changes(
            namespace="codex_thread_titles", tool_name="codex", records=changed,
        ), 1)
        self.assertEqual(self.queue.claim_batch()[0].metadata["title"], "B")

    def test_recreated_queue_reconciles_first_observation_again(self) -> None:
        thread_id = "019f144c-82d6-70d0-95e8-e01e7b813e98"
        record = {thread_id: {
            "metadata_type": "codex_thread_title",
            "tool": "codex",
            "thread_id": thread_id,
            "title": "Already renamed before install",
            "revision": 123_456,
        }}
        rebuilt = SyncQueue(self.root / "rebuilt.db")
        try:
            self.assertEqual(rebuilt.enqueue_metadata_changes(
                namespace="codex_thread_titles",
                tool_name="codex",
                records=record,
            ), 1)
            item = rebuilt.claim_batch()[0]
            self.assertEqual(item.metadata["title"], "Already renamed before install")
            self.assertEqual(item.metadata["revision"], 123_456)
        finally:
            rebuilt.close()


class SyncQueueMigrationTests(unittest.TestCase):
    def test_v1_migration_preserves_deltas_and_deduplicates_pending_full(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "sync_queue.db"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.executescript("""
                    CREATE TABLE queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name TEXT NOT NULL, category TEXT NOT NULL,
                        content_type TEXT NOT NULL, relative_path TEXT NOT NULL,
                        content TEXT NOT NULL, content_hash TEXT NOT NULL,
                        file_size INTEGER NOT NULL, sync_strategy TEXT NOT NULL,
                        is_partial INTEGER NOT NULL DEFAULT 0,
                        offset INTEGER NOT NULL DEFAULT 0,
                        metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending'
                    );
                    CREATE TABLE file_state (
                        tool_name TEXT NOT NULL, relative_path TEXT NOT NULL,
                        last_hash TEXT, last_offset INTEGER NOT NULL DEFAULT 0,
                        last_synced_at REAL,
                        PRIMARY KEY (tool_name, relative_path)
                    );
                """)
                rows = [
                    ("full.jsonl", "old", "full", 0, 1.0),
                    ("full.jsonl", "new", "full", 0, 2.0),
                    ("delta.jsonl", "one", "delta", 1, 3.0),
                    ("delta.jsonl", "two", "delta", 1, 4.0),
                ]
                for path, content_hash, strategy, partial, created_at in rows:
                    connection.execute(
                        """INSERT INTO queue (
                           tool_name, category, content_type, relative_path, content,
                           content_hash, file_size, sync_strategy, is_partial,
                           metadata, created_at
                           ) VALUES ('codex','conversation','jsonl',?,?,?,1,?,?, '{}',?)""",
                        (path, content_hash, content_hash, strategy, partial, created_at),
                    )
                connection.execute(
                    """INSERT INTO file_state
                       VALUES ('codex','full.jsonl','new',42,123.0)"""
                )
                connection.commit()

            queue = SyncQueue(db_path)
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    full_statuses = connection.execute(
                        """SELECT status FROM queue
                           WHERE relative_path='full.jsonl' ORDER BY id"""
                    ).fetchall()
                    delta_statuses = connection.execute(
                        """SELECT status FROM queue
                           WHERE relative_path='delta.jsonl' ORDER BY id"""
                    ).fetchall()
                    state = connection.execute(
                        "SELECT observed_hash, observed_offset, synced_hash FROM file_state"
                    ).fetchone()
                    source_timestamps = connection.execute(
                        "SELECT source_modified_at FROM queue ORDER BY id"
                    ).fetchall()
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(full_statuses, [("superseded",), ("pending",)])
                self.assertEqual(delta_statuses, [("pending",), ("pending",)])
                self.assertEqual(state, ("new", 42, None))
                self.assertEqual(source_timestamps, [(None,), (None,), (None,), (None,)])
                self.assertEqual(version, SyncQueue.SCHEMA_VERSION)
                self.assertTrue(all(
                    item.source_modified_at is None
                    for item in queue.claim_batch(batch_size=10)
                ))
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
