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

from server.api.projects import list_projects  # noqa: E402


class _Result:
    def __init__(self, *, rows: list | None = None, scalar_value=None) -> None:
        self._rows = rows or []
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def all(self):
        return self._rows


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


class ProjectsDeviceScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        now = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
        self.project = SimpleNamespace(
            id=uuid.uuid4(),
            slug="project",
            title="Project",
            tool_id="codex",
            source_path="projects/project",
            visibility="private",
            created_at=now,
            updated_at=now,
        )
        self.owner = SimpleNamespace(id=uuid.uuid4(), role="owner")

    async def test_no_device_preserves_unscoped_owner_query(self) -> None:
        db = _Db([_Result(rows=[(self.project, 8)])])

        projects = await list_projects(
            tool_id=None,
            device_id=None,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(projects[0]["document_count"], 8)
        sql = str(db.statements[0].compile())
        self.assertNotIn("documents.machine_id", sql)
        self.assertNotIn("HAVING", sql)

    async def test_collector_id_scopes_counts_and_excludes_empty_projects(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        db = _Db([
            _Result(scalar_value=machine),
            _Result(rows=[(self.project, 3)]),
        ])

        projects = await list_projects(
            tool_id="codex",
            device_id="collector-facing-id",
            db=db,
            _user=self.owner,
        )

        self.assertEqual(projects[0]["document_count"], 3)
        project_sql = str(db.statements[1].compile())
        self.assertIn("documents.machine_id =", project_sql)
        self.assertIn("projects.tool_id =", project_sql)
        self.assertIn("HAVING count(documents.id) >", project_sql)

    async def test_database_uuid_is_supported_as_a_fallback(self) -> None:
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        db = _Db([
            _Result(scalar_value=None),
            _Result(scalar_value=machine),
            _Result(rows=[(self.project, 1)]),
        ])

        await list_projects(
            tool_id=None,
            device_id=str(machine.id),
            db=db,
            _user=self.owner,
        )

        collector_lookup = str(db.statements[0].compile())
        uuid_lookup = str(db.statements[1].compile())
        self.assertIn("machines.collector_token_hash =", collector_lookup)
        self.assertIn("machines.id =", uuid_lookup)

    async def test_inaccessible_device_returns_not_found(self) -> None:
        user = SimpleNamespace(id=uuid.uuid4(), role="member")
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
        db = _Db([_Result(scalar_value=machine)])

        with self.assertRaises(HTTPException) as raised:
            await list_projects(
                tool_id=None,
                device_id="another-users-device",
                db=db,
                _user=user,
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(db.statements), 1)

    async def test_member_can_scope_projects_to_an_owned_device(self) -> None:
        user = SimpleNamespace(id=uuid.uuid4(), role="viewer")
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=user.id)
        db = _Db([
            _Result(scalar_value=machine),
            _Result(rows=[(self.project, 2)]),
        ])

        projects = await list_projects(
            tool_id=None,
            device_id="owned-device",
            db=db,
            _user=user,
        )

        self.assertEqual(projects[0]["document_count"], 2)

    async def test_uuid_shaped_collector_id_wins_before_database_uuid_fallback(self) -> None:
        collector_id = str(uuid.uuid4())
        machine = SimpleNamespace(id=uuid.uuid4(), user_id=None)
        db = _Db([
            _Result(scalar_value=machine),
            _Result(rows=[(self.project, 1)]),
        ])

        await list_projects(
            tool_id=None,
            device_id=collector_id,
            db=db,
            _user=self.owner,
        )

        self.assertEqual(len(db.statements), 2)
        self.assertIn("machines.collector_token_hash =", str(db.statements[0].compile()))
        self.assertIn("FROM projects", str(db.statements[1].compile()))


if __name__ == "__main__":
    unittest.main()
