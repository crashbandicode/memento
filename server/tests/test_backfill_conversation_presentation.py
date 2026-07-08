from __future__ import annotations

import asyncio
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.scripts.backfill_conversation_presentation import (  # noqa: E402
    BackfillStats,
    _codex_title_from_messages,
    _conversation_embedding_rows_query,
    _embedding_input_changed,
    _externalized_prefix,
    _invalidate_changed_embeddings,
    _is_codex_mirror_pair,
    _normalize_codex_stored_message,
)
from server.services.embedding_service import conversation_embedding_content  # noqa: E402


class ConversationPresentationBackfillTests(unittest.TestCase):
    def test_agents_message_is_reclassified_idempotently(self) -> None:
        message = SimpleNamespace(
            role="user",
            content="# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>",
            message_type="response_item",
        )

        changed, became_context = _normalize_codex_stored_message(message)
        changed_again, became_context_again = _normalize_codex_stored_message(message)

        self.assertTrue(changed)
        self.assertTrue(became_context)
        self.assertEqual(message.role, "system")
        self.assertEqual(message.message_type, "codex_context")
        self.assertFalse(changed_again)
        self.assertTrue(became_context_again)

    def test_wrapped_message_is_replaced_with_request_suffix_idempotently(self) -> None:
        message = SimpleNamespace(
            role="user",
            content=(
                "# Context from my IDE setup:\n"
                "## My request for Codex:\nFix the title"
            ),
            message_type="response_item",
        )

        changed, became_context = _normalize_codex_stored_message(message)
        changed_again, _ = _normalize_codex_stored_message(message)

        self.assertTrue(changed)
        self.assertFalse(became_context)
        self.assertEqual(message.role, "user")
        self.assertEqual(message.content, "Fix the title")
        self.assertFalse(changed_again)

    def test_only_known_adjacent_codex_mirror_rows_are_duplicates(self) -> None:
        timestamp = datetime(2026, 7, 8, 10, 0, 0, tzinfo=timezone.utc)
        response_item = SimpleNamespace(
            role="user",
            message_type="response_item",
            timestamp=timestamp,
            line_number=10,
            content="keep going",
        )
        user_message = SimpleNamespace(
            role="user",
            message_type="user_message",
            timestamp=timestamp,
            line_number=11,
            content="keep going",
        )
        repeated_history = SimpleNamespace(
            role="user",
            message_type="history_user_message",
            timestamp=None,
            line_number=-2,
            content="keep going",
        )

        self.assertTrue(_is_codex_mirror_pair(response_item, user_message))
        self.assertFalse(_is_codex_mirror_pair(response_item, repeated_history))
        self.assertFalse(_is_codex_mirror_pair(user_message, user_message))

    def test_bad_title_uses_first_retained_human_prompt(self) -> None:
        messages = [
            SimpleNamespace(
                line_number=1,
                role="system",
                content="# AGENTS.md instructions",
            ),
            SimpleNamespace(
                line_number=2,
                role="user",
                content="Find the root cause",
            ),
        ]

        title = _codex_title_from_messages(
            "AGENTS.md instructions for C:\\repo <INSTRUCTIONS>...",
            "sessions/thread.jsonl",
            messages,
        )

        self.assertEqual(title, "Find the root cause")

    def test_legitimate_title_is_preserved(self) -> None:
        title = _codex_title_from_messages(
            "My manually curated title",
            "sessions/thread.jsonl",
            [
                SimpleNamespace(
                    line_number=1,
                    role="user",
                    content="A different first prompt",
                )
            ],
        )

        self.assertEqual(title, "My manually curated title")

    def test_agents_only_subagent_uses_readable_agent_path_title(self) -> None:
        title = _codex_title_from_messages(
            "# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>",
            "sessions/rollout-22222222-2222-4222-8222-222222222222.jsonl",
            [
                SimpleNamespace(
                    line_number=1,
                    role="system",
                    content="# AGENTS.md instructions",
                )
            ],
            {"agent_path": "/root/server_repair_review"},
        )

        self.assertEqual(title, "server repair review")

    def test_subagent_path_overrides_inherited_root_prompt_and_title(self) -> None:
        title = _codex_title_from_messages(
            "Investigate the root production incident",
            "sessions/child.jsonl",
            [
                SimpleNamespace(
                    line_number=1,
                    role="user",
                    content="Investigate the root production incident",
                )
            ],
            {
                "thread_source": "subagent",
                "agent_path": "/root/search_pagination_repair",
                "agent_nickname": "Noether",
            },
        )

        self.assertEqual(title, "search pagination repair")

    def test_changed_documents_invalidate_vectors_and_reset_claims(self) -> None:
        db = SimpleNamespace(execute=AsyncMock())
        document_id = uuid.uuid4()

        asyncio.run(_invalidate_changed_embeddings(db, {document_id}))

        self.assertEqual(db.execute.await_count, 2)
        delete_statement = db.execute.await_args_list[0].args[0]
        update_statement = db.execute.await_args_list[1].args[0]
        self.assertIn("DELETE FROM document_embeddings", str(delete_statement))
        params = update_statement.compile().params
        self.assertEqual(params["embedding_status"], "pending")
        self.assertEqual(params["embedding_attempts"], 0)
        self.assertIsNone(params["embedding_claim_token"])
        self.assertIsNone(params["embedding_claimed_at"])

    def test_inline_transcript_message_repairs_keep_existing_embeddings(self) -> None:
        self.assertFalse(_embedding_input_changed(
            has_inline_content=True,
            previous_message_content="# AGENTS.md\n\nAnswer",
            current_message_content="Answer",
        ))

    def test_externalized_transcript_invalidates_only_for_changed_model_input(self) -> None:
        self.assertTrue(_embedding_input_changed(
            has_inline_content=False,
            previous_message_content="# AGENTS.md\n\nAnswer",
            current_message_content="Answer",
        ))
        self.assertFalse(_embedding_input_changed(
            has_inline_content=False,
            previous_message_content="Answer",
            current_message_content="Answer",
        ))

    def test_conversation_embedding_content_preserves_runtime_limits(self) -> None:
        messages = ["  first  ", "x" * 5_000, None] + ["tail"] * 100

        content = conversation_embedding_content(messages)

        parts = content.split("\n\n")
        self.assertEqual(parts[0], "first")
        self.assertEqual(len(parts[1]), 4_000)
        self.assertEqual(parts.count("tail"), 97)

    def test_fallback_embedding_query_is_bounded_and_deterministic(self) -> None:
        statement = _conversation_embedding_rows_query({uuid.uuid4()})

        sql = " ".join(str(statement).lower().split())
        params = statement.compile().params
        self.assertIn("row_number() over", sql)
        self.assertIn(
            "order by conversation_messages.line_number, "
            "conversation_messages.id",
            sql,
        )
        self.assertIn(100, params.values())
        self.assertIn(4_000, params.values())

    def test_externalized_prefix_read_failure_aborts_the_batch(self) -> None:
        with patch(
            "server.scripts.backfill_conversation_presentation."
            "read_large_content_prefix",
            side_effect=OSError("MinIO unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "externalized transcript"):
                asyncio.run(_externalized_prefix("raw/user/device/job.txt"))

    def test_stats_accumulate_across_committed_batches(self) -> None:
        total = BackfillStats(normalized_codex_prompts=2)
        total.add(BackfillStats(
            normalized_codex_prompts=3,
            renamed_conversations=4,
        ))

        self.assertEqual(total.normalized_codex_prompts, 5)
        self.assertEqual(total.renamed_conversations, 4)


if __name__ == "__main__":
    unittest.main()
