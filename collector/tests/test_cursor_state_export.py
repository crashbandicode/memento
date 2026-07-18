from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from collector.cursor_state_export import (
    CursorStateExporter,
    enqueue_cursor_state_snapshots,
)
from collector.tools.cursor import CursorTool


class FixtureCursorTool(CursorTool):
    def __init__(self, root: Path, database: Path) -> None:
        super().__init__(state_database_path=database)
        self._root = root

    @property
    def root_path(self) -> Path:
        return self._root


def _write_state_fixture(tmp_path: Path) -> tuple[FixtureCursorTool, Path, str]:
    session_id = "18f25182-cddc-4102-81f9-408fecf0655c"
    root = tmp_path / ".cursor"
    transcript = (
        root
        / "projects"
        / "c-Users-intpa-demo"
        / "agent-transcripts"
        / session_id
        / f"{session_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role":"user","message":{"content":"sparse"}}\n')

    user_root = tmp_path / "Cursor" / "User"
    database = user_root / "globalStorage" / "state.vscdb"
    database.parent.mkdir(parents=True)
    workspace = user_root / "workspaceStorage" / "workspace-1" / "workspace.json"
    workspace.parent.mkdir(parents=True)
    workspace.write_text(
        json.dumps({"folder": "file:///C:/Users/intpa/demo"}),
        encoding="utf-8",
    )

    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE composerHeaders (
            composerId TEXT PRIMARY KEY,
            workspaceId TEXT,
            createdAt TEXT,
            lastUpdatedAt TEXT,
            isArchived INTEGER,
            isSubagent INTEGER,
            recency REAL,
            checkpointAt TEXT,
            value TEXT
        );
        CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    headers = [
        {"bubbleId": "user-1", "type": 1},
        {"bubbleId": "thought-1", "type": 2},
        {"bubbleId": "assistant-1", "type": 2},
        {"bubbleId": "tool-1", "type": 2},
        {"bubbleId": "tasks-1", "type": 2},
    ]
    composer = {
        "name": "Readable renamed thread",
        "status": "aborted",
        "modelConfig": {
            "modelName": "grok-4.5",
            "selectedModels": [{
                "modelId": "grok-4.5",
                "parameters": [{"id": "effort", "value": "high"}],
            }],
        },
        "fullConversationHeadersOnly": headers,
        "todos": [
            {"id": "1", "content": "Inspect", "status": "completed"},
            {"id": "2", "content": "Report", "status": "completed"},
        ],
        "conversationState": "opaque-state-must-not-sync",
        "blobEncryptionKey": "secret-must-not-sync",
    }
    bubbles = {
        "user-1": {
            "bubbleId": "user-1",
            "type": 1,
            "createdAt": "2026-07-18T14:19:00Z",
            "text": "Free the resources",
            "modelInfo": {"modelName": "grok-4.5"},
        },
        "thought-1": {
            "bubbleId": "thought-1",
            "type": 2,
            "createdAt": "2026-07-18T14:19:01Z",
            "thinking": {"text": "I should stop the cron safely."},
            "thinkingDurationMs": 1000,
        },
        "assistant-1": {
            "bubbleId": "assistant-1",
            "type": 2,
            "createdAt": "2026-07-18T14:19:02Z",
            "text": "Stopping it now.",
        },
        "tool-1": {
            "bubbleId": "tool-1",
            "type": 2,
            "createdAt": "2026-07-18T14:19:03Z",
            "toolFormerData": {
                "name": "run_terminal_command_v2",
                "status": "cancelled",
                "params": '{"command":"Stop-Process"}',
                "result": '{"output":"stopped"}',
                "toolCallId": "call-1",
                "toolCallBinary": "opaque-binary-must-not-sync",
            },
        },
        "tasks-1": {
            "bubbleId": "tasks-1",
            "type": 2,
            "createdAt": "2026-07-18T14:19:04Z",
            "todos": [
                {"id": "1", "content": "Inspect", "status": "completed"},
                {"id": "2", "content": "Report", "status": "pending"},
            ],
        },
    }
    connection.execute(
        "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            "workspace-1",
            "2026-07-18T14:00:00Z",
            "2026-07-18T14:20:00Z",
            0,
            0,
            1,
            "2026-07-18T14:20:00Z",
            json.dumps({"name": "Readable renamed thread"}),
        ),
    )
    connection.execute(
        "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "subagent-id",
            "workspace-1",
            "2026-07-18T14:00:00Z",
            "2026-07-18T14:21:00Z",
            0,
            1,
            1,
            "2026-07-18T14:21:00Z",
            "{}",
        ),
    )
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        (f"composerData:{session_id}", json.dumps(composer)),
    )
    connection.executemany(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        [
            (f"bubbleId:{session_id}:{bubble_id}", json.dumps(bubble))
            for bubble_id, bubble in bubbles.items()
        ],
    )
    connection.commit()
    connection.close()
    return FixtureCursorTool(root, database), transcript, session_id


