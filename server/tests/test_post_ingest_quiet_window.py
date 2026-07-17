from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from celery.exceptions import Retry
from fastapi import UploadFile

from server.api import ingest as ingest_api
from server.services.ingest_service import _logical_document_file_size
from server.tasks import post_ingest


def _state(
    *,
    content_hash: str = "current-revision",
    category: str = "conversation",
    file_size_bytes: int | None = None,
    synced_at: datetime | None = None,
    embedding_status: str = "pending",
    knowledge_status: str = "pending",
) -> post_ingest._DocumentState:
    return post_ingest._DocumentState(
        tool_id="codex",
        category=category,
        content_hash=content_hash,
        file_size_bytes=file_size_bytes
        if file_size_bytes is not None
        else post_ingest.CONVERSATION_QUIET_WINDOW_MIN_BYTES,
        synced_at=synced_at or datetime.now(timezone.utc),
        embedding_status=embedding_status,
        knowledge_status=knowledge_status,
    )


def test_configured_conversation_dispatch_uses_bounded_quiet_window() -> None:
    assert 120 <= post_ingest.POST_INGEST_QUIET_SECONDS <= 300
    assert (
        post_ingest.initial_post_ingest_countdown(
            "conversation", post_ingest.CONVERSATION_QUIET_WINDOW_MIN_BYTES
        )
        == post_ingest.POST_INGEST_QUIET_SECONDS
    )
    assert post_ingest.initial_post_ingest_countdown("memory", 100_000_000) is None
    assert (
        post_ingest.initial_post_ingest_countdown(
            "conversation", post_ingest.CONVERSATION_QUIET_WINDOW_MIN_BYTES - 1
        )
        is None
    )


def test_zero_threshold_delays_small_conversations(monkeypatch) -> None:
    monkeypatch.setattr(
        post_ingest,
        "CONVERSATION_QUIET_WINDOW_MIN_BYTES",
        0,
    )

    assert (
        post_ingest.initial_post_ingest_countdown("conversation", 512)
        == post_ingest.POST_INGEST_QUIET_SECONDS
    )
    assert post_ingest.initial_post_ingest_countdown("config", 512) is None


def test_delta_uses_cumulative_source_size_for_quiet_classification() -> None:
    logical_size = _logical_document_file_size(
        mode="delta",
        payload_size=20_000,
        offset=160_000_000,
        existing_size=159_980_000,
    )

    assert logical_size == 160_000_000
    assert (
        post_ingest.initial_post_ingest_countdown("conversation", logical_size)
        == post_ingest.POST_INGEST_QUIET_SECONDS
    )


@pytest.mark.asyncio
async def test_multipart_upload_uses_measured_size_when_reported_size_is_zero(
    monkeypatch,
) -> None:
    payload = b"x" * (post_ingest.CONVERSATION_QUIET_WINDOW_MIN_BYTES + 1)
    captured: dict = {}
    document_id = uuid4()

    async def _ensure_device(*_args, **_kwargs):
        return SimpleNamespace(id=uuid4())

    async def _ingest_file(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=document_id)

    monkeypatch.setattr(ingest_api, "ensure_device", _ensure_device)
    monkeypatch.setattr(ingest_api, "ingest_file", _ingest_file)
    upload = UploadFile(file=io.BytesIO(payload), filename="conversation.jsonl")
    metadata = json.dumps(
        {
            "tool": "codex",
            "category": "conversation",
            "content_type": "jsonl",
            "relative_path": "sessions/conversation.jsonl",
            "hash": "a" * 64,
            "file_size": 0,
            "mode": "full",
            "offset": 0,
        }
    )

    response = await ingest_api.ingest_file_upload(
        metadata=metadata,
        content=upload,
        _collector_user=SimpleNamespace(id=uuid4()),
        _throttle=None,
        db=SimpleNamespace(),
        x_device_id="test-device",
        x_device_name="test",
        x_device_platform="windows",
    )

    assert response.document_id == str(document_id)
    assert captured["file_size"] == len(payload)
    assert (
        post_ingest.initial_post_ingest_countdown(
            captured["category"], captured["file_size"]
        )
        == post_ingest.POST_INGEST_QUIET_SECONDS
    )


