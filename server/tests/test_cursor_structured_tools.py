from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.conversations import (  # noqa: E402
    _parsed_tool_calls,
    _stored_attachments,
    _stored_tool_calls,
)
from server.services.conversation_parser import NormalizedMessage  # noqa: E402
from server.services.ingest_service import (  # noqa: E402
    _conversation_message_metadata,
    _pending_question_interactions,
)


class CursorStructuredToolStorageTests(unittest.TestCase):
    def test_ingest_metadata_and_both_api_paths_have_the_same_shape(self) -> None:
        message = NormalizedMessage(
            role="assistant",
            content="I will inspect it.",
            thinking="separate reasoning",
            attachments=[
                {"type": "image", "name": "screenshot.png"},
            ],
            tool_calls=[
                {"name": "Read", "input": '{"path":"/tmp/input.json"}'},
                {
                    "name": "AskQuestion",
                    "input": '{"questions":[{"id":"ship","prompt":"Ship it?","options":[{"id":"yes","label":"Yes"}]}]}',
                },
            ],
            interaction={
                "kind": "question",
                "id": "question-1",
                "source": "cursor",
                "tool_name": "AskQuestion",
                "questions": [{
                    "id": "ship",
                    "header": "",
                    "prompt": "Ship it?",
                    "type": "single_select",
                    "allow_custom": True,
                    "options": [{"id": "yes", "label": "Yes"}],
                }],
            },
            interaction_response={
                "kind": "question_response",
                "interaction_id": "question-1",
                "status": "answered",
                "answers": [{
                    "question_id": "ship",
                    "text": "Yes",
                    "selected_option_ids": ["yes"],
                }],
                "raw_text": "Yes",
            },
        )

        metadata = _conversation_message_metadata(message)

        self.assertEqual(metadata["thinking"], "separate reasoning")
        self.assertEqual(metadata["interaction"], message.interaction)
        self.assertEqual(metadata["interaction_response"], message.interaction_response)
        self.assertEqual(
            _stored_attachments(metadata),
            [{"type": "image", "name": "screenshot.png"}],
        )
        parsed_calls = _parsed_tool_calls(message)
        stored_calls = _stored_tool_calls(metadata)
        self.assertEqual(stored_calls, parsed_calls)
        self.assertEqual(parsed_calls[1]["interaction"]["questions"][0]["id"], "ship")

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

    def test_delta_lookback_does_not_revive_stale_cursor_question(self) -> None:
        interaction = {
            "kind": "question",
            "id": "cursor-question-1",
            "source": "cursor",
            "questions": [],
        }
        recent_rows = [
            SimpleNamespace(line_number=15, metadata_={}),
            SimpleNamespace(
                line_number=10,
                metadata_={"tool_calls": [{"interaction": interaction}]},
            ),
        ]

        self.assertEqual(_pending_question_interactions(recent_rows), [])

    def test_delta_lookback_keeps_immediate_cursor_and_id_linked_questions(self) -> None:
        cursor_interaction = {
            "kind": "question",
            "id": "cursor-question-1",
            "source": "cursor",
            "questions": [],
        }
        codex_interaction = {
            "kind": "question",
            "id": "codex-question-1",
            "source": "codex",
            "questions": [],
        }
        recent_rows = [
            SimpleNamespace(line_number=20, metadata_={}),
            SimpleNamespace(
                line_number=18,
                metadata_={"tool_calls": [{"interaction": cursor_interaction}]},
            ),
            SimpleNamespace(line_number=2, metadata_={"interaction": codex_interaction}),
        ]

        self.assertEqual(
            _pending_question_interactions(recent_rows),
            [codex_interaction, cursor_interaction],
        )


if __name__ == "__main__":
    unittest.main()
