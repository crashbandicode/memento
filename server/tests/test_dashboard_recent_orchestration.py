from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.dashboard import _row_metadata  # noqa: E402
from server.services.dashboard_projection import _hierarchy_metadata  # noqa: E402


class DashboardRecentOrchestrationTests(unittest.TestCase):
    def test_row_metadata_surfaces_hierarchy_orchestration(self) -> None:
        row = SimpleNamespace(
            hierarchy_metadata={"orchestration": "claw", "is_subagent": True},
            session_id="delegate-1",
            root_thread_id=None,
            parent_thread_id=None,
            is_subagent=True,
            tool_id="cursor",
        )

        metadata = _row_metadata(row)

        self.assertEqual(metadata["orchestration"], "claw")
        self.assertEqual(metadata["session_id"], "delegate-1")
        self.assertTrue(metadata["is_subagent"])

    def test_projection_keeps_orchestration_after_refresh(self) -> None:
        document = SimpleNamespace(
            metadata_={
                "orchestration": "claw",
                "orchestration_run_id": "session-1",
                "orchestration_parent_document_id": None,
                "is_subagent": True,
                "session_id": "delegate-1",
            },
        )

        metadata = _hierarchy_metadata(document, None)

        self.assertEqual(metadata["orchestration"], "claw")
        self.assertEqual(metadata["orchestration_run_id"], "session-1")
        self.assertEqual(metadata["session_id"], "delegate-1")
        self.assertTrue(metadata["is_subagent"])
        self.assertNotIn("orchestration_parent_document_id", metadata)
