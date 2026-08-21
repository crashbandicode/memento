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
    }
