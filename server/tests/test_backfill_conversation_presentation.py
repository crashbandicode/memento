from __future__ import annotations

import asyncio
import json
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
    _claude_context_identities,
    _codex_title_from_messages,
    _conversation_embedding_rows_query,
    _cursor_repair_document_ids_query,
    _cursor_title_from_messages,
    _embedding_input_changed,
    _externalized_claude_context_identities,
    _externalized_prefix,
    _has_leading_cursor_timestamp,
    _invalidate_changed_embeddings,
    _is_codex_mirror_pair,
    _normalize_codex_stored_message,
    _normalize_cursor_stored_message,
    _repair_cursor_batch,
)
from server.services.embedding_service import conversation_embedding_content  # noqa: E402


class ConversationPresentationBackfillTests(unittest.TestCase):
    @patch(
        "server.scripts.backfill_conversation_presentation."
        "iter_large_content_lines"
    )
    def test_externalized_claude_context_is_streamed_record_by_record(
        self,
        iter_lines,
    ) -> None:
        iter_lines.return_value = iter(
            [
                json.dumps(
                    {
                        "type": "user",
                        "isMeta": True,
                        "timestamp": "2026-07-20T10:00:00Z",
                        "message": {
                            "role": "user",
                            "content": "Injected workspace context",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-07-20T10:00:01Z",
                        "message": {"role": "assistant", "content": "reply"},
                    }
                ),
            ]
        )

        identities, records = _externalized_claude_context_identities("raw/x")

        self.assertEqual(records, 1)
        self.assertEqual(len(identities), 1)
        iter_lines.assert_called_once_with("raw/x")

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
        response_item.content = "keep going\n[local image metadata]"
        self.assertTrue(_is_codex_mirror_pair(response_item, user_message))
        self.assertFalse(_is_codex_mirror_pair(response_item, repeated_history))
        self.assertFalse(_is_codex_mirror_pair(user_message, user_message))

    def test_cursor_envelope_is_cleaned_and_timestamped_idempotently(self) -> None:
        message = SimpleNamespace(
            role="user",
            content=(
                "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                "(UTC-4)</timestamp>\nMove the workspace"
            ),
            timestamp=None,
        )

        changed, backfilled, preserved = _normalize_cursor_stored_message(message)
        changed_again, backfilled_again, preserved_again = (
            _normalize_cursor_stored_message(message)
        )

        self.assertTrue(changed)
        self.assertTrue(backfilled)
        self.assertFalse(preserved)
        self.assertEqual(message.content, "Move the workspace")
        self.assertEqual(
            message.timestamp,
            datetime.fromisoformat("2026-06-24T09:08:00-04:00"),
        )
        self.assertFalse(changed_again)
        self.assertFalse(backfilled_again)
        self.assertFalse(preserved_again)

    def test_cursor_repair_preserves_existing_structured_timestamp(self) -> None:
        existing = datetime(2026, 6, 24, 13, 9, tzinfo=timezone.utc)
        message = SimpleNamespace(
            role="user",
            content=(
                "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                "(UTC-4)</timestamp>\nKeep this prompt"
            ),
            timestamp=existing,
        )

        changed, backfilled, preserved = _normalize_cursor_stored_message(message)

        self.assertTrue(changed)
        self.assertFalse(backfilled)
        self.assertTrue(preserved)
        self.assertEqual(message.timestamp, existing)
        self.assertEqual(message.content, "Keep this prompt")

    def test_cursor_context_is_separated_and_preserved_in_metadata(self) -> None:
        message = SimpleNamespace(
            role="user",
            content=(
                "<uploaded_documents>\n- C:\\tmp\\report.md\n"
                "</uploaded_documents>\n\nReview the report."
            ),
            metadata_={},
            timestamp=None,
        )

        changed, backfilled, preserved = _normalize_cursor_stored_message(message)

        self.assertTrue(changed)
        self.assertFalse(backfilled)
        self.assertFalse(preserved)
        self.assertEqual(message.content, "Review the report.")
        self.assertIn("uploaded_documents", message.metadata_["session_context"])

    def test_claude_context_scan_uses_native_metadata_not_prompt_text(self) -> None:
        synthetic = json.dumps({
            "type": "user",
            "isMeta": True,
            "timestamp": "2026-06-26T13:29:54.177Z",
            "message": {"role": "user", "content": "Continue the monitor."},
        })
        human = json.dumps({
            "type": "user",
            "timestamp": "2026-06-26T13:30:54.177Z",
            "message": {"role": "user", "content": "Continue the monitor."},
        })

        identities, count = _claude_context_identities(
            f"{synthetic}\n{human}\n"
        )

        self.assertEqual(count, 1)
        self.assertEqual(
            identities,
            {("Continue the monitor.", "2026-06-26T13:29:54")},
        )

    def test_invalid_cursor_timestamp_candidate_is_an_exact_noop(self) -> None:
        content = "<timestamp>sometime soon</timestamp> keep literal text"
        message = SimpleNamespace(content=content, timestamp=None)

        changed, backfilled, preserved = _normalize_cursor_stored_message(message)

        self.assertTrue(_has_leading_cursor_timestamp(content))
        self.assertFalse(changed)
        self.assertFalse(backfilled)
        self.assertFalse(preserved)
        self.assertEqual(message.content, content)
        self.assertIsNone(message.timestamp)

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

    def test_truncated_cursor_context_title_uses_first_human_prompt(self) -> None:
        title, manual_preserved = _cursor_title_from_messages(
            '<plugin_info kind="matched_installed"> display_name: Datadog…',
            [
                SimpleNamespace(
                    line_number=1,
                    role="user",
                    content="Can you inspect the dashboard without modifying it?",
                )
            ],
            {},
        )

        self.assertFalse(manual_preserved)
        self.assertEqual(
            title,
            "Can you inspect the dashboard without modifying it?",
        )

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

    def test_cursor_envelope_title_is_rederived_from_cleaned_prompt(self) -> None:
        title, manual_preserved = _cursor_title_from_messages(
            (
                "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                "(UTC-4)</timestamp> Move the workspace"
            ),
            [
                SimpleNamespace(
                    line_number=1,
                    role="user",
                    content="Move the workspace",
                )
            ],
        )

        self.assertEqual(title, "Move the workspace")
        self.assertFalse(manual_preserved)

    def test_cursor_repair_does_not_overwrite_manual_title(self) -> None:
        current = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp> Deliberate literal title"
        )

        title, manual_preserved = _cursor_title_from_messages(
            current,
            [SimpleNamespace(line_number=1, role="user", content="Other prompt")],
            {"memento_title_source": "memento_user"},
        )

        self.assertEqual(title, current)
        self.assertTrue(manual_preserved)

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
            previous_message_content="<timestamp>metadata</timestamp> " + "A" * 150,
            current_message_content="A" * 150,
        ))
        self.assertFalse(_embedding_input_changed(
            has_inline_content=False,
            previous_message_content="Answer",
            current_message_content="Answer",
        ))
        self.assertFalse(_embedding_input_changed(
            has_inline_content=False,
            previous_message_content="old short text",
            current_message_content="new short text",
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

    def test_cursor_candidate_query_is_narrow_and_guarded(self) -> None:
        statement = _cursor_repair_document_ids_query()

        sql = " ".join(str(statement).lower().split())
        params = statement.compile().params
        self.assertIn("documents.tool_id", sql)
        self.assertIn("documents.category", sql)
        self.assertIn("exists (select 1", sql)
        self.assertGreaterEqual(sql.count("~*"), 1)
        self.assertIn(r"^\s*<timestamp>", params.values())
        self.assertTrue(
            any(
                isinstance(value, str) and "external_links" in value
                for value in params.values()
            )
        )

    def test_cursor_batch_refreshes_dependent_state_and_model_input(self) -> None:
        document_id = uuid.uuid4()
        message_id = uuid.uuid4()
        document = SimpleNamespace(
            id=document_id,
            title=(
                "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                "(UTC-4)</timestamp> Move the workspace"
            ),
            metadata_={},
            activity_at=None,
        )
        message = SimpleNamespace(
            id=message_id,
            document_id=document_id,
            line_number=1,
            role="user",
            content=(
                "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                "(UTC-4)</timestamp>\nMove the workspace " + "x" * 150
            ),
            timestamp=None,
        )
        document_result = SimpleNamespace(all=lambda: [(document, False)])
        message_result = SimpleNamespace(scalars=lambda: [message])
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[document_result, message_result]),
            flush=AsyncMock(),
            delete=AsyncMock(),
        )
        before = "<timestamp>metadata</timestamp> " + "x" * 150
        after = "x" * 150

        async def refresh_activity(_db, doc):
            doc.activity_at = message.timestamp
            return doc.activity_at

        with (
            patch(
                "server.scripts.backfill_conversation_presentation."
                "_conversation_embedding_content_by_document",
                new=AsyncMock(side_effect=[
                    {document_id: before},
                    {document_id: after},
                ]),
            ),
            patch(
                "server.scripts.backfill_conversation_presentation."
                "refresh_document_activity_at",
                new=AsyncMock(side_effect=refresh_activity),
            ) as refresh_mock,
            patch(
                "server.scripts.backfill_conversation_presentation."
                "_refresh_document_search",
                new=AsyncMock(),
            ) as search_mock,
            patch(
                "server.scripts.backfill_conversation_presentation."
                "_invalidate_changed_embeddings",
                new=AsyncMock(),
            ) as invalidate_mock,
        ):
            stats = asyncio.run(_repair_cursor_batch(db, [document_id]))

        self.assertEqual(document.title, "Move the workspace…")
        self.assertFalse(message.content.startswith("<timestamp>"))
        self.assertEqual(
            message.timestamp,
            datetime.fromisoformat("2026-06-24T09:08:00-04:00"),
        )
        self.assertEqual(stats.cursor_candidate_documents, 1)
        self.assertEqual(stats.normalized_cursor_prompts, 1)
        self.assertEqual(stats.backfilled_cursor_message_timestamps, 1)
        self.assertEqual(stats.rederived_cursor_titles, 1)
        self.assertEqual(stats.refreshed_activity_documents, 1)
        self.assertEqual(stats.updated_activity_documents, 1)
        self.assertEqual(stats.refreshed_search_documents, 1)
        self.assertEqual(stats.invalidated_embedding_documents, 1)
        refresh_mock.assert_awaited_once_with(db, document)
        search_mock.assert_awaited_once_with(db, document)
        invalidate_mock.assert_awaited_once_with(db, {document_id})

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
