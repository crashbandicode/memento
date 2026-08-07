"""Resumable, cross-worker Server-Sent Events backed by Redis Streams.

Redis is the shared delivery and bounded replay layer. A process-local bus is
kept as a best-effort fallback so a single-worker development deployment still
receives live updates while Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any

import redis
import redis.asyncio as aioredis

from ..config import settings

logger = logging.getLogger("memento.realtime")

_STREAM_PREFIX = "memento:realtime:v1"
_STREAM_MAX_EVENTS = 1_000
_STREAM_TTL_SECONDS = 24 * 60 * 60
_STREAM_BLOCK_MS = 25_000
_LOCAL_MAX_EVENTS = 200
_EVENT_ID_RE = re.compile(r"^\d+-\d+$")

_lock = threading.Lock()
_subscribers: list[
    tuple[str | None, asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]
] = []
_recent_events: deque[tuple[str | None, dict[str, Any]]] = deque(
    maxlen=_LOCAL_MAX_EVENTS
)
_last_local_millisecond = 0
_last_local_sequence = 0
_redis_client: redis.Redis | None = None
_redis_client_pid: int | None = None


def _stream_key(user_id: str) -> str:
    return f"{_STREAM_PREFIX}:{user_id}"


def _normalized_event_id(value: str | None) -> str | None:
    candidate = str(value or "").strip()
    return candidate if _EVENT_ID_RE.fullmatch(candidate) else None


def _event_id_tuple(value: str) -> tuple[int, int]:
    milliseconds, sequence = value.split("-", 1)
    return int(milliseconds), int(sequence)


def _next_local_event_id() -> str:
    global _last_local_millisecond, _last_local_sequence
    with _lock:
        current = int(time.time() * 1_000)
        if current <= _last_local_millisecond:
            current = _last_local_millisecond
            _last_local_sequence += 1
        else:
            _last_local_millisecond = current
            _last_local_sequence = 0
        return f"{current}-{_last_local_sequence}"


def _get_redis_publisher() -> redis.Redis:
    """Return a process-safe synchronous client for event publication.

    Publication runs through ``asyncio.to_thread`` so a Redis outage cannot
    block the API event loop. The sync client also remains safe in Celery tasks
    that create a fresh event loop for every ``asyncio.run`` call.
    """
    global _redis_client, _redis_client_pid
    process_id = os.getpid()
    if _redis_client is None or _redis_client_pid != process_id:
        if _redis_client is not None:
            try:
                _redis_client.close()
            except Exception:
                pass
        _redis_client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        _redis_client_pid = process_id
    return _redis_client


def _append_redis_event(
    event_type: str,
    data: dict[str, Any],
    user_id: str,
    timestamp: float,
) -> str:
    payload = json.dumps(
        {"type": event_type, "data": data, "timestamp": timestamp},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    pipeline = _get_redis_publisher().pipeline(transaction=False)
    pipeline.xadd(
        _stream_key(user_id),
        {"event": payload},
        maxlen=_STREAM_MAX_EVENTS,
        approximate=True,
    )
    pipeline.expire(_stream_key(user_id), _STREAM_TTL_SECONDS)
    result = pipeline.execute()[0]
    return result.decode() if isinstance(result, bytes) else str(result)


def _offer_local(
    queue: asyncio.Queue[dict[str, Any]],
    event: dict[str, Any],
) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # A bounded queue must not retain a silently incomplete sequence.
        # Replace it with one reset marker so the client reconciles once.
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        queue.put_nowait(
            {
                "type": "realtime_reset",
                "data": {"reason": "subscriber_overflow"},
                "timestamp": time.time(),
            }
        )


def _dispatch_local(
    event: dict[str, Any],
    user_id: str | None,
) -> None:
    with _lock:
        _recent_events.append((user_id, event))
        subscribers = list(_subscribers)
    for subscriber_user_id, loop, queue in subscribers:
        if user_id is not None and subscriber_user_id != user_id:
            continue
        try:
            loop.call_soon_threadsafe(_offer_local, queue, event)
        except RuntimeError:
            # The subscriber's event loop closed between snapshot and dispatch.
            continue


async def publish_event(
    event_type: str,
    data: dict[str, Any],
    user_id: str | None = None,
) -> dict[str, Any]:
    """Publish one event to Redis after commit, with a local fallback."""
    timestamp = time.time()
    event_id: str | None = None
    if user_id is not None:
        try:
            event_id = await asyncio.to_thread(
                _append_redis_event,
                event_type,
                data,
                user_id,
                timestamp,
            )
        except Exception as exc:
            logger.debug("Redis realtime publish unavailable: %s", exc)
    event = {
        "id": event_id or _next_local_event_id(),
        "type": event_type,
        "data": data,
        "timestamp": timestamp,
    }
    _dispatch_local(event, user_id)
    return event


def _decode_stream_event(
    event_id: str | bytes,
    fields: dict[str | bytes, str | bytes],
) -> dict[str, Any] | None:
    raw = fields.get("event") or fields.get(b"event")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        return None
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None
    normalized_id = event_id.decode() if isinstance(event_id, bytes) else str(event_id)
    event["id"] = normalized_id
    return event


async def _subscribe_redis(
    user_id: str,
    last_event_id: str | None,
) -> AsyncGenerator[dict[str, Any], None]:
    client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=30,
        health_check_interval=30,
    )
    key = _stream_key(user_id)
    cursor = _normalized_event_id(last_event_id)
    try:
        oldest_rows = await client.xrange(key, min="-", max="+", count=1)
        latest_rows = await client.xrevrange(key, max="+", min="-", count=1)
        latest_id = str(latest_rows[0][0]) if latest_rows else "0-0"

        if cursor is None:
            # Establish a watermark without replaying unrelated stale events.
            cursor = latest_id
            yield {
                "id": cursor,
                "type": "stream_ready",
                "data": {},
                "timestamp": time.time(),
            }
        elif not oldest_rows:
            # The bounded stream expired while this client was disconnected.
            cursor = "0-0"
            yield {
                "type": "realtime_reset",
                "data": {"reason": "replay_expired"},
                "timestamp": time.time(),
            }
        else:
            oldest_id = str(oldest_rows[0][0])
            if _event_id_tuple(cursor) < _event_id_tuple(oldest_id):
                # Partial replay is misleading. Reconcile once, then continue
                # strictly after the current stream tail.
                cursor = latest_id
                yield {
                    "id": cursor,
                    "type": "realtime_reset",
                    "data": {"reason": "replay_trimmed"},
                    "timestamp": time.time(),
                }

        while True:
            rows = await client.xread(
                {key: cursor},
                count=100,
                block=_STREAM_BLOCK_MS,
            )
            if not rows:
                yield {"type": "keepalive", "data": {}, "timestamp": time.time()}
                continue
            for _stream, entries in rows:
                for event_id, fields in entries:
                    cursor = str(event_id)
                    event = _decode_stream_event(event_id, fields)
                    if event is not None:
                        yield event
    finally:
        await client.aclose()


async def _subscribe_local(
    user_id: str,
    last_event_id: str | None,
) -> AsyncGenerator[dict[str, Any], None]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    loop = asyncio.get_running_loop()
    cursor = _normalized_event_id(last_event_id)
    with _lock:
        eligible = [
            event
            for event_user_id, event in _recent_events
            if event_user_id is None or event_user_id == user_id
        ]
        _subscribers.append((user_id, loop, queue))

    try:
        if cursor is None:
            latest_id = str(eligible[-1]["id"]) if eligible else "0-0"
            yield {
                "id": latest_id,
                "type": "stream_ready",
                "data": {},
                "timestamp": time.time(),
            }
        elif eligible and _event_id_tuple(cursor) < _event_id_tuple(
            str(eligible[0]["id"])
        ):
            yield {
                "id": str(eligible[-1]["id"]),
                "type": "realtime_reset",
                "data": {"reason": "local_replay_trimmed"},
                "timestamp": time.time(),
            }
        else:
            for event in eligible:
                if _event_id_tuple(str(event["id"])) > _event_id_tuple(cursor):
                    yield event

        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield {"type": "keepalive", "data": {}, "timestamp": time.time()}
    finally:
        with _lock:
            _subscribers[:] = [
                (uid, subscriber_loop, subscriber_queue)
                for uid, subscriber_loop, subscriber_queue in _subscribers
                if subscriber_queue is not queue
            ]


async def subscribe(
    user_id: str,
    last_event_id: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield user-scoped events, resuming only after an explicit event ID."""
    try:
        async for event in _subscribe_redis(user_id, last_event_id):
            yield event
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Redis realtime subscribe unavailable: %s", exc)
        async for event in _subscribe_local(user_id, last_event_id):
            yield event


def format_sse(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
    event_id = _normalized_event_id(str(event.get("id") or ""))
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event['type']}\ndata: {data}\n\n"
