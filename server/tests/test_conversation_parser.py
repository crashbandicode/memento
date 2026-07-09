from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_parser import (  # noqa: E402
    count_conversation_messages,
    extract_codex_session_metadata,
    normalize_codex_user_payload,
    normalize_cursor_user_payload,
    parse_conversation,
    parse_conversation_line,
    strip_terminal_sequences,
)


class ConversationParserTests(unittest.TestCase):
    def test_codex_request_wrapper_keeps_only_the_human_request(self) -> None:
        wrapped = (
            "# Context from my IDE setup:\n\n"
            "## Open tabs:\n- REPORT.md\n\n"
            "## My request for Codex:\n"
            "Explain the drift and propose a fix."
        )
        raw = json.dumps({
            "type": "response_item",
            "timestamp": "2026-07-08T10:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": wrapped}],
            },
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Explain the drift and propose a fix.")

    def test_codex_files_wrapper_is_normalized_for_event_messages(self) -> None:
        wrapped = (
            "# Files mentioned by the user:\n\n"
            "## report.png\n\n"
            "## My request for Codex:\nRepair the card title."
        )
        raw = json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": wrapped},
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Repair the card title.")

    def test_codex_agents_envelope_is_system_context_not_a_prompt(self) -> None:
        content = (
            "# AGENTS.md instructions for C:\\repo\n\n"
            "<INSTRUCTIONS>Use PowerShell.</INSTRUCTIONS>"
        )
        raw = json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": content}],
            },
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.raw_type, "codex_context")
        self.assertEqual(msg.content, content)

    def test_codex_environment_context_is_preserved_as_system_context(self) -> None:
        role, content = normalize_codex_user_payload(
            "<environment_context><cwd>C:\\repo</cwd></environment_context>"
        )

        self.assertEqual(role, "system")
        self.assertIn("environment_context", content)

    def test_codex_plain_prompt_is_not_over_normalized(self) -> None:
        role, content = normalize_codex_user_payload(
            "Please explain how AGENTS.md instructions are loaded."
        )

        self.assertEqual(role, "user")
        self.assertEqual(
            content,
            "Please explain how AGENTS.md instructions are loaded.",
        )

    def test_codex_plain_prompt_quoting_request_marker_is_not_truncated(self) -> None:
        prompt = (
            "Please preserve this template exactly:\n\n"
            "## My request for Codex:\nplaceholder"
        )

        role, content = normalize_codex_user_payload(prompt)

        self.assertEqual(role, "user")
        self.assertEqual(content, prompt)

    def test_codex_session_metadata_uses_current_thread_and_root_ids(self) -> None:
        root_id = "11111111-1111-4111-8111-111111111111"
        current_id = "22222222-2222-4222-8222-222222222222"
        raw = json.dumps({
            "type": "session_meta",
            "payload": {
                "session_id": root_id,
                "id": current_id,
                "forked_from_id": root_id,
                "parent_thread_id": root_id,
                "thread_source": "subagent",
                "agent_path": "/root/reviewer",
                "agent_nickname": "Noether",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": root_id,
                            "depth": 1,
                            "agent_path": "/root/reviewer",
                            "agent_nickname": "Noether",
                        }
                    }
                },
            },
        })

        metadata = extract_codex_session_metadata(raw)

        self.assertEqual(metadata["session_id"], current_id)
        self.assertEqual(metadata["thread_id"], current_id)
        self.assertEqual(metadata["root_session_id"], root_id)
        self.assertEqual(metadata["parent_thread_id"], root_id)
        self.assertEqual(metadata["forked_from_id"], root_id)
        self.assertEqual(metadata["thread_source"], "subagent")
        self.assertEqual(metadata["agent_path"], "/root/reviewer")
        self.assertEqual(metadata["agent_nickname"], "Noether")
        self.assertEqual(metadata["agent_depth"], 1)

    def test_codex_session_metadata_survives_a_truncated_range_prefix(self) -> None:
        current_id = "33333333-3333-4333-8333-333333333333"
        raw = (
            '{"type":"session_meta","payload":{'
            f'"session_id":"{current_id}","id":"{current_id}",'
            '"thread_source":"user","base_instructions":"unfinished'
        )

        metadata = extract_codex_session_metadata(raw)

        self.assertEqual(metadata["session_id"], current_id)
        self.assertEqual(metadata["root_session_id"], current_id)
        self.assertEqual(metadata["thread_source"], "user")

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

    def test_cursor_timestamp_envelope_is_removed_and_parsed(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": [{
                    "type": "text",
                    "text": (
                        "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                        "(UTC-4)</timestamp>\n"
                        "<user_query>\nMove this workspace to Windows.\n"
                        "</user_query>"
                    ),
                }],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Move this workspace to Windows.")
        self.assertEqual(msg.timestamp, "2026-06-24T09:08:00-04:00")

    def test_cursor_utc_timestamp_envelope_uses_explicit_utc(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": (
                    "<timestamp>Monday, Jun 15, 2026, 7:51 PM "
                    "(UTC)</timestamp>\n"
                    "<user_query>Continue the investigation.</user_query>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Continue the investigation.")
        self.assertEqual(msg.timestamp, "2026-06-15T19:51:00+00:00")

    def test_cursor_positive_fractional_utc_offset_is_parsed(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": (
                    "<timestamp>Friday, Jun 12, 2026, 8:42 AM "
                    "(UTC+5:30)</timestamp>\n"
                    "<user_query>Check the deployment.</user_query>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Check the deployment.")
        self.assertEqual(msg.timestamp, "2026-06-12T08:42:00+05:30")

    def test_cursor_native_timestamp_wins_over_envelope_timestamp(self) -> None:
        raw = json.dumps({
            "role": "user",
            "timestamp": "2026-06-24T13:09:00Z",
            "message": {
                "content": (
                    "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                    "(UTC-4)</timestamp>\n"
                    "<user_query>Use the native timestamp.</user_query>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Use the native timestamp.")
        self.assertEqual(msg.timestamp, "2026-06-24T13:09:00Z")

    def test_cursor_legacy_user_query_wrapper_without_timestamp_is_removed(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": "<user_query>Plain wrapped prompt.</user_query>",
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Plain wrapped prompt.")
        self.assertEqual(msg.timestamp, "")

    def test_cursor_normalizer_is_noop_without_valid_timestamp_envelope(self) -> None:
        content = "<user_query>Backfill must not alter this.</user_query>"

        normalized, timestamp = normalize_cursor_user_payload(content)

        self.assertEqual(normalized, content)
        self.assertEqual(timestamp, "")

    def test_cursor_normalizer_handles_stored_prompt_without_query_wrapper(self) -> None:
        content = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp>\nAlready-normalized stored prompt."
        )

        normalized, timestamp = normalize_cursor_user_payload(content)

        self.assertEqual(normalized, "Already-normalized stored prompt.")
        self.assertEqual(timestamp, "2026-06-24T09:08:00-04:00")

    def test_cursor_impossible_utc_offset_is_preserved(self) -> None:
        content = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC+14:30)</timestamp>\n"
            "<user_query>Keep impossible metadata literal.</user_query>"
        )

        normalized, timestamp = normalize_cursor_user_payload(content)

        self.assertEqual(normalized, content)
        self.assertEqual(timestamp, "")

    def test_cursor_mid_prompt_literal_tags_are_preserved(self) -> None:
        content = (
            "Explain why <timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp> and <user_query> are shown."
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.timestamp, "")

    def test_cursor_malformed_leading_timestamp_is_preserved(self) -> None:
        content = (
            "<timestamp>not a Cursor timestamp</timestamp>\n"
            "<user_query>Keep this literal example.</user_query>"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.timestamp, "")

    def test_cursor_assistant_markup_is_not_normalized(self) -> None:
        content = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp>\n"
            "<user_query>This is assistant-authored markup.</user_query>"
        )
        raw = json.dumps({
            "role": "assistant",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.timestamp, "")

    def test_cursor_redacted_transport_text_becomes_structured_tool_call(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "[REDACTED]"},
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {
                            "merge": False,
                            "todos": [{"id": "1", "status": "in_progress"}],
                        },
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.content, "")
        self.assertEqual(msg.tool_calls[0]["name"], "TodoWrite")
        self.assertEqual(
            json.loads(msg.tool_calls[0]["input"])["merge"],
            False,
        )

    def test_cursor_keeps_prose_separate_from_multiple_tool_calls(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will inspect both files."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/tmp/one.py"},
                    },
                    {
                        "type": "toolCall",
                        "name": "Shell",
                        "arguments": {"command": "ls -la /tmp"},
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "I will inspect both files.")
        self.assertEqual(
            [call["name"] for call in msg.tool_calls],
            ["Read", "Shell"],
        )
        self.assertNotIn("[Tool:", msg.content)

    def test_cursor_removes_redacted_transport_line_appended_to_prose(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "Running the next check.\n[REDACTED]",
                    },
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {"command": "ls -la"},
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Running the next check.")
        self.assertEqual(msg.tool_calls[0]["name"], "Shell")

    def test_cursor_call_only_assistant_message_is_retained(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"path": "/tmp/results.jsonl"},
                }],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "")
        self.assertEqual(len(msg.tool_calls), 1)

    def test_cursor_call_only_rows_keep_count_and_pagination_in_lockstep(self) -> None:
        rows = [
            {
                "role": "user",
                "message": {"content": "Inspect the file."},
            },
            {
                "role": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/tmp/results.jsonl"},
                    }],
                },
            },
            {
                "role": "assistant",
                "message": {"content": "The file is valid."},
            },
        ]
        raw_content = "\n".join(json.dumps(row) for row in rows)

        total = count_conversation_messages(raw_content, "cursor")
        page = parse_conversation(
            raw_content,
            "cursor",
            offset=1,
            limit=1,
        )

        self.assertEqual(total, 3)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0].content, "")
        self.assertEqual(page[0].tool_calls[0]["name"], "Read")

    def test_cursor_malformed_tool_fields_are_safe_and_calls_are_bounded(self) -> None:
        calls = [
            {"type": "tool_use", "name": ["not", "a", "name"], "input": None},
            "not a content block",
        ]
        calls.extend(
            {"type": "tool_use", "name": f"Tool{i}", "input": {"n": i}}
            for i in range(40)
        )
        raw = json.dumps({
            "role": "assistant",
            "message": {"content": calls},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(len(msg.tool_calls), 32)
        self.assertEqual(msg.tool_calls[0], {"name": "Tool", "input": "null"})

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
