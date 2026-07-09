from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.hierarchy import list_device_tool_files  # noqa: E402
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
        )
        self.owner = SimpleNamespace(role="owner")

    async def test_tool_files_return_and_order_by_effective_activity(self) -> None:
        db = _Db([_Result(rows=[self.document])])

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
        self.assertIn("ORDER BY CASE WHEN", sql)
        self.assertIn("coalesce(documents.activity_at", sql)

    async def test_device_files_return_and_order_by_effective_activity(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        self.document.machine_id = machine.id
        db = _Db([
            _Result(scalar_value=machine),
            _Result(scalar_value=1),
            _Result(rows=[self.document]),
        ])

        payload = await list_device_tool_files(
            "device-token",
            "codex",
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
        sql = str(db.statements[2].compile())
        self.assertIn("ORDER BY CASE WHEN", sql)
        self.assertIn("coalesce(documents.activity_at", sql)


if __name__ == "__main__":
    unittest.main()
