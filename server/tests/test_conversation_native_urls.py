from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_identity import (  # noqa: E402
    conversation_native_id,
    conversation_resume_id,
    native_conversation_tool_id,
    native_conversation_tool_slug,
    native_conversation_url,
    select_canonical_conversation_document,
)


class ConversationNativeUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = str(uuid.uuid4())

    def test_supported_tools_have_stable_public_slugs(self) -> None:
        expected = {
            "claude": "claude_code",
            "codex": "codex",
            "cursor": "cursor",
        }
        for slug, tool_id in expected.items():
            with self.subTest(slug=slug):
                self.assertEqual(native_conversation_tool_id(slug), tool_id)
                self.assertEqual(native_conversation_tool_slug(tool_id), slug)

        self.assertIsNone(native_conversation_tool_id("unknown"))
        self.assertIsNone(native_conversation_tool_slug("unknown"))

    def test_native_url_uses_the_tool_session_uuid(self) -> None:
        for tool_id, slug in (
            ("claude_code", "claude"),
            ("codex", "codex"),
            ("cursor", "cursor"),
        ):
            with self.subTest(tool_id=tool_id):
                metadata = {"session_id": self.session_id}
                self.assertEqual(
                    conversation_native_id(tool_id, "conversation", metadata),
                    self.session_id,
                )
                self.assertEqual(
                    native_conversation_url(tool_id, "conversation", metadata),
                    f"/conversations/{slug}/{self.session_id}",
                )

    def test_resume_id_uses_root_without_changing_the_native_url(self) -> None:
        root_id = str(uuid.uuid4())
        metadata = {
            "session_id": self.session_id,
            "root_session_id": root_id,
        }
        self.assertEqual(
            conversation_native_id("codex", "conversation", metadata),
            self.session_id,
        )
        self.assertEqual(
            conversation_resume_id("codex", "conversation", metadata),
            root_id,
        )
        self.assertEqual(
            native_conversation_url("codex", "conversation", metadata),
            f"/conversations/codex/{self.session_id}",
        )

    def test_invalid_or_non_conversation_metadata_has_no_native_url(self) -> None:
        invalid_cases = (
            ("codex", "conversation", {"session_id": "not-a-uuid"}),
            ("codex", "memory", {"session_id": self.session_id}),
            ("other", "conversation", {"session_id": self.session_id}),
            ("codex", "conversation", None),
        )
        for tool_id, category, metadata in invalid_cases:
            with self.subTest(tool_id=tool_id, category=category):
                self.assertIsNone(
                    conversation_native_id(tool_id, category, metadata)
                )
                self.assertIsNone(native_conversation_url(tool_id, category, metadata))

    def test_claude_duplicate_selection_is_deterministic(self) -> None:
        now = datetime.now(timezone.utc)
        older = {
            "id": "older",
            "activity_at": now - timedelta(minutes=1),
            "source_modified_at": now,
            "file_size_bytes": 200,
            "synced_at": now,
        }
        active = {
            "id": "active",
            "activity_at": now,
            "source_modified_at": now - timedelta(minutes=1),
            "file_size_bytes": 100,
            "synced_at": now,
        }
        selected = select_canonical_conversation_document(
            [older, active],
            tool_id="claude_code",
            session_id=self.session_id,
        )
        self.assertEqual(selected["id"], "active")


if __name__ == "__main__":
    unittest.main()
