from __future__ import annotations

import asyncio
import io
import unittest
from datetime import datetime, timezone

from server.services.conversation_markdown import (
    ConversationMarkdownInfo,
    ExportMessage,
    MarkdownExportOptions,
    parse_prompt_selection,
    safe_markdown_filename,
    write_conversation_markdown,
)


def message(
    line: int,
    role: str,
    content: str,
    *,
    metadata: dict | None = None,
    day: int = 1,
) -> ExportMessage:
    return ExportMessage(
        line_number=line,
        role=role,
        content=content,
        metadata=metadata or {},
        timestamp=datetime(2026, 7, day, 12, tzinfo=timezone.utc),
    )


async def stream(items: list[ExportMessage]):
    for item in items:
        yield item


class PromptSelectionTests(unittest.TestCase):
    def test_parses_merges_and_supports_open_end(self) -> None:
        selection = parse_prompt_selection("1-3, 3-5, 8, 10-")
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.intervals, ((1, 5), (8, 8), (10, None)))
        self.assertTrue(selection.includes(4))
        self.assertFalse(selection.includes(7))
        self.assertTrue(selection.includes(1000))

    def test_rejects_reversed_or_malformed_ranges(self) -> None:
        for value in ("3-1", "1--3", "x", "0", "1,", "-3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_prompt_selection(value)

    def test_empty_range_means_all_prompts(self) -> None:
        self.assertIsNone(parse_prompt_selection("  "))


class MarkdownRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.info = ConversationMarkdownInfo(
            title="Ship the export",
            tool_id="codex",
            document_id="12345678-aaaa-bbbb-cccc-1234567890ab",
            relative_path="sessions/thread.jsonl",
            activity_at=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
            message_count=6,
            project_title="Memento",
            machine_name="dreamland-yoga",
        )

    def render(
        self,
        items: list[ExportMessage],
        options: MarkdownExportOptions | None = None,
        responses: dict | None = None,
    ) -> tuple[str, object]:
        writer = io.StringIO()
        stats = asyncio.run(write_conversation_markdown(
            writer,
            self.info,
            stream(items),
            options or MarkdownExportOptions(),
            responses or {},
        ))
        return writer.getvalue(), stats

    def test_prompt_range_keeps_the_complete_selected_turn(self) -> None:
        items = [
            message(1, "user", "First request", day=1),
            message(2, "assistant", "First answer", day=1),
            message(3, "tool", "first output", metadata={"tool_name": "Shell"}, day=1),
            message(4, "user", "Second request", day=2),
            message(5, "assistant", "Second answer", day=3),
            message(6, "user", "Third request", day=3),
        ]
        text, stats = self.render(
            items,
            MarkdownExportOptions(prompt_selection=parse_prompt_selection("2")),
        )
        self.assertNotIn("First request", text)
        self.assertNotIn("First answer", text)
        self.assertIn("## Prompt 2 — You", text)
        self.assertIn("Second request", text)
        self.assertIn("Second answer", text)
        self.assertNotIn("Third request", text)
        self.assertEqual(stats.prompts_seen, 3)
        self.assertEqual(stats.prompts_exported, 1)

    def test_date_filter_uses_prompt_date_not_response_date(self) -> None:
        items = [
            message(1, "user", "Keep this", day=2),
            message(2, "assistant", "Late response remains", day=3),
            message(3, "user", "Drop this", day=3),
        ]
        text, _stats = self.render(
            items,
            MarkdownExportOptions(
                start_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 2, 23, 59, tzinfo=timezone.utc),
            ),
        )
        self.assertIn("Keep this", text)
        self.assertIn("Late response remains", text)
        self.assertNotIn("Drop this", text)

    def test_tools_questions_answers_and_code_remain_structured(self) -> None:
        interaction = {
            "id": "question-1",
            "source": "codex",
            "questions": [{
                "id": "deploy",
                "header": "Deploy",
                "prompt": "Where should this go?",
                "options": [
                    {"id": "stage", "label": "Staging"},
                    {"id": "prod", "label": "Production", "description": "Live users"},
                ],
            }],
        }
        response = {
            "interaction_id": "question-1",
            "status": "answered",
            "answers": [{
                "question_id": "deploy",
                "selected_option_ids": ["prod"],
                "text": "Production",
            }],
        }
        items = [
            message(1, "user", "Please deploy"),
            message(2, "assistant", "Here is code:\n\n```python\nprint('ok')\n```", metadata={
                "thinking": "Check the release first.",
                "tool_calls": [
                    {"name": "Shell", "input": '{"command":"deploy"}'},
                    {"name": "request_user_input", "input": "", "interaction": interaction},
                ],
            }),
            message(3, "user", "Production", metadata={"interaction_response": response}),
        ]
        text, _stats = self.render(items, responses={"question-1": response})
        self.assertIn("```python", text)
        self.assertIn("<summary>Thinking</summary>", text)
        self.assertIn("<summary><strong>Tool</strong> · Shell</summary>", text)
        self.assertIn("- [x] Production — Live users", text)
        self.assertIn("**Response:** Production", text)
        self.assertEqual(text.count("Production"), 2)

    def test_optional_sections_can_be_removed(self) -> None:
        items = [
            message(1, "user", "Hello", metadata={"session_context": "Private context"}),
            message(2, "assistant", "Answer", metadata={
                "thinking": "Hidden reasoning",
                "tool_calls": [{"name": "Read", "input": "file.txt"}],
            }),
        ]
        text, _stats = self.render(items, MarkdownExportOptions(
            include_tools=False,
            include_thinking=False,
            include_session_context=False,
            include_timestamps=False,
        ))
        self.assertNotIn("Private context", text)
        self.assertNotIn("Hidden reasoning", text)
        self.assertNotIn("<strong>Tool</strong>", text)
        self.assertNotIn("UTC", text.split("---", 1)[1])


class FilenameTests(unittest.TestCase):
    def test_filename_is_portable_and_stable(self) -> None:
        value = safe_markdown_filename('Fix: <bad> / name?*', "12345678-aaaa-bbbb")
        self.assertEqual(value, "Fix bad name--aaaabbbb.md")


if __name__ == "__main__":
    unittest.main()
