from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from server.db.models import ConversationTaskState, Document
from server.services.conversation_parser import NormalizedMessage, TaskStateTracker
from server.services.conversation_tasks import (
    TaskCursorError,
    _build_root_response,
    _candidate_key,
    _state_from_metadata,
    canonical_task_state,
    decode_task_cursor,
    encode_task_cursor,
    task_state_counts,
    task_state_hash,
)


def _state(
    *tasks: tuple[str, str, str],
    revision: int = 1,
    current: bool = False,
    quality: str = "authoritative",
) -> dict:
    return {
        "version": 1,
        "source": "claude_code",
        "revision": revision,
        "is_current": current,
        "quality": quality,
        "source_ids": [f"source-{revision}"],
        "tasks": [
            {
                "id": task_id,
                "content": content,
                "status": status,
                "active_form": "",
            }
            for task_id, content, status in tasks
        ],
    }


def _tool(name: str, payload: dict, *, source_id: str = "call") -> NormalizedMessage:
    return NormalizedMessage(
        role="tool",
        content="",
        tool_name=name,
        tool_input=json.dumps(payload),
        tool_call_id=source_id,
        source_id=f"row-{source_id}",
    )


def test_seeded_task_update_retains_unchanged_tasks_and_revision() -> None:
    tracker = TaskStateTracker(
        "claude_code",
        _state(
            ("1", "Inspect", "in_progress"),
            ("2", "Verify", "pending"),
            revision=7,
            current=True,
            quality="explicit_current",
        ),
        incremental=True,
    )
    message = _tool("TaskUpdate", {"taskId": "1", "status": "completed"})

    tracker.apply(message)

    assert message.task_state is not None
    assert message.task_state["revision"] == 8
    assert message.task_state["is_current"] is True
    assert message.task_state["quality"] == "explicit_current"
    assert [(task["id"], task["status"]) for task in message.task_state["tasks"]] == [
        ("1", "completed"),
        ("2", "pending"),
    ]


def test_seeded_task_stop_retains_other_tasks() -> None:
    tracker = TaskStateTracker(
        "claude_code",
        _state(
            ("same", "Root task", "in_progress"),
            ("other", "Keep me", "blocked"),
            revision=2,
        ),
        incremental=True,
    )
    message = _tool("TaskStop", {"task_id": "same"})

    tracker.apply(message)

    assert message.task_state is not None
    assert [(task["id"], task["status"]) for task in message.task_state["tasks"]] == [
        ("same", "cancelled"),
        ("other", "blocked"),
    ]


def test_unseeded_incremental_update_is_partial_not_authoritative() -> None:
    tracker = TaskStateTracker("claude_code", incremental=True)
    update = _tool("TaskUpdate", {"taskId": "missing", "status": "completed"})
    stop = _tool("TaskStop", {"taskId": "also-missing"})

    tracker.apply(update)
    tracker.apply(stop)

    assert update.task_state is not None
    assert update.task_state["quality"] == "partial"
    assert stop.task_state is not None
    assert stop.task_state["quality"] == "partial"
    assert {task["id"] for task in stop.task_state["tasks"]} == {
        "missing",
        "also-missing",
    }


def test_full_todowrite_repairs_partial_and_preserves_explicit_empty() -> None:
    tracker = TaskStateTracker("cursor", incremental=True)
    update = _tool("TaskUpdate", {"taskId": "missing", "status": "completed"})
    replacement = _tool(
        "TodoWrite",
        {"todos": [], "merge": False, "is_current": True},
    )

    tracker.apply(update)
    tracker.apply(replacement)

    assert replacement.task_state is not None
    assert replacement.task_state["tasks"] == []
    assert replacement.task_state["quality"] == "explicit_current"
    assert replacement.task_state["is_current"] is True


def test_codex_direct_and_nested_update_plan_replacements() -> None:
    tracker = TaskStateTracker("codex")
    direct = _tool(
        "update_plan",
        {"plan": [{"step": "Direct", "status": "in_progress"}]},
        source_id="direct",
    )
    nested = NormalizedMessage(
        role="tool",
        content="",
        tool_name="exec",
        tool_input=(
            'await tools.update_plan({plan:['
            '{step:"Nested",status:"completed"},'
            '{step:"Next",status:"pending"}]})'
        ),
        tool_call_id="nested",
    )

    tracker.apply(direct)
    tracker.apply(nested)

    assert direct.task_state is not None
    assert nested.task_state is not None
    assert nested.task_state["revision"] == 2
    assert nested.task_state["is_current"] is True
    assert [task["content"] for task in nested.task_state["tasks"]] == [
        "Nested",
        "Next",
    ]


