from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.db.models import Document, SyncState  # noqa: E402
from server.services.ingest_service import (  # noqa: E402
    DeltaBaseMismatch,
    _merge_delta_metadata,
    _set_stored_source_identity,
    ingest_file,
)


class _ScalarResult:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _OrderedSession:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.statements = []
        self.added = []

    async def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if not self.results:
            raise AssertionError("unexpected database execute")
        return _ScalarResult(self.results.pop(0))

    def add(self, value) -> None:
        self.added.append(value)


def _document(*, content_hash: str, timestamp: float, offset: int = 100) -> Document:
    document = Document(
        id=uuid.uuid4(),
        tool_id="codex",
        machine_id=uuid.uuid4(),
        relative_path="sessions/thread.jsonl",
        category="conversation",
        content_type="jsonl",
        content="full",
        content_hash=content_hash,
        file_size_bytes=offset,
        metadata_={},
        needs_review=False,
        source_modified_at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
    )
    _set_stored_source_identity(
        document,
        document.content,
        revision_hash=content_hash,
    )
    return document


def _sync_state(doc: Document, *, offset: int) -> SyncState:
    return SyncState(
        machine_id=doc.machine_id,
        tool_id=doc.tool_id,
        relative_path=doc.relative_path,
        last_hash=doc.content_hash,
        last_offset=offset,
    )


def _ingest_kwargs(doc: Document, **overrides) -> dict:
    kwargs = {
        "tool_id": doc.tool_id,
        "category": doc.category,
        "content_type": doc.content_type,
        "relative_path": doc.relative_path,
        "content": "incoming",
        "content_hash": "incoming-hash",
        "file_size": 200,
        "mode": "full",
        "offset": 200,
        "metadata": {},
        "timestamp": 200.0,
        "machine_id": str(doc.machine_id),
        "user_id": str(uuid.uuid4()),
        "schedule_post_ingest": False,
    }
    kwargs.update(overrides)
    return kwargs