def test_quiet_window_counts_from_latest_document_update(monkeypatch) -> None:
    monkeypatch.setattr(post_ingest, "POST_INGEST_QUIET_SECONDS", 180)
    now = datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc)

    assert (
        post_ingest._quiet_seconds_remaining(
            _state(synced_at=now - timedelta(seconds=61)), now=now
        )
        == 119
    )
    assert (
        post_ingest._quiet_seconds_remaining(
            _state(synced_at=now - timedelta(seconds=180)), now=now
        )
        == 0
    )


def _unlocked():
    @asynccontextmanager
    async def _lock(_document_id: UUID):
        yield True

    return _lock


@pytest.mark.asyncio
async def test_superseded_task_never_reaches_post_ingest(monkeypatch) -> None:
    document_id = uuid4()

    async def _load(_document_id: UUID):
        return _state(content_hash="new-revision")

    async def _unexpected(*_args):
        pytest.fail("superseded revision reached post-ingest")

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _unlocked())
    monkeypatch.setattr(post_ingest, "_load_document_state", _load)
    monkeypatch.setattr(post_ingest, "_run_post_ingest_inner", _unexpected)

    result = await post_ingest._process_document_post_ingest(
        document_id, "old-revision"
    )

    assert result["status"] == "superseded"
    assert result["current_revision"] == "new-revision"


@pytest.mark.asyncio
async def test_growing_large_conversation_defers_without_processing(
    monkeypatch,
) -> None:
    document_id = uuid4()

    async def _load(_document_id: UUID):
        return _state(synced_at=datetime.now(timezone.utc))

    async def _unexpected(*_args):
        pytest.fail("non-quiet conversation reached post-ingest")

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _unlocked())
    monkeypatch.setattr(post_ingest, "_load_document_state", _load)
    monkeypatch.setattr(post_ingest, "_run_post_ingest_inner", _unexpected)

    result = await post_ingest._process_document_post_ingest(
        document_id, "current-revision"
    )

    assert result["status"] == "deferred"
    assert 1 <= result["countdown"] <= post_ingest.POST_INGEST_QUIET_SECONDS


@pytest.mark.asyncio
async def test_completed_status_does_not_bypass_current_revision_quiet_window(
    monkeypatch,
) -> None:
    document_id = uuid4()

    async def _load(_document_id: UUID):
        return _state(
            synced_at=datetime.now(timezone.utc),
            embedding_status="ok",
            knowledge_status="ok",
        )

    async def _unexpected(*_args):
        pytest.fail("completed duplicate reached post-ingest")

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _unlocked())
    monkeypatch.setattr(post_ingest, "_load_document_state", _load)
    monkeypatch.setattr(post_ingest, "_run_post_ingest_inner", _unexpected)

    result = await post_ingest._process_document_post_ingest(
        document_id, "current-revision"
    )

    assert result["status"] == "deferred"


@pytest.mark.asyncio
async def test_quiet_current_revision_is_processed_once(monkeypatch) -> None:
    document_id = uuid4()
    calls: list[tuple[UUID, str, str, str | None]] = []

    async def _load(_document_id: UUID):
        return _state(
            synced_at=datetime.now(timezone.utc)
            - timedelta(seconds=post_ingest.POST_INGEST_QUIET_SECONDS + 1)
        )

    async def _run(
        doc_id: UUID,
        tool_id: str,
        category: str,
        expected_revision: str | None,
    ):
        calls.append((doc_id, tool_id, category, expected_revision))

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _unlocked())
    monkeypatch.setattr(post_ingest, "_load_document_state", _load)
    monkeypatch.setattr(post_ingest, "_run_post_ingest_inner", _run)

    result = await post_ingest._process_document_post_ingest(
        document_id, "current-revision"
    )

    assert result["status"] == "processed"
    assert calls == [
        (document_id, "codex", "conversation", "current-revision")
    ]


