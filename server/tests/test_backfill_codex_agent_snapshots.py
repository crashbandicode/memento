from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.scripts.backfill_codex_agent_snapshots import (  # noqa: E402
    CodexAgentResultRow,
    plan_agent_snapshot_updates,
)


class CodexAgentSnapshotBackfillTests(unittest.TestCase):
    def test_plans_only_strict_subagent_snapshots(self) -> None:
        document_id = uuid.uuid4()
        rows = [
            CodexAgentResultRow(
                id=1,
                document_id=document_id,
                line_number=10,
                content=json.dumps({
                    "agents": [
                        {"agent_name": "/root", "agent_status": "running"},
                        {"agent_name": "/root/index_review", "agent_status": "running"},
                    ],
                }),
                metadata={"tool_name": "Tool result", "source_id": "call-1:output"},
            ),
            CodexAgentResultRow(
                id=2,
                document_id=document_id,
                line_number=11,
                content=json.dumps({"agents": [{"name": "sales", "status": "active"}]}),
                metadata={"tool_name": "Tool result"},
            ),
        ]

        updates = plan_agent_snapshot_updates(rows)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].content, "1 subagent · 1 running")
        self.assertEqual(updates[0].metadata["tool_name"], "Subagent status")
        self.assertEqual(updates[0].metadata["source_id"], "call-1:output")
        self.assertEqual(
            updates[0].metadata["agent_event"]["agents"][0]["label"],
            "Index Review",
        )


if __name__ == "__main__":
    unittest.main()