def test_explicit_current_candidate_outranks_later_transition_and_duplicates() -> None:
    explicit = SimpleNamespace(
        id=10,
        line_number=2,
        metadata_={"task_state": _state(
            ("1", "Current", "pending"),
            current=True,
            quality="explicit_current",
        )},
    )
    duplicate = SimpleNamespace(
        id=11,
        line_number=3,
        metadata_={"task_state": _state(
            ("1", "Current", "pending"),
            current=True,
            quality="explicit_current",
        )},
    )
    later_transition = SimpleNamespace(
        id=12,
        line_number=99,
        metadata_={"task_state": _state(
            ("1", "Historical", "completed"),
            quality="authoritative",
        )},
    )

    assert max(
        [explicit, duplicate, later_transition],
        key=_candidate_key,
    ) is duplicate


def test_canonical_hash_dedupes_revision_and_source_transport() -> None:
    first = canonical_task_state(_state(
        ("1", "Same", "pending"),
        revision=1,
    ))
    second = canonical_task_state({
        **_state(("1", "Same", "pending"), revision=99),
        "source_ids": ["different"],
    })

    assert first is not None
    assert second is not None
    assert task_state_hash(first) == task_state_hash(second)
    assert task_state_counts(first)["outstanding"] == 1


def test_legacy_explicit_snapshots_win_but_unseeded_mutations_are_partial() -> None:
    explicit = _state(("1", "Current", "pending"), current=True)
    explicit.pop("quality")
    mutation = _state(("1", "Changed", "completed"))
    mutation.pop("quality")

    explicit_state = _state_from_metadata({"task_state": explicit})
    mutation_state = _state_from_metadata(
        {"task_state": mutation, "tool_name": "TaskUpdate"}
    )

    assert explicit_state is not None
    assert explicit_state["quality"] == "explicit_current"
    assert mutation_state is not None
    assert mutation_state["quality"] == "partial"


def _document(
    thread_id: str,
    *,
    root_id: str,
    parent_id: str | None = None,
    depth: int = 0,
    title: str = "Agent",
) -> Document:
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    metadata = {
        "session_id": thread_id,
        "thread_id": thread_id,
        "root_session_id": root_id,
        "agent_depth": depth,
    }
    if parent_id:
        metadata.update(
            {
                "parent_thread_id": parent_id,
                "thread_source": "subagent",
                "is_subagent": True,
                "agent_id": thread_id,
            }
        )
    return Document(
        id=uuid4(),
        tool_id="codex",
        relative_path=f"sessions/{thread_id}.jsonl",
        category="conversation",
        content_type="jsonl",
        title=title,
        content_hash="hash",
        file_size_bytes=10,
        metadata_=metadata,
        activity_at=now,
        source_modified_at=now,
        synced_at=now,
    )


def _projection(document: Document, state: dict) -> ConversationTaskState:
    normalized = canonical_task_state(state)
    assert normalized is not None
    counts = task_state_counts(normalized)
    metadata = document.metadata_
    return ConversationTaskState(
        document_id=document.id,
        machine_id=None,
        user_id=None,
        tool_id=document.tool_id,
        thread_id=metadata["thread_id"],
        root_thread_id=metadata["root_session_id"],
        parent_thread_id=metadata.get("parent_thread_id"),
        agent_id=metadata.get("agent_id") or metadata["thread_id"],
        agent_path=None,
        agent_depth=metadata["agent_depth"],
        source_message_id=123,
        source_line_number=7,
        source_ids=normalized["source_ids"],
        revision=normalized["revision"],
        state=normalized,
        state_hash=task_state_hash(normalized),
        explicit_current=normalized["is_current"],
        quality=normalized["quality"],
        projection_version=1,
        pending_count=counts["pending"],
        in_progress_count=counts["in_progress"],
        blocked_count=counts["blocked"],
        completed_count=counts["completed"],
        cancelled_count=counts["cancelled"],
        outstanding_count=counts["outstanding"],
        total_count=counts["total"],
        observed_at=datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
        verified_at=None,
    )


