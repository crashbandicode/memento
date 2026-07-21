from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.scripts.backfill_cursor_subagent_events import (  # noqa: E402
    plan_document_updates,
    plan_message_updates,
)


class BackfillCursorSubagentEventsTests(unittest.TestCase):
    def test_plans_task_v2_agent_event_overlay(self) -> None:
        rows = [SimpleNamespace(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            line_number=10,
            message_type="cursor_state_tool",
            content=json.dumps({"agentId": "child-1"}),
            metadata={
                "tool_name": "task_v2",
                "tool_input": json.dumps({
                    "description": "Explore Farm UI sites patterns",
                }),
            },
        )]

        updates = plan_message_updates(rows)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].metadata["agent_event"]["kind"], "completed")
        self.assertEqual(
            updates[0].metadata["agent_event"]["agent_thread_id"],
            "child-1",
        )
        self.assertEqual(updates[0].content, "Explore Farm UI sites patterns completed")

    def test_enriches_path_linked_child_metadata(self) -> None:
        rows = [SimpleNamespace(
            id=uuid.uuid4(),
            relative_path=(
                "projects/demo/agent-transcripts/root/"
                "subagents/child-1.jsonl"
            ),
            metadata={"session_id": "child-1", "is_subagent": True},
        )]

        updates = plan_document_updates(
            rows,
            agent_paths_by_session={
                "child-1": "/root/explore_farm_ui_sites_patterns",
            },
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].metadata["agent_depth"], 1)
        self.assertEqual(updates[0].metadata["root_session_id"], "root")
        self.assertEqual(
            updates[0].metadata["agent_path"],
            "/root/explore_farm_ui_sites_patterns",
        )


if __name__ == "__main__":
    unittest.main()
