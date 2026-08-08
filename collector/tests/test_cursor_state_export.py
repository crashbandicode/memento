from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from collector.cursor_state_export import (
    CursorStateExporter,
    _iso_timestamp,
    _model_selection,
    _tool_record,
    _workspace_folder_path,
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


def test_model_selection_reads_current_cursor_reasoning_parameter() -> None:
    assert _model_selection({
        "modelName": "gpt-5.6-sol",
        "maxMode": True,
        "selectedModels": [{
            "modelId": "gpt-5.6-sol",
            "parameters": [
                {"id": "context", "value": "272k"},
                {"id": "reasoning", "value": "xhigh"},
                {"id": "fast", "value": "false"},
            ],
        }],
    }) == ("gpt-5.6-sol", "xhigh")


def test_pending_question_uses_cursor_interaction_status() -> None:
    record = _tool_record(
        {
            "name": "ask_question",
            "status": "completed",
            "additionalData": {"status": "pending"},
            "params": {
                "title": "Sample question",
                "questions": [{
                    "id": "choice",
                    "prompt": "Which option?",
                    "options": [{"id": "safe", "label": "Safe"}],
                }],
            },
            "toolCallId": "call-question-1",
        },
        source_id="question-1",
        timestamp="2026-07-24T20:00:00Z",
        model="grok-4.5",
        reasoning_effort="high",
    )

    assert record["tool_status"] == "pending"
    assert record["content"] == "Status: pending"
    assert "Which option?" in str(record["tool_input"])


def test_plan_mode_request_projects_native_params_and_pending_status() -> None:
    record = _tool_record(
        {
            "name": "switch_mode",
            "status": "loading",
            "params": json.dumps({
                "fromModeId": "agent",
                "toModeId": "plan",
                "explanation": "Confirm the architecture before editing.",
            }),
            "rawArgs": "{}",
            "result": "{}",
            "toolCallId": "call-plan-1",
        },
        source_id="plan-1",
        timestamp="2026-07-26T22:42:27Z",
        model="grok-4.5",
        reasoning_effort="high",
    )

    assert record["tool_status"] == "loading"
    assert json.loads(str(record["tool_input"]))["toModeId"] == "plan"
    assert "Confirm the architecture" in str(record["tool_input"])
    assert record["content"].startswith("Status: loading")


def test_skipped_plan_mode_request_projects_native_timeout_reason() -> None:
    record = _tool_record(
        {
            "name": "switch_mode",
            "status": "cancelled",
            "additionalData": {"skipReason": "timeout"},
            "params": json.dumps({
                "fromModeId": "agent",
                "toModeId": "plan",
                "explanation": "Confirm the architecture before editing.",
            }),
            "rawArgs": "{}",
            "result": "{}",
            "toolCallId": "call-plan-1",
        },
        source_id="plan-1",
        timestamp="2026-07-26T22:42:27Z",
        model="grok-4.5",
        reasoning_effort="high",
    )

    assert record["tool_status"] == "cancelled"
    assert record["tool_status_reason"] == "timeout"


def test_subagent_task_projects_requested_model_and_exact_start_time() -> None:
    record = _tool_record(
        {
            "name": "task_v2",
            "status": "completed",
            "params": {
                "description": "Add Terra/Opus to allowlist",
                "prompt": (
                    "ADDITIONAL DIRECTIVE: Do NOT go off on tooling rabbit holes.\n\n"
                    "Update the model allowlists."
                ),
                "subagentType": "unspecified",
                "model": "gpt-5.6-sol-xhigh",
                "name": "general-purpose",
            },
            "result": {
                "agentId": "bc319435-7c07-4287-8428-f28257207520",
                "isBackground": True,
            },
            "toolCallId": "call-628beaa1",
        },
        source_id="ba873814-11b5-451f-b5e1-556764456c40:tool",
        timestamp="2026-07-30T12:50:50.840Z",
        model="grok-4.5",
        reasoning_effort="high",
    )

    params = json.loads(str(record["tool_input"]))
    assert record["timestamp"] == "2026-07-30T12:50:50.840Z"
    assert record["model"] == "grok-4.5"
    assert params["model"] == "gpt-5.6-sol-xhigh"
    assert params["prompt"].startswith("ADDITIONAL DIRECTIVE:")
    assert json.loads(str(record["content"]))["isBackground"] is True


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


def _add_subagent_state_fixture(
    tool: FixtureCursorTool,
    root_session_id: str,
) -> tuple[Path, str]:
    session_id = "subagent-id"
    nested_transcript = (
        tool.root_path
        / "projects"
        / "c-Users-intpa-demo"
        / "agent-transcripts"
        / root_session_id
        / "subagents"
        / f"{session_id}.jsonl"
    )
    nested_transcript.parent.mkdir(parents=True)
    observed_thinking = (
        "**Investigating tool paths**\n\n"
        "I need to locate artifacts, which involves searching for specific strings."
    )
    nested_transcript.write_text(
        "\n".join([
            json.dumps({
                "role": "user",
                "message": {
                    "content": (
                        "<timestamp>Wednesday, Jul 29, 2026, 10:34 PM "
                        "(UTC-4)</timestamp>\n<user_query>Investigate</user_query>"
                    )
                },
            }),
            # Exact production shape: no outer ID or timestamp, and prose plus
            # tool calls flattened into one compatibility record.
            json.dumps({
                "role": "assistant",
                "message": {"content": [
                    {"type": "text", "text": observed_thinking},
                    {
                        "type": "tool_use",
                        "name": "rg",
                        "input": {"pattern": "handoff"},
                    },
                ]},
            }),
            json.dumps({
                "role": "assistant",
                "bubbleId": "compatibility-fallback",
                "timestamp": "2026-07-30T02:35:10.123Z",
                "message": {"content": "Compatibility timestamp fallback"},
            }),
        ]),
        encoding="utf-8",
    )
    # Cursor can mirror a child at a top-level path. The exporter must prefer
    # the nested path because it carries the hierarchy identity.
    top_level_copy = (
        tool.root_path
        / "projects"
        / "c-Users-intpa-demo"
        / "agent-transcripts"
        / session_id
        / f"{session_id}.jsonl"
    )
    top_level_copy.parent.mkdir(parents=True)
    top_level_copy.write_text(nested_transcript.read_text(encoding="utf-8"))

    headers = [
        {"bubbleId": "subagent-user", "type": 1},
        {"bubbleId": "observed-thinking", "type": 2},
        {"bubbleId": "subagent-tool", "type": 2},
        {"bubbleId": "updated-assistant", "type": 2},
        {"bubbleId": "compatibility-fallback", "type": 2},
        {"bubbleId": "malformed-time", "type": 2},
    ]
    composer = {
        "name": "RC num_alloc USUSP",
        "status": "completed",
        "fullConversationHeadersOnly": headers,
        "modelConfig": {"modelName": "gpt-5.6-sol-xhigh"},
    }
    bubbles = {
        "subagent-user": {
            "bubbleId": "subagent-user",
            "type": 1,
            "createdAt": 1_785_378_873_625,
            "text": "Investigate",
        },
        "observed-thinking": {
            "bubbleId": "observed-thinking",
            "type": 2,
            "createdAt": "2026-07-30T02:34:42.569Z",
            "thinking": {"text": observed_thinking},
            "thinkingDurationMs": 933,
        },
        "subagent-tool": {
            "bubbleId": "subagent-tool",
            "type": 2,
            "createdAt": 1_785_378_883.5,
            "toolFormerData": {
                "name": "ripgrep_raw_search",
                "status": "completed",
                "params": {"pattern": "handoff"},
                "result": {"matches": 2},
                "toolCallId": "call-subagent-rg",
            },
        },
        "updated-assistant": {
            "bubbleId": "updated-assistant",
            "type": 2,
            "updatedAt": "2026-07-29T22:35:00-04:00",
            "text": "Native updated time fallback",
        },
        "compatibility-fallback": {
            "bubbleId": "compatibility-fallback",
            "type": 2,
            "text": "Compatibility timestamp fallback",
        },
        "malformed-time": {
            "bubbleId": "malformed-time",
            "type": 2,
            "createdAt": "not-a-time",
            "updatedAt": "NaN",
            "text": "No legitimate source time",
        },
    }
    connection = sqlite3.connect(tool.state_database_path)
    connection.execute(
        """
        UPDATE composerHeaders
        SET checkpointAt=?, value=?
        WHERE composerId=?
        """,
        (
            1_785_379_816_738,
            json.dumps({
                "name": "RC num_alloc USUSP",
                "subagentInfo": {
                    "parentComposerId": root_session_id,
                    "rootParentConversationId": root_session_id,
                    "subagentTypeName": "generalPurpose",
                },
            }),
            session_id,
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
    return nested_transcript, session_id


def test_live_state_supersedes_sparse_transcript_and_projects_whitelist(tmp_path):
    tool, transcript, session_id = _write_state_fixture(tmp_path)

    assert session_id in tool.authoritative_session_ids(max_age=0)
    assert "subagent-id" not in tool.authoritative_session_ids(max_age=0)
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


def test_workspace_folder_path_decodes_windows_hosted_wsl_uris() -> None:
    assert _workspace_folder_path(
        "file://wsl.localhost/Ubuntu/home/patrick/services/memento"
    ) == "/home/patrick/services/memento"
    assert _workspace_folder_path(
        "vscode-remote://wsl+Ubuntu/home/patrick/My%20Project"
    ) == "/home/patrick/My Project"
    assert _workspace_folder_path("file:///C:/Users/intpa/demo") == (
        "C:/Users/intpa/demo"
    )


def test_terminal_composer_ignores_unreferenced_checkpoint_bubbles(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["status"] = "completed"
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        (
            f"bubbleId:{session_id}:superseded-checkpoint-copy",
            json.dumps({
                "bubbleId": "superseded-checkpoint-copy",
                "type": 2,
                "createdAt": "2026-07-18T14:19:02Z",
                "text": "Stopping it now.",
            }),
        ),
    )
    connection.commit()
    connection.close()

    snapshot = CursorStateExporter(tool).export_changed(limit=20)[0]
    records = [json.loads(line) for line in snapshot.content.splitlines()]

    assert "superseded-checkpoint-copy" not in {
        record.get("id") for record in records
    }
    assert sum(
        record.get("message", {}).get("content") == "Stopping it now."
        for record in records
    ) == 1


def test_live_composer_keeps_unreferenced_inflight_bubble(tmp_path):
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
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        (
            f"bubbleId:{session_id}:inflight",
            json.dumps({
                "bubbleId": "inflight",
                "type": 2,
                "createdAt": "2026-07-18T14:19:05Z",
                "text": "Still working.",
            }),
        ),
    )
    connection.commit()
    connection.close()

    snapshot = CursorStateExporter(tool).export_changed(limit=20)[0]

    assert any(
        json.loads(line).get("id") == "inflight"
        for line in snapshot.content.splitlines()
    )


def test_subagent_state_supersedes_timestamp_free_compatibility_transcript(
    tmp_path,
):
    tool, _transcript, root_session_id = _write_state_fixture(tmp_path)
    nested_transcript, session_id = _add_subagent_state_fixture(
        tool,
        root_session_id,
    )

    assert session_id in tool.authoritative_session_ids(max_age=0)
    assert tool.classify_file(nested_transcript) is None

    snapshots = CursorStateExporter(tool).export_changed(limit=20)
    snapshot = next(
        item for item in snapshots
        if item.metadata["session_id"] == session_id
    )
    records = [json.loads(line) for line in snapshot.content.splitlines()]

    assert [record["type"] for record in records] == [
        "user",
        "cursor_state_thinking",
        "cursor_state_tool",
        "assistant",
        "assistant",
        "assistant",
    ]
    assert [record["timestamp"] for record in records] == [
        "2026-07-30T02:34:33.625Z",
        "2026-07-30T02:34:42.569Z",
        "2026-07-30T02:34:43.500Z",
        "2026-07-30T02:35:00Z",
        "2026-07-30T02:35:10.123Z",
        "",
    ]
    observed = records[1]
    assert observed["id"] == "observed-thinking:thinking"
    assert observed["thinking_duration_ms"] == 933
    assert "Investigating tool paths" in (
        observed["message"]["content"][0]["thinking"]
    )
    assert records[2]["tool_name"] == "Ripgrep"
    assert snapshot.relative_path.endswith(
        f"/{root_session_id}/subagents/{session_id}.jsonl"
    )
    assert snapshot.metadata["is_subagent"] is True
    assert snapshot.metadata["parent_thread_id"] == root_session_id
    assert snapshot.metadata["root_session_id"] == root_session_id


def test_cursor_timestamp_normalization_rejects_malformed_values() -> None:
    assert _iso_timestamp(1_785_378_883.5) == "2026-07-30T02:34:43.500Z"
    assert _iso_timestamp(1_785_378_883_500) == "2026-07-30T02:34:43.500Z"
    assert _iso_timestamp("2026-07-29T22:34:43.500-04:00") == (
        "2026-07-30T02:34:43.500Z"
    )
    assert _iso_timestamp("not-a-time") == ""
    assert _iso_timestamp(float("inf")) == ""
    assert _iso_timestamp(True) == ""


def test_exporter_emits_only_changed_revisions_and_resync_can_invalidate(tmp_path):
    tool, _transcript, _session_id = _write_state_fixture(tmp_path)
    exporter = CursorStateExporter(tool)

    assert len(exporter.export_changed()) == 1
    assert exporter.export_changed() == []

    exporter.invalidate()
    assert len(exporter.export_changed()) == 1


def test_unchanged_state_token_skips_sqlite_and_real_change_is_visible(
    tmp_path,
    monkeypatch,
):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    exporter = CursorStateExporter(tool)
    assert len(exporter.export_changed()) == 1
    assert exporter.needs_export() is False

    with monkeypatch.context() as context:
        context.setattr(
            "collector.cursor_state_export.sqlite3.connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("unchanged database was reopened")
            ),
        )
        assert exporter.export_changed() == []

    connection = sqlite3.connect(tool.state_database_path)
    connection.execute(
        "UPDATE composerHeaders SET lastUpdatedAt=? WHERE composerId=?",
        ("2026-07-18T14:22:00Z", session_id),
    )
    connection.commit()
    connection.close()

    assert exporter.needs_export() is True
    assert len(exporter.export_changed()) == 1


def test_authoritative_ownership_is_cached_per_session(tmp_path, monkeypatch):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)

    assert tool.has_authoritative_state(session_id) is True
    with monkeypatch.context() as context:
        context.setattr(
            "collector.tools.cursor.sqlite3.connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("known composer triggered another database scan")
            ),
        )
        assert tool.has_authoritative_state(session_id) is True


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


