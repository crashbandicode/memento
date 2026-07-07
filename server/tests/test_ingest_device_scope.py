from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.ingest_service import (  # noqa: E402
    _scoped_document_select,
    _scoped_sync_state_select,
    _update_sync_state,
)
from server.services.device_service import (  # noqa: E402
    DeviceOwnershipError,
    ensure_device,
)


def _compile(statement):
    return statement.compile(dialect=postgresql.dialect())


class DeviceScopedSelectTests(unittest.TestCase):
    def test_document_identity_includes_device_and_owner(self) -> None:
        machine_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        compiled = _compile(_scoped_document_select(
            "codex", "sessions/shared.jsonl", machine_id, user_id,
        ))
        sql = str(compiled)

        self.assertIn("documents.machine_id =", sql)
        self.assertIn("machines.user_id =", sql)
        self.assertIn(machine_id, compiled.params.values())
        self.assertIn(user_id, compiled.params.values())

    def test_same_tool_and_path_on_two_devices_have_distinct_keys(self) -> None:
        machine_a = str(uuid.uuid4())
        machine_b = str(uuid.uuid4())

        compiled_a = _compile(_scoped_document_select(
            "codex", "sessions/shared.jsonl", machine_a, None,
        ))
        compiled_b = _compile(_scoped_document_select(
            "codex", "sessions/shared.jsonl", machine_b, None,
        ))

        self.assertIn(machine_a, compiled_a.params.values())
        self.assertNotIn(machine_b, compiled_a.params.values())
        self.assertIn(machine_b, compiled_b.params.values())
        self.assertNotIn(machine_a, compiled_b.params.values())

    def test_sync_state_identity_includes_device_and_owner(self) -> None:
        machine_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        compiled = _compile(_scoped_sync_state_select(
            "codex", "sessions/shared.jsonl", machine_id, user_id,
        ))
        sql = str(compiled)

        self.assertIn("sync_state.machine_id =", sql)
        self.assertIn("machines.user_id =", sql)
        self.assertIn(machine_id, compiled.params.values())
        self.assertIn(user_id, compiled.params.values())


class _NoRowResult:
    def scalar_one_or_none(self):
        return None


class _CaptureSession:
    def __init__(self) -> None:
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _NoRowResult()

    def add(self, value) -> None:
        self.added.append(value)


class SyncStateUpsertTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_sync_state_persists_machine_id(self) -> None:
        machine_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        db = _CaptureSession()

        await _update_sync_state(
            db, "codex", "sessions/shared.jsonl", "hash-a", 123,
            machine_id, user_id,
        )

        self.assertEqual(len(db.added), 1)
        self.assertEqual(str(db.added[0].machine_id), machine_id)
        compiled = _compile(db.statements[0])
        self.assertIn(machine_id, compiled.params.values())
        self.assertIn(user_id, compiled.params.values())


class _MachineResult:
    def __init__(self, machine) -> None:
        self.machine = machine

    def scalar_one_or_none(self):
        return self.machine


class _DeviceSession:
    def __init__(self, machine) -> None:
        self.machine = machine
        self.calls = 0

    async def execute(self, _statement, _parameters=None):
        self.calls += 1
        if self.calls == 1:
            return _NoRowResult()
        return _MachineResult(self.machine)


class DeviceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_device_cannot_be_claimed_by_another_user(self) -> None:
        owner_id = uuid.uuid4()
        intruder_id = uuid.uuid4()
        machine = SimpleNamespace(
            user_id=owner_id,
            name="Original",
            last_heartbeat=None,
        )
        db = _DeviceSession(machine)

        with self.assertRaises(DeviceOwnershipError):
            await ensure_device(
                db,
                "shared-device-id",
                "Spoofed",
                "Windows",
                user_id=intruder_id,
            )

        self.assertEqual(machine.user_id, owner_id)
        self.assertEqual(machine.name, "Original")


if __name__ == "__main__":
    unittest.main()
