from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.dashboard import (  # noqa: E402
    _is_unlinked_claw_row,
    _row_metadata,
    _select_recent_conversation_rows,
    corrected_claw_delegate_count,
    partition_dashboard_candidates_before_limit,
)
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

    def test_unlinked_claw_rows_do_not_consume_primary_recent_budget(self) -> None:
        primary = SimpleNamespace(
            id="primary",
            hierarchy_metadata={},
            session_id="primary",
            root_thread_id=None,
            parent_thread_id=None,
            is_subagent=False,
            tool_id="claude_code",
            activity_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )
        claws = [
            SimpleNamespace(
                id=f"claw-{index}",
                hierarchy_metadata={"orchestration": "claw", "is_subagent": True},
                session_id=f"claw-{index}",
                root_thread_id=None,
                parent_thread_id=None,
                is_subagent=True,
                tool_id="cursor",
                activity_at=datetime(2026, 8, 28, 13, index, tzinfo=timezone.utc),
            )
            for index in range(21)
        ]
        rows, count = _select_recent_conversation_rows(
            [*claws, primary],
            set(),
            lambda row: (row.activity_at, str(row.id)),
        )
        ids = [row.id for row in rows]
        self.assertEqual(count, 21)
        self.assertIn("primary", ids)
        self.assertEqual(len([row_id for row_id in ids if str(row_id).startswith("claw-")]), 20)

    def test_unlinked_claws_are_partitioned_before_candidate_limit(self) -> None:
        primary = SimpleNamespace(
            id="primary",
            hierarchy_metadata={},
            session_id="primary",
            root_thread_id=None,
            parent_thread_id=None,
            is_subagent=False,
            tool_id="claude_code",
            activity_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )
        claws = [
            SimpleNamespace(
                id=f"claw-{index}",
                hierarchy_metadata={"orchestration": "claw", "is_subagent": True},
                session_id=f"claw-{index}",
                root_thread_id=None,
                parent_thread_id=None,
                is_subagent=True,
                tool_id="cursor",
                activity_at=(
                    datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
                    + timedelta(seconds=index)
                ),
            )
            for index in range(601)
        ]
        partitioned = partition_dashboard_candidates_before_limit(
            [*claws, primary],
            activity_key=lambda row: (row.activity_at, str(row.id)),
        )
        primary_ids = [row.id for row in partitioned["primary_candidates"]]
        self.assertEqual(partitioned["claw_count"], 601)
        self.assertIn("primary", primary_ids)
        self.assertEqual(len(partitioned["claw_sample"]), 20)
        self.assertTrue(
            all(str(row.id).startswith("claw-") for row in partitioned["claw_sample"])
        )

    def test_parent_linked_claws_stay_in_primary_candidate_set(self) -> None:
        primary = SimpleNamespace(
            id="primary",
            hierarchy_metadata={},
            session_id="primary",
            root_thread_id=None,
            parent_thread_id=None,
            is_subagent=False,
            tool_id="claude_code",
            activity_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )
        linked = SimpleNamespace(
            id="linked-child",
            hierarchy_metadata={
                "orchestration": "claw",
                "orchestration_parent_document_id": "parent-doc",
                "is_subagent": True,
            },
            session_id="linked-child",
            root_thread_id=None,
            parent_thread_id=None,
            is_subagent=True,
            tool_id="cursor",
            activity_at=datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc),
        )
        partitioned = partition_dashboard_candidates_before_limit(
            [linked, primary],
            activity_key=lambda row: (row.activity_at, str(row.id)),
        )
        primary_ids = [row.id for row in partitioned["primary_candidates"]]
        self.assertEqual(partitioned["claw_count"], 0)
        self.assertEqual(partitioned["claw_sample"], [])
        self.assertEqual(primary_ids, ["linked-child", "primary"])

    def _unlinked_claw_row(self, row_id: str, *, relative_path: str = "", pending: int = 0):
        return SimpleNamespace(
            id=row_id,
            hierarchy_metadata={
                "orchestration": "claw",
                "is_subagent": True,
            },
            session_id=row_id,
            root_thread_id=None,
            parent_thread_id=None,
            is_subagent=True,
            tool_id="cursor",
            relative_path=relative_path,
            pending_question_count=pending,
            activity_at=datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc),
        )

    def test_path_linked_claw_rows_are_not_unlinked(self) -> None:
        path_linked = self._unlinked_claw_row(
            "path-child",
            relative_path="projects/root/subagents/agent-1.jsonl",
        )
        plain = self._unlinked_claw_row("plain-delegate")
        self.assertFalse(_is_unlinked_claw_row(path_linked))
        self.assertTrue(_is_unlinked_claw_row(plain))

    def test_aggregate_count_subtracts_folded_and_attention_rows(self) -> None:
        folded = self._unlinked_claw_row("folded-delegate")
        elevated = self._unlinked_claw_row("attention-delegate", pending=1)
        grouped = self._unlinked_claw_row("grouped-delegate")
        # Raw SQL count saw all three plus two rows beyond the candidate cap.
        corrected = corrected_claw_delegate_count(
            5,
            [folded, elevated, grouped],
            visible_document_ids={"attention-delegate", "grouped-delegate"},
            attention_ids={"attention-delegate"},
        )
        # folded (not visible) and elevated (attention) leave the group;
        # grouped and the two unloaded beyond-cap rows remain.
        self.assertEqual(corrected, 3)

    def test_aggregate_count_never_negative(self) -> None:
        folded = self._unlinked_claw_row("folded-delegate")
        self.assertEqual(
            corrected_claw_delegate_count(
                0,
                [folded],
                visible_document_ids=set(),
                attention_ids=set(),
            ),
            0,
        )
