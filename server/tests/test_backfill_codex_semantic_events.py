from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.scripts.backfill_codex_semantic_events import (  # noqa: E402
    TimelineRow,
    _parsed_rows,
    plan_semantic_inserts,
)


def row(
    position: int,
    *,
    line_number: int | None = None,
    role: str = "assistant",
    message_type: str = "message",
    content: str = "",
    source_id: str = "",
    timestamp: datetime | None = None,
) -> TimelineRow:
    metadata = {"source_id": source_id} if source_id else {}
    if message_type == "reasoning":
        metadata["thinking"] = content
        content = ""
    return TimelineRow(
        position=position,
        line_number=line_number,
        role=role,
        message_type=message_type,
        content=content,
        metadata=metadata,
        timestamp=timestamp,
    )


class SemanticBackfillTests(unittest.TestCase):
    def test_supplemental_delta_keeps_only_keyed_rows_and_updates_snapshot(self) -> None:
        base = (
            '{"type":"response_item","payload":{"type":"message","id":"m1",'
            '"role":"assistant","content":[{"type":"output_text","text":"before"}]}}\n'
            '{"type":"response_item","payload":{"type":"reasoning","id":"r1",'
            '"summary":[{"type":"summary_text","text":"early"}]}}'
        )
        delta = (
            '{"type":"response_item","payload":{"type":"reasoning","id":"r1",'
            '"summary":[{"type":"summary_text","text":"latest"}]}}\n'
            '{"type":"event_msg","payload":{"type":"sub_agent_activity",'
            '"event_id":"a1","agent_thread_id":"t1","agent_path":"/root/review",'
            '"kind":"started"}}'
        )

        parsed = _parsed_rows(base, "codex", [delta])

        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[1].metadata["thinking"], "latest")
        self.assertEqual(parsed[2].message_type, "agent_event")

    def test_inserts_before_next_unique_source_anchor(self) -> None:
        parsed = [
            row(1, content="before", source_id="m1"),
            row(2, message_type="reasoning", content="visible summary", source_id="r1"),
            row(3, content="after", source_id="m2"),
        ]
        existing = [
            row(0, line_number=10, content="before", source_id="m1"),
            row(0, line_number=11, content="after", source_id="m2"),
        ]

        planned = plan_semantic_inserts(parsed, existing)

        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].anchor, 11)
        self.assertEqual(planned[0].row.metadata["source_id"], "r1")

    def test_existing_semantic_source_id_is_idempotent(self) -> None:
        parsed = [
            row(1, content="before", source_id="m1"),
            row(2, message_type="agent_event", role="tool", content="Agent updated", source_id="a1"),
            row(3, content="after", source_id="m2"),
        ]
        existing = [
            row(0, line_number=1, content="before", source_id="m1"),
            row(0, line_number=2, message_type="agent_event", role="tool", content="Agent updated", source_id="a1"),
            row(0, line_number=3, content="after", source_id="m2"),
        ]

        self.assertEqual(plan_semantic_inserts(parsed, existing), [])

    def test_supplemental_semantic_row_uses_timestamp_anchor(self) -> None:
        parsed = [
            row(1, content="base", source_id="m1"),
            row(
                2,
                message_type="agent_event",
                role="tool",
                content="Review started",
                source_id="a1",
                timestamp=datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
            ),
        ]
        existing = [
            row(
                0,
                line_number=10,
                content="base",
                source_id="m1",
                timestamp=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
            ),
            row(
                0,
                line_number=11,
                content="live suffix",
                source_id="m2",
                timestamp=datetime(2026, 7, 18, 13, 0, tzinfo=UTC),
            ),
        ]

        planned = plan_semantic_inserts(parsed, existing)

        self.assertEqual(planned[0].anchor, 11)


if __name__ == "__main__":
    unittest.main()
