from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    search_conversation_messages,
)


class _Result:
    def __init__(self, *, rows: list | None = None, scalar_value=None) -> None:
        self._rows = rows or []
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalar(self):
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
        return self.results.pop(0)


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

    async def test_around_line_uses_index_and_reports_row_offset(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=4306),
                _Result(scalar_value=4281),
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
        message_sql = str(db.statements[3].compile())
        self.assertIn("conversation_messages.line_number >=", message_sql)
        self.assertNotIn("documents.content", message_sql)

    async def test_tail_returns_latest_normalized_rows(self) -> None:
        db = _Db(
            [
                _Result(scalar_value=self.doc),
                _Result(scalar_value=4306),
                _Result(rows=[self.message(4305), self.message(4306)]),
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
        self.assertIn("OFFSET", message_sql.upper())
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
        self.assertEqual(
            payload["location"],
            {
                "host": "dreamland-yoga",
                "path": r"C:\Users\intpa\memento",
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
        self.assertGreaterEqual(hierarchy_sql.count("documents.metadata ->>"), 3)
        self.assertTrue(any(value == "root_session_id" for value in hierarchy_params))
        self.assertTrue(any(value == "session_id" for value in hierarchy_params))

    def test_location_falls_back_to_absolute_project_source_path(self) -> None:
        document = SimpleNamespace(
            machine=SimpleNamespace(name="butterbridge (Linux)"),
            metadata_={"project_path": "home-patrick-services-memento"},
            project=SimpleNamespace(source_path="/home/patrick/services/memento"),
        )

        self.assertEqual(
            _conversation_location(document),
            {
                "host": "butterbridge",
                "path": "/home/patrick/services/memento",
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