class IngestOrderingTests(unittest.IsolatedAsyncioTestCase):
    def test_delta_metadata_accumulates_counts_and_preserves_first_timestamp(
        self,
    ) -> None:
        merged = _merge_delta_metadata(
            {
                "total_lines": 10,
                "message_types": {"event_msg": 7, "response_item": 3},
                "first_timestamp": "first",
                "last_timestamp": "old-last",
            },
            {
                "total_lines": 4,
                "message_types": {"event_msg": 1, "response_item": 3},
                "first_timestamp": "tail-first",
                "last_timestamp": "new-last",
            },
        )

        self.assertEqual(merged["total_lines"], 14)
        self.assertEqual(
            merged["message_types"],
            {
                "event_msg": 8,
                "response_item": 6,
            },
        )
        self.assertEqual(merged["first_timestamp"], "first")
        self.assertEqual(merged["last_timestamp"], "new-last")

    async def test_committed_newer_full_rejects_older_full_under_source_lock(
        self,
    ) -> None:
        doc = _document(content_hash="newer-hash", timestamp=300.0, offset=300)
        sync = _sync_state(doc, offset=300)
        db = _OrderedSession(None, sync, doc)

        result = await ingest_file(
            db,
            **_ingest_kwargs(
                doc,
                content_hash="older-hash",
                file_size=100,
                offset=100,
                timestamp=100.0,
            ),
        )

        self.assertIs(result, doc)
        self.assertEqual(doc.content_hash, "newer-hash")
        self.assertEqual(getattr(doc, "_memento_ingest_disposition"), "superseded")
        self.assertIn("pg_advisory_xact_lock", str(db.statements[0][0]))

    async def test_authoritative_rebase_can_replace_a_higher_committed_offset(
        self,
    ) -> None:
        doc = _document(content_hash="newer-hash", timestamp=300.0, offset=300)
        sync = _sync_state(doc, offset=300)
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("authoritative processing reached")),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "authoritative processing reached",
            ):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="bounded-rebase-hash",
                        file_size=100,
                        offset=100,
                        timestamp=100.0,
                        authoritative_rebase=True,
                    ),
                )

    async def test_same_hash_full_repairs_offset_and_advances_source_time(self) -> None:
        doc = _document(content_hash="same-hash", timestamp=100.0)
        sync = _sync_state(doc, offset=0)
        db = _OrderedSession(None, sync, doc, sync)

        result = await ingest_file(
            db,
            **_ingest_kwargs(
                doc,
                content_hash="same-hash",
                file_size=300,
                offset=300,
                timestamp=300.0,
            ),
        )

        self.assertIs(result, doc)
        self.assertEqual(sync.last_offset, 300)
        self.assertEqual(sync.last_hash, "same-hash")
        self.assertEqual(
            doc.source_modified_at,
            datetime.fromtimestamp(300.0, tz=timezone.utc),
        )
        self.assertEqual(getattr(doc, "_memento_ingest_disposition"), "idempotent")

    async def test_future_source_time_is_bounded_by_server_receipt(self) -> None:
        doc = _document(content_hash="same-hash", timestamp=100.0)
        sync = _sync_state(doc, offset=0)
        db = _OrderedSession(None, sync, doc, sync)
        before = datetime.now(timezone.utc)

        result = await ingest_file(
            db,
            **_ingest_kwargs(
                doc,
                content_hash="same-hash",
                file_size=300,
                offset=300,
                timestamp=4_102_444_800.0,
            ),
        )
        after = datetime.now(timezone.utc)

        self.assertIs(result, doc)
        self.assertGreaterEqual(doc.source_modified_at, before)
        self.assertLessEqual(doc.source_modified_at, after)

    async def test_preexisting_future_source_time_no_longer_blocks_full(self) -> None:
        doc = _document(
            content_hash="poisoned-hash",
            timestamp=4_102_444_800.0,
            offset=100,
        )
        doc.synced_at = datetime.fromtimestamp(150.0, tz=timezone.utc)
        sync = _sync_state(doc, offset=100)
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("newer full accepted")),
        ):
            with self.assertRaisesRegex(RuntimeError, "newer full accepted"):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="valid-newer-hash",
                        file_size=200,
                        offset=200,
                        timestamp=200.0,
                    ),
                )

        self.assertEqual(
            doc.source_modified_at,
            datetime.fromtimestamp(150.0, tz=timezone.utc),
        )

    async def test_stale_same_hash_full_cannot_downgrade_offset_or_source_time(
        self,
    ) -> None:
        doc = _document(content_hash="same-hash", timestamp=300.0, offset=300)
        sync = _sync_state(doc, offset=300)
        db = _OrderedSession(None, sync, doc, sync)

        result = await ingest_file(
            db,
            **_ingest_kwargs(
                doc,
                content_hash="same-hash",
                file_size=100,
                offset=100,
                timestamp=100.0,
            ),
        )

        self.assertIs(result, doc)
        self.assertEqual(sync.last_offset, 300)
        self.assertEqual(
            doc.source_modified_at,
            datetime.fromtimestamp(300.0, tz=timezone.utc),
        )

    async def test_externalized_same_hash_wrong_pointer_enters_repair_path(
        self,
    ) -> None:
        doc = _document(content_hash="same-hash", timestamp=100.0)
        doc.content = None
        doc.content_s3_key = "raw/user/device/old-full.txt"
        sync = _sync_state(doc, offset=100)
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("repair path reached")),
        ):
            with self.assertRaisesRegex(RuntimeError, "repair path reached"):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="same-hash",
                        file_size=100,
                        offset=100,
                        timestamp=300.0,
                        persist_content=False,
                        content_s3_key="raw/user/device/new-full.txt",
                        content_already_sanitized=True,
                    ),
                )

    async def test_same_hash_full_repairs_unverified_inline_snapshot(self) -> None:
        doc = _document(content_hash="same-hash", timestamp=100.0)
        doc.metadata_ = {}
        sync = _sync_state(doc, offset=100)
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("repair path reached")),
        ):
            with self.assertRaisesRegex(RuntimeError, "repair path reached"):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="same-hash",
                        file_size=100,
                        offset=100,
                        timestamp=300.0,
                    ),
                )

    async def test_mismatched_sync_state_cannot_discard_needed_content(self) -> None:
        doc = _document(content_hash="committed-hash", timestamp=100.0)
        sync = _sync_state(doc, offset=200)
        sync.last_hash = "incoming-hash"
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("processing path reached")),
        ):
            with self.assertRaisesRegex(RuntimeError, "processing path reached"):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="incoming-hash",
                        file_size=200,
                        offset=200,
                        timestamp=200.0,
                    ),
                )

    async def test_mismatched_sync_hash_cannot_discard_delta_by_offset(self) -> None:
        doc = _document(content_hash="committed-hash", timestamp=100.0)
        sync = _sync_state(doc, offset=999)
        sync.last_hash = "uncommitted-hash"
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("delta processing reached")),
        ):
            with self.assertRaisesRegex(RuntimeError, "delta processing reached"):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="delta-hash",
                        file_size=10,
                        mode="delta",
                        offset=200,
                        timestamp=200.0,
                    ),
                )

    async def test_guarded_delta_rejects_mismatched_committed_base(self) -> None:
        doc = _document(content_hash="committed-hash", timestamp=100.0, offset=100)
        sync = _sync_state(doc, offset=100)
        db = _OrderedSession(None, sync, doc)

        with self.assertRaises(DeltaBaseMismatch) as raised:
            await ingest_file(
                db,
                **_ingest_kwargs(
                    doc,
                    content_hash="delta-hash",
                    file_size=10,
                    mode="delta",
                    offset=110,
                    base_hash="wrong-hash",
                    base_offset=100,
                ),
            )

        self.assertEqual(raised.exception.expected_hash, "committed-hash")
        self.assertEqual(raised.exception.expected_offset, 100)
        self.assertEqual(doc.content_hash, "committed-hash")

    async def test_guarded_delta_accepts_exact_committed_base(self) -> None:
        doc = _document(content_hash="committed-hash", timestamp=100.0, offset=100)
        sync = _sync_state(doc, offset=100)
        db = _OrderedSession(None, sync, doc)

        with patch(
            "server.services.ingest_service.ensure_tool",
            new=AsyncMock(side_effect=RuntimeError("guard passed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "guard passed"):
                await ingest_file(
                    db,
                    **_ingest_kwargs(
                        doc,
                        content_hash="delta-hash",
                        file_size=10,
                        mode="delta",
                        offset=110,
                        base_hash="committed-hash",
                        base_offset=100,
                    ),
                )

    async def test_delayed_guarded_delta_behind_committed_offset_is_stale(self) -> None:
        doc = _document(content_hash="current-hash", timestamp=200.0, offset=120)
        sync = _sync_state(doc, offset=120)
        db = _OrderedSession(None, sync, doc)

        result = await ingest_file(
            db,
            **_ingest_kwargs(
                doc,
                content_hash="older-delta-hash",
                file_size=10,
                mode="delta",
                offset=110,
                base_hash="older-base-hash",
                base_offset=100,
                timestamp=150.0,
            ),
        )

        self.assertIs(result, doc)
        self.assertEqual(getattr(doc, "_memento_ingest_disposition"), "stale_delta")
        self.assertEqual(doc.content_hash, "current-hash")

    async def test_guarded_delta_retry_is_idempotent_after_lost_response(self) -> None:
        doc = _document(content_hash="delta-hash", timestamp=200.0, offset=110)
        sync = _sync_state(doc, offset=110)
        db = _OrderedSession(None, sync, doc)

        result = await ingest_file(
            db,
            **_ingest_kwargs(
                doc,
                content_hash="delta-hash",
                file_size=10,
                mode="delta",
                offset=110,
                base_hash="committed-hash",
                base_offset=100,
            ),
        )

        self.assertIs(result, doc)
        self.assertEqual(getattr(doc, "_memento_ingest_disposition"), "idempotent")


if __name__ == "__main__":
    unittest.main()
