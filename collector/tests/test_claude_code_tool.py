"""Focused classification tests for Claude Code sidecar metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collector.tools import claude_code as claude_module
from collector.tools.base import Category, ContentType, SyncStrategy
from collector.tools.claude_code import ClaudeCodeTool


@pytest.fixture
def claude_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudeCodeTool:
    monkeypatch.setitem(claude_module.TOOL_PATHS, "claude_code", tmp_path)
    return ClaudeCodeTool()


def test_subagent_metadata_is_state_not_conversation(
    claude_tool: ClaudeCodeTool,
) -> None:
    sidecar = (
        claude_tool.root_path
        / "projects"
        / "demo-project"
        / "session-id"
        / "subagents"
        / "agent-abc.meta.json"
    )
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "agentType": "general-purpose",
            "description": "Review the parser",
            "spawnDepth": 1,
        }),
        encoding="utf-8",
    )

    classification = claude_tool.classify_file(sidecar)

    assert classification is not None
    assert classification.category is Category.STATE
    assert classification.content_type is ContentType.JSON
    assert classification.sync_strategy is SyncStrategy.FULL
    assert classification.metadata["is_subagent_meta"] is True
    sidecar_watch = next(
        watch
        for watch in claude_tool.get_watch_paths()
        if watch.pattern == "**/*.meta.json"
    )
    assert sidecar_watch.category is Category.STATE


def test_jsonl_sibling_remains_a_conversation(claude_tool: ClaudeCodeTool) -> None:
    transcript = (
        claude_tool.root_path
        / "projects"
        / "demo-project"
        / "session-id"
        / "subagents"
        / "agent-abc.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")

    classification = claude_tool.classify_file(transcript)

    assert classification is not None
    assert classification.category is Category.CONVERSATION
    assert classification.metadata["is_subagent"] is True
    assert classification.metadata["parent_thread_id"] == "session-id"
    assert classification.metadata["root_session_id"] == "session-id"
    assert classification.metadata["agent_depth"] == 1


def test_nested_jsonl_subagent_tracks_immediate_parent(
    claude_tool: ClaudeCodeTool,
) -> None:
    transcript = (
        claude_tool.root_path
        / "projects"
        / "demo-project"
        / "session-id"
        / "subagents"
        / "child-id"
        / "subagents"
        / "grandchild.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")

    classification = claude_tool.classify_file(transcript)

    assert classification is not None
    assert classification.metadata["root_session_id"] == "session-id"
    assert classification.metadata["parent_thread_id"] == "child-id"
    assert classification.metadata["agent_depth"] == 2
