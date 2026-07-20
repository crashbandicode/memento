from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api import devices  # noqa: E402


class _Result:
    def __init__(self, row=None) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, result: _Result) -> None:
        self.result = result
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.result


class TargetedRepairCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        devices._command_queue.clear()

    async def test_targeted_repair_preserves_the_requested_conversation(self) -> None:
        machine = SimpleNamespace(
            id=uuid.uuid4(),
            collector_token_hash="collector",
            name="Yoga",
        )
        document_id = uuid.uuid4()
        db = _Db(
            _Result(
                SimpleNamespace(
                    tool_id="codex",
                    relative_path="sessions/2026/07/16/thread.jsonl",
                )
            )
        )

        with patch.object(
            devices,
            "_verify_device_ownership",
            AsyncMock(return_value=machine),
        ):
            response = await devices.send_command(
                machine.id,
                action="repair-conversations",
                document_id=document_id,
                db=db,
                _user=SimpleNamespace(id=uuid.uuid4(), role="owner"),
            )

        self.assertEqual(response["status"], "queued")
        self.assertEqual(
            devices._command_queue["collector"][0]["paths"],
            [
                {
                    "tool_name": "codex",
                    "relative_path": "sessions/2026/07/16/thread.jsonl",
                }
            ],
        )
        sql = str(db.statements[0].compile())
        self.assertIn("documents.machine_id =", sql)
        self.assertIn("documents.id =", sql)


if __name__ == "__main__":
    unittest.main()
