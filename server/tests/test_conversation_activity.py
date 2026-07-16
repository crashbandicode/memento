from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_activity import (  # noqa: E402
    ConversationActivitySummary,
    conversation_activity_summaries,
    conversation_list_timestamp_expression,
    conversation_activity_at_query,
    effective_conversation_activity,
    historical_conversation_activity_query,
    is_low_activity_messages,
    is_low_activity_summary,
    refresh_document_activity_at,
)
from server.api.hierarchy import _device_file_row  # noqa: E402
from server.api.tools import _document_summary  # noqa: E402
from server.db.models import Document  # noqa: E402


class ConversationActivityTests(unittest.TestCase):
    def test_effective_activity_prefers_real_turn_timestamp(self) -> None:
        activity = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)
        source_modified = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
        synced = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)

        self.assertEqual(
            effective_conversation_activity(activity, source_modified, synced),
            activity,
        )

    def test_effective_activity_bounds_future_source_mtime(self) -> None:
        source_modified = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        synced = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)

        self.assertEqual(
            effective_conversation_activity(None, source_modified, synced),
            synced,
        )

    def test_list_timestamp_expression_distinguishes_conversations(self) -> None:
        expression = conversation_list_timestamp_expression(
            Document.category,
            Document.activity_at,
            Document.source_modified_at,
            Document.synced_at,
        )
        sql = str(select(expression).compile())

        self.assertIn("documents.category =", sql)
        self.assertIn("coalesce(documents.activity_at", sql)
        self.assertIn("ELSE documents.synced_at", sql)

    def test_list_timestamp_expression_orders_effective_activity(self) -> None:
        metadata = MetaData()
        rows = Table(
            "activity_rows",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("category", String, nullable=False),
            Column("activity_at", DateTime),
            Column("source_modified_at", DateTime),
            Column("synced_at", DateTime, nullable=False),
        )
        engine = create_engine("sqlite://")
        metadata.create_all(engine)
        may = datetime(2026, 5, 15, 12)
        june = datetime(2026, 6, 24, 12)
        july = datetime(2026, 7, 7, 12)
        with engine.begin() as connection:
            connection.execute(insert(rows), [
                {
                    "id": 1,
                    "category": "conversation",
                    "activity_at": None,
                    "source_modified_at": may,
                    "synced_at": july,
                },
                {
                    "id": 2,
                    "category": "conversation",
                    "activity_at": june,
                    "source_modified_at": may,
                    "synced_at": july,
                },
                {
                    "id": 3,
                    "category": "memory",
                    "activity_at": None,
                    "source_modified_at": may,
                    "synced_at": july,
                },
            ])
            display_timestamp = conversation_list_timestamp_expression(
                rows.c.category,
                rows.c.activity_at,
                rows.c.source_modified_at,
                rows.c.synced_at,
            )
            ordered_ids = connection.execute(
                select(rows.c.id).order_by(display_timestamp.desc(), rows.c.id.desc())
            ).scalars().all()

        self.assertEqual(ordered_ids, [3, 2, 1])

    def test_tool_only_and_one_sided_threads_are_low_activity(self) -> None:
        self.assertTrue(is_low_activity_summary(0, 0, 0))
        self.assertTrue(is_low_activity_summary(1, 0, 500))
        self.assertTrue(is_low_activity_summary(0, 1, 500))

    def test_tiny_single_exchange_is_low_activity(self) -> None:
        self.assertTrue(is_low_activity_messages([
            {"role": "user", "content": "thanks"},
            {"role": "assistant", "content": "you're welcome"},
        ]))

    def test_substantive_single_exchange_stays_visible(self) -> None:
        self.assertFalse(is_low_activity_messages([
            {"role": "user", "content": "Explain the production failure " * 4},
            {"role": "assistant", "content": "The root cause is the routing configuration."},
        ]))

    def test_multi_turn_exchange_stays_visible(self) -> None:
        self.assertFalse(is_low_activity_summary(2, 2, 20))

    def test_activity_query_excludes_tool_and_system_rows(self) -> None:
        sql = str(conversation_activity_at_query("doc-id").compile())

        self.assertIn("max(conversation_messages.timestamp)", sql)
        self.assertIn("conversation_messages.timestamp IS NOT NULL", sql)
        self.assertIn("conversation_messages.role IN", sql)

    def test_snapshot_activity_query_caps_message_time(self) -> None:
        cutoff = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
        sql = str(
            historical_conversation_activity_query(
                ["first-doc", "second-doc"],
                cutoff,
            ).compile()
        )

        self.assertIn("max(conversation_messages.timestamp)", sql)
        self.assertIn("conversation_messages.timestamp <=", sql)
        self.assertIn("GROUP BY conversation_messages.document_id", sql)


class RefreshDocumentActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_persists_latest_real_message_time(self) -> None:
        expected = datetime(2026, 7, 8, 15, 30, tzinfo=timezone.utc)

        class Result:
            def scalar_one_or_none(self):
                return expected

        class Db:
            async def execute(self, _statement):
                return Result()

        document = SimpleNamespace(id="doc-id", activity_at=None)
        actual = await refresh_document_activity_at(Db(), document)

        self.assertEqual(actual, expected)
        self.assertEqual(document.activity_at, expected)

    async def test_page_summary_uses_one_grouped_query(self) -> None:
        document_id = "document-id"

        class Result:
            def all(self):
                return [(document_id, 7, 1, 2, 240)]

        class Db:
            def __init__(self):
                self.statements = []

            async def execute(self, statement):
                self.statements.append(statement)
                return Result()

        db = Db()
        summaries = await conversation_activity_summaries(
            db,
            [document_id, document_id],
        )

        self.assertEqual(
            summaries[document_id],
            ConversationActivitySummary(7, 1, 2, 240),
        )
        self.assertFalse(summaries[document_id].is_low_activity)
        self.assertEqual(len(db.statements), 1)
        sql = str(db.statements[0].compile())
        self.assertIn("GROUP BY conversation_messages.document_id", sql)


class ConversationBrowseActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.activity = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)
        self.source_modified = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
        self.synced = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)

    def _document(self, category: str) -> SimpleNamespace:
        return SimpleNamespace(
            id="document-id",
            tool_id="codex",
            machine_id="machine-id",
            relative_path="sessions/thread.jsonl",
            category=category,
            content_type="jsonl",
            title="Historical thread",
            file_size_bytes=123,
            activity_at=self.activity,
            source_modified_at=self.source_modified,
            synced_at=self.synced,
            ai_summary=None,
            metadata_={},
        )

    def _device_row(self, category: str) -> tuple:
        document = self._document(category)
        return (
            document.id,
            document.title,
            document.relative_path,
            document.category,
            document.content_type,
            document.file_size_bytes,
            document.activity_at,
            document.source_modified_at,
            document.synced_at,
        )

    def test_tool_file_summary_exposes_effective_conversation_activity(self) -> None:
        summary = _document_summary(
            self._document("conversation"),
            {"machine-id": "dreamland-yoga (Linux)"},
        )

        self.assertEqual(summary.activity_at, self.activity.isoformat())
        self.assertEqual(summary.synced_at, self.synced.isoformat())
        self.assertEqual(summary.device_name, "dreamland-yoga (Linux)")

    def test_device_file_row_exposes_effective_conversation_activity(self) -> None:
        row = _device_file_row(self._device_row("conversation"))

        self.assertEqual(row["activity_at"], self.activity.isoformat())
        self.assertEqual(row["synced_at"], self.synced.isoformat())

    def test_yoga_timestamp_less_thread_uses_historical_source_mtime(self) -> None:
        document = self._document("conversation")
        document.activity_at = None
        document.source_modified_at = datetime(
            2026,
            6,
            24,
            15,
            8,
            tzinfo=timezone.utc,
        )

        summary = _document_summary(document, {})
        row = _device_file_row((
            document.id,
            document.title,
            document.relative_path,
            document.category,
            document.content_type,
            document.file_size_bytes,
            document.activity_at,
            document.source_modified_at,
            document.synced_at,
        ))

        self.assertEqual(summary.activity_at, document.source_modified_at.isoformat())
        self.assertEqual(row["activity_at"], document.source_modified_at.isoformat())

    def test_non_conversation_rows_do_not_claim_conversation_activity(self) -> None:
        summary = _document_summary(self._document("memory"), {})
        row = _device_file_row(self._device_row("memory"))

        self.assertIsNone(summary.activity_at)
        self.assertIsNone(row["activity_at"])


if __name__ == "__main__":
    unittest.main()
