from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.hierarchy import list_device_tool_files  # noqa: E402
from server.api.projects import get_project  # noqa: E402
from server.api.tools import list_tool_files  # noqa: E402


class _Result:
    def __init__(
        self,
        *,
        rows: list | None = None,
        scalar_value=None,
    ) -> None:
        self._rows = rows or []
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalar(self):
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


class BrowseActivityApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.activity = datetime(2026, 5, 15, 12, tzinfo=timezone.utc)
        self.source_modified = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
        self.synced = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
        self.document = SimpleNamespace(
            id=uuid.uuid4(),
            tool_id="codex",
            machine_id=None,
            relative_path="sessions/thread.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Historical thread",
            file_size_bytes=123,
            activity_at=self.activity,
            source_modified_at=self.source_modified,
            synced_at=self.synced,
            ai_summary=None,
            metadata_={},
        )
        self.owner = SimpleNamespace(role="owner")

    async def test_tool_files_return_and_order_by_effective_activity(self) -> None:
        db = _Db([
            _Result(rows=[self.document]),
            _Result(rows=[]),
        ])

        rows = await list_tool_files(
            "codex",
            category=None,
            device_id=None,
            offset=0,
            limit=50,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(rows[0].activity_at, self.activity.isoformat())
        sql = str(db.statements[0].compile())
        self.assertNotIn("documents.content,", sql)
        self.assertIn("documents.metadata", sql)

    async def test_tool_files_fold_children_and_classify_visible_roots(self) -> None:
        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        root_thread_id = str(uuid.uuid4())
        root = SimpleNamespace(
            **{
                **self.document.__dict__,
                "id": root_id,
                "title": "Root",
                "metadata_": {
                    "session_id": root_thread_id,
                    "thread_id": root_thread_id,
                    "thread_source": "user",
                },
            }
        )
        child = SimpleNamespace(
            **{
                **self.document.__dict__,
                "id": child_id,
                "title": "Child",
                "activity_at": self.activity.replace(day=16),
                "metadata_": {
                    "session_id": str(uuid.uuid4()),
                    "thread_id": str(uuid.uuid4()),
                    "thread_source": "subagent",
                    "root_session_id": root_thread_id,
                },
            }
        )
        db = _Db([
            _Result(rows=[root, child]),
            _Result(rows=[(root_id, 12, 2, 3, 900)]),
        ])

        rows = await list_tool_files(
            "codex",
            category=None,
            device_id=None,
            offset=0,
            limit=50,
            db=db,
            _user=self.owner,
        )

        self.assertEqual([row.id for row in rows], [str(root_id)])
        self.assertEqual(rows[0].subagent_count, 1)
        self.assertEqual(rows[0].message_count, 12)
        self.assertFalse(rows[0].is_low_activity)
        self.assertEqual(rows[0].activity_at, child.activity_at.isoformat())

    async def test_device_files_return_and_order_by_effective_activity(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        self.document.machine_id = machine.id
        device_row = (
            self.document.id,
            self.document.title,
            self.document.relative_path,
            self.document.category,
            self.document.content_type,
            self.document.file_size_bytes,
            self.document.activity_at,
            self.document.source_modified_at,
            self.document.synced_at,
        )
        db = _Db([
            _Result(scalar_value=machine),
            _Result(scalar_value=1),
            _Result(rows=[device_row]),
            _Result(rows=[]),
        ])

        payload = await list_device_tool_files(
            "device-token",
            "obsidian",
            project_id=None,
            category=None,
            offset=0,
            limit=50,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            payload["files"][0]["activity_at"],
            self.activity.isoformat(),
        )
        self.assertIsNone(payload["project"])
        sql = str(db.statements[2].compile())
        self.assertIn("ORDER BY CASE WHEN", sql)
        self.assertIn("coalesce(documents.activity_at", sql)

    async def test_device_files_project_summary_pagination_and_lean_queries(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        project_id = uuid.uuid4()
        first_id = uuid.uuid4()
        second_id = uuid.uuid4()
        project_row = (
            project_id,
            "lean-project",
            "Lean project",
            "codex",
            "C:/src/lean-project",
        )
        rows = [
            (
                first_id,
                "First",
                "sessions/first.jsonl",
                "conversation",
                "jsonl",
                101,
                self.activity,
                self.source_modified,
                self.synced,
            ),
            (
                second_id,
                "Second",
                "sessions/second.jsonl",
                "conversation",
                "jsonl",
                99,
                None,
                self.source_modified,
                self.synced,
            ),
        ]
        db = _Db([
            _Result(scalar_value=machine),
            _Result(rows=[project_row]),
            _Result(scalar_value=341),
            _Result(rows=rows),
            _Result(rows=[]),
        ])

        payload = await list_device_tool_files(
            "device-token",
            "obsidian",
            project_id=str(project_id),
            category="conversation",
            offset=100,
            limit=2,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["total"], 341)
        self.assertEqual(
            payload["project"],
            {
                "id": str(project_id),
                "slug": "lean-project",
                "title": "Lean project",
                "tool_id": "codex",
                "source_path": "C:/src/lean-project",
            },
        )
        self.assertEqual([row["id"] for row in payload["files"]], [
            str(first_id),
            str(second_id),
        ])

        project_sql = str(db.statements[1].compile())
        self.assertIn("documents.machine_id", project_sql)
        self.assertIn("documents.tool_id", project_sql)

        count_sql = str(db.statements[2].compile())
        self.assertNotIn("FROM (SELECT", count_sql)
        self.assertIn("count(documents.id)", count_sql)

        files_statement = db.statements[3]
        selected_keys = {column.key for column in files_statement.selected_columns}
        self.assertEqual(
            selected_keys,
            {
                "id",
                "title",
                "relative_path",
                "category",
                "content_type",
                "file_size_bytes",
                "activity_at",
                "source_modified_at",
                "synced_at",
            },
        )
        self.assertNotIn("content", selected_keys)
        self.assertNotIn("rendered_html", selected_keys)
        self.assertEqual(files_statement._offset_clause.value, 100)
        self.assertEqual(files_statement._limit_clause.value, 2)
        files_sql = str(files_statement.compile())
        self.assertIn("ORDER BY CASE WHEN", files_sql)
        self.assertIn("documents.category =", files_sql)

    async def test_device_files_no_project_summary_and_null_filter(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        db = _Db([
            _Result(scalar_value=machine),
            _Result(scalar_value=0),
            _Result(rows=[]),
        ])

        payload = await list_device_tool_files(
            "device-token",
            "obsidian",
            project_id="none",
            category=None,
            offset=0,
            limit=50,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(
            payload["project"],
            {
                "id": "none",
                "slug": "",
                "title": "(No Project)",
                "tool_id": "obsidian",
                "source_path": None,
            },
        )
        self.assertIn("documents.project_id IS NULL", str(db.statements[1].compile()))

    async def test_device_files_reject_malformed_project_id(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        db = _Db([_Result(scalar_value=machine)])

        with self.assertRaises(HTTPException) as raised:
            await list_device_tool_files(
                "device-token",
                "obsidian",
                project_id="not-a-uuid",
                category=None,
                offset=0,
                limit=50,
                db=db,
                _user=self.owner,
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(len(db.statements), 1)

    async def test_device_files_reject_unknown_device_for_project_detail(self) -> None:
        db = _Db([_Result(scalar_value=None), _Result(scalar_value=None)])

        with self.assertRaises(HTTPException) as raised:
            await list_device_tool_files(
                "unknown-device",
                "obsidian",
                project_id=str(uuid.uuid4()),
                category=None,
                offset=0,
                limit=50,
                db=db,
                _user=self.owner,
            )

        self.assertEqual(raised.exception.status_code, 404)

    async def test_device_files_reject_project_outside_device_scope(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        db = _Db([
            _Result(scalar_value=machine),
            _Result(rows=[]),
        ])

        with self.assertRaises(HTTPException) as raised:
            await list_device_tool_files(
                "device-token",
                "obsidian",
                project_id=str(uuid.uuid4()),
                category=None,
                offset=0,
                limit=50,
                db=db,
                _user=self.owner,
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(db.statements), 2)

    async def test_codex_device_files_fold_subagents_before_pagination(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        project_id = uuid.uuid4()
        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        root_thread_id = str(uuid.uuid4())
        root_activity = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
        child_activity = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        project_row = (
            project_id,
            "codex-project",
            "Codex project",
            "codex",
            "C:/src/codex-project",
        )
        root_row = (
            root_id,
            "Root",
            "sessions/root.jsonl",
            "conversation",
            "jsonl",
            100,
            root_activity,
            root_activity,
            root_activity,
            {
                "session_id": root_thread_id,
                "thread_id": root_thread_id,
                "thread_source": "user",
            },
        )
        child_row = (
            child_id,
            "Child",
            "sessions/child.jsonl",
            "conversation",
            "jsonl",
            90,
            child_activity,
            child_activity,
            child_activity,
            {
                "session_id": str(uuid.uuid4()),
                "thread_id": str(uuid.uuid4()),
                "thread_source": "subagent",
                "root_session_id": root_thread_id,
            },
        )
        db = _Db([
            _Result(scalar_value=machine),
            _Result(rows=[project_row]),
            _Result(rows=[root_row, child_row]),
            _Result(rows=[(root_id, 8, 2, 2, 480)]),
        ])

        payload = await list_device_tool_files(
            "device-token",
            "codex",
            project_id=str(project_id),
            category=None,
            offset=0,
            limit=100,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["total"], 1)
        self.assertEqual([item["id"] for item in payload["files"]], [str(root_id)])
        self.assertEqual(payload["files"][0]["subagent_count"], 1)
        self.assertEqual(payload["files"][0]["message_count"], 8)
        self.assertFalse(payload["files"][0]["is_low_activity"])
        self.assertEqual(
            payload["files"][0]["activity_at"],
            child_activity.isoformat(),
        )
        selected_keys = {
            column.key for column in db.statements[2].selected_columns
        }
        self.assertIn("metadata", selected_keys)
        self.assertNotIn("content", selected_keys)

    async def test_default_project_detail_does_not_select_document_payloads(self) -> None:
        project = SimpleNamespace(
            id=uuid.uuid4(),
            slug="lean-project",
            title="Lean project",
            tool_id="codex",
            source_path="C:/src/lean-project",
            visibility="private",
        )
        db = _Db([
            _Result(scalar_value=project),
            _Result(rows=[]),
            _Result(rows=[]),
        ])

        payload = await get_project(
            project.id,
            include_content=False,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(payload["documents"], [])
        docs_sql = str(db.statements[2].compile())
        self.assertNotIn("documents.content,", docs_sql)
        self.assertNotIn("documents.rendered_html", docs_sql)
        self.assertNotIn("documents.ai_summary", docs_sql)
        self.assertIn("documents.relative_path", docs_sql)

    async def test_project_detail_include_content_keeps_payload_columns(self) -> None:
        project = SimpleNamespace(
            id=uuid.uuid4(),
            slug="full-project",
            title="Full project",
            tool_id="codex",
            source_path="C:/src/full-project",
            visibility="private",
        )
        db = _Db([
            _Result(scalar_value=project),
            _Result(rows=[]),
            _Result(rows=[]),
        ])

        await get_project(
            project.id,
            include_content=True,
            db=db,
            _user=self.owner,
        )

        docs_sql = str(db.statements[2].compile())
        self.assertIn("documents.content,", docs_sql)
        self.assertIn("documents.rendered_html", docs_sql)

    async def test_project_detail_requires_a_document_visible_to_the_user(self) -> None:
        machine_id = uuid.uuid4()
        viewer = SimpleNamespace(id=uuid.uuid4(), role="viewer")
        db = _Db([
            _Result(rows=[(machine_id,)]),
            _Result(scalar_value=None),
        ])

        with self.assertRaises(HTTPException) as raised:
            await get_project(
                uuid.uuid4(),
                include_content=False,
                db=db,
                _user=viewer,
            )

        self.assertEqual(raised.exception.status_code, 404)
        project_sql = str(db.statements[1].compile())
        self.assertIn("JOIN documents", project_sql)
        self.assertIn("documents.machine_id IN", project_sql)


if __name__ == "__main__":
    unittest.main()
