from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mcp_server.usage import aggregate_usage_cycle, direct_conversation_details


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return _Scalars(self.rows)

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, _statement):
        return _Result(self.results.pop(0))


@pytest.mark.asyncio
async def test_direct_cycle_counts_unattributed_event_without_message_activity() -> (
    None
):
    document_id = uuid.uuid4()
    db = _Db(
        [
            [],
            [
                SimpleNamespace(
                    document_id=document_id,
                    attribution_status="missing_model",
                )
            ],
            [],
            [],
        ]
    )

    payload = await aggregate_usage_cycle(
        db,
        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
        until=datetime(2026, 9, 1, tzinfo=timezone.utc),
        tool="all",
        include_threads=False,
    )

    assert payload["conversation_count"] == 1
    assert payload["unattributed"]["missing_model"] == 1


@pytest.mark.asyncio
async def test_direct_details_prefers_native_last_activity_metadata() -> None:
    imported_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
    native_last = "2026-08-20T12:34:56+00:00"
    db = _Db(
        [
            [
                SimpleNamespace(
                    activity_at=imported_at,
                    delivery_metadata={
                        "_assistant_last_activity_at": native_last,
                    },
                )
            ],
            [],
        ]
    )

    payload = await direct_conversation_details(
        db,
        uuid.uuid4(),
        metadata={},
        fallback_last=imported_at,
    )

    assert payload["last_activity_at"] == native_last


@pytest.mark.asyncio
async def test_direct_cycle_threads_include_hierarchy_metadata() -> None:
    document_id = uuid.uuid4()
    activity = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    db = _Db(
        [
            [SimpleNamespace(id=document_id, tool_id="codex")],
            [
                SimpleNamespace(
                    document_id=document_id,
                    attribution_status="attributed",
                )
            ],
            [],
            [],
            [
                SimpleNamespace(
                    id=document_id,
                    tool_id="codex",
                    title="Review current draft",
                    relative_path="sessions/native-child.jsonl",
                    metadata={
                        "session_id": "native-child",
                        "root_session_id": "native-root",
                        "parent_thread_id": "native-root",
                        "thread_source": "subagent",
                    },
                    activity_at=activity,
                    delivery_activity_at=activity,
                )
            ],
            [],
        ]
    )

    payload = await aggregate_usage_cycle(
        db,
        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
        until=datetime(2026, 9, 1, tzinfo=timezone.utc),
        tool="all",
        include_threads=True,
    )

    thread = payload["threads"][0]
    assert thread["is_subagent"] is True
    assert thread["thread_source"] == "subagent"
    assert thread["parent_thread_id"] == "native-root"
    assert thread["orchestration"] is None
    assert thread["orchestration_parent_document_id"] is None
