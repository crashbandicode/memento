from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.ingest_service import (  # noqa: E402
    _friendly_conversation_title,
    _has_generated_conversation_title,
)


class ConversationTitleTests(unittest.TestCase):
    def test_generated_identifiers_are_detected_with_source_extensions(self) -> None:
        self.assertTrue(_has_generated_conversation_title(
            "8d612b2c-1111-2222-3333-444444444444.jsonl"
        ))
        self.assertTrue(_has_generated_conversation_title(
            "agent-8d612b2c111122223333444444444444"
        ))
        self.assertFalse(_has_generated_conversation_title("Readable project setup"))

    def test_first_prompt_becomes_a_compact_readable_title(self) -> None:
        prompt = (
            "# Help me understand why the deployment is failing and propose "
            "a root-cause fix that we can verify safely in production"
        )

        title = _friendly_conversation_title(prompt, max_length=64)

        self.assertEqual(
            title,
            "Help me understand why the deployment is failing and propose…",
        )
        self.assertLessEqual(len(title or ""), 64)

    def test_claude_local_commands_cannot_become_titles(self) -> None:
        self.assertIsNone(_friendly_conversation_title(
            "<local-command-caveat>Generated locally</local-command-caveat>"
        ))


if __name__ == "__main__":
    unittest.main()
