from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from server.services.conversation_usage_cycle import aggregate_usage_cycle


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


class _Db:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, _statement):
        return _Result(self.results.pop(0))


@pytest.mark.asyncio
async def test_cycle_reports_exact_models_and_explicit_cursor_gap() -> None:
    codex_id = uuid.uuid4()
    cursor_id = uuid.uuid4()
    db = _Db(
        [
            [
                SimpleNamespace(id=codex_id, tool_id="codex"),
                SimpleNamespace(id=cursor_id, tool_id="cursor"),
            ],
            [
                SimpleNamespace(
                    document_id=codex_id,
                    attribution_status="attributed",
                )
            ],
            [],
            [
                SimpleNamespace(
                    model="gpt-5.6-sol",
                    reasoning_effort="xhigh",
                    conversation_count=1,
                    input_tokens=100,
                    uncached_input_tokens=60,
                    cached_input_tokens=40,
                    cache_write_input_tokens=0,
                    output_tokens=20,
                    reasoning_output_tokens=5,
                    total_tokens=120,
                )
            ],
        ]
    )

    payload = await aggregate_usage_cycle(
        db,
        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
        until=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert payload["conversation_count"] == 2
    assert payload["attributed_conversation_count"] == 1
    assert payload["unattributed"] == {
        "conversation_count": 1,
        "cursor_null": 1,
        "missing_timestamps": 0,
        "missing_model": 0,
        "counter_reset": 0,
        "missing_usage": 0,
    }
    assert payload["models"] == [
        {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "conversation_count": 1,
            "token_usage": {
                "input_tokens": 100,
                "uncached_input_tokens": 60,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120,
                "input_uncached": 60,
                "cache_read_tokens": 40,
            },
        }
    ]


@pytest.mark.asyncio
async def test_cycle_counts_unattributed_event_without_message_activity() -> None:
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
    )

    assert payload["conversation_count"] == 1
    assert payload["attributed_conversation_count"] == 0
    assert payload["unattributed"]["conversation_count"] == 1
    assert payload["unattributed"]["missing_model"] == 1
