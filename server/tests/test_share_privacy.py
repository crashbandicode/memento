from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.share import _public_timeline_payload  # noqa: E402


class PublicTimelinePrivacyTests(unittest.TestCase):
    def test_child_routes_and_orphan_metadata_are_not_shared(self) -> None:
        source = {
            "project": {"id": "project-1"},
            "sessions": [{
                "conversation_id": "root-document",
                "subagent_count": 2,
                "logical_session_id": "private-root-thread",
                "is_subagent_orphan": False,
                "subagents": [
                    {
                        "id": "private-child-document",
                        "relative_path": "/private/session.jsonl",
                        "agent_path": "/root/private-task",
                    }
                ],
                "messages": [{"role": "user", "content": "shared"}],
            }],
        }

        public = _public_timeline_payload(source)
        session = public["sessions"][0]

        self.assertEqual(session["subagent_count"], 2)
        self.assertNotIn("subagents", session)
        self.assertNotIn("is_subagent_orphan", session)
        self.assertNotIn("logical_session_id", session)
        self.assertIn("subagents", source["sessions"][0])


if __name__ == "__main__":
    unittest.main()
