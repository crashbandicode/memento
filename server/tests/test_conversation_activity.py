from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_activity import (  # noqa: E402
    is_low_activity_messages,
    is_low_activity_summary,
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


if __name__ == "__main__":
    unittest.main()
