import json
import unittest
import uuid

from server.scripts.backfill_question_interactions import (
    QuestionRow,
    plan_question_overlays,
)


class QuestionInteractionBackfillTests(unittest.TestCase):
    def test_cursor_combined_question_and_multiselect_answer_are_recovered(self):
        row = QuestionRow(
            id=1,
            document_id=uuid.uuid4(),
            line_number=10,
            tool_id="cursor",
            content=json.dumps({
                "answers": [{
                    "questionId": "targets",
                    "selectedOptionIds": ["api", "web"],
                    "freeformText": "",
                }],
            }),
            metadata={
                "tool_name": "ask_question",
                "source_id": "question-1",
                "tool_input": json.dumps({
                    "questions": [{
                        "id": "targets",
                        "prompt": "Which targets?",
                        "allowMultiple": True,
                        "options": [
                            {"id": "api", "label": "API"},
                            {"id": "web", "label": "Web"},
                        ],
                    }],
                }),
            },
        )

        updates = plan_question_overlays([row])

        self.assertEqual(len(updates), 1)
        patch = updates[0].metadata_patch
        self.assertEqual(
            patch["interaction"]["questions"][0]["type"],
            "multi_select",
        )
        self.assertEqual(
            patch["interaction_response"]["answers"][0]["selected_option_ids"],
            ["api", "web"],
        )

    def test_empty_legacy_question_payload_is_not_guessed(self):
        row = QuestionRow(
            id=1,
            document_id=uuid.uuid4(),
            line_number=10,
            tool_id="claude_code",
            content="[AskUserQuestion]",
            metadata={"tool_name": "AskUserQuestion", "tool_input": "{}"},
        )

        self.assertEqual(plan_question_overlays([row]), [])

    def test_existing_metadata_is_idempotent(self):
        row = QuestionRow(
            id=1,
            document_id=uuid.uuid4(),
            line_number=10,
            tool_id="cursor",
            content=json.dumps({"answers": []}),
            metadata={
                "tool_name": "ask_question",
                "tool_input": json.dumps({
                    "questions": [{"id": "x", "prompt": "Continue?"}],
                }),
                "interaction": {"kind": "question", "questions": []},
                "interaction_response": {"kind": "question_response"},
            },
        )

        self.assertEqual(plan_question_overlays([row]), [])


if __name__ == "__main__":
    unittest.main()
