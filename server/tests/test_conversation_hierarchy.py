from __future__ import annotations

import json
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
    clear_stale_briefing_keys_for_full_replacement,
    conversation_briefing_from_raw_prefix,
    conversation_briefing_kind,
    conversation_briefing_session_id,
    conversation_display_title,
    conversation_is_chain_primary,
    conversation_message_user_origin,
    conversation_user_role_origin,
    effective_conversation_timestamp,
    fold_codex_subagents,
    fold_conversation_subagents,
    merge_authoritative_subagent_summaries,
    merge_subagent_event_summaries,
    path_linked_subagent_identity,
    persist_conversation_briefing_metadata,
    resolve_conversation_briefing,
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
    extra_metadata: dict | None = None,
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
    metadata.update(extra_metadata or {})
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
    def test_cross_tool_orchestration_child_folds_into_proven_parent(self) -> None:
        root = _ref("root-doc", session_id="codex-root", source="root")
        child = _ref(
            "child-doc",
            session_id="claude-child",
            tool_id="claude_code",
            extra_metadata={
                "orchestration": "claw",
                "orchestration_parent_document_id": "root-doc",
                "orchestration_run_id": "fanout-12345678",
                "orchestration_run_kind": "fanout",
                "orchestration_agent_key": "reviewer",
                "orchestration_agent_name": "Security review",
                "orchestration_agent_codename": "Sentinel",
                "subagent_model": "claude-opus-4-1",
                "subagent_reasoning_effort": "high",
            },
        )

        result = fold_conversation_subagents([root, child])
        summaries = build_subagent_summaries(result, [root, child])

        self.assertEqual(result.visible_document_ids, {"root-doc"})
        self.assertEqual(result.subagent_counts, {"root-doc": 1})
        self.assertEqual(result.canonical_document_ids["child-doc"], "root-doc")
        self.assertEqual(summaries["root-doc"][0]["title"], "Security review")
        self.assertEqual(summaries["root-doc"][0]["agent_nickname"], "Sentinel")
        self.assertEqual(summaries["root-doc"][0]["tool_id"], "claude_code")
        self.assertEqual(summaries["root-doc"][0]["orchestration"], "claw")
        self.assertEqual(
            conversation_user_role_origin(
                child.tool_id,
                child.relative_path,
                child.metadata,
            ),
            "parent_agent",
        )

    def test_unresolved_orchestration_child_remains_visible(self) -> None:
        child = _ref(
            "child-doc",
            session_id="cursor-child",
            tool_id="cursor",
            extra_metadata={
                "orchestration": "claw",
                "orchestration_parent_document_id": "missing-root-doc",
            },
        )

        result = fold_conversation_subagents([child])

        self.assertEqual(result.visible_document_ids, {"child-doc"})
        self.assertEqual(result.orphan_document_ids, {"child-doc"})
        self.assertNotIn("child-doc", result.subagent_counts)

    def test_resolved_cross_tool_child_is_hidden_in_tool_scoped_list(self) -> None:
        child = _ref(
            "child-doc",
            session_id="claude-child",
            tool_id="claude_code",
            extra_metadata={
                "orchestration": "claw",
                "orchestration_parent_document_id": "codex-parent-doc",
                "orchestration_relation_resolved": True,
            },
        )

        result = fold_conversation_subagents([child])

        self.assertEqual(result.visible_document_ids, set())
        self.assertEqual(result.orphan_document_ids, set())
        self.assertEqual(
            result.canonical_document_ids["child-doc"],
            "codex-parent-doc",
        )

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
        self.assertEqual(
            conversation_user_role_origin(
                "cursor",
                (
                    "projects/sample/agent-transcripts/cursor-thread/"
                    "subagents/cursor-child-thread.jsonl"
                ),
                {
                    "is_subagent": True,
                    "root_session_id": "cursor-thread",
                    "parent_thread_id": "cursor-thread",
                },
            ),
            "parent_agent",
        )

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

    def test_subagent_summary_prefers_claude_launch_description(self) -> None:
        root = _ref(
            "root",
            session_id="root-thread",
            tool_id="claude_code",
            path="projects/yoga/root-thread.jsonl",
        )
        child = _ref(
            "child",
            session_id="agent-afceda9d5a896fb52",
            root_session_id="root-thread",
            agent_path="/root/unrelated_path_label",
            is_subagent=True,
            tool_id="claude_code",
            path=(
                "projects/yoga/root-thread/subagents/"
                "agent-afceda9d5a896fb52.jsonl"
            ),
        )
        child = ConversationRef(
            document_id=child.document_id,
            tool_id=child.tool_id,
            relative_path=child.relative_path,
            metadata={
                **(child.metadata or {}),
                "agent_launch_description": (
                    "Hoist wave engine into WaveDrainEngine mixin"
                ),
                "agent_id": "afceda9d5a896fb52",
                "agent_tool_use_id": "toolu-launch-wave-engine",
                "_assistant_model": "claude-opus-4-8",
                "_assistant_reasoning_effort": "xhigh",
            },
            title="TOOLING — HARD RULES...",
            activity_at=child.activity_at,
            synced_at=child.synced_at,
            file_size_bytes=child.file_size_bytes,
        )

        hierarchy = fold_conversation_subagents([root, child])
        summaries = build_subagent_summaries(hierarchy, [root, child])

        self.assertEqual(
            summaries["root"][0]["title"],
            "Hoist wave engine into WaveDrainEngine mixin",
        )
        self.assertEqual(
            summaries["root"][0]["agent_tool_use_id"],
            "toolu-launch-wave-engine",
        )
        self.assertEqual(
            summaries["root"][0]["user_role_origin"],
            "parent_agent",
        )
        self.assertEqual(
            summaries["root"][0]["model"],
            "claude-opus-4-8",
        )
        self.assertEqual(
            summaries["root"][0]["model_family"],
            "anthropic",
        )
        self.assertEqual(
            summaries["root"][0]["reasoning_effort"],
            "xhigh",
        )

    def test_claude_display_title_override_is_child_only(self) -> None:
        description = "Inspect lifecycle exact matching"
        child_path = "projects/demo/root/subagents/agent-child.jsonl"

        self.assertEqual(
            conversation_display_title(
                "claude_code",
                child_path,
                {
                    "is_subagent": True,
                    "agent_launch_description": description,
                },
                "Raw first prompt",
            ),
            description,
        )
        self.assertEqual(
            conversation_user_role_origin(
                "claude_code",
                child_path,
                {"is_subagent": True},
            ),
            "parent_agent",
        )
        self.assertEqual(
            conversation_display_title(
                "claude_code",
                "projects/demo/root.jsonl",
                {"agent_launch_description": description},
                "Root title",
            ),
            "Root title",
        )
        self.assertIsNone(
            conversation_user_role_origin(
                "claude_code",
                "projects/demo/root.jsonl",
                {},
            )
        )

    def test_claude_origin_uses_explicit_subagent_boolean_values(self) -> None:
        root_path = "projects/demo/root-thread.jsonl"
        child_metadata = {
            "parent_thread_id": "root-thread",
            "root_session_id": "root-thread",
            "agent_launch_description": "Inspect the parser",
        }

        for false_value in (False, "false", " FALSE "):
            with self.subTest(root_is_subagent=false_value):
                metadata = {**child_metadata, "is_subagent": false_value}
                self.assertIsNone(
                    conversation_user_role_origin(
                        "claude_code",
                        root_path,
                        metadata,
                    )
                )
                self.assertEqual(
                    conversation_display_title(
                        "claude_code",
                        root_path,
                        metadata,
                        "Root title",
                    ),
                    "Root title",
                )

        for true_value in (True, "true", " TRUE "):
            with self.subTest(child_is_subagent=true_value):
                metadata = {**child_metadata, "is_subagent": true_value}
                self.assertEqual(
                    conversation_user_role_origin(
                        "claude_code",
                        root_path,
                        metadata,
                    ),
                    "parent_agent",
                )
                self.assertEqual(
                    conversation_display_title(
                        "claude_code",
                        root_path,
                        metadata,
                        "Raw first prompt",
                    ),
                    "Inspect the parser",
                )

    def test_chain_primary_threads_are_not_parent_agent_origins(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        metadata = {
            "orchestration": "claw",
            "orchestration_parent_document_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "is_subagent": True,
            "first_user_message": f"MEMENTO-HANDOFF-FROM: {parent_id}\nContinue.",
        }
        self.assertEqual(conversation_briefing_kind(metadata["first_user_message"]), "handoff")
        self.assertTrue(conversation_is_chain_primary(metadata))
        self.assertIsNone(
            conversation_user_role_origin(
                "claude_code",
                "projects/demo/successor.jsonl",
                metadata,
            )
        )
        tangent = {
            **metadata,
            "first_user_message": (
                "MEMENTO-TANGENT-FROM: aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\n"
            ),
        }
        self.assertEqual(conversation_briefing_kind(tangent["first_user_message"]), "tangent")
        self.assertIsNone(
            conversation_user_role_origin("cursor", "agent-transcripts/t.jsonl", tangent)
        )

    def test_delegate_marker_is_classified_separately_from_chain_primaries(self) -> None:
        parent_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        content = f"MEMENTO-DELEGATE-FROM: {parent_id}\nImplement the design."
        self.assertEqual(conversation_briefing_kind(content), "delegate")
        self.assertEqual(conversation_briefing_session_id(content), parent_id)
        self.assertFalse(
            conversation_is_chain_primary({"first_user_message": content})
        )
        self.assertEqual(
            conversation_user_role_origin(
                "cursor",
                "agent-transcripts/delegate.jsonl",
                {"orchestration": "claw", "is_subagent": True},
            ),
            "parent_agent",
        )

    def test_briefing_kind_requires_a_parseable_uuid(self) -> None:
        invalid_delegate = "MEMENTO-DELEGATE-FROM: not-a-uuid\nwork"
        invalid_handoff = "MEMENTO-HANDOFF-FROM: not-a-uuid\nContinue."
        self.assertIsNone(conversation_briefing_kind(invalid_delegate))
        self.assertIsNone(conversation_briefing_session_id(invalid_delegate))
        self.assertIsNone(conversation_briefing_kind(invalid_handoff))
        self.assertFalse(
            conversation_is_chain_primary({"first_user_message": invalid_handoff})
        )
        self.assertEqual(
            resolve_conversation_briefing(persisted_user_content=invalid_delegate),
            (None, None),
        )

    def test_chain_primary_without_normalized_rows_uses_raw_or_durable_marker(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\nContinue."
        self.assertTrue(
            conversation_is_chain_primary(
                {},
                first_user_content="",
                raw_prefix=handoff,
            )
        )
        self.assertEqual(
            resolve_conversation_briefing(
                persisted_user_content="",
                metadata={},
                raw_prefix=handoff,
            ),
            ("handoff", parent_id),
        )
        jsonl = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": handoff},
            }
        ) + "\n"
        self.assertEqual(
            conversation_briefing_kind(conversation_briefing_from_raw_prefix(jsonl)),
            "handoff",
        )
        durable = persist_conversation_briefing_metadata({}, handoff)
        self.assertEqual(durable["briefing_kind"], "handoff")
        self.assertEqual(durable["briefing_session_id"], parent_id)
        self.assertTrue(
            conversation_is_chain_primary(
                durable,
                first_user_content=None,
                raw_prefix=None,
            )
        )

    def test_raw_prefix_skips_leading_non_user_records(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\nContinue."
        prefix = "\n".join(
            [
                json.dumps({"type": "summary", "summary": "prior turn"}),
                json.dumps(
                    {
                        "type": "system",
                        "content": "ignore this",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": handoff},
                    }
                ),
            ]
        )
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=prefix),
            ("handoff", parent_id),
        )
        self.assertTrue(
            conversation_is_chain_primary({}, raw_prefix=prefix)
        )

    def test_raw_prefix_rejects_assistant_marker_false_positive(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\nContinue."
        prefix = json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": handoff},
            }
        )
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=prefix),
            (None, None),
        )
        self.assertFalse(
            conversation_is_chain_primary({}, raw_prefix=prefix)
        )
        self.assertEqual(conversation_briefing_from_raw_prefix(prefix), "")

    def test_raw_prefix_extracts_marker_from_truncated_first_user_record(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("x" * 20000)
        line = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": handoff},
            }
        )
        truncated = line[:800]
        self.assertLess(len(truncated), len(line))
        self.assertFalse(truncated.endswith("}"))
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated),
            ("handoff", parent_id),
        )
        self.assertTrue(
            conversation_is_chain_primary({}, raw_prefix=truncated)
        )
        summary_then_truncated = (
            json.dumps({"type": "summary", "summary": "prior turn"})
            + "\n"
            + truncated
        )
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=summary_then_truncated),
            ("handoff", parent_id),
        )

    def test_raw_prefix_unresolvable_truncated_record_is_not_a_marker(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("y" * 5000)
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": handoff},
            }
        )
        truncated_assistant = assistant_line[:900]
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated_assistant),
            (None, None),
        )
        truncated_summary = '{"type": "summary", "summary": "' + ("z" * 500)
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated_summary),
            (None, None),
        )
        truncated_user_uuid = (
            '{"type":"user","message":{"role":"user","content":'
            '"MEMENTO-HANDOFF-FROM: aaaa'
        )
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated_user_uuid),
            (None, None),
        )

    def test_truncated_user_with_message_before_type_resolves(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("x" * 20000)
        line = json.dumps(
            {
                "message": {"role": "user", "content": handoff},
                "type": "user",
            }
        )
        truncated = line[:800]
        self.assertFalse(truncated.endswith("}"))
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated),
            ("handoff", parent_id),
        )

    def test_truncated_user_with_early_content_like_field_resolves(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("x" * 20000)
        line = json.dumps(
            {
                "content_digest": "unrelated early field",
                "type": "user",
                "message": {"role": "user", "content": handoff},
            }
        )
        truncated = line[:900]
        self.assertFalse(truncated.endswith("}"))
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated),
            ("handoff", parent_id),
        )

    def test_truncated_assistant_with_nested_user_type_is_rejected(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("x" * 20000)
        line = json.dumps(
            {
                "metadata": {"type": "user"},
                "type": "assistant",
                "message": {"role": "assistant", "content": handoff},
            }
        )
        truncated = line[:900]
        self.assertFalse(truncated.endswith("}"))
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated),
            (None, None),
        )
        self.assertEqual(conversation_briefing_from_raw_prefix(truncated), "")

    def test_truncated_user_with_nested_assistant_metadata_resolves(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("x" * 20000)
        line = json.dumps(
            {
                "metadata": {"type": "assistant"},
                "type": "user",
                "message": {"role": "user", "content": handoff},
            }
        )
        truncated = line[:900]
        self.assertFalse(truncated.endswith("}"))
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated),
            ("handoff", parent_id),
        )

    def test_truncated_user_with_mid_content_marker_stays_unresolved(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        content = (
            "Ordinary opening sentence about the routing protocol. "
            f"MEMENTO-HANDOFF-FROM: {parent_id}\n" + ("x" * 20000)
        )
        line = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": content},
            }
        )
        truncated = line[:900]
        self.assertFalse(truncated.endswith("}"))
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=truncated),
            (None, None),
        )
        self.assertEqual(conversation_briefing_from_raw_prefix(truncated), "")
        self.assertFalse(
            conversation_is_chain_primary({}, raw_prefix=truncated)
        )

    def test_raw_prefix_first_user_without_marker_is_authoritative(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        handoff = f"MEMENTO-HANDOFF-FROM: {parent_id}\nContinue."
        prefix = "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "ordinary first turn"},
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": handoff},
                    }
                ),
            ]
        )
        self.assertEqual(
            resolve_conversation_briefing(raw_prefix=prefix),
            (None, None),
        )

    def test_full_replacement_clears_stale_durable_briefing_keys(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        existing = {
            "briefing_kind": "handoff",
            "briefing_session_id": parent_id,
            "session_id": "successor",
        }
        clear_stale_briefing_keys_for_full_replacement(
            existing,
            "ordinary replacement",
        )
        self.assertNotIn("briefing_kind", existing)
        self.assertNotIn("briefing_session_id", existing)

        preserved = {
            "briefing_kind": "handoff",
            "briefing_session_id": parent_id,
        }
        clear_stale_briefing_keys_for_full_replacement(preserved, "")
        self.assertEqual(preserved["briefing_kind"], "handoff")
        replacement_marker = (
            "MEMENTO-TANGENT-FROM: bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb\nAsk."
        )
        clear_stale_briefing_keys_for_full_replacement(preserved, replacement_marker)
        self.assertEqual(preserved["briefing_kind"], "handoff")

    def test_global_export_keeps_claw_thread_with_explicit_human_prompt(self) -> None:
        from server.api.conversation_exports import conversation_export_prompt_is_included

        thread_origin = conversation_user_role_origin(
            "cursor",
            "agent-transcripts/delegate.jsonl",
            {"orchestration": "claw", "is_subagent": True},
        )
        self.assertEqual(thread_origin, "parent_agent")
        rows = [
            {"message_origin": "parent_agent"},
            {"message_origin": "human"},
        ]
        included = [
            metadata
            for metadata in rows
            if conversation_export_prompt_is_included(metadata, thread_origin)
        ]
        self.assertEqual(included, [{"message_origin": "human"}])
        self.assertFalse(
            conversation_export_prompt_is_included(
                {"message_origin": "parent_agent"},
                thread_origin,
            )
        )

    def test_per_message_origin_prefers_stored_value(self) -> None:
        self.assertEqual(
            conversation_message_user_origin(
                "user",
                {"message_origin": "human"},
                "parent_agent",
            ),
            "human",
        )
        self.assertEqual(
            conversation_message_user_origin(
                "user",
                {"message_origin": "parent_agent"},
                None,
            ),
            "parent_agent",
        )
        self.assertEqual(
            conversation_message_user_origin("user", {}, "parent_agent"),
            "parent_agent",
        )
        self.assertIsNone(conversation_message_user_origin("assistant", {}, "parent_agent"))

    def test_parentless_claw_delegate_is_visible_orphan(self) -> None:
        child = ConversationRef(
            document_id="child",
            tool_id="cursor",
            relative_path="agent-transcripts/delegate.jsonl",
            metadata={
                "orchestration": "claw",
                "is_subagent": True,
                "session_id": "delegate",
            },
        )
        hierarchy = fold_conversation_subagents([child])
        self.assertIn("child", hierarchy.visible_document_ids)
        self.assertIn("child", hierarchy.orphan_document_ids)

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
        self.assertEqual(
            summaries[0]["started_at"],
            "2026-07-20T08:39:45+00:00",
        )

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

    def test_claude_lifecycle_reconciles_tool_use_to_sidecar_agent_id(self) -> None:
        summaries = merge_subagent_event_summaries([{
            "id": "child-document",
            "session_id": "agent-afceda9d5a896fb52",
            "agent_id": "afceda9d5a896fb52",
            "agent_tool_use_id": "toolu-agent-launch",
            "title": "Inspect exact lifecycle identity",
        }], [
            {
                "agent_tool_use_id": "toolu-agent-launch",
                "label": "Inspect exact lifecycle identity",
                "kind": "started",
                "timestamp": "2026-07-30T10:00:00Z",
            },
            {
                "agent_tool_use_id": "toolu-agent-launch",
                "agent_thread_id": "afceda9d5a896fb52",
                "label": "Inspect exact lifecycle identity",
                "kind": "completed",
                "timestamp": "2026-07-30T10:01:00Z",
            },
        ])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["id"], "child-document")
        self.assertEqual(summaries[0]["status"], "completed")
        self.assertEqual(
            summaries[0]["completed_at"],
            "2026-07-30T10:01:00Z",
        )

    def test_claude_duplicate_descriptions_remain_distinct_lifecycle_cards(self) -> None:
        summaries = merge_subagent_event_summaries([], [
            {
                "agent_tool_use_id": "toolu-one",
                "label": "Same description",
                "kind": "started",
            },
            {
                "agent_tool_use_id": "toolu-two",
                "label": "Same description",
                "kind": "started",
            },
        ])

        self.assertEqual(len(summaries), 2)
        self.assertEqual(
            {summary["agent_tool_use_id"] for summary in summaries},
            {"toolu-one", "toolu-two"},
        )

    def test_terminal_lifecycle_status_is_not_reopened_by_replayed_launch(self) -> None:
        summaries = merge_subagent_event_summaries([], [
            {
                "agent_tool_use_id": "toolu-terminal",
                "label": "Terminal task",
                "kind": "completed",
                "timestamp": "2026-07-30T10:01:00Z",
            },
            {
                "agent_tool_use_id": "toolu-terminal",
                "label": "Terminal task",
                "kind": "started",
                "timestamp": "2026-07-30T10:00:00Z",
            },
        ])

        self.assertEqual(summaries[0]["status"], "completed")

    def test_lifecycle_merge_carries_source_model_and_both_times(self) -> None:
        summaries = merge_subagent_event_summaries([], [
            {
                "agent_thread_id": "cursor-child",
                "agent_path": "/root/add_terra_opus_to_allowlist",
                "label": "Add Terra/Opus to allowlist",
                "kind": "started",
                "timestamp": "2026-07-30T12:50:50.840+00:00",
                "started_at": "2026-07-30T12:50:50.840Z",
                "model": "gpt-5.6-sol-xhigh",
                "reasoning_effort": "xhigh",
            },
            {
                "agent_thread_id": "cursor-child",
                "agent_path": "/root/add_terra_opus_to_allowlist",
                "label": "Add Terra/Opus to allowlist",
                "kind": "completed",
                "timestamp": "2026-07-30T12:51:53.073+00:00",
                "completed_at": "2026-07-30T12:51:53.073Z",
            },
        ])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["model"], "gpt-5.6-sol-xhigh")
        self.assertEqual(summaries[0]["model_family"], "openai")
        self.assertEqual(summaries[0]["reasoning_effort"], "xhigh")
        self.assertEqual(
            summaries[0]["started_at"],
            "2026-07-30T12:50:50.840Z",
        )
        self.assertEqual(
            summaries[0]["completed_at"],
            "2026-07-30T12:51:53.073Z",
        )
        self.assertEqual(summaries[0]["status"], "completed")

    def test_lifecycle_merge_omits_unobserved_model_and_start_time(self) -> None:
        summaries = merge_subagent_event_summaries([], [{
            "agent_thread_id": "cursor-child",
            "agent_path": "/root/legacy_child",
            "label": "Legacy child",
            "kind": "completed",
            "timestamp": "2026-07-30T12:51:53+00:00",
        }])

        self.assertIsNone(summaries[0]["model"])
        self.assertIsNone(summaries[0]["started_at"])
        self.assertEqual(
            summaries[0]["completed_at"],
            "2026-07-30T12:51:53+00:00",
        )

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

    def test_authoritative_orchestrator_adds_failed_agent_without_document(self) -> None:
        summaries = merge_authoritative_subagent_summaries([], [{
            "id": None,
            "agent_tool_use_id": "session-run:agent-main",
            "title": "Unavailable Cursor model probe",
            "orchestration": "claw",
            "tool_id": "cursor",
            "model": "gemini-3.7-flash-high",
            "status": "failed",
            "document_ready": False,
        }])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "failed")
        self.assertEqual(summaries[0]["orchestration"], "claw")
        self.assertFalse(summaries[0]["document_ready"])

    def test_authoritative_orchestrator_overlays_matching_native_child(self) -> None:
        summaries = merge_authoritative_subagent_summaries([{
            "id": "child-document",
            "agent_tool_use_id": "session-run:agent-main",
            "title": "Native fallback title",
            "status": "unknown",
            "document_ready": True,
            "relative_path": "sessions/child.jsonl",
        }], [{
            "id": "child-document",
            "agent_tool_use_id": "session-run:agent-main",
            "title": "Named Claw task",
            "orchestration": "claw",
            "status": "completed",
            "document_ready": True,
        }])

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"], "Named Claw task")
        self.assertEqual(summaries[0]["status"], "completed")
        self.assertEqual(summaries[0]["relative_path"], "sessions/child.jsonl")

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
