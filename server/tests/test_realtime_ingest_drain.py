"""Portable timing tests for the Phase 3 marker-authoritative drain loop."""

from __future__ import annotations

import logging
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

    from server.config import settings
    from server.db import session as session_module
    from server.services import ingest_service as ingest_module
    from server.services import realtime_raw_writer as raw_module
    from server.tasks import ingest_spool as task_module

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", False)
    job_ids = ("a" * 64, "b" * 64)
    manifests = {job_id: {"meta": {"file_size": 10}} for job_id in job_ids}

    def read_bytes(job_id, *, manifest):
        del manifest
        return (
            {
                "meta": {
                    "tool": "claude_code",
                    "category": "conversation",
                    "content_type": "jsonl",
                    "relative_path": (
                        "sessions/parent/subagents/agent-drain-fixture.jsonl"
                    ),
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
        raise raw_module.RawWriterUnsupported(
            "Claude transcript/sidecar pairing needs the legacy reducer"
        )

    monkeypatch.setattr(
        raw_module, "ingest_conversation_raw_chain", unsupported_chain
    )
    outcomes = task_module._RealtimeWriterOutcomeCounters()
    monkeypatch.setattr(task_module, "_realtime_writer_outcomes", outcomes)

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
    assert outcomes.legacy_fallback_chains == {
        "Claude transcript/sidecar pairing needs the legacy reducer": 1
    }


@pytest.mark.asyncio
async def test_subagent_transcript_chain_records_raw_commit_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.config import settings
    from server.services import realtime_raw_writer as raw_module
    from server.tasks import ingest_spool as task_module

    monkeypatch.setattr(settings, "realtime_ingest_raw_subagent_transcripts", True)
    job_ids = ("c" * 64, "d" * 64)
    manifests = {job_id: {"meta": {"file_size": 10}} for job_id in job_ids}
    raw_calls: list[list[dict]] = []

    def read_bytes(job_id, *, manifest):
        del manifest
        return (
            {
                "meta": {
                    "tool": "claude_code",
                    "category": "conversation",
                    "content_type": "jsonl",
                    "relative_path": (
                        "sessions/parent/subagents/agent-drain-fixture.jsonl"
                    ),
                    "hash": f"hash-{job_id[:6]}",
                    "file_size": 10,
                    "mode": "delta",
                    "offset": 10,
                    "metadata": {"session_id": "drain-fixture"},
                }
            },
            b'{"type":"user"}',
        )

    monkeypatch.setattr(task_module, "read_ready_job_bytes", read_bytes)

    async def committed_chain(*, frames, **_kwargs):
        raw_calls.append(frames)
        return raw_module.RawDocument(__import__("uuid").uuid4()), None

    monkeypatch.setattr(raw_module, "ingest_conversation_raw_chain", committed_chain)
    outcomes = task_module._RealtimeWriterOutcomeCounters()
    monkeypatch.setattr(task_module, "_realtime_writer_outcomes", outcomes)

    result = await task_module._ingest_realtime_delta_chain(
        payload_jobs=tuple((job_id, manifests[job_id]) for job_id in job_ids),
        machine_id="machine-1",
        user_id=__import__("uuid").uuid4(),
    )

    assert result["status"] == "committed"
    assert result["coalesced_frames"] == 2
    assert [frame["relative_path"] for frame in raw_calls[0]] == [
        "sessions/parent/subagents/agent-drain-fixture.jsonl",
        "sessions/parent/subagents/agent-drain-fixture.jsonl",
    ]
    assert outcomes.raw_committed_chains == 1
    assert outcomes.raw_committed_frames == 2
    assert outcomes.legacy_fallback_chains == {}


def test_raw_writer_outcome_counters_log_bounded_per_reason_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from server.tasks import ingest_spool as task_module

    now = [0.0]
    counters = task_module._RealtimeWriterOutcomeCounters(
        clock=lambda: now[0],
        log_interval_seconds=60.0,
        log_every_chains=100,
        max_reasons=1,
    )
    with caplog.at_level(logging.INFO, logger="ingest_spool"):
        counters.record_raw_committed(frames=3)
        counters.record_legacy_fallback(
            reason="Claude transcript/sidecar pairing needs the legacy reducer",
            frames=2,
        )
        counters.record_legacy_fallback(
            reason="Cursor projection reordering needs the legacy reducer",
            frames=1,
        )
        counters.record_raw_handled(disposition="idempotent", frames=4)
        now[0] = 60.0
        counters.record_raw_committed(frames=1)

    record = next(
        item
        for item in caplog.records
        if "Realtime raw-writer outcomes" in item.getMessage()
    )
    message = record.getMessage()
    assert "total_handled_chains=5 total_handled_frames=11" in message
    assert "raw_committed_chains=2 raw_committed_frames=4" in message
    assert "legacy_fallback_chains_by_reason" in message
    assert "Claude transcript/sidecar pairing needs the legacy reducer" in message
    assert "other/raw-writer-unsupported" in message


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
