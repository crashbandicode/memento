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
    fold_conversation_subagents,
    merge_subagent_event_summaries,
    path_linked_subagent_identity,
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
    is_subagent: bool | None = None,
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
            "is_subagent": is_subagent,
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

    def test_orphan_and_unlinked_metadata_stay_visible(self) -> None:
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
        self.assertEqual(result.subagent_counts, {"legacy-doc": 1})
        self.assertEqual(result.orphan_document_ids, {"legacy-doc"})

    def test_claude_path_children_fold_into_root(self) -> None:
        root = _ref(
            "claude-root",
            session_id="claude-thread",
            tool_id="claude_code",
            path="projects/sample/claude-thread.jsonl",
            activity_timestamp="2026-07-08T10:00:00+00:00",
        )
        child_a = _ref(
            "claude-child-a",
            session_id="agent-a",
            tool_id="claude_code",
            path="projects/sample/claude-thread/subagents/agent-a.jsonl",
            is_subagent=True,
            activity_timestamp="2026-07-08T12:00:00+00:00",
        )
        child_b = _ref(
            "claude-child-b",
            session_id="agent-b",
            tool_id="claude_code",
            path="projects/sample/claude-thread/subagents/agent-b.jsonl",
            is_subagent=True,
        )

        result = fold_conversation_subagents([root, child_a, child_b])
        activity = build_logical_activity_map(result, [root, child_a, child_b])

        self.assertEqual(result.visible_document_ids, {"claude-root"})
        self.assertEqual(result.subagent_counts, {"claude-root": 2})
        self.assertEqual(
            activity["claude-root"],
            datetime(2026, 7, 8, 12, tzinfo=timezone.utc),
        )

    def test_cursor_path_children_fold_into_root(self) -> None:
        root = _ref(
            "cursor-root",
            session_id="cursor-thread",
            tool_id="cursor",
            path=(
                "projects/sample/agent-transcripts/cursor-thread/"
                "cursor-thread.jsonl"
            ),
        )
        child = _ref(
            "cursor-child",
            session_id="cursor-child-thread",
            tool_id="cursor",
            path=(
                "projects/sample/agent-transcripts/cursor-thread/"
                "subagents/cursor-child-thread.jsonl"
            ),
            is_subagent=True,
        )

        result = fold_conversation_subagents([root, child])

        self.assertEqual(result.visible_document_ids, {"cursor-root"})
        self.assertEqual(result.subagent_counts, {"cursor-root": 1})

    def test_cursor_root_copies_are_canonicalized_across_hosts(self) -> None:
        old = _ref(
            "cursor-old",
            session_id="cursor-thread",
            tool_id="cursor",
            path="projects/windows/cursor-thread.jsonl",
            timestamp="2026-07-08T10:00:00+00:00",
        )
        new = _ref(
            "cursor-new",
            session_id="cursor-thread",
            tool_id="cursor",
            path="projects/linux/cursor-thread.jsonl",
            timestamp="2026-07-08T12:00:00+00:00",
        )

        result = fold_conversation_subagents([old, new])

        self.assertEqual(result.visible_document_ids, {"cursor-new"})
        self.assertEqual(
            result.canonical_document_ids["cursor-old"],
            "cursor-new",
        )

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

    def test_lifecycle_event_surfaces_child_before_document_ingest(self) -> None:
        summaries = merge_subagent_event_summaries([], [{
            "agent_thread_id": "child-thread",
            "agent_path": "/root/events_eof_handoff_trace",
            "label": "Events EOF Handoff Trace",
            "kind": "started",
            "timestamp": "2026-07-20T08:39:45+00:00",
        }])

        self.assertEqual(len(summaries), 1)
        self.assertIsNone(summaries[0]["id"])
        self.assertFalse(summaries[0]["document_ready"])
        self.assertEqual(summaries[0]["session_id"], "child-thread")
        self.assertEqual(summaries[0]["status"], "running")

    def test_lifecycle_event_enriches_ready_child_without_duplicate(self) -> None:
        summaries = merge_subagent_event_summaries([{
            "id": "child-document",
            "session_id": "child-thread",
            "title": "events eof handoff trace",
            "agent_nickname": "Franklin the 2nd",
            "agent_path": "/root/events_eof_handoff_trace",
        }], [{
            "agent_thread_id": "child-thread",
            "agent_path": "/root/events_eof_handoff_trace",
            "label": "Events EOF Handoff Trace",
            "kind": "completed",
            "timestamp": "2026-07-20T09:02:00+00:00",
        }])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["id"], "child-document")
        self.assertTrue(summaries[0]["document_ready"])
        self.assertEqual(summaries[0]["agent_nickname"], "Franklin the 2nd")
        self.assertEqual(summaries[0]["status"], "completed")

    def test_lifecycle_event_fills_missing_path_linked_identity(self) -> None:
        summaries = merge_subagent_event_summaries([{
            "id": "cursor-child",
            "session_id": "94d64099-e015-4fdb-848a-efaf7acc1695",
            "title": "PowerShell 7 only, never Bash tool. Interpreter…",
        }], [{
            "agent_thread_id": "94d64099-e015-4fdb-848a-efaf7acc1695",
            "agent_path": "/root/rno_api_mongo_diagnosis",
            "label": "RNO API Mongo diagnosis",
            "kind": "completed",
            "timestamp": "2026-07-21T12:00:00+00:00",
        }])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"], "RNO API Mongo diagnosis")
        self.assertEqual(
            summaries[0]["agent_path"],
            "/root/rno_api_mongo_diagnosis",
        )
        self.assertEqual(summaries[0]["status"], "completed")
        self.assertTrue(summaries[0]["document_ready"])

    def test_path_linked_identity_counts_nested_depth(self) -> None:
        identity = path_linked_subagent_identity(
            "projects/demo/agent-transcripts/root/subagents/child/"
            "subagents/grandchild.jsonl"
        )

        self.assertEqual(identity["root_session_id"], "root")
        self.assertEqual(identity["parent_thread_id"], "child")
        self.assertEqual(identity["agent_depth"], 2)

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
