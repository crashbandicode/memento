from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_parser import (  # noqa: E402
    parse_conversation_line,
    strip_terminal_sequences,
)


class ConversationParserTests(unittest.TestCase):
    def test_claude_tool_result_is_not_classified_as_user(self) -> None:
        raw = json.dumps({
            "type": "user",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-123",
                    "content": "alpha-\u001b[7mmatch\u001b[0m-omega",
                }],
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.raw_type, "tool_result")
        self.assertEqual(msg.tool_name, "Tool result")
        self.assertEqual(msg.content, "alpha-match-omega")

    def test_terminal_sequence_stripping_handles_csi_and_osc(self) -> None:
        value = "a\u001b[31mred\u001b[0m b\u001b]0;title\u0007c"
        self.assertEqual(strip_terminal_sequences(value), "ared bc")

    def test_claude_standalone_tool_use_is_rendered_as_tool(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Need to inspect the directory"},
                    {
                        "type": "tool_use",
                        "name": "Run Terminal Command",
                        "input": {"command": "Get-ChildItem C:\\\\Users"},
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.raw_type, "tool_use")
        self.assertEqual(msg.tool_name, "Run Terminal Command")
        self.assertIn("Get-ChildItem", msg.tool_input)

    def test_claude_local_command_is_compact_tool_context(self) -> None:
        raw = json.dumps({
            "type": "user",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "user",
                "content": (
                    "<local-command-caveat>Caveat text</local-command-caveat>\n"
                    "<command-name>/model</command-name>\n"
                    "<command-message>model</command-message>\n"
                    "<command-args>opus</command-args>\n"
                    "<local-command-stdout>Set model to opus</local-command-stdout>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.raw_type, "local_command")
        self.assertEqual(msg.tool_name, "/model")
        self.assertEqual(msg.tool_input, "opus")
        self.assertEqual(msg.content, "Set model to opus")

    def test_claude_local_command_caveat_is_still_hidden(self) -> None:
        raw = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    "<local-command-caveat>Generated locally</local-command-caveat>"
                ),
            },
        })

        self.assertIsNone(parse_conversation_line(raw, "claude_code"))

    def test_antigravity_message_preserves_separate_thinking(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "timestamp": "2026-04-05T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Final answer"}],
            },
            "response_text": "Final answer",
            "thinking_text": "Internal reasoning",
            "content_source": "response",
        })

        msg = parse_conversation_line(raw, "antigravity")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.content, "Final answer")
        self.assertEqual(msg.thinking, "Internal reasoning")
        self.assertEqual(msg.raw_type, "response")

    def test_antigravity_message_falls_back_to_thinking_when_response_missing(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "timestamp": "2026-04-05T10:00:00Z",
            "message": {"role": "assistant", "content": []},
            "thinking_text": "Only thinking available",
            "fallback_source": "thinking_fallback",
        })

        msg = parse_conversation_line(raw, "antigravity")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Only thinking available")
        self.assertEqual(msg.thinking, "Only thinking available")
        self.assertEqual(msg.raw_type, "thinking_fallback")


if __name__ == "__main__":
    unittest.main()
