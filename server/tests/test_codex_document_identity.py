from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_identity import (  # noqa: E402
    codex_session_id,
    select_canonical_conversation_document,
    should_relocate_conversation_document,
)
from server.services.ingest_service import (  # noqa: E402
    _scoped_conversation_identity_select,
    _source_lock_id,
)
from server.scripts.consolidate_cursor_sessions import (  # noqa: E402
    build_session_consolidation_plan,
)


class CodexDocumentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = str(uuid.uuid4())

    def test_identity_requires_matching_rollout_and_thread_uuid(self) -> None:
        metadata = {
            "session_id": self.session_id,
            "thread_id": self.session_id,
            "root_session_id": str(uuid.uuid4()),
        }
        self.assertEqual(
            codex_session_id("codex", "conversation", metadata),
            self.session_id,
        )
        self.assertIsNone(codex_session_id(
            "codex",
            "conversation",
            {**metadata, "thread_id": str(uuid.uuid4())},
        ))
        self.assertIsNone(codex_session_id(
            "codex",
            "conversation",
            {"session_id": self.session_id},
        ))
        self.assertIsNone(codex_session_id("cursor", "conversation", metadata))

    def test_newest_revision_wins_across_active_archive_paths(self) -> None:
        now = datetime.now(timezone.utc)
        active = {
            "id": "active",
            "relative_path": f"sessions/2026/07/20/{self.session_id}.jsonl",
            "source_modified_at": now - timedelta(minutes=2),
            "file_size_bytes": 100,
            "synced_at": now,
        }
        archive = {
            "id": "archive",
            "relative_path": f"archived_sessions/{self.session_id}.jsonl",
            "source_modified_at": now,
            "file_size_bytes": 120,
            "synced_at": now,
        }
        selected = select_canonical_conversation_document(
            [active, archive],
            tool_id="codex",
            session_id=self.session_id,
        )
        self.assertEqual(selected["id"], "archive")

        archive["source_modified_at"] = now
        active["source_modified_at"] = now
        active["file_size_bytes"] = archive["file_size_bytes"]
        self.assertEqual(
            select_canonical_conversation_document(
                [archive, active],
                tool_id="codex",
                session_id=self.session_id,
            )["id"],
            "active",
        )

    def test_relocation_follows_newer_revision_and_active_tie_break(self) -> None:
        now = datetime.now(timezone.utc)
        active = f"sessions/2026/07/20/{self.session_id}.jsonl"
        archive = f"archived_sessions/{self.session_id}.jsonl"
        self.assertTrue(should_relocate_conversation_document(
            tool_id="codex",
            session_id=self.session_id,
            current_path=active,
            incoming_path=archive,
            current_modified_at=now - timedelta(seconds=1),
            incoming_modified_at=now,
        ))
        self.assertTrue(should_relocate_conversation_document(
            tool_id="codex",
            session_id=self.session_id,
            current_path=archive,
            incoming_path=active,
            current_modified_at=now,
            incoming_modified_at=now,
        ))

    def test_identity_select_is_machine_owner_and_thread_scoped(self) -> None:
        machine_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        statement = _scoped_conversation_identity_select(
            "codex",
            self.session_id,
            machine_id,
            user_id,
        ).with_for_update()
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("documents.machine_id =", sql)
        self.assertIn("documents.tool_id =", sql)
        self.assertIn("documents.category =", sql)
        self.assertIn("documents.metadata ->>", sql)
        self.assertIn("thread_id", compiled.params.values())
        self.assertIn("machines.user_id =", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn(machine_id, compiled.params.values())
        self.assertIn(user_id, compiled.params.values())
        self.assertIn(self.session_id, compiled.params.values())

    def test_alias_paths_share_one_ingest_lock(self) -> None:
        common = {
            "machine_id": str(uuid.uuid4()),
            "user_id": None,
            "tool_id": "codex",
            "source_identity": self.session_id,
        }
        active = _source_lock_id(relative_path="sessions/a.jsonl", **common)
        archive = _source_lock_id(relative_path="archived_sessions/a.jsonl", **common)
        self.assertEqual(active, archive)

    def test_consolidation_plan_keeps_the_newest_revision(self) -> None:
        now = datetime.now(timezone.utc)
        machine_id = uuid.uuid4()
        rows = [
            {
                "id": uuid.uuid4(),
                "machine_id": machine_id,
                "session_id": self.session_id,
                "relative_path": f"sessions/2026/07/20/{self.session_id}.jsonl",
                "source_modified_at": now - timedelta(minutes=1),
                "file_size_bytes": 100,
                "synced_at": now,
                "created_at": now - timedelta(days=1),
            },
            {
                "id": uuid.uuid4(),
                "machine_id": machine_id,
                "session_id": self.session_id,
                "relative_path": f"archived_sessions/{self.session_id}.jsonl",
                "source_modified_at": now,
                "file_size_bytes": 120,
                "synced_at": now,
                "created_at": now,
            },
        ]
        plan = build_session_consolidation_plan(rows, tool_id="codex")
        self.assertEqual(len(plan), 1)
        self.assertTrue(
            plan[0]["canonical"]["relative_path"].startswith("archived_sessions/")
        )
        self.assertEqual(len(plan[0]["aliases"]), 1)


if __name__ == "__main__":
    unittest.main()
