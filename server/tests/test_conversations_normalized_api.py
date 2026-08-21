from __future__ import annotations

import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.conversations import (  # noqa: E402
    _conversation_location,
    get_conversation,
    get_conversation_messages,
    get_conversation_prompts,
    get_latest_agent_message,
    get_pending_conversation_interactions,
    resolve_conversation_reference,
    search_conversation_messages,
)
from server.services.claude_lineage import (  # noqa: E402
    canonical_permission_fingerprint,
)


class _Result:
    def __init__(self, *, rows: list | None = None, scalar_value=None) -> None:
        self._rows = rows or []
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalar(self):
        return self._scalar_value

    def first(self):
        if self._rows:
            return self._rows[0]
        return self._scalar_value

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()


class ConversationsNormalizedApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        self.owner = SimpleNamespace(id=uuid.uuid4(), role="owner")
        self.doc_id = uuid.uuid4()
        self.doc = SimpleNamespace(
            id=self.doc_id,
            machine_id=uuid.uuid4(),
            tool_id="codex",
            title="Large thread",
            relative_path="sessions/root.jsonl",
            metadata_={
                "project_path": r"C:\Users\intpa\memento",
                "session_id": str(uuid.uuid4()),
                "thread_id": str(uuid.uuid4()),
                "thread_source": "user",
            },
            machine=SimpleNamespace(name="dreamland-yoga (Windows)"),
            project=SimpleNamespace(source_path="/home/patrick/services/memento"),
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=64 * 1024 * 1024,
        )

    def message(
        self,
        line_number: int,
        role: str = "assistant",
        document_id=None,
    ):
        return SimpleNamespace(
            id=line_number,
            document_id=document_id or self.doc_id,
            line_number=line_number,
            role=role,
            message_type=role,
            content=f"message {line_number}",
            metadata_={
                "thinking": "reasoning" if role == "assistant" else None,
                "model": "gpt-5.6-sol" if role == "assistant" else "",
                "reasoning_effort": "xhigh" if role == "assistant" else "",
                "service_tier": "priority" if role == "assistant" else "",
                "agent_mode": "plan" if role == "assistant" else "",
                "tool_name": "shell" if role == "assistant" else "",
                "tool_input": "Get-Item" if role == "assistant" else "",
                "tool_calls": [],
            },
            timestamp=self.now,
        )

    def read_model(self, **overrides):
        values = {
            "document_id": self.doc_id,
            "machine_id": self.doc.machine_id,
            "tool_id": self.doc.tool_id,
            "thread_id": self.doc.metadata_["session_id"],
            "root_thread_id": self.doc.metadata_["session_id"],
            "parent_thread_id": None,
            "agent_id": None,
            "agent_tool_use_id": None,
            "agent_depth": 0,
            "is_subagent": False,
            "message_count": 4306,
            "projected_through_line": 4306,
            "latest_assistant_line": 4306,
            "generation": 7,
            "projection_version": 1,
            "pending_interactions": [],
            "inferred_responses": [],
            "live_activities": [],
            "agent_events": [],
            "runtime": {},
            "lifecycle": {},
            "latest_human_at": "",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    async def test_legacy_conversation_reference_parses_without_duplicate_query(self) -> None:
        db = _Db([])

        resolved = await resolve_conversation_reference(
            str(self.doc_id),
            db=db,
            _user=self.owner,
        )

        self.assertEqual(resolved, self.doc_id)
        self.assertEqual(db.statements, [])

    async def test_native_reference_selects_canonical_cursor_copy(self) -> None:
        native_id = str(uuid.uuid4())
        placeholder = SimpleNamespace(
            id=uuid.uuid4(),
            tool_id="cursor",
            relative_path=(
                f"projects/empty-window/agent-transcripts/{native_id}/"
                f"{native_id}.jsonl"
            ),
            metadata_={"session_id": native_id},
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=1_000,
        )
        canonical = SimpleNamespace(
            id=uuid.uuid4(),
            tool_id="cursor",
            relative_path=(
                f"projects/c-Users-intpa-app/agent-transcripts/{native_id}/"
                f"{native_id}.jsonl"
            ),
            metadata_={"session_id": native_id},
            source_modified_at=self.now - timedelta(days=1),
            activity_at=self.now - timedelta(days=1),
            synced_at=self.now - timedelta(days=1),
            file_size_bytes=100,
        )
        db = _Db([_Result(rows=[placeholder, canonical])])

        resolved = await resolve_conversation_reference(
            f"cursor~{native_id}",
            db=db,
            _user=self.owner,
        )

        self.assertEqual(resolved, canonical.id)
        compiled = db.statements[0].compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("documents.tool_id =", sql)
        self.assertIn("session_id", compiled.params.values())
        self.assertIn(native_id, compiled.params.values())

    async def test_native_reference_is_scoped_to_non_admin_machines(self) -> None:
        native_id = str(uuid.uuid4())
        machine_id = uuid.uuid4()
        user = SimpleNamespace(id=uuid.uuid4(), role="member")
        candidate = SimpleNamespace(
            id=uuid.uuid4(),
            tool_id="codex",
            relative_path=f"sessions/{native_id}.jsonl",
            metadata_={"session_id": native_id},
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=100,
        )
        db = _Db([
            _Result(rows=[(machine_id,)]),
            _Result(rows=[candidate]),
        ])

        resolved = await resolve_conversation_reference(
            f"codex~{native_id}",
            db=db,
            _user=user,
        )

        self.assertEqual(resolved, candidate.id)
        self.assertEqual(len(db.statements), 2)
        compiled = db.statements[1].compile(dialect=postgresql.dialect())
        self.assertIn("documents.machine_id IN", str(compiled))
        self.assertTrue(any(
            isinstance(value, list) and machine_id in value
            for value in compiled.params.values()
        ))

    async def test_invalid_or_missing_conversation_reference_returns_404(self) -> None:
        for conversation_ref, results in (
            ("codex~not-a-uuid", []),
            (f"codex~{uuid.uuid4()}", [_Result(rows=[])]),
        ):
            with self.subTest(conversation_ref=conversation_ref):
                db = _Db(results)
                with self.assertRaises(HTTPException) as caught:
                    await resolve_conversation_reference(
                        conversation_ref,
                        db=db,
                        _user=self.owner,
                    )
                self.assertEqual(caught.exception.status_code, 404)

    async def test_projected_tail_is_count_and_offset_free(self) -> None:
        projection = self.read_model()
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[self.message(4306), self.message(4305)]),
        ])

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=2,
            tail=True,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["total"], 4306)
        self.assertEqual(payload["offset"], 4304)
        self.assertTrue(payload["has_earlier"])
        self.assertFalse(payload["has_more"])
        self.assertEqual(len(db.statements), 2)
        sql = str(db.statements[1].compile()).upper()
        self.assertNotIn("COUNT(", sql)
        self.assertNotIn("OFFSET", sql)
        self.assertIn("DESC", sql)

    async def test_projected_keyset_page_uses_limit_plus_one(self) -> None:
        projection = self.read_model()
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[
                self.message(101),
                self.message(102),
                self.message(103),
            ]),
        ])

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=2,
            tail=False,
            line_number=None,
            context_before=0,
            after_line=100,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            [item["line_number"] for item in payload["messages"]],
            [101, 102],
        )
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_after_line"], 102)
        sql = str(db.statements[1].compile()).upper()
        self.assertIn("LINE_NUMBER >", sql)
        self.assertNotIn("OFFSET", sql)
        self.assertNotIn("COUNT(", sql)

    async def test_messages_prefer_indexed_normalized_rows(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=2),
                _Result(rows=[self.message(1, "user"), self.message(2)]),
            ]
        )

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["total"], 2)
        self.assertEqual([item["line_number"] for item in payload["messages"]], [1, 2])
        self.assertEqual(payload["messages"][1]["tool_name"], "shell")
        self.assertEqual(payload["messages"][1]["model"], "gpt-5.6-sol")
        self.assertEqual(payload["messages"][1]["reasoning_effort"], "xhigh")
        self.assertEqual(payload["messages"][1]["service_tier"], "priority")
        self.assertEqual(payload["messages"][1]["agent_mode"], "plan")
        self.assertEqual(len(db.statements), 3)
        for statement in db.statements:
            self.assertNotIn("documents.content", str(statement.compile()))

    async def test_claude_child_messages_expose_parent_agent_origin(self) -> None:
        self.doc.tool_id = "claude_code"
        self.doc.relative_path = (
            "projects/demo/root-thread/subagents/agent-child.jsonl"
        )
        self.doc.metadata_.update({
            "is_subagent": True,
            "root_session_id": "root-thread",
            "parent_thread_id": "root-thread",
            "session_id": "agent-child",
        })
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=1),
                _Result(rows=[self.message(1, "user")]),
            ]
        )

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][0]["origin"], "parent_agent")

    async def test_cursor_child_messages_expose_parent_agent_origin(self) -> None:
        self.doc.tool_id = "cursor"
        self.doc.relative_path = (
            "projects/demo/agent-transcripts/root-thread/"
            "subagents/cursor-child.jsonl"
        )
        self.doc.metadata_.update({
            "is_subagent": True,
            "root_session_id": "root-thread",
            "parent_thread_id": "root-thread",
            "session_id": "cursor-child",
        })
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=1),
                _Result(rows=[self.message(1, "user")]),
            ]
        )

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][0]["origin"], "parent_agent")

    async def test_root_claude_historical_answered_interaction_is_preserved(
        self,
    ) -> None:
        self.doc.tool_id = "claude_code"
        self.doc.relative_path = "projects/demo/root-thread.jsonl"
        self.doc.metadata_.update({
            "is_subagent": False,
            "session_id": "root-thread",
        })
        question = self.message(27426, "tool")
        question.message_type = "tool_use"
        question.content = "[AskUserQuestion]"
        question.metadata_ = {
            "tool_name": "AskUserQuestion",
            "interaction": {
                "id": "toolu-question",
                "kind": "question",
                "source": "claude_code",
                "tool_name": "AskUserQuestion",
                "questions": [{
                    "id": "next",
                    "header": "Next step",
                    "prompt": "How should I proceed?",
                    "type": "single_select",
                    "allow_custom": True,
                    "options": [
                        {"id": "hold", "label": "Hold"},
                        {"id": "continue", "label": "Continue"},
                    ],
                }],
            },
        }
        response = self.message(27427, "tool")
        response.message_type = "tool_result"
        response.metadata_ = {
            "interaction_response": {
                "interaction_id": "toolu-question",
                "status": "answered",
                "answers": [{
                    "question_id": "next",
                    "text": "Continue",
                    "selected_option_ids": ["continue"],
                }],
                "raw_text": "Continue",
            }
        }
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=2),
            _Result(rows=[question, response]),
        ])

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            payload["messages"][0]["interaction"]["id"],
            "toolu-question",
        )
        self.assertEqual(
            payload["messages"][1]["interaction_response"]["interaction_id"],
            "toolu-question",
        )
        self.assertIsNone(payload["messages"][0]["origin"])
        self.assertIsNone(payload["messages"][1]["origin"])

    async def test_messages_return_exact_cursor_native_timestamp(self) -> None:
        self.doc.tool_id = "cursor"
        observed = self.message(6, "assistant")
        observed.message_type = "cursor_state_thinking"
        observed.content = ""
        observed.timestamp = datetime(
            2026,
            7,
            30,
            2,
            34,
            42,
            569000,
            tzinfo=timezone.utc,
        )
        observed.metadata_.update({
            "source_id": (
                "eed14e37-1842-434a-8aa7-9271f86ac661:thinking"
            ),
            "thinking": "**Investigating tool paths**",
        })
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=1),
            _Result(rows=[observed]),
        ])

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        message = payload["messages"][0]
        self.assertEqual(
            message["timestamp"],
            "2026-07-30T02:34:42.569000+00:00",
        )
        self.assertEqual(message["thinking"], "**Investigating tool paths**")
        self.assertEqual(message["raw_type"], "cursor_state_thinking")

    async def test_messages_expose_task_completion_as_agent_event(self) -> None:
        completion = self.message(2, "tool")
        completion.message_type = "agent_event"
        completion.content = "Start batch 1 pull tlv02+rno completed"
        completion.metadata_.update({
            "source_id": "native-task-notification",
            "tool_name": "Task completion",
            "agent_event": {
                "kind": "completed",
                "activity_type": "shell",
                "task_kind": "shell",
                "task_id": "913821",
                "status": "success",
                "label": "Start batch 1 pull tlv02+rno",
                "result_summary": "Batch 1 pull completed.",
                "model": "gpt-5.6-sol-xhigh",
                "started_at": "2026-07-30T12:50:50.840Z",
                "completed_at": "2026-07-30T12:51:53.073Z",
            },
        })
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=1),
                _Result(rows=[completion]),
            ]
        )

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        message = payload["messages"][0]
        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["raw_type"], "agent_event")
        self.assertEqual(message["tool_name"], "Task completion")
        self.assertEqual(message["agent_event"]["kind"], "completed")
        self.assertEqual(message["agent_event"]["activity_type"], "shell")
        self.assertEqual(message["agent_event"]["task_id"], "913821")
        self.assertEqual(
            message["agent_event"]["result_summary"],
            "Batch 1 pull completed.",
        )
        self.assertEqual(
            message["agent_event"]["model"],
            "gpt-5.6-sol-xhigh",
        )
        self.assertEqual(
            message["agent_event"]["started_at"],
            "2026-07-30T12:50:50.840Z",
        )
        self.assertEqual(
            message["agent_event"]["completed_at"],
            "2026-07-30T12:51:53.073Z",
        )

    async def test_subagent_event_recovers_actual_child_model_and_effort(self) -> None:
        self.doc.tool_id = "claude_code"
        launch = self.message(2, "tool")
        launch.message_type = "agent_event"
        launch.metadata_ = {
            "agent_event": {
                "version": 2,
                "source": "claude_agent",
                "kind": "started",
                "activity_type": "subagent",
                "agent_tool_use_id": "toolu-stale",
                "agent_thread_id": "aa53b331b57f1bde5",
                "label": "Tune stale threshold to CLEAN_PERIOD",
            },
        }
        child_metadata = {
            "is_subagent": True,
            "agent_id": "aa53b331b57f1bde5",
            "agent_tool_use_id": "toolu-stale",
            "_assistant_model": "claude-opus-4-8",
            "_assistant_reasoning_effort": "xhigh",
            "subagent_lifecycle_status": "completed",
            "subagent_lifecycle_source": "claude_child_transcript",
            "subagent_lifecycle_at": "2026-08-01T13:56:28.772Z",
        }
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=1),
            _Result(rows=[launch]),
            _Result(rows=[child_metadata]),
        ])

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=50,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        event = payload["messages"][0]["agent_event"]
        self.assertEqual(event["model"], "claude-opus-4-8")
        self.assertEqual(event["model_family"], "anthropic")
        self.assertEqual(event["reasoning_effort"], "xhigh")
        self.assertEqual(event["resolved_status"], "completed")
        self.assertEqual(event["completed_at"], "2026-08-01T13:56:28.772Z")

    async def test_around_line_uses_index_and_reports_row_offset(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=4306),
                _Result(rows=[self.message(4282), self.message(4283)]),
            ]
        )

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=120,
            line_number=4294,
            context_before=12,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["total"], 4306)
        self.assertEqual(payload["offset"], 4281)
        message_sql = str(db.statements[2].compile())
        self.assertIn("conversation_messages.line_number >=", message_sql)
        self.assertNotIn("documents.content", message_sql)

    async def test_tail_returns_latest_normalized_rows(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=4306),
                _Result(rows=[self.message(4306), self.message(4305)]),
            ]
        )

        payload = await get_conversation_messages(
            self.doc_id,
            offset=0,
            limit=2,
            tail=True,
            line_number=None,
            context_before=0,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["total"], 4306)
        self.assertEqual(payload["offset"], 4304)
        self.assertEqual(
            [item["line_number"] for item in payload["messages"]],
            [4305, 4306],
        )
        message_sql = str(db.statements[2].compile())
        self.assertNotIn("OFFSET", message_sql.upper())
        self.assertIn("DESC", message_sql.upper())
        self.assertNotIn("documents.content", message_sql)

    async def test_pending_interactions_include_out_of_window_subagent_questions(
        self,
    ) -> None:
        child_id = uuid.uuid4()
        answered = self.message(10)
        answered.metadata_ = {
            "interaction": {
                "id": "answered-question",
                "kind": "question",
                "source": "codex",
                "tool_name": "request_user_input",
                "questions": [],
            }
        }
        answered.timestamp = self.now
        response = self.message(11, "user")
        response.metadata_ = {
            "interaction_response": {
                "interaction_id": "answered-question",
                "status": "answered",
                "answers": [],
                "raw_text": "Done",
            }
        }
        response.timestamp = self.now + timedelta(minutes=1)
        abandoned = self.message(12)
        abandoned.metadata_ = {
            "interaction": {
                "id": "question-before-restart",
                "kind": "question",
                "source": "codex",
                "tool_name": "request_user_input",
                "questions": [],
            }
        }
        abandoned.timestamp = self.now + timedelta(minutes=2)
        resumed = self.message(13, "user")
        resumed.content = "Keep going, you got rebooted."
        resumed.metadata_ = {}
        resumed.timestamp = self.now + timedelta(minutes=3)
        pending = self.message(2, document_id=child_id)
        pending.metadata_ = {
            "model": "gpt-5.6-sol",
            "agent_mode": "plan",
            "tool_calls": [
                {
                    "name": "request_user_input",
                    "input": "{}",
                    "interaction": {
                        "id": "child-question",
                        "kind": "question",
                        "source": "codex",
                        "tool_name": "request_user_input",
                        "questions": [
                            {
                                "id": "scope",
                                "prompt": "Which scope?",
                                "type": "single_select",
                                "allow_custom": True,
                                "options": [],
                            }
                        ],
                    },
                }
            ],
        }
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(
                    rows=[
                        (self.doc_id, self.doc.title, self.doc.metadata_),
                        (child_id, "Child", {}),
                    ]
                ),
                _Result(rows=[answered, response, abandoned, resumed, pending]),
            ]
        )

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["interactions"][0]["interaction"]["id"],
            "child-question",
        )
        self.assertEqual(
            payload["interactions"][0]["document_id"],
            str(child_id),
        )
        self.assertEqual(
            payload["inferred_responses"][0]["response"]["interaction_id"],
            "question-before-restart",
        )
        self.assertEqual(
            payload["inferred_responses"][0]["response"]["raw_text"],
            resumed.content,
        )
        pending_sql = str(db.statements[2].compile(dialect=postgresql.dialect()))
        self.assertIn("conversation_messages.metadata ? ", pending_sql)

    async def test_pending_interactions_include_live_metadata_preview(self) -> None:
        self.doc.metadata_["_live_interaction_signals"] = {
            "live-question": {
                "timestamp": "2026-07-24T23:04:05Z",
                "interaction": {
                    "id": "live-question",
                    "kind": "question",
                    "source": "codex",
                    "tool_name": "request_user_input",
                    "questions": [
                        {
                            "id": "speed",
                            "prompt": "Proceed?",
                            "type": "single_select",
                            "allow_custom": True,
                            "options": [],
                        }
                    ],
                },
            }
        }
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(
                    rows=[
                        (self.doc_id, self.doc.title, self.doc.metadata_),
                    ]
                ),
                _Result(rows=[]),
            ]
        )

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["interactions"][0]["interaction"]["id"],
            "live-question",
        )
        self.assertEqual(payload["interactions"][0]["line_number"], 0)

    async def test_live_claude_preview_repairs_stored_option_mojibake(self) -> None:
        self.doc.tool_id = "claude_code"
        self.doc.metadata_["_live_interaction_signals"] = {
            "toolu-side-tail": {
                "timestamp": "2026-07-31T17:46:17Z",
                "interaction": {
                    "id": "toolu-side-tail",
                    "kind": "question",
                    "source": "claude_code",
                    "tool_name": "AskUserQuestion",
                    "questions": [
                        {
                            "id": "Side-tail",
                            "header": "Side-tail",
                            "prompt": (
                                "Proceed to eliminate the accept/switch side-tail "
                                "and source forwarding from JOB_START?"
                            ),
                            "type": "single_select",
                            "allow_custom": True,
                            "options": [
                                {
                                    "id": "Yes â€” delete it now",
                                    "label": "Yes â€” delete it now",
                                },
                                {
                                    "id": "Not yet â€” keep side-tail",
                                    "label": "Not yet â€” keep side-tail",
                                },
                            ],
                        },
                        {
                            "id": "Queue freshness",
                            "header": "Queue freshness",
                            "prompt": (
                                "JOB_SWITCH queue freshness once the side-tail "
                                "is gone?"
                            ),
                            "type": "single_select",
                            "allow_custom": True,
                            "options": [],
                        },
                    ],
                },
            }
        }
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 1)
        item = payload["interactions"][0]
        self.assertEqual(item["line_number"], 0)
        self.assertEqual(len(item["interaction"]["questions"]), 2)
        options = item["interaction"]["questions"][0]["options"]
        self.assertEqual(
            [option["label"] for option in options],
            ["Yes — delete it now", "Not yet — keep side-tail"],
        )
        self.assertNotIn("â€”", json.dumps(item, ensure_ascii=False))

    async def test_root_claude_pending_permission_preview_is_visible(self) -> None:
        self.doc.tool_id = "claude_code"
        self.doc.relative_path = "projects/demo/root-thread.jsonl"
        self.doc.metadata_.update({
            "is_subagent": "false",
            "root_session_id": "root-thread",
            "_live_interaction_signals": {
                "permission-1": {
                    "timestamp": "2026-07-30T16:06:52Z",
                    "interaction": {
                        "id": "permission-1",
                        "kind": "question",
                        "interaction_type": "permission_request",
                        "source": "claude_code",
                        "tool_name": "PermissionRequest",
                        "requested_tool": "PowerShell",
                        "questions": [{
                            "id": "permission-decision",
                            "header": "PowerShell",
                            "prompt": (
                                "Claude Code wants permission to use PowerShell."
                            ),
                            "type": "single_select",
                            "allow_custom": False,
                            "options": [
                                {"id": "allow", "label": "Yes"},
                                {
                                    "id": "allow-always",
                                    "label": (
                                        "Yes, and allow Claude to use "
                                        "PowerShell for this session"
                                    ),
                                },
                                {"id": "deny", "label": "No"},
                            ],
                        }],
                    },
                }
            },
        })
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(
                rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]
            ),
            _Result(rows=[]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 1)
        interaction = payload["interactions"][0]["interaction"]
        self.assertEqual(interaction["interaction_type"], "permission_request")
        self.assertEqual(
            [option["id"] for option in interaction["questions"][0]["options"]],
            ["allow", "allow-always", "deny"],
        )
        self.assertEqual(len(payload["inline_interactions"]), 1)
        self.assertEqual(
            payload["inline_interactions"][0]["status"],
            "pending",
        )

    async def test_answered_permission_history_is_inline_not_pending(self) -> None:
        self.doc.tool_id = "claude_code"
        interaction = {
            "id": "permission-answered",
            "kind": "question",
            "interaction_type": "permission_request",
            "source": "claude_code",
            "tool_name": "PermissionRequest",
            "requested_tool": "Bash",
            "questions": [{
                "id": "permission-decision",
                "header": "Bash",
                "prompt": "Claude Code wants permission to use Bash.",
                "type": "single_select",
                "allow_custom": False,
                "options": [
                    {"id": "allow", "label": "Yes"},
                    {"id": "deny", "label": "No"},
                ],
            }],
        }
        response = {
            "kind": "question_response",
            "interaction_id": "permission-answered",
            "status": "answered",
            "answers": [],
            "raw_text": "",
        }
        self.doc.metadata_["_interaction_history"] = [
            {
                "interaction": interaction,
                "timestamp": "2026-07-30T16:06:52Z",
                "status": "answered",
                "response": response,
                "anchor_line_number": 0,
            }
        ]
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["interactions"], [])
        self.assertEqual(len(payload["inline_interactions"]), 1)
        item = payload["inline_interactions"][0]
        self.assertEqual(item["interaction"], interaction)
        self.assertEqual(item["status"], "answered")
        self.assertEqual(item["response"], response)

    async def test_live_shell_activity_is_returned_inline(self) -> None:
        self.doc.tool_id = "claude_code"
        activity_at = datetime.now(timezone.utc).isoformat()
        self.doc.metadata_["_live_shell_activities"] = {
            "toolu-shell-live": {
                "id": "toolu-shell-live",
                "activity_type": "shell",
                "status": "running",
                "tool_name": "PowerShell",
                "command": "Start-Sleep -Seconds 30",
                "started_at": activity_at,
                "updated_at": activity_at,
                "anchor_line_number": 34406,
            }
        }
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(len(payload["live_activities"]), 1)
        activity = payload["live_activities"][0]
        self.assertEqual(activity["activity_id"], "toolu-shell-live")
        self.assertEqual(activity["status"], "running")
        self.assertEqual(activity["line_number"], 34406)
        self.assertEqual(activity["command"], "Start-Sleep -Seconds 30")

    async def test_projected_shell_activities_apply_running_and_terminal_ttls(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)

        def activity(activity_id: str, status: str, age: timedelta | None):
            timestamp = (now - age).isoformat() if age is not None else None
            return {
                "document_id": str(self.doc_id),
                "activity_id": activity_id,
                "activity_type": "shell",
                "status": status,
                "tool_name": "PowerShell",
                "command": "python worker.py",
                "started_at": timestamp,
                "updated_at": timestamp,
            }

        projection = self.read_model(live_activities=[
            activity("recent-running", "running", timedelta(hours=23)),
            activity("stale-running", "running", timedelta(hours=25)),
            activity("recent-terminal", "completed", timedelta(minutes=59)),
            activity("stale-terminal", "failed", timedelta(minutes=61)),
            activity("missing-time", "running", None),
        ])
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[(self.doc, projection)]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            {
                item["activity_id"]
                for item in payload["live_activities"]
            },
            {"recent-running", "recent-terminal"},
        )

    async def test_fallback_shell_activities_use_same_ttls_and_reject_no_time(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.doc.metadata_["_live_shell_activities"] = {
            "recent-running": {
                "id": "recent-running",
                "status": "running",
                "tool_name": "PowerShell",
                "command": "python live.py",
                "updated_at": (now - timedelta(hours=23)).isoformat(),
            },
            "stale-running": {
                "id": "stale-running",
                "status": "running",
                "tool_name": "PowerShell",
                "command": "python stale.py",
                "updated_at": (now - timedelta(hours=25)).isoformat(),
            },
            "recent-terminal": {
                "id": "recent-terminal",
                "status": "completed",
                "tool_name": "PowerShell",
                "command": "python done.py",
                "updated_at": (now - timedelta(minutes=59)).isoformat(),
            },
            "stale-terminal": {
                "id": "stale-terminal",
                "status": "failed",
                "tool_name": "PowerShell",
                "command": "python failed.py",
                "updated_at": (now - timedelta(minutes=61)).isoformat(),
            },
            "missing-time": {
                "id": "missing-time",
                "status": "running",
                "tool_name": "PowerShell",
                "command": "python unknown.py",
            },
        }
        canonical_without_time = SimpleNamespace(
            id=101,
            document_id=self.doc_id,
            line_number=88,
            message_type="tool_call",
            timestamp=None,
            metadata_={
                "tool_call_id": "canonical-missing-time",
                "tool_name": "exec_command",
                "tool_input": '{"command":"python unknown.py"}',
            },
        )
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[]),
            _Result(rows=[canonical_without_time]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            {
                item["activity_id"]
                for item in payload["live_activities"]
            },
            {"recent-running", "recent-terminal"},
        )

    async def test_recent_canonical_shell_call_is_inferred_as_running(self) -> None:
        self.doc.tool_id = "codex"
        shell_call = SimpleNamespace(
            id=101,
            document_id=self.doc_id,
            line_number=88,
            message_type="tool_call",
            timestamp=datetime.now(timezone.utc),
            metadata_={
                "tool_call_id": "call-shell-live",
                "tool_name": "exec_command",
                "tool_input": '{"command":"python -m pytest -q"}',
            },
        )
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[]),
            _Result(rows=[shell_call]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(len(payload["live_activities"]), 1)
        activity = payload["live_activities"][0]
        self.assertEqual(activity["activity_id"], "call-shell-live")
        self.assertEqual(activity["status"], "running")
        self.assertEqual(activity["line_number"], 88)
        self.assertEqual(activity["command"], "python -m pytest -q")

        shell_output = SimpleNamespace(
            id=102,
            document_id=self.doc_id,
            line_number=89,
            message_type="tool_output",
            timestamp=datetime.now(timezone.utc),
            metadata_={"tool_call_id": "call-shell-live"},
        )
        resolved_db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[]),
            _Result(rows=[shell_output, shell_call]),
        ])
        resolved = await get_pending_conversation_interactions(
            self.doc_id,
            db=resolved_db,
            _user=self.owner,
        )
        self.assertEqual(resolved["live_activities"], [])

    async def test_pending_interactions_recover_ask_user_permission_wrapper(
        self,
    ) -> None:
        good = {
            "id": "toolu-drift",
            "kind": "question",
            "source": "claude_code",
            "tool_name": "AskUserQuestion",
            "questions": [{
                "id": "drift",
                "header": "Drift monitor",
                "prompt": "How should I tune drift?",
                "type": "single_select",
                "allow_custom": True,
                "options": [
                    {"id": "two-tier", "label": "Two-tier"},
                    {"id": "leave", "label": "Leave as-is"},
                ],
            }],
        }
        malformed = {
            "id": "memento-permission-drift",
            "kind": "question",
            "interaction_type": "permission_request",
            "source": "claude_code",
            "tool_name": "PermissionRequest",
            "requested_tool": "AskUserQuestion",
            "questions": [{
                "id": "permission-decision",
                "header": "AskUserQuestion",
                "prompt": "Claude Code wants permission to use AskUserQuestion.",
                "type": "single_select",
                "allow_custom": False,
                "options": [
                    {
                        "id": "allow",
                        "label": "Yes",
                        "description": json.dumps({
                            "questions": [{
                                "header": "Drift monitor",
                                "question": "How should I tune drift?",
                                "options": [
                                    {"label": "Two-tier"},
                                    {"label": "Leave as-is"},
                                ],
                            }]
                        }),
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
        }
        self.doc.metadata_["_live_interaction_signals"] = {
            "memento-permission-drift": {
                "timestamp": "2026-07-31T10:00:00Z",
                "interaction": malformed,
            },
            "toolu-drift": {
                "timestamp": "2026-07-31T10:00:01Z",
                "interaction": good,
            },
        }
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(
                rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]
            ),
            _Result(rows=[]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 1)
        interaction = payload["interactions"][0]["interaction"]
        self.assertNotIn("interaction_type", interaction)
        self.assertEqual(interaction["questions"][0]["header"], "Drift monitor")
        self.assertNotIn(
            "Permission request",
            interaction["questions"][0]["prompt"],
        )

    async def test_pending_interactions_ignore_question_replayed_after_human_turn(
        self,
    ) -> None:
        interaction = {
            "id": "replayed-question",
            "kind": "question",
            "source": "cursor",
            "tool_name": "ask_question",
            "questions": [{
                "id": "scope",
                "prompt": "Which scope?",
                "type": "single_select",
                "allow_custom": True,
                "options": [],
            }],
        }
        original = self.message(10)
        original.metadata_ = {"interaction": interaction}
        original.timestamp = self.now
        human = self.message(11, "user")
        human.content = "Use the first option."
        human.metadata_ = {}
        human.timestamp = self.now + timedelta(minutes=1)
        replay = self.message(12)
        replay.metadata_ = {"interaction": interaction}
        replay.timestamp = self.now
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(
                    rows=[
                        (self.doc_id, self.doc.title, self.doc.metadata_),
                    ]
                ),
                _Result(rows=[original, human, replay]),
            ]
        )

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["interactions"], [])
        self.assertEqual(
            payload["inferred_responses"][0]["response"]["interaction_id"],
            "replayed-question",
        )

    async def test_latest_agent_message_uses_indexed_assistant_line(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=4298),
            ]
        )

        payload = await get_latest_agent_message(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload, {"line_number": 4298})
        self.assertEqual(len(db.statements), 2)
        latest_sql = str(db.statements[1].compile()).upper()
        self.assertIn("COALESCE", latest_sql)
        self.assertIn("ORDER BY", latest_sql)
        self.assertIn("DESC", latest_sql)
        self.assertNotIn("DOCUMENTS.CONTENT", latest_sql)

    async def test_prompts_prefer_normalized_rows(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=2),
                _Result(
                    rows=[
                        (7, 12, "A prompt", self.now, {}),
                        (
                            8,
                            13,
                            "The selected answer",
                            self.now,
                            {"interaction_response": {"interaction_id": "question-1"}},
                        ),
                        (
                            9,
                            14,
                            "[AUTO HEALTH-CHECK — runs every 5 min]\nCheck status.",
                            self.now,
                            {},
                        ),
                    ]
                ),
            ]
        )

        payload = await get_conversation_prompts(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["prompts"][0]["line_number"], 12)
        self.assertEqual(payload["prompts"][0]["content"], "A prompt")
        self.assertEqual(len(payload["prompts"]), 1)
        for statement in db.statements:
            self.assertNotIn("documents.content", str(statement.compile()))
        prompt_sql = str(db.statements[2].compile()).upper()
        self.assertNotIn(" LIMIT ", prompt_sql)

    async def test_prompt_refresh_reads_only_projection_delta(self) -> None:
        projection = self.read_model(
            projected_through_line=45,
        )
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(
                rows=[
                    SimpleNamespace(
                        message_id=9,
                        line_number=44,
                        content="New prompt",
                        timestamp=self.now,
                    )
                ]
            ),
        ])

        payload = await get_conversation_prompts(
            self.doc_id,
            after_line=12,
            generation=7,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            [item["line_number"] for item in payload["prompts"]],
            [44],
        )
        self.assertFalse(payload["reset"])
        self.assertEqual(payload["projected_through_line"], 45)
        self.assertEqual(len(db.statements), 2)
        identity_sql = str(db.statements[0].compile()).upper()
        prompt_sql = str(db.statements[1].compile()).upper()
        self.assertIn("CONVERSATION_READ_MODELS", identity_sql)
        self.assertIn("CONVERSATION_PROMPT_PROJECTIONS", prompt_sql)
        self.assertIn("LINE_NUMBER >", prompt_sql)
        self.assertNotIn("CONVERSATION_MESSAGES", prompt_sql)
        self.assertNotIn("OFFSET", prompt_sql)

    async def test_projected_pending_interactions_do_not_replay_messages(
        self,
    ) -> None:
        interaction = {
            "id": "projected-question",
            "kind": "question",
            "source": "codex",
            "tool_name": "request_user_input",
            "questions": [],
        }
        projection = self.read_model(
            pending_interactions=[{
                "document_id": str(self.doc_id),
                "message_id": 88,
                "line_number": 88,
                "interaction": interaction,
                "timestamp": self.now.isoformat(),
            }]
        )
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[(self.doc, projection)]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["interactions"][0]["interaction"]["id"],
            "projected-question",
        )
        self.assertEqual(len(db.statements), 2)
        for statement in db.statements:
            sql = str(statement.compile(dialect=postgresql.dialect())).upper()
            self.assertNotIn("CONVERSATION_MESSAGES", sql)
            self.assertNotIn("METADATA ?", sql)

    async def test_projected_permission_history_drops_superseded_branch_anchor(
        self,
    ) -> None:
        self.doc.tool_id = "claude_code"
        def history_entry(interaction_id: str, anchor: int) -> dict:
            return {
                "interaction": {
                    "id": interaction_id,
                    "kind": "question",
                    "source": "claude_code",
                    "tool_name": "PermissionRequest",
                    "requested_tool": "Bash",
                    "interaction_type": "permission_request",
                    "questions": [],
                },
                "timestamp": "2026-08-13T23:24:44Z",
                "status": "answered",
                "response": {
                    "kind": "question_response",
                    "interaction_id": interaction_id,
                    "status": "answered",
                    "answers": [],
                    "raw_text": "",
                },
                "anchor_line_number": anchor,
            }

        self.doc.metadata_["_interaction_history"] = [
            history_entry("permission-on-current-branch", 812),
            history_entry("permission-from-abandoned-branch", 2203),
        ]
        projection = self.read_model(projected_through_line=813)
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[(self.doc, projection)]),
        ])

        payload = await get_pending_conversation_interactions(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            [
                item["interaction"]["id"]
                for item in payload["inline_interactions"]
            ],
            ["permission-on-current-branch"],
        )
        self.assertEqual(len(db.statements), 2)
        for statement in db.statements:
            sql = str(statement.compile(dialect=postgresql.dialect())).upper()
            self.assertNotIn("CONVERSATION_MESSAGES", sql)

    async def test_claude_lineage_history_predicate_matches_projected_and_fallback(self) -> None:
        self.doc.tool_id = "claude_code"

        def history(interaction_id: str, requested_tool: str, origin: dict) -> dict:
            return {
                "interaction": {
                    "id": interaction_id,
                    "kind": "question",
                    "source": "claude_code",
                    "tool_name": "PermissionRequest",
                    "requested_tool": requested_tool,
                    "tool_input": {"command": interaction_id},
                    "interaction_type": "permission_request",
                    "questions": [],
                },
                "timestamp": "2026-08-14T12:00:00Z",
                "status": "answered",
                "response": {"kind": "question_response", "status": "answered"},
                "anchor_line_number": 5,
                "interaction_origin": origin,
            }

        def origin(
            kind: str,
            record_uuid: str,
            requested_tool: str,
            interaction_id: str,
            *,
            agent_id: str = "",
            is_sidechain: bool = False,
        ) -> dict:
            return {
                "version": 1,
                "kind": kind,
                "record_uuid": record_uuid,
                "parent_uuid": "",
                "tool_use_id": "tool-use",
                "fingerprint": canonical_permission_fingerprint(
                    requested_tool,
                    {"command": interaction_id},
                ),
                "agent_id": agent_id,
                "is_sidechain": is_sidechain,
            }

        self.doc.metadata_["_interaction_history"] = [
            history(
                "inactive",
                "Bash",
                origin("claude_record", "inactive-record", "Bash", "inactive"),
            ),
            history(
                "active",
                "PowerShell",
                origin(
                    "claude_record",
                    "active-record",
                    "PowerShell",
                    "active",
                ),
            ),
            history(
                "subagent",
                "Read",
                origin(
                    "claude_subagent_record",
                    "child-record",
                    "Read",
                    "subagent",
                    agent_id="agent-child",
                    is_sidechain=True,
                ),
            ),
            history(
                "hook",
                "Write",
                origin("hook_only", "", "Write", "hook"),
            ),
        ]
        self.doc.metadata_["_live_interaction_signals"] = {
            "inactive-live": {
                "interaction": history(
                    "inactive-live",
                    "Bash",
                    {},
                )["interaction"],
                "timestamp": "2026-08-14T12:01:00Z",
                "interaction_origin": origin(
                    "claude_record",
                    "inactive-live-record",
                    "Bash",
                    "inactive-live",
                ),
            },
            "active-live": {
                "interaction": history(
                    "active-live",
                    "PowerShell",
                    {},
                )["interaction"],
                "timestamp": "2026-08-14T12:01:01Z",
                "interaction_origin": origin(
                    "claude_record",
                    "active-live-record",
                    "PowerShell",
                    "active-live",
                ),
            },
            "subagent-live": {
                "interaction": history(
                    "subagent-live",
                    "Read",
                    {},
                )["interaction"],
                "timestamp": "2026-08-14T12:01:02Z",
                "interaction_origin": origin(
                    "claude_subagent_record",
                    "child-live-record",
                    "Read",
                    "subagent-live",
                    agent_id="agent-child",
                    is_sidechain=True,
                ),
            },
            "hook-live": {
                "interaction": history(
                    "hook-live",
                    "Write",
                    {},
                )["interaction"],
                "timestamp": "2026-08-14T12:01:03Z",
                "interaction_origin": origin(
                    "hook_only",
                    "",
                    "Write",
                    "hook-live",
                ),
            },
        }
        projection = self.read_model(projected_through_line=5)
        projected_db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[(self.doc, projection)]),
            _Result(rows=[
                (self.doc_id, "inactive-record", False, False, False, ""),
                (self.doc_id, "active-record", True, False, False, ""),
                (self.doc_id, "child-record", True, True, True, "agent-child"),
                (self.doc_id, "inactive-live-record", False, False, False, ""),
                (self.doc_id, "active-live-record", True, False, False, ""),
                (self.doc_id, "child-live-record", True, True, True, "agent-child"),
            ]),
        ])
        projected = await get_pending_conversation_interactions(
            self.doc_id,
            db=projected_db,
            _user=self.owner,
        )

        fallback_doc = SimpleNamespace(**self.doc.__dict__)
        fallback_db = _Db([
            _Result(scalar_value=fallback_doc),
            _Result(rows=[(self.doc_id, self.doc.title, self.doc.metadata_)]),
            _Result(rows=[(self.doc_id, 5)]),
            _Result(rows=[]),
            _Result(rows=[
                (self.doc_id, "inactive-record", False, False, False, ""),
                (self.doc_id, "active-record", True, False, False, ""),
                (self.doc_id, "child-record", True, True, True, "agent-child"),
                (self.doc_id, "inactive-live-record", False, False, False, ""),
                (self.doc_id, "active-live-record", True, False, False, ""),
                (self.doc_id, "child-live-record", True, True, True, "agent-child"),
            ]),
            _Result(rows=[]),
        ])
        fallback = await get_pending_conversation_interactions(
            self.doc_id,
            db=fallback_db,
            _user=self.owner,
        )

        expected = ["active", "active-live", "hook", "hook-live"]
        self.assertEqual(
            sorted(
                item["interaction"]["id"]
                for item in projected["inline_interactions"]
            ),
            expected,
        )
        self.assertEqual(
            sorted(
                item["interaction"]["id"]
                for item in fallback["inline_interactions"]
            ),
            expected,
        )

    async def test_claude_child_parent_messages_are_not_prompt_navigation(self) -> None:
        self.doc.tool_id = "claude_code"
        self.doc.relative_path = (
            "projects/demo/root-thread/subagents/agent-child.jsonl"
        )
        self.doc.metadata_.update({
            "is_subagent": True,
            "root_session_id": "root-thread",
            "parent_thread_id": "root-thread",
            "session_id": "agent-child",
        })
        db = _Db([_Result(scalar_value=self.doc)])

        payload = await get_conversation_prompts(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload, {"prompts": []})
        self.assertEqual(len(db.statements), 1)

    async def test_cursor_child_parent_messages_are_not_prompt_navigation(self) -> None:
        self.doc.tool_id = "cursor"
        self.doc.relative_path = (
            "projects/demo/agent-transcripts/root-thread/"
            "subagents/cursor-child.jsonl"
        )
        self.doc.metadata_.update({
            "is_subagent": True,
            "root_session_id": "root-thread",
            "parent_thread_id": "root-thread",
            "session_id": "cursor-child",
        })
        db = _Db([_Result(scalar_value=self.doc)])

        payload = await get_conversation_prompts(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload, {"prompts": []})
        self.assertEqual(len(db.statements), 1)

    async def test_root_claude_false_string_remains_prompt_navigation(self) -> None:
        self.doc.tool_id = "claude_code"
        self.doc.relative_path = "projects/demo/root-thread.jsonl"
        self.doc.metadata_.update({
            "is_subagent": "false",
            "root_session_id": "root-thread",
            "parent_thread_id": "root-thread",
            "session_id": "root-thread",
        })
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=1),
            _Result(rows=[(7, 12, "A root prompt", self.now, {})]),
        ])

        payload = await get_conversation_prompts(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            [(item["line_number"], item["content"]) for item in payload["prompts"]],
            [(12, "A root prompt")],
        )

    @patch(
        "server.api.conversations.suggest_corrected_query",
        new_callable=AsyncMock,
    )
    async def test_search_returns_bounded_normalized_hits_in_line_order(
        self,
        correction: AsyncMock,
    ) -> None:
        correction.return_value = "stale clean lookup"
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(rows=[]),
                _Result(
                    rows=[
                        {
                            "id": 9,
                            "line_number": 22,
                            "role": "assistant",
                            "content": "The stale clean lookup is now indexed.",
                            "timestamp": self.now,
                            "score": 4.1,
                            "match_type": "full_text",
                        },
                    ]
                ),
            ]
        )

        payload = await search_conversation_messages(
            self.doc_id,
            q="stale clean lokup",
            after_line=None,
            limit=50,
            db=db,
            _user=self.owner,
        )

        self.assertEqual([row["line_number"] for row in payload["results"]], [22])
        self.assertEqual(payload["results"][0]["match_type"], "fuzzy")
        self.assertEqual(payload["corrected_query"], "stale clean lookup")
        self.assertFalse(payload["has_more"])
        search_sql = str(db.statements[1].compile())
        self.assertIn("conversation_messages.document_id", search_sql)
        self.assertIn("conversation_messages.role IN", search_sql)
        self.assertNotIn("documents.content", search_sql)
        self.assertNotIn(" %> ", search_sql)

    async def test_metadata_counts_normalized_rows_and_scopes_codex_hierarchy(
        self,
    ) -> None:
        root_thread_id = self.doc.metadata_["session_id"]
        self.doc.metadata_["thread_id"] = root_thread_id
        child = SimpleNamespace(
            id=uuid.uuid4(),
            machine_id=self.doc.machine_id,
            tool_id="codex",
            title="Child",
            relative_path="sessions/child.jsonl",
            metadata_={
                "session_id": str(uuid.uuid4()),
                "thread_id": str(uuid.uuid4()),
                "thread_source": "subagent",
                "root_session_id": root_thread_id,
            },
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=60 * 1024 * 1024,
        )
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=4306),
                _Result(scalar_value=None),
                _Result(rows=[self.doc, child]),
                _Result(rows=[]),
            ]
        )

        payload = await get_conversation(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["message_count"], 4306)
        self.assertEqual(payload["subagent_count"], 1)
        self.assertEqual(payload["native_id"], root_thread_id)
        self.assertEqual(payload["resume_id"], root_thread_id)
        self.assertEqual(
            payload["canonical_url"],
            f"/conversations/codex/{root_thread_id}",
        )
        self.assertEqual(
            payload["location"],
            {
                "host": "dreamland-yoga",
                "path": r"C:\Users\intpa\memento",
                "platform": "Windows",
            },
        )
        self.assertNotIn("documents.content", str(db.statements[0].compile()))
        task_statement = db.statements[2]
        compiled_task = task_statement.compile(dialect=postgresql.dialect())
        task_sql = str(compiled_task)
        self.assertIn("conversation_task_states", task_sql)
        self.assertNotIn("conversation_messages", task_sql)
        self.assertNotIn("jsonb_extract_path_text", task_sql)
        hierarchy_statement = db.statements[3]
        hierarchy_sql = str(hierarchy_statement.compile())
        hierarchy_params = hierarchy_statement.compile().params.values()
        self.assertGreaterEqual(
            hierarchy_sql.count("document_delivery_state.delivery_metadata"),
            3,
        )
        self.assertIn("documents.metadata", hierarchy_sql)
        self.assertIn("coalesce", hierarchy_sql)
        self.assertTrue(any(value == "root_session_id" for value in hierarchy_params))
        self.assertTrue(any(value == "session_id" for value in hierarchy_params))

    async def test_metadata_refresh_uses_materialized_hierarchy_and_events(
        self,
    ) -> None:
        projection = self.read_model(message_count=4306)
        db = _Db([
            _Result(rows=[(self.doc, projection)]),
            _Result(scalar_value=None),
            _Result(rows=[(self.doc, projection)]),
        ])

        payload = await get_conversation(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["message_count"], 4306)
        self.assertEqual(payload["subagent_count"], 0)
        self.assertEqual(len(db.statements), 5)
        for statement in db.statements:
            sql = str(statement.compile(dialect=postgresql.dialect())).upper()
            self.assertNotIn("COUNT(", sql)
            if "CANVAS_ARTIFACT_REFERENCES" not in sql:
                self.assertNotIn("CONVERSATION_MESSAGES", sql)
            self.assertNotIn("JSONB_EXTRACT_PATH_TEXT", sql)
        hierarchy_sql = str(
            db.statements[2].compile(dialect=postgresql.dialect())
        ).upper()
        self.assertIn("CONVERSATION_READ_MODELS.ROOT_THREAD_ID", hierarchy_sql)
        self.assertIn(" LIMIT ", hierarchy_sql)

    async def test_metadata_exposes_thread_identity_and_token_usage(self) -> None:
        token_usage = {
            "source": "codex",
            "input_tokens": 31_051,
            "cached_input_tokens": 19_712,
            "output_tokens": 421,
            "total_tokens": 31_472,
        }
        self.doc.metadata_.update(
            {
                "_assistant_model": "gpt-5.6-sol",
                "_assistant_reasoning_effort": "xhigh",
                "_assistant_service_tier": "priority",
                "_assistant_token_usage": token_usage,
            }
        )
        projection = self.read_model(
            runtime={
                "model": "gpt-5.6-sol",
                "model_family": "gpt",
                "reasoning_effort": "xhigh",
                "service_tier": "priority",
                "token_usage": token_usage,
            }
        )
        db = _Db(
            [
                _Result(rows=[(self.doc, projection)]),
                _Result(scalar_value=None),
                _Result(rows=[(self.doc, projection)]),
            ]
        )

        payload = await get_conversation(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["model_family"], "gpt")
        self.assertEqual(payload["reasoning_effort"], "xhigh")
        self.assertEqual(payload["service_tier"], "priority")
        self.assertEqual(payload["token_usage"], token_usage)

    async def test_direct_claude_child_api_uses_launch_description_title(self) -> None:
        root_thread_id = "root-thread"
        description = "Verify lifecycle exact matching"
        self.doc.tool_id = "claude_code"
        self.doc.title = "Raw parent dispatch"
        self.doc.relative_path = (
            f"projects/demo/{root_thread_id}/subagents/agent-child.jsonl"
        )
        self.doc.metadata_ = {
            "session_id": "agent-child",
            "root_session_id": root_thread_id,
            "parent_thread_id": root_thread_id,
            "is_subagent": True,
            "agent_id": "child",
            "agent_tool_use_id": "toolu-child",
            "agent_launch_description": description,
        }
        root = SimpleNamespace(
            id=uuid.uuid4(),
            machine_id=self.doc.machine_id,
            tool_id="claude_code",
            title="Root title",
            relative_path=f"projects/demo/{root_thread_id}.jsonl",
            metadata_={"session_id": root_thread_id},
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=1024,
        )
        grandchild = SimpleNamespace(
            id=uuid.uuid4(),
            machine_id=self.doc.machine_id,
            tool_id="claude_code",
            title="Raw nested dispatch",
            relative_path=(
                f"projects/demo/{root_thread_id}/subagents/agent-child/"
                "subagents/agent-grandchild.jsonl"
            ),
            metadata_={
                "session_id": "agent-grandchild",
                "root_session_id": root_thread_id,
                "parent_thread_id": "agent-child",
                "is_subagent": True,
                "agent_depth": 2,
                "agent_id": "grandchild",
                "agent_tool_use_id": "toolu-grandchild",
                "agent_launch_description": "Review nested lifecycle",
            },
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=512,
        )
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=2),
                _Result(scalar_value=None),
                _Result(rows=[root, self.doc, grandchild]),
                _Result(rows=[]),
            ]
        )

        payload = await get_conversation(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["title"], description)
        self.assertEqual(payload["user_role_origin"], "parent_agent")
        self.assertEqual(payload["subagent_count"], 1)
        self.assertEqual(
            payload["subagents"][0]["title"],
            "Review nested lifecycle",
        )

    def test_location_falls_back_to_absolute_project_source_path(self) -> None:
        document = SimpleNamespace(
            machine=SimpleNamespace(name="butterbridge (WSL2)"),
            metadata_={"project_path": "home-patrick-services-memento"},
            project=SimpleNamespace(source_path="/home/patrick/services/memento"),
        )

        self.assertEqual(
            _conversation_location(document),
            {
                "host": "butterbridge",
                "path": "/home/patrick/services/memento",
                "platform": "WSL2",
            },
        )

    def test_location_reports_windows_platform_for_native_paths(self) -> None:
        document = SimpleNamespace(
            machine=SimpleNamespace(name="dreamland-yoga (Windows)"),
            metadata_={"cwd": r"C:\Users\intpa\project"},
            project=None,
        )

        self.assertEqual(
            _conversation_location(document),
            {
                "host": "dreamland-yoga",
                "path": r"C:\Users\intpa\project",
                "platform": "Windows",
            },
        )

    def test_location_omits_hash_like_paths(self) -> None:
        document = SimpleNamespace(
            machine=SimpleNamespace(name="dreamland-yoga (Windows)"),
            metadata_={"project_path": "C--Users-intpa-memento"},
            project=SimpleNamespace(source_path="memento"),
        )

        self.assertIsNone(_conversation_location(document))


if __name__ == "__main__":
    unittest.main()
