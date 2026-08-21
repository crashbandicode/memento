import json
from types import SimpleNamespace

import pytest

from mcp_server import server


@pytest.mark.asyncio
async def test_conversation_info_returns_structured_runtime(monkeypatch) -> None:
    remote = SimpleNamespace(
        get_conversation=lambda _doc_id: None,
    )

    async def get_conversation(_doc_id: str) -> dict:
        return {
            "id": "document-id",
            "native_id": "native-id",
            "tool_id": "codex",
            "title": "Token thread",
            "model": "gpt-5.6-sol",
            "model_family": "openai",
            "reasoning_effort": "xhigh",
            "service_tier": "priority",
            "token_usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
            "models": [
                {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                    "token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                    },
                }
            ],
            "started_at": "2026-08-01T12:00:00+00:00",
            "last_activity_at": "2026-08-21T12:00:00+00:00",
        }

    remote.get_conversation = get_conversation
    monkeypatch.setattr(server, "_remote", remote)

    payload = json.loads(await server.memory_conversation_info("document-id"))

    assert payload == {
        "schema_version": 1,
        "document_id": "document-id",
        "native_id": "native-id",
        "tool": "codex",
        "title": "Token thread",
        "model": "gpt-5.6-sol",
        "model_family": "openai",
        "reasoning_effort": "xhigh",
        "service_tier": "priority",
        "token_usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
        "models": [
            {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "token_usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            }
        ],
        "started_at": "2026-08-01T12:00:00+00:00",
        "last_activity_at": "2026-08-21T12:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_usage_cycle_forwards_normalized_boundaries(monkeypatch) -> None:
    calls = []

    async def get_usage_cycle(**kwargs) -> dict:
        calls.append(kwargs)
        return {
            "schema_version": 1,
            "conversation_count": 2,
            "models": [],
        }

    monkeypatch.setattr(
        server,
        "_remote",
        SimpleNamespace(get_usage_cycle=get_usage_cycle),
    )

    payload = json.loads(
        await server.memory_usage_cycle(
            "2026-08-01T00:00:00Z",
            "2026-09-01T00:00:00-04:00",
            tool="claude",
            include_threads=True,
        )
    )

    assert payload["conversation_count"] == 2
    assert calls == [
        {
            "since": "2026-08-01T00:00:00+00:00",
            "until": "2026-09-01T04:00:00+00:00",
            "tool": "claude",
            "include_threads": True,
        }
    ]


@pytest.mark.asyncio
async def test_usage_cycle_rejects_ambiguous_or_reversed_ranges(monkeypatch) -> None:
    monkeypatch.setattr(server, "_remote", SimpleNamespace())

    ambiguous = json.loads(
        await server.memory_usage_cycle(
            "2026-08-01T00:00:00",
            "2026-09-01T00:00:00Z",
        )
    )
    reversed_range = json.loads(
        await server.memory_usage_cycle(
            "2026-09-01T00:00:00Z",
            "2026-08-01T00:00:00Z",
        )
    )

    assert ambiguous["error"] == "since must include a timezone"
    assert reversed_range["error"] == "since must be before until"