@pytest.mark.asyncio
async def test_legacy_delivery_fences_revision_observed_at_preflight(
    monkeypatch,
) -> None:
    document_id = uuid4()
    observed_revisions: list[str | None] = []

    async def _load(_document_id: UUID):
        return _state(
            content_hash="observed-revision",
            synced_at=datetime.now(timezone.utc)
            - timedelta(seconds=post_ingest.POST_INGEST_QUIET_SECONDS + 1),
        )

    async def _run(
        _doc_id: UUID,
        _tool_id: str,
        _category: str,
        expected_revision: str | None,
    ):
        observed_revisions.append(expected_revision)

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _unlocked())
    monkeypatch.setattr(post_ingest, "_load_document_state", _load)
    monkeypatch.setattr(post_ingest, "_run_post_ingest_inner", _run)

    result = await post_ingest._process_document_post_ingest(document_id, None)

    assert result["status"] == "processed"
    assert observed_revisions == ["observed-revision"]


@pytest.mark.asyncio
async def test_overlapping_delivery_is_retried_before_loading(monkeypatch) -> None:
    document_id = uuid4()

    @asynccontextmanager
    async def _locked(_document_id: UUID):
        yield False

    async def _unexpected(_document_id: UUID):
        pytest.fail("contended delivery queried the document")

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _locked)
    monkeypatch.setattr(post_ingest, "_load_document_state", _unexpected)

    result = await post_ingest._process_document_post_ingest(
        document_id, "current-revision"
    )

    assert result == {
        "status": "deferred",
        "document_id": str(document_id),
        "countdown": post_ingest.POST_INGEST_CONTENTION_RETRY_SECONDS,
        "reason": "locked",
    }


@pytest.mark.asyncio
async def test_embedding_processing_delivery_is_retried(monkeypatch) -> None:
    document_id = uuid4()

    async def _load(_document_id: UUID):
        return _state(
            synced_at=datetime.now(timezone.utc)
            - timedelta(seconds=post_ingest.POST_INGEST_QUIET_SECONDS + 1),
            embedding_status="processing",
        )

    async def _unexpected(*_args):
        pytest.fail("processing revision reached duplicate post-ingest work")

    monkeypatch.setattr(post_ingest, "_document_post_ingest_lock", _unlocked())
    monkeypatch.setattr(post_ingest, "_load_document_state", _load)
    monkeypatch.setattr(post_ingest, "_run_post_ingest_inner", _unexpected)

    result = await post_ingest._process_document_post_ingest(
        document_id, "current-revision"
    )

    assert result["status"] == "deferred"
    assert result["reason"] == "embedding_processing"


def test_celery_task_retries_deferred_work(monkeypatch) -> None:
    document_id = uuid4()

    async def _deferred(_document_id: UUID, _expected_revision: str | None):
        return {
            "status": "deferred",
            "document_id": str(document_id),
            "countdown": 37,
        }

    monkeypatch.setattr(post_ingest, "_process_document_post_ingest", _deferred)

    with pytest.raises(Retry):
        post_ingest.process_document_post_ingest.run(
            str(document_id),
            "codex",
            "conversation",
            "current-revision",
        )


@pytest.mark.asyncio
async def test_repeated_ingests_queue_one_coalesced_delivery(monkeypatch) -> None:
    document_id = uuid4()
    claims = iter((True, False))
    revisions: list[str] = []
    queued: list[dict] = []

    def _claim(_document_id, revision: str, _token: str) -> bool:
        revisions.append(revision)
        return next(claims)

    def _apply_async(**kwargs) -> None:
        queued.append(kwargs)

    monkeypatch.setattr(post_ingest, "_claim_coalesced_schedule", _claim)
    monkeypatch.setattr(
        post_ingest.process_document_post_ingest,
        "apply_async",
        _apply_async,
    )

    first = await post_ingest.schedule_coalesced_post_ingest(
        document_id,
        "codex",
        "conversation",
        "revision-1",
        countdown=180,
    )
    second = await post_ingest.schedule_coalesced_post_ingest(
        document_id,
        "codex",
        "conversation",
        "revision-2",
        countdown=180,
    )

    assert first is True
    assert second is False
    assert revisions == ["revision-1", "revision-2"]
    assert len(queued) == 1
    assert queued[0]["countdown"] == 180
    assert queued[0]["retry"] is False
    assert queued[0]["args"][:4] == [
        str(document_id),
        "codex",
        "conversation",
        None,
    ]
    assert len(queued[0]["args"][4]) == 32


