from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.conversations import (  # noqa: E402
    get_conversation,
    get_conversation_messages,
    get_conversation_prompts,
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
                "session_id": str(uuid.uuid4()),
                "thread_id": str(uuid.uuid4()),
                "thread_source": "user",
            },
            source_modified_at=self.now,
            activity_at=self.now,
            synced_at=self.now,
            file_size_bytes=64 * 1024 * 1024,
        )

    def message(self, line_number: int, role: str = "assistant"):
        return SimpleNamespace(
            id=line_number,
            line_number=line_number,
            role=role,
            message_type=role,
            content=f"message {line_number}",
            metadata_={
                "thinking": "reasoning" if role == "assistant" else None,
                "tool_name": "shell" if role == "assistant" else "",
                "tool_input": "Get-Item" if role == "assistant" else "",
                "tool_calls": [],
            },
            timestamp=self.now,
        )

    async def test_messages_prefer_indexed_normalized_rows(self) -> None:
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=2),
            _Result(rows=[self.message(1, "user"), self.message(2)]),
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

        self.assertEqual(payload["total"], 2)
        self.assertEqual([item["line_number"] for item in payload["messages"]], [1, 2])
        self.assertEqual(payload["messages"][1]["tool_name"], "shell")
        self.assertEqual(len(db.statements), 3)
        for statement in db.statements:
            self.assertNotIn("documents.content", str(statement.compile()))

    async def test_around_line_uses_index_and_reports_row_offset(self) -> None:
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=4306),
            _Result(scalar_value=4281),
            _Result(rows=[self.message(4282), self.message(4283)]),
        ])

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
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=4306),
            _Result(rows=[self.message(4305), self.message(4306)]),
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
        self.assertEqual(
            [item["line_number"] for item in payload["messages"]],
            [4305, 4306],
        )
        message_sql = str(db.statements[2].compile())
        self.assertIn("OFFSET", message_sql.upper())
        self.assertNotIn("documents.content", message_sql)

    async def test_prompts_prefer_normalized_rows(self) -> None:
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=2),
            _Result(rows=[
                (7, 12, "A prompt", self.now, {}),
                (
                    8,
                    13,
                    "The selected answer",
                    self.now,
                    {"interaction_response": {"interaction_id": "question-1"}},
                ),
            ]),
        ])

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
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(rows=[]),
            _Result(rows=[
                {
                    "id": 9,
                    "line_number": 22,
                    "role": "assistant",
                    "content": "The stale clean lookup is now indexed.",
                    "timestamp": self.now,
                    "score": 4.1,
                    "match_type": "full_text",
                },
            ]),
        ])

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

    async def test_metadata_counts_normalized_rows_and_scopes_codex_hierarchy(self) -> None:
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
        db = _Db([
            _Result(scalar_value=self.doc),
            _Result(scalar_value=4306),
            _Result(rows=[self.doc, child]),
        ])

        payload = await get_conversation(
            self.doc_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["message_count"], 4306)
        self.assertEqual(payload["subagent_count"], 1)
        self.assertNotIn("documents.content", str(db.statements[0].compile()))
        hierarchy_statement = db.statements[2]
        hierarchy_sql = str(hierarchy_statement.compile())
        hierarchy_params = hierarchy_statement.compile().params.values()
        self.assertGreaterEqual(hierarchy_sql.count("documents.metadata ->>"), 3)
        self.assertTrue(any(value == "root_session_id" for value in hierarchy_params))
        self.assertTrue(any(value == "session_id" for value in hierarchy_params))


if __name__ == "__main__":
    unittest.main()
