from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.ingest import (  # noqa: E402
    IngestFileRequest,
    IngestMetadataRequest,
    _reject_synthetic_metadata_file_upload,
    ingest_file_chunk,
    ingest_file_endpoint,
    ingest_file_upload,
    ingest_metadata_endpoint,
)
from server.services.thread_metadata_service import (  # noqa: E402
    ThreadTitleUpdateResult,
    apply_codex_thread_title_update,
    apply_conversation_interaction_update,
    codex_thread_documents_select,
    sanitize_explicit_codex_title,
)
from server.services.ingest_service import (  # noqa: E402
    CURRENT_PENDING_QUESTIONS_KEY,
    LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY,
    LIVE_INTERACTION_SIGNALS_KEY,
    PENDING_QUESTION_COUNT_KEY,
)


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        if len(self._rows) != 1:
            raise AssertionError("expected at most one scalar row")
        return self._rows[0]


class _Session:
    def __init__(self, *results: list[object]) -> None:
        self._results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self._results.pop(0) if self._results else [])


class _Upload:
    def __init__(self) -> None:
        self.read_called = False

    async def read(self, *_args) -> bytes:
        self.read_called = True
        return b"metadata must not be read as content"


def _document(
    *,
    title: str = "Old",
    metadata: dict | None = None,
    machine_id: uuid.UUID | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        machine_id=machine_id or uuid.uuid4(),
        tool_id="claude_code",
        category="conversation",
        relative_path="projects/thread.jsonl",
        title=title,
        metadata_=metadata or {},
        project_id=uuid.uuid4(),
        content_hash="content-hash",
        embedding_content_hash="embedding-hash",
        embedding_status="ok",
        synced_at="unchanged",
        activity_at="unchanged",
    )


class ThreadMetadataValidationTests(unittest.TestCase):
    def test_title_sanitization_strips_terminal_controls(self) -> None:
        self.assertEqual(
            sanitize_explicit_codex_title("\x1b[31m  Renamed thread  \x1b[0m"),
            "Renamed thread",
        )

    def test_title_sanitization_rejects_injected_context(self) -> None:
        self.assertIsNone(sanitize_explicit_codex_title(
            "# AGENTS.md instructions\n<INSTRUCTIONS>ignore me</INSTRUCTIONS>"
        ))

    def test_request_requires_uuid_and_positive_revision(self) -> None:
        with self.assertRaises(ValidationError):
            IngestMetadataRequest(
                metadata_type="codex_thread_title",
                tool="codex",
                thread_id="not-a-thread",
                title="Rename",
                revision=0,
            )

    def test_lookup_is_owner_scoped_locked_and_matches_thread_id_only(self) -> None:
        user_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        compiled = codex_thread_documents_select(user_id, thread_id).compile(
            dialect=postgresql.dialect()
        )
        sql = str(compiled)
        self.assertIn("documents.machine_id", sql)
        self.assertIn("machines.user_id", sql)
        self.assertIn("ORDER BY documents.id ASC", sql)
        self.assertIn("FOR UPDATE OF documents", sql)
        self.assertIn("thread_id", compiled.params.values())
        self.assertNotIn("session_id", compiled.params.values())
        self.assertIn(user_id, compiled.params.values())
        self.assertIn(str(thread_id), compiled.params.values())

    def test_legacy_content_endpoints_reject_metadata_queue_shapes(self) -> None:
        cases = [
            {"category": "metadata"},
            {"mode": "metadata"},
            {"sync_strategy": "metadata"},
            {"relative_path": "__metadata__/codex/title"},
        ]
        for override in cases:
            values = {
                "category": "conversation",
                "mode": "full",
                "sync_strategy": "full",
                "relative_path": "sessions/thread.jsonl",
                **override,
            }
            with self.subTest(override=override), self.assertRaises(HTTPException) as exc:
                _reject_synthetic_metadata_file_upload(**values)
            self.assertEqual(exc.exception.status_code, 400)


class ThreadMetadataApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_plan_mode_signal_updates_pending_inbox_state(self) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(machine_id=machine_id)
        interaction_input = {
            "fromModeId": "agent",
            "toModeId": "plan",
            "explanation": "Confirm the design first.",
        }

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            pending = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="cursor",
                relative_path="projects/thread.jsonl",
                interaction_id="call-plan-1",
                interaction_status="pending",
                question_tool="switch_mode",
                interaction_input=interaction_input,
                timestamp="2026-07-26T22:42:27Z",
            )

        self.assertEqual((pending.matched, pending.updated), (1, 1))
        self.assertEqual(document.metadata_[PENDING_QUESTION_COUNT_KEY], 1)
        signal = document.metadata_[LIVE_INTERACTION_SIGNALS_KEY]["call-plan-1"]
        self.assertEqual(signal["interaction"]["interaction_type"], "mode_switch")

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            skipped = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="cursor",
                relative_path="projects/thread.jsonl",
                interaction_id="call-plan-1",
                interaction_status="cancelled",
                question_tool="switch_mode",
                interaction_input=interaction_input,
            )

        self.assertEqual((skipped.matched, skipped.updated), (1, 1))
        self.assertNotIn(PENDING_QUESTION_COUNT_KEY, document.metadata_)
        self.assertNotIn(LIVE_INTERACTION_SIGNALS_KEY, document.metadata_)

    async def test_live_interaction_updates_pending_inbox_state(self) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(machine_id=machine_id)

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            pending = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="question-1",
                interaction_status="pending",
                question_tool="AskUserQuestion",
                interaction_input={
                    "questions": [{
                        "question": "Continue?",
                        "header": "Next",
                        "options": [{"label": "Yes"}],
                    }]
                },
                timestamp="2026-07-24T23:04:05Z",
            )

        self.assertEqual((pending.matched, pending.updated), (1, 1))
        self.assertEqual(
            document.metadata_[CURRENT_PENDING_QUESTIONS_KEY],
            ["question-1"],
        )
        self.assertEqual(document.metadata_[PENDING_QUESTION_COUNT_KEY], 1)
        self.assertIn(
            "question-1",
            document.metadata_[LIVE_INTERACTION_SIGNALS_KEY],
        )

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            answered = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="question-1",
                interaction_status="answered",
                question_tool="AskUserQuestion",
                interaction_input={},
            )

        self.assertEqual((answered.matched, answered.updated), (1, 1))
        self.assertNotIn(CURRENT_PENDING_QUESTIONS_KEY, document.metadata_)
        self.assertNotIn(PENDING_QUESTION_COUNT_KEY, document.metadata_)
        self.assertNotIn(LIVE_INTERACTION_SIGNALS_KEY, document.metadata_)

    async def test_live_claude_permission_request_is_stored(self) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(machine_id=machine_id)

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            result = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="permission-1",
                interaction_status="pending",
                question_tool="PermissionRequest",
                interaction_input={
                    "interaction_type": "permission_request",
                    "requested_tool": "PowerShell",
                    "tool_input": {"command": "git push fork main"},
                },
                timestamp="2026-07-30T16:06:52Z",
            )

        self.assertEqual((result.matched, result.updated), (1, 1))
        signal = document.metadata_[LIVE_INTERACTION_SIGNALS_KEY]["permission-1"]
        self.assertEqual(
            signal["interaction"]["interaction_type"],
            "permission_request",
        )
        self.assertEqual(
            signal["interaction"]["questions"][0]["options"][0]["description"],
            "git push fork main",
        )

    async def test_live_ask_user_permission_wrapper_is_question_and_closes(
        self,
    ) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(machine_id=machine_id)
        wrapper_input = {
            "interaction_type": "permission_request",
            "requested_tool": "AskUserQuestion",
            "tool_input": {
                "questions": [{
                    "question": "Choose the fleet approach.",
                    "header": "Fleet approach",
                    "options": [
                        {
                            "label": "Fresh-venv sweep",
                            "description": "Roll in verified waves.",
                        },
                        {
                            "label": "Fix fleet_deploy first",
                            "description": "Harden the deployment tool first.",
                        },
                    ],
                }]
            },
        }

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            pending = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="wrapper-question-1",
                interaction_status="pending",
                question_tool="PermissionRequest",
                interaction_input=wrapper_input,
                timestamp="2026-07-31T03:05:21Z",
            )

        self.assertEqual((pending.matched, pending.updated), (1, 1))
        signal = document.metadata_[LIVE_INTERACTION_SIGNALS_KEY][
            "wrapper-question-1"
        ]
        interaction = signal["interaction"]
        self.assertNotIn("interaction_type", interaction)
        self.assertEqual(interaction["questions"][0]["header"], "Fleet approach")
        self.assertEqual(
            [
                option["label"]
                for option in interaction["questions"][0]["options"]
            ],
            ["Fresh-venv sweep", "Fix fleet_deploy first"],
        )

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            answered = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="wrapper-question-1",
                interaction_status="answered",
                question_tool="PermissionRequest",
                interaction_input=wrapper_input,
            )

        self.assertEqual((answered.matched, answered.updated), (1, 1))
        self.assertNotIn(CURRENT_PENDING_QUESTIONS_KEY, document.metadata_)
        self.assertNotIn(LIVE_INTERACTION_SIGNALS_KEY, document.metadata_)

    async def test_live_question_prunes_duplicate_ask_user_permission_wrapper(
        self,
    ) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(
            machine_id=machine_id,
            metadata={
                LIVE_INTERACTION_SIGNALS_KEY: {
                    "memento-permission-fleet": {
                        "timestamp": "2026-07-31T03:05:20Z",
                        "tool_name": "PermissionRequest",
                        "interaction": {
                            "id": "memento-permission-fleet",
                            "kind": "question",
                            "interaction_type": "permission_request",
                            "source": "claude_code",
                            "tool_name": "PermissionRequest",
                            "requested_tool": "AskUserQuestion",
                            "questions": [{
                                "id": "permission-decision",
                                "header": "AskUserQuestion",
                                "prompt": (
                                    "Claude Code wants permission to use "
                                    "AskUserQuestion."
                                ),
                                "type": "single_select",
                                "allow_custom": False,
                                "options": [
                                    {
                                        "id": "allow",
                                        "label": "Yes",
                                        "description": (
                                            '{"questions":[{"header":'
                                            '"Fleet approach","question":'
                                            '"Choose the fleet approach.",'
                                            '"options":[{"label":'
                                            '"Fresh-venv sweep"}]}]}'
                                        ),
                                    },
                                    {
                                        "id": "allow-always",
                                        "label": (
                                            "Yes, and allow Claude to use "
                                            "AskUserQuestion for this session"
                                        ),
                                    },
                                    {"id": "deny", "label": "No"},
                                ],
                            }],
                        },
                    }
                },
                CURRENT_PENDING_QUESTIONS_KEY: ["memento-permission-fleet"],
                PENDING_QUESTION_COUNT_KEY: 1,
            },
        )

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            pending = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="toolu-fleet",
                interaction_status="pending",
                question_tool="AskUserQuestion",
                interaction_input={
                    "questions": [{
                        "header": "Fleet approach",
                        "question": "Choose the fleet approach.",
                        "options": [{"label": "Fresh-venv sweep"}],
                    }]
                },
                timestamp="2026-07-31T03:05:21Z",
            )

        self.assertEqual((pending.matched, pending.updated), (1, 1))
        signals = document.metadata_[LIVE_INTERACTION_SIGNALS_KEY]
        self.assertIn("toolu-fleet", signals)
        self.assertNotIn("memento-permission-fleet", signals)
        self.assertEqual(
            signals["toolu-fleet"]["interaction"]["questions"][0]["header"],
            "Fleet approach",
        )

    async def test_question_before_permission_wrapper_stays_single_and_publishes(
        self,
    ) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(machine_id=machine_id)
        question_input = {
            "questions": [{
                "question": "How should the drift monitor be set?",
                "header": "Drift monitor",
                "options": [
                    {"label": "WARN 300s / CRIT 900s"},
                    {"label": "Leave drift as-is"},
                ],
            }]
        }
        wrapper_input = {
            "interaction_type": "permission_request",
            "requested_tool": "AskUserQuestion",
            "tool_input": question_input,
        }

        with (
            patch(
                "server.services.thread_metadata_service.cache_delete_prefix",
                new=AsyncMock(),
            ),
            patch("server.services.sse_service.publish_event") as publish_event,
        ):
            question = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="toolu-drift",
                interaction_status="pending",
                question_tool="AskUserQuestion",
                interaction_input=question_input,
                timestamp="2026-07-31T10:40:30Z",
            )
            wrapper = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="claude_code",
                relative_path="projects/thread.jsonl",
                interaction_id="memento-permission-drift",
                interaction_status="pending",
                question_tool="PermissionRequest",
                interaction_input=wrapper_input,
                timestamp="2026-07-31T10:40:32Z",
            )

        self.assertEqual((question.matched, question.updated), (1, 1))
        self.assertEqual((wrapper.matched, wrapper.updated), (1, 0))
        self.assertEqual(
            document.metadata_[CURRENT_PENDING_QUESTIONS_KEY],
            ["toolu-drift"],
        )
        self.assertEqual(document.metadata_[PENDING_QUESTION_COUNT_KEY], 1)
        self.assertEqual(
            list(document.metadata_[LIVE_INTERACTION_SIGNALS_KEY]),
            ["toolu-drift"],
        )
        publish_event.assert_called_once()
        event_type, event_data = publish_event.call_args.args
        self.assertEqual(event_type, "file_synced")
        self.assertEqual(event_data["document_id"], str(document.id))
        self.assertEqual(
            publish_event.call_args.kwargs["user_id"],
            str(user_id),
        )

    async def test_live_interaction_does_not_reopen_before_latest_human_turn(
        self,
    ) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(
            machine_id=machine_id,
            metadata={
                LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY: "2026-07-24T23:07:30Z"
            },
        )

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            result = await apply_conversation_interaction_update(
                _Session([document]),
                machine_id=machine_id,
                user_id=user_id,
                tool_id="cursor",
                relative_path="projects/thread.jsonl",
                interaction_id="question-1",
                interaction_status="pending",
                question_tool="ask_question",
                interaction_input={
                    "questions": [{
                        "id": "next",
                        "prompt": "Continue?",
                        "options": [{"id": "yes", "label": "Yes"}],
                    }]
                },
                timestamp="2026-07-24T23:04:05Z",
            )

        self.assertEqual((result.matched, result.updated), (1, 0))
        self.assertNotIn(CURRENT_PENDING_QUESTIONS_KEY, document.metadata_)
        self.assertNotIn(PENDING_QUESTION_COUNT_KEY, document.metadata_)
        self.assertNotIn(LIVE_INTERACTION_SIGNALS_KEY, document.metadata_)

    async def test_applies_monotonic_rename_without_touching_content_state(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(machine_id=machine_id)
        db = _Session([document.id], [document])
        user_id = uuid.uuid4()

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ) as invalidate:
            result = await apply_codex_thread_title_update(
                db,
                machine_id=machine_id,
                thread_id=uuid.uuid4(),
                title="New source title",
                title_kind="custom",
                revision=200,
                user_id=user_id,
            )

        self.assertEqual((result.matched, result.updated, result.ignored), (1, 1, 0))
        self.assertEqual(document.title, "New source title")
        self.assertEqual(document.metadata_["codex_title_revision"], 200)
        self.assertEqual(
            document.metadata_["codex_title_revisions"],
            {str(machine_id): 200},
        )
        self.assertEqual(
            document.metadata_["memento_title_source"], "codex_explicit_rename"
        )
        self.assertEqual(document.content_hash, "content-hash")
        self.assertEqual(document.embedding_content_hash, "embedding-hash")
        self.assertEqual(document.embedding_status, "ok")
        self.assertEqual(document.synced_at, "unchanged")
        self.assertEqual(document.activity_at, "unchanged")
        self.assertEqual(invalidate.await_count, 2)
        compiled_statements = [
            statement.compile(dialect=postgresql.dialect())
            for statement in db.statements
        ]
        tsv_updates = [
            statement
            for statement in compiled_statements
            if "content_tsv" in str(statement) and "UPDATE documents" in str(statement)
        ]
        self.assertEqual(len(tsv_updates), 1)
        indexed_values = " ".join(str(value) for value in tsv_updates[0].params.values())
        self.assertIn("new source title", indexed_values.lower())
        self.assertNotIn("old", indexed_values.lower())

    async def test_lower_duplicate_revision_and_manual_title_are_preserved(self) -> None:
        machine_id = uuid.uuid4()
        stale = _document(
            title="Already applied",
            metadata={"codex_title_revision": 500},
            machine_id=machine_id,
        )
        manual = _document(
            metadata={"title_source": "manual"},
            machine_id=machine_id,
        )
        db = _Session([stale.id], [stale, manual])

        result = await apply_codex_thread_title_update(
            db,
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Already applied",
            title_kind="custom",
            revision=400,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.matched, result.updated, result.ignored), (2, 0, 2))
        self.assertEqual(stale.title, "Already applied")
        self.assertEqual(manual.title, "Old")

    async def test_duplicate_revision_and_value_is_idempotent_success(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(
            title="Already applied",
            metadata={
                "codex_title_revision": 200,
                "codex_title_revisions": {str(machine_id): 200},
                "memento_title_source": "codex_explicit_rename",
            },
            machine_id=machine_id,
        )
        result = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Already applied",
            title_kind="custom",
            revision=200,
            user_id=uuid.uuid4(),
        )

        self.assertTrue(result.valid)
        self.assertEqual((result.matched, result.updated, result.ignored), (1, 0, 0))

    async def test_equal_revision_new_title_converges_after_queue_recreation(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(
            title="First title in this millisecond",
            metadata={
                "codex_title_revision": 200,
                "codex_title_revisions": {str(machine_id): 200},
            },
            machine_id=machine_id,
        )
        result = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Second title in this millisecond",
            title_kind="custom",
            revision=200,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.matched, result.updated, result.ignored), (1, 1, 0))
        self.assertEqual(document.title, "Second title in this millisecond")

    async def test_restored_state_db_lower_revision_converges_current_title(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(
            title="Title from the pre-restore database",
            metadata={
                "codex_title_revision": 9_000,
                "codex_title_revisions": {str(machine_id): 9_000},
            },
            machine_id=machine_id,
        )
        result = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Current title from restored database",
            title_kind="custom",
            revision=100,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.matched, result.updated, result.ignored), (1, 1, 0))
        self.assertEqual(document.title, "Current title from restored database")
        self.assertEqual(document.metadata_["codex_title_revision"], 100)
        self.assertEqual(
            document.metadata_["codex_title_revisions"][str(machine_id)],
            100,
        )

    async def test_source_rename_updates_canonical_copy_on_another_host(self) -> None:
        user_id = uuid.uuid4()
        source_machine = uuid.uuid4()
        canonical_machine = uuid.uuid4()
        source = _document(
            title="Source old",
            metadata={"codex_title_revision": 10},
            machine_id=source_machine,
        )
        canonical = _document(
            title="Canonical old",
            metadata={
                "codex_title_revision": 9_000,
                "codex_title_revisions": {str(canonical_machine): 9_000},
            },
            machine_id=canonical_machine,
        )
        db = _Session([source.id], [source, canonical])

        with patch(
            "server.services.thread_metadata_service.cache_delete_prefix",
            new=AsyncMock(),
        ):
            result = await apply_codex_thread_title_update(
                db,
                machine_id=source_machine,
                thread_id=uuid.uuid4(),
                title="Visible on canonical host",
                title_kind="custom",
                revision=11,
                user_id=user_id,
            )

        self.assertEqual((result.matched, result.updated, result.ignored), (2, 2, 0))
        self.assertEqual(source.title, "Visible on canonical host")
        self.assertEqual(canonical.title, "Visible on canonical host")
        self.assertEqual(source.metadata_["codex_title_revision"], 11)
        # Per-host clocks are not comparable: the canonical copy keeps its own
        # scalar while recording the source host's independent revision.
        self.assertEqual(canonical.metadata_["codex_title_revision"], 9_000)
        self.assertEqual(
            canonical.metadata_["codex_title_revisions"][str(source_machine)],
            11,
        )

    async def test_manual_title_is_not_overwritten_by_source_rename(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(
            title="Memento manual title",
            metadata={"title_source": "manual", "codex_title_revision": 100},
            machine_id=machine_id,
        )
        result = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Codex source title",
            title_kind="custom",
            revision=101,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.matched, result.updated, result.ignored), (1, 0, 1))
        self.assertEqual(document.title, "Memento manual title")
        self.assertEqual(
            document.metadata_["codex_title_revisions"][str(machine_id)],
            101,
        )

    async def test_initial_fallback_reconciles_then_custom_title_becomes_explicit(self) -> None:
        machine_id = uuid.uuid4()
        user_id = uuid.uuid4()
        document = _document(title="Opaque rollout", machine_id=machine_id)
        fallback = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Initial prompt",
            title_kind="fallback",
            revision=1,
            user_id=user_id,
        )
        self.assertEqual((fallback.updated, fallback.ignored), (1, 0))
        self.assertEqual(document.title, "Initial prompt")
        self.assertEqual(
            document.metadata_["memento_title_source"],
            "codex_source_fallback",
        )

        custom = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="netbird setup",
            title_kind="custom",
            revision=2,
            user_id=user_id,
        )
        self.assertEqual((custom.updated, custom.ignored), (1, 0))
        self.assertEqual(document.title, "netbird setup")
        self.assertEqual(
            document.metadata_["memento_title_source"],
            "codex_explicit_rename",
        )

    async def test_fallback_acknowledges_revision_without_reverting_explicit_title(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(
            title="netbird setup",
            machine_id=machine_id,
            metadata={
                "memento_title_source": "codex_explicit_rename",
                "codex_title_revision": 2,
                "codex_title_revisions": {str(machine_id): 2},
            },
        )
        result = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Initial prompt",
            title_kind="fallback",
            revision=3,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.updated, result.ignored), (0, 1))
        self.assertEqual(document.title, "netbird setup")
        self.assertEqual(document.metadata_["codex_title_revision"], 3)
        self.assertEqual(
            document.metadata_["codex_title_revisions"][str(machine_id)],
            3,
        )
        self.assertEqual(
            document.metadata_["memento_title_source"],
            "codex_explicit_rename",
        )

    async def test_fallback_cannot_revert_explicit_title_on_canonical_host(self) -> None:
        source_machine = uuid.uuid4()
        canonical_machine = uuid.uuid4()
        source = _document(
            title="netbird setup",
            machine_id=source_machine,
            metadata={
                "memento_title_source": "codex_explicit_rename",
                "codex_title_revision": 2,
                "codex_title_revisions": {str(source_machine): 2},
            },
        )
        canonical = _document(
            title="netbird setup",
            machine_id=canonical_machine,
            metadata={
                "memento_title_source": "codex_explicit_rename",
                "codex_title_revision": 50,
                "codex_title_revisions": {
                    str(source_machine): 2,
                    str(canonical_machine): 50,
                },
            },
        )
        result = await apply_codex_thread_title_update(
            _Session([source.id], [source, canonical]),
            machine_id=source_machine,
            thread_id=uuid.uuid4(),
            title="Initial prompt",
            title_kind="fallback",
            revision=3,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.updated, result.ignored), (0, 2))
        self.assertEqual(source.title, "netbird setup")
        self.assertEqual(canonical.title, "netbird setup")
        self.assertEqual(
            canonical.metadata_["codex_title_revisions"][str(source_machine)],
            3,
        )

    async def test_legacy_unknown_title_cannot_revert_explicit_marker(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(
            title="netbird setup",
            machine_id=machine_id,
            metadata={
                "memento_title_source": "codex_explicit_rename",
                "codex_title_revision": 2,
            },
        )
        result = await apply_codex_thread_title_update(
            _Session([document.id], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Initial prompt",
            title_kind="unknown",
            revision=3,
            user_id=uuid.uuid4(),
        )

        self.assertEqual((result.updated, result.ignored), (0, 1))
        self.assertEqual(document.title, "netbird setup")

    async def test_all_legacy_file_endpoints_reject_metadata_records(self) -> None:
        user = SimpleNamespace(id=uuid.uuid4())
        request = IngestFileRequest(
            tool="codex",
            category="metadata",
            content_type="json",
            relative_path="__metadata__/codex/title",
            hash="metadata-hash",
            sync_strategy="metadata",
            content="",
        )
        with self.assertRaises(HTTPException) as json_exc:
            await ingest_file_endpoint(
                request,
                _collector_user=user,
                _throttle=None,
                db=_Session(),
            )
        self.assertEqual(json_exc.exception.status_code, 400)

        metadata = json.dumps(request.model_dump())
        multipart_upload = _Upload()
        with self.assertRaises(HTTPException) as multipart_exc:
            await ingest_file_upload(
                metadata=metadata,
                content=multipart_upload,
                _collector_user=user,
                _throttle=None,
                db=_Session(),
            )
        self.assertEqual(multipart_exc.exception.status_code, 400)
        self.assertFalse(multipart_upload.read_called)

        chunk_upload = _Upload()
        chunk_metadata = json.dumps({
            **request.model_dump(),
            "chunk_index": 0,
            "total_chunks": 1,
            "upload_id": "metadata-upload",
        })
        with self.assertRaises(HTTPException) as chunk_exc:
            await ingest_file_chunk(
                metadata=chunk_metadata,
                content=chunk_upload,
                _collector_user=user,
                _throttle=None,
                db=_Session(),
            )
        self.assertEqual(chunk_exc.exception.status_code, 400)
        self.assertFalse(chunk_upload.read_called)

    async def test_missing_transcript_returns_404_for_durable_retry(self) -> None:
        user_id = uuid.uuid4()
        request = IngestMetadataRequest(
            metadata_type="codex_thread_title",
            tool="codex",
            thread_id=uuid.uuid4(),
            title="Rename before transcript arrives",
            revision=123,
        )
        machine = SimpleNamespace(id=uuid.uuid4())
        with (
            patch(
                "server.api.ingest.ensure_device",
                new=AsyncMock(return_value=machine),
            ),
            patch(
                "server.api.ingest.apply_codex_thread_title_update",
                new=AsyncMock(
                    return_value=ThreadTitleUpdateResult(0, 0, 0)
                ),
            ),
            self.assertRaises(HTTPException) as exc,
        ):
            await ingest_metadata_endpoint(
                request,
                _collector_user=SimpleNamespace(id=user_id),
                _throttle=None,
                db=_Session(),
                x_device_id="device",
                x_device_name="Device",
                x_device_platform="Windows",
            )
        self.assertEqual(exc.exception.status_code, 404)

    async def test_exact_path_fallback_requires_one_row(self) -> None:
        machine_id = uuid.uuid4()
        document = _document(machine_id=machine_id)
        result = await apply_codex_thread_title_update(
            _Session([], [document]),
            machine_id=machine_id,
            thread_id=uuid.uuid4(),
            title="Legacy row renamed",
            title_kind="custom",
            revision=300,
            user_id=uuid.uuid4(),
            relative_path="sessions/2026/thread.jsonl",
        )
        self.assertEqual((result.matched, result.updated), (1, 1))

        ambiguous = await apply_codex_thread_title_update(
            _Session([], [_document(), _document()]),
            machine_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            title="Ambiguous",
            title_kind="custom",
            revision=301,
            user_id=uuid.uuid4(),
            relative_path="sessions/2026/thread.jsonl",
        )
        self.assertEqual((ambiguous.matched, ambiguous.updated), (0, 0))


if __name__ == "__main__":
    unittest.main()