def test_live_state_supersedes_sparse_transcript_and_projects_whitelist(tmp_path):
    tool, transcript, session_id = _write_state_fixture(tmp_path)

    assert session_id in tool.authoritative_session_ids(max_age=0)
    assert tool.classify_file(transcript) is None
    source_classification = tool.classify_transcript_source(transcript)
    assert source_classification is not None
    assert source_classification.sync_strategy.value == "full"

    exporter = CursorStateExporter(tool)
    snapshots = exporter.export_changed(limit=20)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    records = [json.loads(line) for line in snapshot.content.splitlines()]
    assert [record["type"] for record in records] == [
        "cursor_state_task",
        "user",
        "cursor_state_thinking",
        "assistant",
        "cursor_state_tool",
        "cursor_state_task",
        "cursor_state_status",
    ]
    assert records[2]["model"] == "grok-4.5"
    assert records[2]["reasoning_effort"] == "high"
    assert records[2]["message"]["content"][0]["thinking"] == (
        "I should stop the cron safely."
    )
    assert records[0]["tool_name"] == "Task progress 2/2"
    assert json.loads(records[0]["tool_input"])["is_current"] is True
    assert records[4]["tool_name"] == "PowerShell"
    assert records[4]["tool_status"] == "cancelled"
    assert records[5]["tool_name"] == "Task progress 1/2"
    assert records[-1]["tool_name"] == "Turn interrupted"
    assert "opaque-state-must-not-sync" not in snapshot.content
    assert "secret-must-not-sync" not in snapshot.content
    assert "opaque-binary-must-not-sync" not in snapshot.content
    assert snapshot.metadata["title"] == "Readable renamed thread"
    assert snapshot.metadata["project_path"] == "C:/Users/intpa/demo"
    assert snapshot.metadata["source"] == "cursor_state_v1"


def test_exporter_emits_only_changed_revisions_and_resync_can_invalidate(tmp_path):
    tool, _transcript, _session_id = _write_state_fixture(tmp_path)
    exporter = CursorStateExporter(tool)

    assert len(exporter.export_changed()) == 1
    assert exporter.export_changed() == []

    exporter.invalidate()
    assert len(exporter.export_changed()) == 1


def test_empty_header_does_not_starve_older_valid_composers(tmp_path):
    tool, _transcript, _session_id = _write_state_fixture(tmp_path)
    connection = sqlite3.connect(tool.state_database_path)
    connection.execute(
        "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "stale-header-without-data",
            "workspace-1",
            "2026-07-18T14:00:00Z",
            "2026-07-18T15:00:00Z",
            0,
            0,
            1,
            "2026-07-18T15:00:00Z",
            "{}",
        ),
    )
    connection.commit()
    connection.close()

    exporter = CursorStateExporter(tool)
    snapshots = exporter.export_changed(limit=1)

    assert len(snapshots) == 1
    assert "Readable renamed thread" == snapshots[0].metadata["title"]
    assert exporter.export_changed(limit=1) == []


def test_enqueue_uses_complete_snapshot_and_state_database_source(tmp_path):
    tool, _transcript, _session_id = _write_state_fixture(tmp_path)
    queue = SimpleNamespace(items=[])

    def enqueue(**kwargs):
        queue.items.append(kwargs)
        return 1

    queue.enqueue = enqueue
    queue.get_delta_base = lambda _tool, _path: (None, 0)
    queued = enqueue_cursor_state_snapshots(CursorStateExporter(tool), queue)

    assert queued == 1
    assert queue.items[0]["sync_strategy"] == "full"
    assert queue.items[0]["tool_name"] == "cursor"
    assert queue.items[0]["source_path"].endswith("state.vscdb")


def test_enqueue_sends_only_new_records_when_existing_projection_is_prefix(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["status"] = "generating"
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.commit()
    connection.close()

    exporter = CursorStateExporter(tool)
    initial = exporter.export_changed()[0]

    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["status"] = "completed"
    composer["fullConversationHeadersOnly"].append({
        "bubbleId": "assistant-2",
        "type": 2,
    })
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        (
            f"bubbleId:{session_id}:assistant-2",
            json.dumps({
                "bubbleId": "assistant-2",
                "type": 2,
                "createdAt": "2026-07-18T14:21:00Z",
                "text": "The resources are free.",
            }),
        ),
    )
    connection.execute(
        "UPDATE composerHeaders SET lastUpdatedAt=? WHERE composerId=?",
        ("2026-07-18T14:21:00Z", session_id),
    )
    connection.commit()
    connection.close()

    queue = SimpleNamespace(items=[])
    queue.enqueue = lambda **kwargs: queue.items.append(kwargs) or 1
    queue.get_delta_base = lambda _tool, _path: (
        initial.content_hash,
        len(initial.content.encode("utf-8")),
    )

    assert enqueue_cursor_state_snapshots(exporter, queue) == 1
    item = queue.items[0]
    assert item["sync_strategy"] == "delta"
    assert item["is_partial"] is True
    assert item["base_hash"] == initial.content_hash
    assert "The resources are free." in item["content"]
    assert "Free the resources" not in item["content"]
