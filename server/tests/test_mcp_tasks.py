from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp_server.remote_client import RemoteClient
from server.api.mcp_mount import HTTP_MCP_EXCLUDED_TOOLS

from mcp_server import server as mcp_server


@pytest.mark.asyncio
async def test_remote_client_forwards_every_task_selector(monkeypatch) -> None:
    client = RemoteClient("https://memento.example", "token")
    request = AsyncMock(return_value={"schema_version": 1, "root_threads": []})
    monkeypatch.setattr(client, "_get", request)

    result = await client.get_tasks(
        document_id="doc",
        thread_id="thread",
        agent_id="agent",
        subagent_id="subagent",
        tool="cursor",
        status="completed",
        include_history=True,
        cursor="cursor-token",
        limit=4,
        max_tasks=20,
        history_limit=3,
    )

    assert result["schema_version"] == 1
    request.assert_awaited_once_with(
        "/api/tasks",
        {
            "status": "completed",
            "include_history": True,
            "limit": 4,
            "max_tasks": 20,
            "history_limit": 3,
            "document_id": "doc",
            "thread_id": "thread",
            "agent_id": "agent",
            "subagent_id": "subagent",
            "tool": "cursor",
            "cursor": "cursor-token",
        },
    )


@pytest.mark.asyncio
async def test_remote_client_maps_only_missing_route_404_to_unsupported(
    monkeypatch,
) -> None:
    client = RemoteClient("https://memento.example", "token")
    request = httpx.Request("GET", "https://memento.example/api/tasks")
    response = httpx.Response(
        404,
        json={"detail": "Not Found"},
        request=request,
    )
    monkeypatch.setattr(
        client,
        "_get",
        AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "missing",
                request=request,
                response=response,
            )
        ),
    )

    result = await client.get_tasks()

    assert result["error"]["code"] == "unsupported_server"


@pytest.mark.asyncio
async def test_remote_client_preserves_document_404(monkeypatch) -> None:
    client = RemoteClient("https://memento.example", "token")
    request = httpx.Request("GET", "https://memento.example/api/tasks")
    response = httpx.Response(
        404,
        json={"detail": "Document not found"},
        request=request,
    )
    error = httpx.HTTPStatusError(
        "missing document",
        request=request,
        response=response,
    )
    monkeypatch.setattr(client, "_get", AsyncMock(side_effect=error))

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_tasks(document_id="missing")


@pytest.mark.asyncio
async def test_memory_tasks_returns_schema_versioned_json(monkeypatch) -> None:
    remote = SimpleRemote()
    monkeypatch.setattr(mcp_server, "_remote", remote)

    raw = await mcp_server.memory_tasks(
        thread_id="thread",
        status="outstanding",
        max_tasks=5,
    )
    result = json.loads(raw)

    assert result == {"schema_version": 1, "root_threads": []}
    assert remote.kwargs["thread_id"] == "thread"
    assert remote.kwargs["max_tasks"] == 5


@pytest.mark.asyncio
async def test_memory_tasks_direct_mode_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_remote", None)
    monkeypatch.setattr(mcp_server, "_session_factory", object())

    result = json.loads(await mcp_server.memory_tasks())

    assert result["schema_version"] == 1
    assert result["error"]["code"] == "direct_mode_user_scope_required"


def test_memory_tasks_is_registered_only_on_standalone_server() -> None:
    tools = mcp_server.mcp._tool_manager._tools

    assert "memory_tasks" in tools
    assert HTTP_MCP_EXCLUDED_TOOLS == {"memory_tasks"}


class SimpleRemote:
    def __init__(self) -> None:
        self.kwargs = {}

    async def get_tasks(self, **kwargs):
        self.kwargs = kwargs
        return {"schema_version": 1, "root_threads": []}
