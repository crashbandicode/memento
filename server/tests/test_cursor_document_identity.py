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
    cursor_document_preference,
    cursor_path_rank,
    cursor_session_id,
    select_canonical_cursor_document,
    should_relocate_cursor_document,
)
from server.services.ingest_service import (  # noqa: E402
    _scoped_cursor_identity_select,
    _source_lock_id,
)
from server.scripts.consolidate_cursor_sessions import (  # noqa: E402
    build_cursor_consolidation_plan,
)


class CursorDocumentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = str(uuid.uuid4())

    def test_only_uuid_cursor_conversations_receive_stable_identity(self) -> None:
        self.assertEqual(
            cursor_session_id(
                "cursor",
                "conversation",
                {"session_id": self.session_id},
            ),
            self.session_id,
        )
        self.assertIsNone(
            cursor_session_id("cursor", "conversation", {"session_id": "shared"})
        )
        self.assertIsNone(
            cursor_session_id("codex", "conversation", {"session_id": self.session_id})
        )
        self.assertIsNone(
            cursor_session_id("cursor", "config", {"session_id": self.session_id})
        )

    def test_real_project_and_promoted_root_paths_outrank_aliases(self) -> None:
        placeholder = (
            f"projects/empty-window/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )
        project = (
            f"projects/c-Users-intpa-app/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )
        subagent = (
            "projects/home-intpa-app/agent-transcripts/root/subagents/"
            f"{self.session_id}.jsonl"
        )
        promoted = (
            f"projects/home-intpa-app/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )

        self.assertGreater(
            cursor_path_rank(project, self.session_id),
            cursor_path_rank(placeholder, self.session_id),
        )
        self.assertGreater(
            cursor_path_rank(promoted, self.session_id),
            cursor_path_rank(subagent, self.session_id),
        )

    def test_preference_uses_path_quality_then_monotonic_revision(self) -> None:
        now = datetime.now(timezone.utc)
        placeholder = (
            f"projects/empty-window/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )
        project = (
            f"projects/c-Users-intpa-app/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )

        better_path = cursor_document_preference(
            session_id=self.session_id,
            relative_path=project,
            source_modified_at=now - timedelta(days=1),
            file_size_bytes=100,
            synced_at=now - timedelta(days=1),
            document_id="project",
        )
        newer_placeholder = cursor_document_preference(
            session_id=self.session_id,
            relative_path=placeholder,
            source_modified_at=now,
            file_size_bytes=1_000,
            synced_at=now,
            document_id="placeholder",
        )
        self.assertGreater(better_path, newer_placeholder)

        older = cursor_document_preference(
            session_id=self.session_id,
            relative_path=project,
            source_modified_at=now - timedelta(minutes=1),
            file_size_bytes=100,
            synced_at=now,
            document_id="older",
        )
        newer = cursor_document_preference(
            session_id=self.session_id,
            relative_path=project,
            source_modified_at=now,
            file_size_bytes=200,
            synced_at=now,
            document_id="newer",
        )
        self.assertGreater(newer, older)

        selected = select_canonical_cursor_document(
            [
                {
                    "id": "placeholder",
                    "relative_path": placeholder,
                    "source_modified_at": now,
                    "file_size_bytes": 1_000,
                    "synced_at": now,
                },
                {
                    "id": "project",
                    "relative_path": project,
                    "source_modified_at": now - timedelta(days=1),
                    "file_size_bytes": 100,
                    "synced_at": now - timedelta(days=1),
                },
            ],
            self.session_id,
        )
        self.assertEqual(selected["id"], "project")

    def test_relocation_does_not_flip_back_to_lower_quality_alias(self) -> None:
        now = datetime.now(timezone.utc)
        placeholder = (
            f"projects/empty-window/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )
        project = (
            f"projects/c-Users-intpa-app/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )

        self.assertTrue(should_relocate_cursor_document(
            session_id=self.session_id,
            current_path=placeholder,
            incoming_path=project,
            current_modified_at=now,
            incoming_modified_at=now,
        ))
        self.assertFalse(should_relocate_cursor_document(
            session_id=self.session_id,
            current_path=project,
            incoming_path=placeholder,
            current_modified_at=now,
            incoming_modified_at=now + timedelta(days=1),
        ))

    def test_identity_select_is_scoped_to_machine_owner_and_session(self) -> None:
        machine_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        statement = _scoped_cursor_identity_select(
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
        self.assertIn("session_id", compiled.params.values())
        self.assertIn("machines.user_id =", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn(machine_id, compiled.params.values())
        self.assertIn(user_id, compiled.params.values())
        self.assertIn(self.session_id, compiled.params.values())

    def test_stable_identity_serializes_alias_paths_under_one_lock(self) -> None:
        machine_id = str(uuid.uuid4())
        common = {
            "machine_id": machine_id,
            "user_id": None,
            "tool_id": "cursor",
            "source_identity": self.session_id,
        }
        first = _source_lock_id(relative_path="projects/empty-window/a.jsonl", **common)
        second = _source_lock_id(relative_path="projects/real/a.jsonl", **common)
        path_only = _source_lock_id(
            machine_id=machine_id,
            user_id=None,
            tool_id="cursor",
            relative_path="projects/real/a.jsonl",
            source_identity=None,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, path_only)

    def test_consolidation_plan_selects_one_canonical_monotonic_revision(self) -> None:
        machine_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        placeholder = (
            f"projects/empty-window/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )
        project = (
            f"projects/c-Users-intpa-app/agent-transcripts/{self.session_id}/"
            f"{self.session_id}.jsonl"
        )
        rows = [
            {
                "id": uuid.uuid4(),
                "machine_id": machine_id,
                "session_id": self.session_id,
                "relative_path": placeholder,
                "source_modified_at": now,
                "file_size_bytes": 1_000,
                "synced_at": now,
                "created_at": now - timedelta(days=2),
            },
            {
                "id": uuid.uuid4(),
                "machine_id": machine_id,
                "session_id": self.session_id,
                "relative_path": project,
                "source_modified_at": now - timedelta(minutes=1),
                "file_size_bytes": 900,
                "synced_at": now,
                "created_at": now - timedelta(days=1),
            },
        ]

        plan = build_cursor_consolidation_plan(rows)

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["canonical"]["relative_path"], project)
        self.assertEqual(
            [alias["relative_path"] for alias in plan[0]["aliases"]],
            [placeholder],
        )


if __name__ == "__main__":
    unittest.main()