def test_recursive_tree_preserves_no_task_and_explicit_empty_agents() -> None:
    root_id = "root"
    child_id = "child"
    grandchild_id = "grandchild"
    root = _document(root_id, root_id=root_id, title="Root")
    child = _document(
        child_id,
        root_id=root_id,
        parent_id=root_id,
        depth=1,
        title="Child",
    )
    grandchild = _document(
        grandchild_id,
        root_id=root_id,
        parent_id=child_id,
        depth=2,
        title="Grandchild",
    )
    root_projection = _projection(
        root,
        _state(
            ("same", "Outstanding root", "pending"),
            ("done", "Completed root", "completed"),
        ),
    )
    empty_projection = _projection(
        grandchild,
        _state(current=True, quality="explicit_current"),
    )

    response = _build_root_response(
        ("codex", root_id),
        [
            (root, root_projection),
            (child, None),
            (grandchild, empty_projection),
        ],
        status="outstanding",
        budget=[20],
        history={},
    )

    root_node = response["agents"][0]
    child_node = root_node["subagents"][0]
    grandchild_node = child_node["subagents"][0]
    assert [task["id"] for task in root_node["task_state"]["tasks"]] == ["same"]
    assert child_node["task_state"] is None
    assert grandchild_node["task_state"]["explicit_empty"] is True
    assert grandchild_node["task_state"]["tasks"] == []


def test_duplicate_task_ids_remain_document_local_and_status_filters_apply() -> None:
    root = _document("root", root_id="root")
    child = _document(
        "child",
        root_id="root",
        parent_id="root",
        depth=1,
    )
    root_projection = _projection(
        root,
        _state(("same", "Root copy", "completed")),
    )
    child_projection = _projection(
        child,
        _state(("same", "Child copy", "blocked")),
    )

    completed = _build_root_response(
        ("codex", "root"),
        [(root, root_projection), (child, child_projection)],
        status="completed",
        budget=[20],
        history={},
    )
    outstanding = _build_root_response(
        ("codex", "root"),
        [(root, root_projection), (child, child_projection)],
        status="outstanding",
        budget=[20],
        history={},
    )

    assert completed["agents"][0]["task_state"]["tasks"][0]["content"] == "Root copy"
    assert (
        completed["agents"][0]["subagents"][0]["task_state"]["tasks"] == []
    )
    assert outstanding["agents"][0]["task_state"]["tasks"] == []
    assert (
        outstanding["agents"][0]["subagents"][0]["task_state"]["tasks"][0][
            "content"
        ]
        == "Child copy"
    )


def test_missing_parents_and_cycles_are_bounded_top_level_nodes() -> None:
    missing = _document(
        "missing",
        root_id="root",
        parent_id="not-uploaded",
        depth=2,
    )
    left = _document("left", root_id="root", parent_id="right", depth=1)
    right = _document("right", root_id="root", parent_id="left", depth=1)

    response = _build_root_response(
        ("codex", "root"),
        [(missing, None), (left, None), (right, None)],
        status="all",
        budget=[5],
        history={},
    )

    qualities = {node["hierarchy_quality"] for node in response["agents"]}
    assert "missing_parent" in qualities
    assert "cycle" in qualities
    assert len(response["agents"]) == 3


def test_task_output_content_and_global_budget_are_truncated() -> None:
    root = _document("root", root_id="root")
    projection = _projection(
        root,
        _state(
            ("1", "x" * 1500, "pending"),
            ("2", "second", "pending"),
        ),
    )

    budget = [1]
    response = _build_root_response(
        ("codex", "root"),
        [(root, projection)],
        status="all",
        budget=budget,
        history={},
    )
    state = response["agents"][0]["task_state"]

    assert len(state["tasks"][0]["content"]) == 1000
    assert state["tasks"][0]["content_truncated"] is True
    assert state["tasks_truncated"] is True
    assert budget == [0, 1]

    exact_budget = [2]
    exact_response = _build_root_response(
        ("codex", "root"),
        [(root, projection)],
        status="all",
        budget=exact_budget,
        history={},
    )
    assert exact_response["agents"][0]["task_state"]["tasks_truncated"] is False
    assert exact_budget == [0, 0]


def test_query_fingerprinted_cursor_rejects_reuse() -> None:
    cursor = encode_task_cursor(20, "fingerprint-a")

    assert decode_task_cursor(cursor, "fingerprint-a") == 20
    with pytest.raises(TaskCursorError):
        decode_task_cursor(cursor, "fingerprint-b")
