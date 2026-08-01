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

from server.api.hierarchy import list_devices_with_tools  # noqa: E402
from server.api.ingest import heartbeat  # noqa: E402
from server.services.device_grouping import (  # noqa: E402
    build_host_groups,
    host_group_id,
    resolve_device_scope_ids,
)


class _Result:
    def __init__(self, *, rows=None, scalar_value=None) -> None:
        self.rows = list(rows or [])
        self.scalar_value = scalar_value

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self.scalar_value


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


def _machine(
    *,
    name: str,
    owner: uuid.UUID | None,
    collector: str,
    machine_id: uuid.UUID | None = None,
):
    return SimpleNamespace(
        id=machine_id or uuid.uuid4(),
        user_id=owner,
        name=name,
        collector_token_hash=collector,
        last_heartbeat=datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
    )


class DeviceGroupingTests(unittest.IsolatedAsyncioTestCase):
    def test_repeated_registrations_and_windows_wsl_group_without_double_count(self):
        owner = uuid.uuid4()
        machines = [
            _machine(
                name="butterbridge (Windows)",
                owner=owner,
                collector=f"windows-{index}",
            )
            for index in range(5)
        ]
        machines.append(
            _machine(
                name="butterbridge (Linux)",
                owner=owner,
                collector="linux-wsl",
            )
        )
        rows = [
            (uuid.uuid4(), machines[0].id, "cursor", "sessions/shared.jsonl"),
            # Same physical conversation on a churned registration: host count 1.
            (uuid.uuid4(), machines[1].id, "cursor", "sessions/shared.jsonl"),
            (uuid.uuid4(), machines[5].id, "claude_code", "projects/wsl.jsonl"),
            (uuid.uuid4(), machines[0].id, "system", "discovery.json"),
        ]

        groups = build_host_groups(machines, rows)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["name"], "butterbridge")
        self.assertEqual(len(group["identities"]), 6)
        self.assertEqual(group["total_files"], 2)
        self.assertEqual(
            group["tools"],
            [
                {"id": "claude_code", "file_count": 1},
                {"id": "cursor", "file_count": 1},
            ],
        )
        self.assertEqual(
            sum(identity["total_files"] for identity in group["identities"]),
            3,
        )
        self.assertIn("Linux · linux-ws", {
            identity["label"] for identity in group["identities"]
        })

    def test_same_hostname_never_crosses_ownership_boundary(self):
        machine_a = _machine(
            name="shared-name (Windows)",
            owner=uuid.uuid4(),
            collector="owner-a",
        )
        machine_b = _machine(
            name="shared-name (Linux)",
            owner=uuid.uuid4(),
            collector="owner-b",
        )

        groups = build_host_groups([machine_a, machine_b], [])

        self.assertEqual(len(groups), 2)
        self.assertNotEqual(groups[0]["group_id"], groups[1]["group_id"])

    def test_group_id_survives_collector_identity_churn(self):
        owner = uuid.uuid4()
        original = _machine(
            name="dreamland-yoga (Windows)",
            owner=owner,
            collector="old-registration",
        )
        replacement = _machine(
            name="DREAMLAND-YOGA (Windows)",
            owner=owner,
            collector="new-registration",
        )

        self.assertEqual(host_group_id(original), host_group_id(replacement))

    async def test_selected_group_resolves_every_authorized_machine(self):
        owner = SimpleNamespace(id=uuid.uuid4(), role="member")
        windows = _machine(
            name="butterbridge (Windows)",
            owner=owner.id,
            collector="windows",
        )
        linux = _machine(
            name="butterbridge (Linux)",
            owner=owner.id,
            collector="linux",
        )
        other = _machine(
            name="dreamland-yoga (Linux)",
            owner=owner.id,
            collector="other",
        )
        db = _Db([_Result(rows=[windows, linux, other])])

        selected = await resolve_device_scope_ids(
            db,
            owner,
            host_group_id(windows),
        )

        self.assertEqual(selected, [windows.id, linux.id])

    async def test_individual_child_collector_id_remains_selectable(self):
        owner = SimpleNamespace(id=uuid.uuid4(), role="member")
        windows = _machine(
            name="butterbridge (Windows)",
            owner=owner.id,
            collector="windows-child",
        )
        db = _Db([_Result(scalar_value=windows)])

        selected = await resolve_device_scope_ids(db, owner, "windows-child")

        self.assertEqual(selected, [windows.id])

    async def test_hierarchy_returns_one_top_level_row_per_host(self):
        owner_id = uuid.uuid4()
        user = SimpleNamespace(id=owner_id, role="member")
        butter = [
            _machine(
                name="butterbridge (Windows)",
                owner=owner_id,
                collector=f"butter-{index}",
            )
            for index in range(6)
        ]
        dream = [
            _machine(
                name=f"dreamland-yoga ({platform})",
                owner=owner_id,
                collector=f"dream-{platform.lower()}",
            )
            for platform in ("Windows", "Linux")
        ]
        rows = [
            (uuid.uuid4(), butter[0].id, "cursor", "butter.jsonl"),
            (uuid.uuid4(), dream[0].id, "codex", "dream.jsonl"),
        ]
        db = _Db([_Result(rows=[*butter, *dream]), _Result(rows=rows)])

        groups = await list_devices_with_tools(db=db, _user=user)

        self.assertEqual(
            [(group["name"], len(group["identities"])) for group in groups],
            [("butterbridge", 6), ("dreamland-yoga", 2)],
        )

    async def test_heartbeat_does_not_replace_public_collector_identity(self):
        machine = SimpleNamespace(
            id=uuid.uuid4(),
            collector_token_hash="persistent-collector-id",
        )
        user = SimpleNamespace(id=uuid.uuid4())
        with patch(
            "server.api.ingest.ensure_device",
            new=AsyncMock(return_value=machine),
        ):
            result = await heartbeat(
                _collector_user=user,
                _throttle=None,
                db=SimpleNamespace(),
                x_device_id="persistent-collector-id",
                x_device_name="butterbridge (Windows)",
                x_device_platform="Windows",
            )

        self.assertEqual(result["device_id"], "persistent-collector-id")
        self.assertEqual(result["machine_id"], str(machine.id))


if __name__ == "__main__":
    unittest.main()