def test_cold_completed_composer_does_not_replay_historical_activity(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["status"] = "completed"
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.commit()
    connection.close()
    queue = SimpleNamespace(items=[], metadata_items=[])

    def enqueue(**kwargs):
        queue.items.append(kwargs)
        return 1

    queue.enqueue = enqueue
    queue.enqueue_metadata_changes = lambda **kwargs: (
        queue.metadata_items.append(kwargs) or 1
    )
    queue.get_delta_base = lambda _tool, _path: (None, 0)
    queued = enqueue_cursor_state_snapshots(CursorStateExporter(tool), queue)

    assert queued == 1
    assert queue.items[0]["sync_strategy"] == "full"
    assert queue.items[0]["tool_name"] == "cursor"
    assert queue.items[0]["source_path"].endswith("state.vscdb")
    assert queue.metadata_items == []


def _set_cursor_shell_state(
    tool: FixtureCursorTool,
    session_id: str,
    *,
    composer_status: str,
    tool_status: str | None,
    timestamp: datetime,
) -> None:
    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["status"] = composer_status
    tool_bubble = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"bubbleId:{session_id}:tool-1",),
    ).fetchone()[0])
    tool_bubble["createdAt"] = timestamp.isoformat()
    if tool_status is None:
        tool_bubble["toolFormerData"].pop("status", None)
    else:
        tool_bubble["toolFormerData"]["status"] = tool_status
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(tool_bubble), f"bubbleId:{session_id}:tool-1"),
    )
    connection.execute(
        """
        UPDATE composerHeaders
        SET lastUpdatedAt=?, checkpointAt=?
        WHERE composerId=?
        """,
        (timestamp.isoformat(), timestamp.isoformat(), session_id),
    )
    connection.commit()
    connection.close()


