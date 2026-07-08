from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.ingest_service import (  # noqa: E402
    _conversation_title_needs_derivation,
    _apply_friendly_conversation_title,
    _friendly_codex_agent_title,
    _friendly_conversation_title,
    _has_generated_conversation_title,
    _prepare_document_metadata,
    _select_updated_document_title,
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

    def test_injected_codex_titles_require_derivation(self) -> None:
        self.assertTrue(_conversation_title_needs_derivation(
            "AGENTS.md instructions for C:\\repo <INSTRUCTIONS>...",
            "codex",
        ))
        self.assertTrue(_conversation_title_needs_derivation(
            "# Context from my IDE setup: ## Open tabs",
            "codex",
        ))
        self.assertTrue(_conversation_title_needs_derivation(
            "Files mentioned by the user: screenshot.png",
            "codex",
        ))
        self.assertFalse(_conversation_title_needs_derivation(
            "Repair the deployment retry logic",
            "codex",
        ))

    def test_codex_title_uses_request_suffix_not_ide_context(self) -> None:
        wrapped = (
            "# Context from my IDE setup:\n\n"
            "## Open tabs:\n- REPORT.md\n\n"
            "## My request for Codex:\nDiagnose the queue race."
        )

        title = _friendly_conversation_title(wrapped, tool_id="codex")

        self.assertEqual(title, "Diagnose the queue race.")

    def test_codex_metadata_candidates_are_normalized_before_ingest(self) -> None:
        metadata, history, first_prompt = _prepare_document_metadata(
            {
                "title": (
                    "# Files mentioned by the user:\nfile.txt\n"
                    "## My request for Codex:\nRepair title selection"
                ),
                "user_history": [
                    {"text": "# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>"},
                    {
                        "text": (
                            "# Context from my IDE setup:\n"
                            "## My request for Codex:\nReal prompt"
                        ),
                        "ts": 42,
                    },
                ],
                "first_user_message": (
                    "# Context from my IDE setup:\n"
                    "## My request for Codex:\nFirst real prompt"
                ),
            },
            tool_id="codex",
        )

        self.assertEqual(metadata["title"], "Repair title selection")
        self.assertEqual(history, [{"text": "Real prompt", "ts": 42}])
        self.assertEqual(first_prompt, "First real prompt")

    def test_context_only_codex_title_is_dropped_before_new_doc_ingest(self) -> None:
        metadata, history, first_prompt = _prepare_document_metadata(
            {
                "title": "# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>",
                "agent_path": "/root/server_repair_review",
            },
            tool_id="codex",
        )

        self.assertNotIn("title", metadata)
        self.assertEqual(history, [])
        self.assertEqual(first_prompt, "")
        self.assertEqual(
            _friendly_codex_agent_title(metadata),
            "server repair review",
        )

    def test_subagent_task_path_overrides_inherited_root_prompt_title(self) -> None:
        doc = SimpleNamespace(
            tool_id="codex",
            title="Investigate the root production incident",
            metadata_={
                "thread_source": "subagent",
                "agent_path": "/root/semantic_search_dedupe",
                "agent_nickname": "Noether",
            },
        )

        title = asyncio.run(_apply_friendly_conversation_title(None, doc))

        self.assertEqual(title, "semantic search dedupe")
        self.assertEqual(doc.title, "semantic search dedupe")

    def test_subagent_nickname_is_used_when_agent_path_is_missing(self) -> None:
        self.assertEqual(
            _friendly_codex_agent_title({"agent_nickname": "Noether"}),
            "Noether",
        )

    def test_legitimate_existing_title_survives_bad_collector_metadata(self) -> None:
        selected = _select_updated_document_title(
            "Repair the deployment retry logic",
            "AGENTS.md instructions for C:\\repo <INSTRUCTIONS>...",
            category="conversation",
            tool_id="codex",
        )

        self.assertEqual(selected, "Repair the deployment retry logic")

    def test_synthetic_existing_title_accepts_a_clean_replacement(self) -> None:
        selected = _select_updated_document_title(
            "AGENTS.md instructions for C:\\repo <INSTRUCTIONS>...",
            "Repair the deployment retry logic",
            category="conversation",
            tool_id="codex",
        )

        self.assertEqual(selected, "Repair the deployment retry logic")

    def test_non_codex_collector_title_updates_still_propagate(self) -> None:
        selected = _select_updated_document_title(
            "Old Claude title",
            "New Claude title",
            category="conversation",
            tool_id="claude_code",
        )

        self.assertEqual(selected, "New Claude title")


if __name__ == "__main__":
    unittest.main()