@pytest.mark.asyncio
async def test_failed_celery_send_releases_coalesced_claim(monkeypatch) -> None:
    document_id = uuid4()
    released: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        post_ingest,
        "_claim_coalesced_schedule",
        lambda *_args: True,
    )

    def _send_failure(**_kwargs) -> None:
        raise RuntimeError("broker unavailable")

    def _complete(_document_id, token: str, revision: str | None):
        released.append((token, revision))
        return "complete"

    monkeypatch.setattr(
        post_ingest.process_document_post_ingest,
        "apply_async",
        _send_failure,
    )
    monkeypatch.setattr(post_ingest, "_complete_coalesced_schedule", _complete)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await post_ingest.schedule_coalesced_post_ingest(
            document_id,
            "codex",
            "conversation",
            "revision-1",
            countdown=180,
        )

    assert len(released) == 1
    assert released[0][1] is None


def test_stale_coalesced_delivery_avoids_database_work(monkeypatch) -> None:
    document_id = uuid4()

    monkeypatch.setattr(
        post_ingest,
        "_coalesced_token_is_current",
        lambda *_args: False,
    )

    async def _unexpected(*_args):
        pytest.fail("stale coalesced delivery queried PostgreSQL")

    monkeypatch.setattr(post_ingest, "_process_document_post_ingest", _unexpected)

    result = post_ingest.process_document_post_ingest.run(
        str(document_id),
        "codex",
        "conversation",
        None,
        "old-token",
    )

    assert result == {"status": "stale", "document_id": str(document_id)}


def test_revision_arriving_during_processing_reuses_delivery(monkeypatch) -> None:
    document_id = uuid4()
    completions: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        post_ingest,
        "_coalesced_token_is_current",
        lambda *_args: True,
    )

    async def _processed(_document_id: UUID, _expected_revision: str | None):
        return {
            "status": "processed",
            "document_id": str(document_id),
            "revision": "revision-1",
        }

    def _complete(_document_id, token: str, revision: str | None):
        completions.append((token, revision))
        return "updated"

    monkeypatch.setattr(post_ingest, "_process_document_post_ingest", _processed)
    monkeypatch.setattr(post_ingest, "_complete_coalesced_schedule", _complete)

    with pytest.raises(Retry):
        post_ingest.process_document_post_ingest.run(
            str(document_id),
            "codex",
            "conversation",
            None,
            "current-token",
        )

    assert completions == [("current-token", "revision-1")]


def test_coalesced_delivery_releases_matching_revision(monkeypatch) -> None:
    document_id = uuid4()
    completions: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        post_ingest,
        "_coalesced_token_is_current",
        lambda *_args: True,
    )

    async def _processed(_document_id: UUID, _expected_revision: str | None):
        return {
            "status": "processed",
            "document_id": str(document_id),
            "revision": "revision-2",
        }

    def _complete(_document_id, token: str, revision: str | None):
        completions.append((token, revision))
        return "complete"

    monkeypatch.setattr(post_ingest, "_process_document_post_ingest", _processed)
    monkeypatch.setattr(post_ingest, "_complete_coalesced_schedule", _complete)

    result = post_ingest.process_document_post_ingest.run(
        str(document_id),
        "codex",
        "conversation",
        None,
        "current-token",
    )

    assert result["status"] == "processed"
    assert completions == [("current-token", "revision-2")]


def test_legacy_three_argument_task_retries_deferred_work(monkeypatch) -> None:
    document_id = uuid4()
    observed_revisions: list[str | None] = []

    async def _deferred(_document_id: UUID, expected_revision: str | None):
        observed_revisions.append(expected_revision)
        return {
            "status": "deferred",
            "document_id": str(document_id),
            "countdown": 37,
        }

    monkeypatch.setattr(post_ingest, "_process_document_post_ingest", _deferred)

    with pytest.raises(Retry):
        post_ingest.process_document_post_ingest.run(
            str(document_id),
            "codex",
            "conversation",
        )

    assert observed_revisions == [None]
