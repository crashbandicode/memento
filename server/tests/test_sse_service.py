from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.session import RealtimeAsyncSession, queue_realtime_event
from server.services import sse_service
from server.services.ingest_service import _conversation_event_changes


class _FakeRedisStream:
    def __init__(
        self,
        *,
        entries: list[tuple[str, dict[str, str]]],
        reads: list[list[tuple[str, list[tuple[str, dict[str, str]]]]]],
    ) -> None:
        self.entries = entries
        self.reads = list(reads)
        self.read_cursors: list[dict[str, str]] = []
        self.closed = False

    async def xrange(self, *_args, **_kwargs):
        return self.entries[:1]

    async def xrevrange(self, *_args, **_kwargs):
        return self.entries[-1:] if self.entries else []

    async def xread(self, streams, **_kwargs):
        self.read_cursors.append(dict(streams))
        if not self.reads:
            raise RuntimeError("test stream exhausted")
        return self.reads.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def _entry(event_id: str, title: str = "updated"):
    payload = json.dumps({
        "type": "file_synced",
        "data": {"document_id": "doc", "title": title},
        "timestamp": 1.0,
    })
    return event_id, {"event": payload}


@pytest.mark.asyncio
async def test_realtime_event_is_published_only_after_commit() -> None:
    session = RealtimeAsyncSession()
    order: list[str] = []
    queue_realtime_event(
        session,
        "file_synced",
        {"document_id": "doc"},
        user_id="user",
    )

    async def committed() -> None:
        order.append("commit")

    async def published(*_args, **_kwargs) -> None:
        order.append("publish")

    with (
        patch.object(AsyncSession, "commit", new=AsyncMock(side_effect=committed)),
        patch(
            "server.services.sse_service.publish_event",
            new=AsyncMock(side_effect=published),
        ),
    ):
        await session.commit()

    assert order == ["commit", "publish"]


@pytest.mark.asyncio
async def test_rollback_discards_queued_realtime_event() -> None:
    session = RealtimeAsyncSession()
    queue_realtime_event(
        session,
        "file_synced",
        {"document_id": "doc"},
        user_id="user",
    )

    with (
        patch.object(AsyncSession, "rollback", new=AsyncMock()),
        patch.object(AsyncSession, "commit", new=AsyncMock()),
        patch(
            "server.services.sse_service.publish_event",
            new=AsyncMock(),
        ) as publish,
    ):
        await session.rollback()
        await session.commit()

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_subscription_starts_at_tail_without_stale_replay() -> None:
    fake = _FakeRedisStream(
        entries=[_entry("100-0", "stale")],
        reads=[[("stream", [_entry("101-0", "live")])]],
    )
    with patch.object(sse_service.aioredis, "from_url", return_value=fake):
        subscription = sse_service._subscribe_redis("user", None)
        ready = await anext(subscription)
        live = await anext(subscription)
        await subscription.aclose()

    assert ready["type"] == "stream_ready"
    assert ready["id"] == "100-0"
    assert live["id"] == "101-0"
    assert live["data"]["title"] == "live"
    assert fake.read_cursors == [
        {sse_service._stream_key("user"): "100-0"},
    ]


@pytest.mark.asyncio
async def test_valid_cursor_replays_only_later_events() -> None:
    fake = _FakeRedisStream(
        entries=[_entry("100-0"), _entry("300-0")],
        reads=[[("stream", [_entry("200-0", "missed")])]],
    )
    with patch.object(sse_service.aioredis, "from_url", return_value=fake):
        subscription = sse_service._subscribe_redis("user", "150-0")
        replayed = await anext(subscription)
        await subscription.aclose()

    assert replayed["id"] == "200-0"
    assert replayed["data"]["title"] == "missed"
    assert fake.read_cursors == [
        {sse_service._stream_key("user"): "150-0"},
    ]


@pytest.mark.asyncio
async def test_trimmed_cursor_resets_once_at_current_tail() -> None:
    fake = _FakeRedisStream(
        entries=[_entry("200-0"), _entry("300-0")],
        reads=[],
    )
    with patch.object(sse_service.aioredis, "from_url", return_value=fake):
        subscription = sse_service._subscribe_redis("user", "100-0")
        reset = await anext(subscription)
        await subscription.aclose()

    assert reset == {
        "id": "300-0",
        "type": "realtime_reset",
        "data": {"reason": "replay_trimmed"},
        "timestamp": reset["timestamp"],
    }


@pytest.mark.asyncio
async def test_publish_uses_redis_stream_id_and_local_fallback_bus() -> None:
    with (
        patch.object(
            sse_service,
            "_append_redis_event",
            return_value="500-2",
        ) as append,
        patch.object(sse_service, "_dispatch_local") as dispatch,
    ):
        event = await sse_service.publish_event(
            "file_synced",
            {"document_id": "doc"},
            user_id="user",
        )

    assert event["id"] == "500-2"
    append.assert_called_once()
    dispatch.assert_called_once_with(event, "user")


def test_sse_frames_include_resumable_event_ids() -> None:
    frame = sse_service.format_sse({
        "id": "123-4",
        "type": "file_synced",
        "data": {"document_id": "doc"},
        "timestamp": 1.0,
    })

    assert frame.startswith("id: 123-4\nevent: file_synced\n")
    assert frame.endswith("\n\n")


def test_tool_only_delta_does_not_refresh_prompt_search_or_inbox() -> None:
    assert _conversation_event_changes(
        mode="delta",
        search_text="",
        title_changed=False,
        interactions_changed=False,
        dashboard_changed=False,
    ) == [
        "conversation.messages",
        "conversation.metadata",
        "project",
    ]


def test_user_interaction_delta_scopes_all_affected_read_models() -> None:
    assert _conversation_event_changes(
        mode="delta",
        search_text="[user] choose one\n",
        title_changed=False,
        interactions_changed=True,
        dashboard_changed=True,
    ) == [
        "conversation.messages",
        "conversation.metadata",
        "conversation.pending_interactions",
        "conversation.prompts",
        "conversation.search",
        "dashboard",
        "project",
    ]
