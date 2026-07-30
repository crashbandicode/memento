from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.scripts.backfill_cursor_system_notifications import (  # noqa: E402
    plan_notification_context_update,
)


class BackfillCursorSystemNotificationsTests(unittest.TestCase):
    def test_notification_with_synthetic_followup_becomes_task_event(self) -> None:
        content = (
            "<timestamp>Thursday, Jul 30, 2026, 8:59 AM (UTC-4)</timestamp>\n"
            "<system_notification>\n"
            "The following task has finished.\n"
            "<task>\nkind: shell\nstatus: success\ntask_id: 913821\n"
            "title: Start batch 1 pull tlv02+rno\n"
            "output_path: C:\\Users\\intpa\\terminals\\913821.txt\n</task>\n"
            "</system_notification>\n"
            "<user_query>Briefly inform the user about the task result and "
            "perform any follow-up actions (if needed). If there's no "
            "follow-ups needed, don't explicitly say that.</user_query>"
        )
        update = plan_notification_context_update(
            content=content,
            role="user",
            message_type="user",
            metadata={"source_id": "abc"},
        )
        self.assertIsNotNone(update)
        assert update is not None
        role, message_type, next_content, metadata = update
        self.assertEqual(role, "tool")
        self.assertEqual(message_type, "agent_event")
        self.assertEqual(next_content, "Start batch 1 pull tlv02+rno completed")
        self.assertEqual(
            metadata["source_id"],
            "cursor-task-completion:shell:913821:completed",
        )
        self.assertEqual(metadata["agent_event"]["activity_type"], "shell")
        self.assertEqual(metadata["agent_event"]["output_path"], (
            "C:\\Users\\intpa\\terminals\\913821.txt"
        ))

    def test_human_prompt_with_notification_prefix_is_left_alone(self) -> None:
        content = (
            "<system_notification>done</system_notification>\n"
            "<user_query>Please continue the investigation.</user_query>"
        )
        update = plan_notification_context_update(
            content=content,
            role="user",
            message_type="user",
            metadata={},
        )
        self.assertIsNone(update)


if __name__ == "__main__":
    unittest.main()
