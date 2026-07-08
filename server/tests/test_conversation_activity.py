from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_activity import (  # noqa: E402
    conversation_activity_at_query,
    historical_conversation_activity_query,
    is_low_activity_messages,
    is_low_activity_summary,
    refresh_document_activity_at,
)


class ConversationActivityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