def _cursor_export_queue() -> SimpleNamespace:
    queue = SimpleNamespace(items=[], metadata_items=[])
    queue.enqueue = lambda **kwargs: queue.items.append(kwargs) or 1
    queue.enqueue_metadata_changes = lambda **kwargs: (
        queue.metadata_items.append(kwargs) or 1
    )
    queue.get_delta_base = lambda _tool, _path: (None, 0)
    return queue


def test_cold_live_composer_publishes_current_unresolved_shell(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _set_cursor_shell_state(
        tool,
        session_id,
        composer_status="generating",
        tool_status="running",
        timestamp=now,
    )
    queue = _cursor_export_queue()

    assert enqueue_cursor_state_snapshots(CursorStateExporter(tool), queue) == 1

    records = queue.metadata_items[0]["records"]
    activity = next(iter(records.values()))
    assert activity["activity_status"] == "running"
    assert activity["command"] == "Stop-Process"
    assert activity["session_id"] == session_id


def test_cold_cursor_shell_with_unknown_status_is_not_running(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    _set_cursor_shell_state(
        tool,
        session_id,
        composer_status="generating",
        tool_status=None,
        timestamp=datetime.now(timezone.utc).replace(microsecond=0),
    )
    queue = _cursor_export_queue()

    assert enqueue_cursor_state_snapshots(CursorStateExporter(tool), queue) == 1
    assert queue.metadata_items == []


def test_cold_recent_terminal_composer_closes_native_running_shell(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    _set_cursor_shell_state(
        tool,
        session_id,
        composer_status="completed",
        tool_status="running",
        timestamp=datetime.now(timezone.utc).replace(microsecond=0),
    )
    queue = _cursor_export_queue()

    assert enqueue_cursor_state_snapshots(CursorStateExporter(tool), queue) == 1

    activity = next(iter(queue.metadata_items[0]["records"].values()))
    assert activity["activity_status"] == "cancelled"


def test_cursor_running_to_terminal_transition_is_published(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    _set_cursor_shell_state(
        tool,
        session_id,
        composer_status="generating",
        tool_status="running",
        timestamp=started_at,
    )
    exporter = CursorStateExporter(tool)
    queue = _cursor_export_queue()
    assert enqueue_cursor_state_snapshots(exporter, queue) == 1

    _set_cursor_shell_state(
        tool,
        session_id,
        composer_status="completed",
        tool_status="completed",
        timestamp=started_at + timedelta(seconds=30),
    )
    queue.metadata_items.clear()

    assert enqueue_cursor_state_snapshots(exporter, queue) == 1
    activity = next(iter(queue.metadata_items[0]["records"].values()))
    assert activity["activity_status"] == "completed"


def test_state_snapshot_uses_native_time_not_projection_observation(tmp_path):
    tool, _transcript, session_id = _write_state_fixture(tmp_path)
    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["fullConversationHeadersOnly"] = [{
        "bubbleId": "tool-1",
        "type": 2,
    }]
    composer["todos"] = []
    composer["status"] = "completed"
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.commit()
    connection.close()

    snapshot = CursorStateExporter(tool).export_changed(limit=20)[0]

    assert {
        json.loads(line)["role"]
        for line in snapshot.content.splitlines()
    } == {"tool"}
    assert snapshot.source_modified_at == datetime(
        2026,
        7,
        18,
        14,
        20,
        tzinfo=timezone.utc,
    ).timestamp()


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


def test_state_export_captures_task_notification_without_compat_transcript(tmp_path):
    tool, transcript, session_id = _write_state_fixture(tmp_path)
    transcript.unlink()
    notification_id = "native-task-notification"
    notification = (
        "<timestamp>Thursday, Jul 30, 2026, 8:59 AM (UTC-4)</timestamp>\n"
        "<system_notification>\n"
        "The following task has finished. If you were already aware, "
        "ignore this notification and do not restate prior responses.\n\n"
        "<task>\n"
        "kind: shell\n"
        "status: success\n"
        "task_id: 913821\n"
        "title: Start batch 1 pull tlv02+rno\n"
        "output_path: C:\\Users\\intpa\\.cursor\\projects\\demo\\"
        "terminals\\913821.txt\n"
        "</task>\n"
        "</system_notification>\n"
        "<user_query>Briefly inform the user about the task result and "
        "perform any follow-up actions (if needed).</user_query>"
    )

    connection = sqlite3.connect(tool.state_database_path)
    composer = json.loads(connection.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"composerData:{session_id}",),
    ).fetchone()[0])
    composer["fullConversationHeadersOnly"].append({
        "bubbleId": notification_id,
        "type": 1,
    })
    connection.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key=?",
        (json.dumps(composer), f"composerData:{session_id}"),
    )
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)",
        (
            f"bubbleId:{session_id}:{notification_id}",
            json.dumps({
                "bubbleId": notification_id,
                "type": 1,
                "createdAt": "2026-07-30T12:59:09.690Z",
                "text": notification,
            }),
        ),
    )
    connection.execute(
        "UPDATE composerHeaders SET lastUpdatedAt=? WHERE composerId=?",
        ("2026-07-30T12:59:10Z", session_id),
    )
    connection.commit()
    connection.close()

    exporter = CursorStateExporter(tool)
    snapshots = exporter.export_changed(limit=20)
    records = [
        json.loads(line)
        for snapshot in snapshots
        for line in snapshot.content.splitlines()
    ]
    projected = next(record for record in records if record["id"] == notification_id)

    assert projected["type"] == "user"
    assert projected["role"] == "user"
    assert projected["timestamp"] == "2026-07-30T12:59:09.690Z"
    assert projected["message"]["content"] == notification
    assert exporter.export_changed(limit=20) == []
