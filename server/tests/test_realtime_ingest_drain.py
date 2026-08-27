"""Portable timing tests for the Phase 3 marker-authoritative drain loop."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))


@pytest.mark.asyncio
async def test_drain_uses_trailing_quiet_window_then_single_source_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.services import realtime_ingest_drain as drain_module

    identity = ("owner", "machine", "codex", "thread.jsonl")
    now = [0.0]
    calls: list[tuple[tuple[str, str, str, str], float]] = []

    monkeypatch.setattr(
        drain_module,
        "_realtime_ready_sources",
        lambda: {identity: {"a" * 64, "b" * 64}},
    )

    async def drain_source(source):
        calls.append((source, now[0]))
        return {"status": "committed", "coalesced_frames": 2}

    monkeypatch.setattr(drain_module, "_drain_source", drain_source)
    drain = drain_module.RealtimeIngestDrain(clock=lambda: now[0])
    assert await drain.run_once() == []
    now[0] = 1.24
    assert await drain.run_once() == []
    now[0] = 1.25
    results = await drain.run_once()
    assert [result["coalesced_frames"] for result in results] == [2]
    assert calls == [(identity, 1.25)]


@pytest.mark.asyncio
async def test_synthetic_150_frames_per_minute_hard_deadline_is_under_freshness_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous arrivals leave budget for commit and local SSE fallback."""
    from server.services import realtime_ingest_drain as drain_module
    from server.services import sse_service

    identity = ("owner", "machine", "codex", "continuous.jsonl")
    now = [0.0]
    jobs: set[str] = set()
    admitted_to_drain: list[float] = []
    monkeypatch.setattr(
        drain_module,
        "_realtime_ready_sources",
        lambda: {identity: set(jobs)} if jobs else {},
    )

    async def drain_source(_source):
        admitted_to_drain.append(now[0])
        jobs.clear()
        return {"status": "committed", "coalesced_frames": 1}

    monkeypatch.setattr(drain_module, "_drain_source", drain_source)
    drain = drain_module.RealtimeIngestDrain(clock=lambda: now[0])
    # 150/minute is one frame every 0.4s.  New ready markers keep extending
    # quiet time, so this exercises the hard maximum rather than the easy case.
    for index in range(6):
        jobs.add(f"{index:064x}")
        now[0] = index * 0.4
        await drain.run_once()
    now[0] = 2.0
    await drain.run_once()
    assert admitted_to_drain == [2.0]
    # A failed two-command Redis pipeline consumes at most two socket timeout
    # intervals before the process-local SSE bus is notified.  Keep additional
    # budget for the raw commit itself inside the 2.5-second freshness gate.
    redis_fallback_budget = 2 * sse_service._PUBLISH_SOCKET_TIMEOUT_SECONDS
    assert admitted_to_drain[0] + redis_fallback_budget < 2.5


@pytest.mark.asyncio
async def test_drain_exits_when_another_process_owns_global_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.services import realtime_ingest_drain as drain_module

    calls: list[str] = []
    monkeypatch.setattr(
        drain_module,
        "spool_realtime_drain_lock",
        lambda **_kwargs: nullcontext(False),
    )
    drain = drain_module.RealtimeIngestDrain()

    async def run_once():
        calls.append("run")
        return []

    monkeypatch.setattr(drain, "run_once", run_once)
    await drain.run()
    assert calls == []


@pytest.mark.asyncio
async def test_unsupported_raw_chain_falls_back_to_legacy_writer_per_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RawWriterUnsupported must route the chain through the legacy path, not
    retry forever (observed live: an authoritative history rebuild looped the
    drain 144 times at ~32% CPU)."""
    from types import SimpleNamespace

    from server.db import session as session_module
    from server.services import ingest_service as ingest_module
    from server.services import realtime_raw_writer as raw_module
    from server.tasks import ingest_spool as task_module

    job_ids = ("a" * 64, "b" * 64)
    manifests = {job_id: {"meta": {"file_size": 10}} for job_id in job_ids}

    def read_bytes(job_id, *, manifest):
        del manifest
        return (
            {
                "meta": {
                    "tool": "codex",
                    "category": "conversation",
                    "content_type": "jsonl",
                    "relative_path": "sessions/x.jsonl",
                    "hash": f"hash-{job_id[:6]}",
                    "file_size": 10,
                    "mode": "delta",
                    "offset": 0,
                    "metadata": {},
                }
            },
            b'{"line": 1}',
        )

    monkeypatch.setattr(task_module, "read_ready_job_bytes", read_bytes)

    async def unsupported_chain(**_kwargs):
        raise raw_module.RawWriterUnsupported("history rebuild needs legacy")

    monkeypatch.setattr(
        raw_module, "ingest_conversation_raw_chain", unsupported_chain
    )

    legacy_calls: list[dict] = []

    async def fake_ingest_file(*, db, writer=None, **frame):
        del db
        legacy_calls.append({"writer": writer, **frame})
        return SimpleNamespace(id="doc-1")

    monkeypatch.setattr(ingest_module, "ingest_file", fake_ingest_file)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(
        session_module, "async_session_factory", lambda: _FakeSession()
    )

    result = await task_module._ingest_realtime_delta_chain(
        payload_jobs=tuple((job_id, manifests[job_id]) for job_id in job_ids),
        machine_id="machine-1",
        user_id=__import__("uuid").uuid4(),
    )

    assert result["status"] == "committed"
    assert result["writer"] == "legacy"
    assert result["coalesced_frames"] == 2
    assert [call["writer"] for call in legacy_calls] == ["legacy", "legacy"]
    assert [call["content_hash"] for call in legacy_calls] == [
        f"hash-{job_ids[0][:6]}",
        f"hash-{job_ids[1][:6]}",
    ]


@pytest.mark.asyncio
async def test_terminal_chain_disposition_fails_every_constituent_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.services import realtime_ingest_drain as drain_module
    from server.tasks import ingest_spool as task_module

    identity = ("owner", "machine", "codex", "thread.jsonl")
    job_ids = ("a" * 64, "b" * 64, "c" * 64)
    manifests = {
        job_id: {
            "job_id": job_id,
            "user_id": identity[0],
            "device_id": identity[1],
            "meta": {
                "tool": identity[2],
                "relative_path": identity[3],
                "realtime_admission": True,
            },
        }
        for job_id in job_ids
    }
    failed: list[str] = []
    monkeypatch.setattr(drain_module, "next_ready_source_head", lambda *_args: job_ids[0])
    monkeypatch.setattr(
        drain_module,
        "try_ready_manifest_metadata",
        lambda job_id, *_args: manifests.get(job_id),
    )
    monkeypatch.setattr(drain_module, "spool_source_lock", lambda *_args, **_kwargs: nullcontext(True))
    monkeypatch.setattr(drain_module, "spool_job_lock", lambda *_args, **_kwargs: nullcontext(True))
    monkeypatch.setattr(drain_module, "ready_job_ids", lambda *_args: list(job_ids))
    monkeypatch.setattr(drain_module, "ready_delta_chain_job_ids", lambda *_args: job_ids)
    monkeypatch.setattr(drain_module, "record_job_attempt", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        drain_module,
        "mark_job_failed",
        lambda job_id, **_kwargs: failed.append(job_id),
    )

    async def stale_chain(*_args, **_kwargs):
        return {"status": "stale_delta", "document_id": "document"}

    monkeypatch.setattr(task_module, "_ingest_ready_job", stale_chain)
    result = await drain_module._drain_source(identity)

    assert result is not None and result["status"] == "failed"
    assert failed == list(job_ids)
