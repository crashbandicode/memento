from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_hierarchy import (  # noqa: E402
    ConversationRef,
    build_logical_activity_map,
    build_subagent_summaries,
    effective_conversation_timestamp,
    fold_codex_subagents,
)


def _ref(
    document_id: str,
    *,
    session_id: str | None = None,
    thread_id: str | None = None,
    root_session_id: str | None = None,
    source: str | None = None,
    depth: int | None = None,
    agent_path: str | None = None,
    agent_nickname: str | None = None,
    path: str = "sessions/thread.jsonl",
    tool_id: str = "codex",
    timestamp: str = "2026-07-08T12:00:00+00:00",
    source_timestamp: str | None = None,
    activity_timestamp: str | None = None,
    file_size_bytes: int = 100,
) -> ConversationRef:
    metadata = {
        key: value
        for key, value in {
            "session_id": session_id,
            "thread_id": thread_id,
            "root_session_id": root_session_id,
            "thread_source": source,
            "agent_depth": depth,
            "agent_path": agent_path,
            "agent_nickname": agent_nickname,
        }.items()
        if value is not None
    }
    return ConversationRef(
        document_id=document_id,
        tool_id=tool_id,
        relative_path=path,
        metadata=metadata,
        source_modified_at=(
            datetime.fromisoformat(source_timestamp).astimezone(timezone.utc)
            if source_timestamp
            else None
        ),
        activity_at=(
            datetime.fromisoformat(activity_timestamp).astimezone(timezone.utc)
            if activity_timestamp
            else None
        ),
        synced_at=datetime.fromisoformat(timestamp).astimezone(timezone.utc),
        file_size_bytes=file_size_bytes,
    )


