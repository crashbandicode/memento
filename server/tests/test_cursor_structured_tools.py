from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.conversations import (  # noqa: E402
    _parsed_tool_calls,
    _stored_tool_calls,
)
from server.services.conversation_parser import NormalizedMessage  # noqa: E402
from server.services.ingest_service import (  # noqa: E402
    _conversation_message_metadata,
)


class CursorStructuredToolStorageTests(unittest.TestCase):
    def test_ingest_metadata_and_both_api_paths_have_the_same_shape(self) -> None:
        message = NormalizedMessage(
            role="assistant",
            content="I will inspect it.",
            thinking="separate reasoning",
            tool_calls=[
                {"name": "Read", "input": '{"path":"/tmp/input.json"}'},
                {"name": "Shell", "input": '{"command":"ls -la"}'},
            ],
        )

        metadata = _conversation_message_metadata(message)

        self.assertEqual(metadata["thinking"], "separate reasoning")
        self.assertEqual(_parsed_tool_calls(message), message.tool_calls)
        self.assertEqual(_stored_tool_calls(metadata), message.tool_calls)

    def test_db_fallback_rejects_malformed_metadata_safely(self) -> None:
        self.assertEqual(_stored_tool_calls(None), [])
        self.assertEqual(_stored_tool_calls({"tool_calls": "not-an-array"}), [])
        self.assertEqual(
            _stored_tool_calls({
                "tool_calls": [
                    None,
                    {"name": "Read", "input": {"path": "/tmp/a"}},
                ],
            }),
            [{"name": "Read", "input": '{"path": "/tmp/a"}'}],
        )


if __name__ == "__main__":
    unittest.main()
