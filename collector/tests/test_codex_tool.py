"""Focused tests for Codex rollout identity metadata."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from collector.tools import codex as codex_module
from collector.tools.codex import CodexTool


ROOT_ID = "019f144c-82d6-70d0-95e8-e01e7b813e98"
DEPTH_ONE_ID = "019f1904-dd99-7232-b69e-d078396d5d4d"
NESTED_ID = "019f1905-792a-7b31-af14-100e93e8baeb"


@pytest.fixture
def codex_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexTool:
    monkeypatch.setitem(codex_module.TOOL_PATHS, "codex", tmp_path)
    monkeypatch.setattr(codex_module, "_thread_info_cache", None)
    monkeypatch.setattr(codex_module, "_thread_info_cache_signature", None)
    monkeypatch.setattr(codex_module, "_history_cache", None)
    return CodexTool()


def _rollout_path(root: Path, thread_id: str) -> Path:
    path = (
        root
        / "sessions"
        / "2026"
        / "06"
        / "30"
        / f"rollout-2026-06-30T10-53-07-{thread_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_records(path: Path, records: list[dict], *, prefix: str = "") -> None:
    content = prefix + "\n".join(json.dumps(record) for record in records) + "\n"
    path.write_text(content, encoding="utf-8")


def _session_meta(**payload: object) -> dict:
    return {"type": "session_meta", "payload": payload}


def _create_state_db(
    root: Path,
    rollout_path: Path,
    title: str,
    *,
    thread_source: str = "user",
    agent_path: str = "",
) -> None:
    with sqlite3.connect(root / "state_5.sqlite") as connection:
        connection.executescript("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                title TEXT NOT NULL,
                first_user_message TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL DEFAULT 0,
                thread_source TEXT,
                agent_path TEXT
            );
        """)
        connection.execute(
            """INSERT INTO threads (
                   id, rollout_path, title, first_user_message,
                   updated_at, updated_at_ms, thread_source, agent_path
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                ROOT_ID,
                str(rollout_path),
                title,
                "First prompt",
                100,
                100_123,
                thread_source,
                agent_path,
            ),
        )


def test_user_root_identity_and_cwd_use_one_initial_read(
    codex_tool: CodexTool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _rollout_path(codex_tool.root_path, ROOT_ID)
    _write_records(
        path,
        [
            _session_meta(
                id=ROOT_ID,
                session_id=ROOT_ID,
                thread_source="user",
                source="vscode",
                cwd=r"C:\Users\intpa\projects\demo-project",
            )
        ],
        prefix="\n\n",
    )

    initial_reads = 0
    original = codex_tool._read_initial_session_meta

    def counted_read(session_path: Path) -> dict:
        nonlocal initial_reads
        initial_reads += 1
        return original(session_path)

    monkeypatch.setattr(codex_tool, "_read_initial_session_meta", counted_read)
    classification = codex_tool.classify_file(path)

    assert classification is not None
    assert initial_reads == 1
    assert classification.metadata == {
        "session_name": path.stem,
        "project_hash": "demo-project",
        "project_path": r"C:\Users\intpa\projects\demo-project",
        "session_id": ROOT_ID,
        "thread_id": ROOT_ID,
        "root_session_id": ROOT_ID,
        "thread_source": "user",
    }


def test_state_title_records_refresh_and_include_revision_and_path(
    codex_tool: CodexTool,
) -> None:
    path = _rollout_path(codex_tool.root_path, ROOT_ID)
    _write_records(path, [_session_meta(id=ROOT_ID, session_id=ROOT_ID)])
    _create_state_db(codex_tool.root_path, path, "Original title")

    first = codex_tool.thread_title_records()[ROOT_ID]
    assert first == {
        "metadata_type": "codex_thread_title",
        "tool": "codex",
        "thread_id": ROOT_ID,
        "title": "Original title",
        "revision": 100_123,
        "relative_path": str(path.relative_to(codex_tool.root_path)).replace("\\", "/"),
    }

    with sqlite3.connect(codex_tool.root_path / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET title=?, updated_at_ms=? WHERE id=?",
            ("Explicitly renamed", 200_456, ROOT_ID),
        )

    second = codex_tool.thread_title_records()[ROOT_ID]
    assert second["title"] == "Explicitly renamed"
    assert second["revision"] == 200_456


def test_subagent_state_titles_are_never_emitted_as_explicit_renames(
    codex_tool: CodexTool,
) -> None:
    path = _rollout_path(codex_tool.root_path, ROOT_ID)
    _write_records(path, [_session_meta(id=ROOT_ID, session_id=ROOT_ID)])
    _create_state_db(
        codex_tool.root_path,
        path,
        "Inherited parent title",
        thread_source="subagent",
    )

    assert codex_tool.thread_title_records() == {}


def test_legacy_agent_path_is_not_treated_as_a_root_rename(
    codex_tool: CodexTool,
) -> None:
    path = _rollout_path(codex_tool.root_path, ROOT_ID)
    _write_records(path, [_session_meta(id=ROOT_ID, session_id=ROOT_ID)])
    _create_state_db(
        codex_tool.root_path,
        path,
        "Inherited legacy title",
        thread_source="",
        agent_path="/root/child_audit",
    )

    assert codex_tool.thread_title_records() == {}


def test_depth_one_subagent_identity(codex_tool: CodexTool) -> None:
    path = _rollout_path(codex_tool.root_path, DEPTH_ONE_ID)
    _write_records(
        path,
        [
            _session_meta(
                id=DEPTH_ONE_ID,
                session_id=ROOT_ID,
                thread_source="subagent",
                parent_thread_id=ROOT_ID,
                forked_from_id=ROOT_ID,
                agent_path="/root/unbounded_job_growth",
                agent_nickname="Leibniz",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": ROOT_ID,
                            "depth": 1,
                            "agent_path": "/root/unbounded_job_growth",
                            "agent_nickname": "Leibniz",
                        }
                    }
                },
            )
        ],
    )

    classification = codex_tool.classify_file(path)

    assert classification is not None
    assert classification.metadata["session_id"] == DEPTH_ONE_ID
    assert classification.metadata["thread_id"] == DEPTH_ONE_ID
    assert classification.metadata["root_session_id"] == ROOT_ID
    assert classification.metadata["thread_source"] == "subagent"
    assert classification.metadata["parent_thread_id"] == ROOT_ID
    assert classification.metadata["forked_from_id"] == ROOT_ID
    assert classification.metadata["agent_path"] == "/root/unbounded_job_growth"
    assert classification.metadata["agent_nickname"] == "Leibniz"
    assert classification.metadata["agent_depth"] == 1


def test_nested_subagent_uses_its_own_first_session_meta(
    codex_tool: CodexTool,
) -> None:
    path = _rollout_path(codex_tool.root_path, NESTED_ID)
    _write_records(
        path,
        [
            _session_meta(
                id=NESTED_ID,
                session_id=ROOT_ID,
                thread_source="subagent",
                parent_thread_id=DEPTH_ONE_ID,
                forked_from_id=DEPTH_ONE_ID,
                agent_path="/root/unbounded_job_growth/mongo_growth_callgraph",
                agent_nickname="Noether",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": DEPTH_ONE_ID,
                            "depth": 2,
                            "agent_path": (
                                "/root/unbounded_job_growth/mongo_growth_callgraph"
                            ),
                            "agent_nickname": "Noether",
                        }
                    }
                },
            ),
            _session_meta(
                id=DEPTH_ONE_ID,
                session_id=ROOT_ID,
                thread_source="subagent",
                parent_thread_id=ROOT_ID,
                agent_path="/root/unbounded_job_growth",
                agent_nickname="Leibniz",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": ROOT_ID,
                            "depth": 1,
                        }
                    }
                },
            ),
            _session_meta(
                id=ROOT_ID,
                session_id=ROOT_ID,
                thread_source="user",
                source="vscode",
            ),
        ],
    )

    classification = codex_tool.classify_file(path)

    assert classification is not None
    metadata = classification.metadata
    assert metadata["thread_id"] == NESTED_ID
    assert metadata["root_session_id"] == ROOT_ID
    assert metadata["parent_thread_id"] == DEPTH_ONE_ID
    assert metadata["forked_from_id"] == DEPTH_ONE_ID
    assert metadata["agent_depth"] == 2
    assert metadata["agent_nickname"] == "Noether"
    assert metadata["agent_path"].endswith("/mongo_growth_callgraph")


@pytest.mark.parametrize("legacy_kind", ["malformed", "missing-id"])
def test_malformed_and_legacy_records_fall_back_to_filename(
    codex_tool: CodexTool,
    legacy_kind: str,
) -> None:
    fallback_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path = _rollout_path(codex_tool.root_path, fallback_id)
    if legacy_kind == "malformed":
        later_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        path.write_text(
            "not-json\n"
            + json.dumps(
                _session_meta(
                    id=later_id,
                    session_id=later_id,
                    thread_source="user",
                )
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        _write_records(
            path,
            [
                _session_meta(
                    cwd=r"C:\Users\intpa\projects\legacy-project",
                    source="vscode",
                )
            ],
        )

    classification = codex_tool.classify_file(path)

    assert classification is not None
    metadata = classification.metadata
    assert metadata["session_id"] == fallback_id
    assert metadata["thread_id"] == fallback_id
    assert metadata["root_session_id"] == fallback_id
    assert "thread_source" not in metadata
    if legacy_kind == "malformed":
        assert "project_path" not in metadata
    else:
        assert metadata["project_hash"] == "legacy-project"
        assert metadata["project_path"].endswith(r"projects\legacy-project")