class ConversationHierarchyTests(unittest.TestCase):
    def test_root_remains_visible_and_counts_distinct_descendants(self) -> None:
        root = _ref("root-doc", session_id="root-thread", source="root")
        child_a = _ref(
            "child-a-doc",
            session_id="child-a",
            root_session_id="root-thread",
            source="subagent",
        )
        duplicate_child_a = _ref(
            "child-a-copy",
            thread_id="child-a",
            root_session_id="root-thread",
            source="subagent",
        )
        nested_child = _ref(
            "nested-doc",
            thread_id="child-b",
            root_session_id="root-thread",
            source="subagent",
            depth=2,
        )

        result = fold_codex_subagents(
            [root, child_a, duplicate_child_a, nested_child]
        )

        self.assertEqual(result.visible_document_ids, {"root-doc"})
        self.assertEqual(result.subagent_counts, {"root-doc": 2})
        self.assertEqual(result.orphan_document_ids, set())
        self.assertEqual(
            result.subagent_document_ids,
            {"root-doc": ("child-a-doc", "nested-doc")},
        )
        self.assertEqual(result.canonical_document_ids["child-a-copy"], "root-doc")
        self.assertEqual(result.canonical_document_ids["nested-doc"], "root-doc")

    def test_missing_root_keeps_one_deterministic_orphan_representative(self) -> None:
        deep = _ref(
            "deep-doc",
            session_id="deep",
            root_session_id="missing-root",
            source="subagent",
            depth=2,
            timestamp="2026-07-08T13:00:00+00:00",
        )
        shallow_old = _ref(
            "shallow-old-doc",
            session_id="shallow-old",
            root_session_id="missing-root",
            source="subagent",
            depth=1,
            timestamp="2026-07-08T11:00:00+00:00",
        )
        shallow_new = _ref(
            "shallow-new-doc",
            session_id="shallow-new",
            root_session_id="missing-root",
            source="subagent",
            depth=1,
            timestamp="2026-07-08T12:00:00+00:00",
        )

        result = fold_codex_subagents([deep, shallow_old, shallow_new])

        self.assertEqual(result.visible_document_ids, {"shallow-new-doc"})
        self.assertEqual(result.orphan_document_ids, {"shallow-new-doc"})
        self.assertEqual(result.subagent_counts, {"shallow-new-doc": 3})
        self.assertEqual(
            result.subagent_document_ids,
            {"shallow-new-doc": (
                "shallow-new-doc",
                "shallow-old-doc",
                "deep-doc",
            )},
        )

    def test_legacy_paths_and_unlinked_metadata_stay_visible(self) -> None:
        legacy = _ref(
            "legacy-doc",
            session_id="legacy-child",
            root_session_id="legacy-root",
            source="subagent",
            path="sessions/legacy-root/subagents/agent-a.jsonl",
        )
        unlinked = _ref("unlinked-doc", session_id="normal")
        other_tool = _ref(
            "other-tool-doc",
            session_id="child",
            root_session_id="normal",
            source="subagent",
            tool_id="claude_code",
        )

        result = fold_codex_subagents([legacy, unlinked, other_tool])

        self.assertEqual(
            result.visible_document_ids,
            {"legacy-doc", "unlinked-doc", "other-tool-doc"},
        )
        self.assertEqual(result.subagent_counts, {})

    def test_non_codex_thread_with_same_uuid_is_not_treated_as_root(self) -> None:
        foreign = _ref("foreign-root", session_id="shared", tool_id="cursor")
        child = _ref(
            "codex-child",
            session_id="child",
            root_session_id="shared",
            source="subagent",
        )

        result = fold_codex_subagents([foreign, child])

        self.assertEqual(
            result.visible_document_ids,
            {"foreign-root", "codex-child"},
        )
        self.assertEqual(result.orphan_document_ids, {"codex-child"})

    def test_multi_host_root_copies_are_canonicalized_before_child_counting(self) -> None:
        old_root = _ref(
            "old-root",
            session_id="root-thread",
            timestamp="2026-07-08T11:00:00+00:00",
            file_size_bytes=900,
        )
        new_small_root = _ref(
            "new-small-root",
            session_id="root-thread",
            timestamp="2026-07-08T12:00:00+00:00",
            file_size_bytes=100,
        )
        new_complete_root = _ref(
            "new-complete-root",
            thread_id="root-thread",
            timestamp="2026-07-08T12:00:00+00:00",
            file_size_bytes=500,
        )
        child = _ref(
            "child",
            session_id="child-thread",
            root_session_id="root-thread",
            source="subagent",
        )

        result = fold_codex_subagents(
            [old_root, new_small_root, new_complete_root, child]
        )

        self.assertEqual(result.visible_document_ids, {"new-complete-root"})
        self.assertEqual(result.subagent_counts, {"new-complete-root": 1})
        self.assertEqual(
            result.subagent_document_ids,
            {"new-complete-root": ("child",)},
        )
        self.assertEqual(
            result.canonical_document_ids["old-root"],
            "new-complete-root",
        )

    def test_subagent_summary_prefers_task_path_over_inherited_title(self) -> None:
        root = _ref("root", session_id="root-thread")
        child = _ref(
            "child",
            session_id="child-thread",
            root_session_id="root-thread",
            source="subagent",
            agent_path="/root/search_pagination_repair",
            agent_nickname="Noether",
        )
        child = ConversationRef(
            document_id=child.document_id,
            tool_id=child.tool_id,
            relative_path=child.relative_path,
            metadata=child.metadata,
            title="Investigate the root production incident",
            activity_at=child.activity_at,
            synced_at=child.synced_at,
            file_size_bytes=child.file_size_bytes,
        )

        hierarchy = fold_codex_subagents([root, child])
        summaries = build_subagent_summaries(hierarchy, [root, child])

        self.assertEqual(summaries["root"][0]["title"], "search pagination repair")

    def test_logical_activity_uses_latest_real_child_turn(self) -> None:
        root = _ref(
            "root",
            session_id="root-thread",
            timestamp="2026-07-08T18:00:00+00:00",
            activity_timestamp="2026-07-08T10:00:00+00:00",
        )
        child = _ref(
            "child",
            session_id="child-thread",
            root_session_id="root-thread",
            source="subagent",
            timestamp="2026-07-08T12:00:00+00:00",
            activity_timestamp="2026-07-08T15:30:00+00:00",
        )

        hierarchy = fold_codex_subagents([root, child])
        activity = build_logical_activity_map(hierarchy, [root, child])
        summaries = build_subagent_summaries(hierarchy, [root, child])

        self.assertEqual(
            activity["root"],
            datetime(2026, 7, 8, 15, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            summaries["root"][0]["activity_at"],
            "2026-07-08T15:30:00+00:00",
        )
        self.assertEqual(
            summaries["root"][0]["timestamp"],
            "2026-07-08T15:30:00+00:00",
        )
        self.assertEqual(
            summaries["root"][0]["synced_at"],
            "2026-07-08T12:00:00+00:00",
        )

    def test_logical_activity_falls_back_without_persisting_import_time(self) -> None:
        root = _ref(
            "root",
            session_id="root-thread",
            timestamp="2026-07-08T18:00:00+00:00",
            source_timestamp="2026-07-08T11:00:00+00:00",
        )
        child = _ref(
            "child",
            session_id="child-thread",
            root_session_id="root-thread",
            source="subagent",
            timestamp="2026-07-08T17:00:00+00:00",
            source_timestamp="2026-07-08T16:00:00+00:00",
        )

        hierarchy = fold_codex_subagents([root, child])
        activity = build_logical_activity_map(hierarchy, [root, child])
        summaries = build_subagent_summaries(hierarchy, [root, child])

        self.assertIsNone(root.activity_at)
        self.assertIsNone(child.activity_at)
        self.assertEqual(
            activity["root"],
            datetime(2026, 7, 8, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(
            summaries["root"][0]["activity_at"],
            "2026-07-08T16:00:00+00:00",
        )

    def test_source_fallback_is_capped_at_sync_time(self) -> None:
        future_mtime = _ref(
            "doc",
            session_id="thread",
            timestamp="2026-07-08T17:00:00+00:00",
            source_timestamp="2026-07-09T17:00:00+00:00",
        )

        self.assertEqual(
            effective_conversation_timestamp(future_mtime),
            datetime(2026, 7, 8, 17, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
