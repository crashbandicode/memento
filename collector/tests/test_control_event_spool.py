from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx

from collector.agents.control_event_spool import (
    ControlEventSpool,
    ControlEventUploader,
    _ROTATE_AFTER_BYTES,
)


def _spool(tmp_path: Path) -> ControlEventSpool:
    return ControlEventSpool(tmp_path / "events.jsonl", tmp_path / "state.json")


def test_emit_read_acknowledge_round_trip(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    first = spool.emit("adapter.session_started", control_session_id="cs-1")
    second = spool.emit("adapter.turn_started", control_session_id="cs-1")

    batch = spool.read_pending()
    assert batch is not None
    assert [event["event_id"] for event in batch.events] == [first, second]
    # Per-session sequence numbers are monotonic.
    assert [event["details"]["session_seq"] for event in batch.events] == [1, 2]

    spool.acknowledge(batch)
    assert spool.read_pending() is None

    third = spool.emit("adapter.turn_completed", control_session_id="cs-1")
    resumed = spool.read_pending()
    assert resumed is not None
    assert [event["event_id"] for event in resumed.events] == [third]
    assert resumed.events[0]["details"]["session_seq"] == 3


def test_cursor_survives_restart(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.emit("adapter.session_started", control_session_id="cs-1")
    batch = spool.read_pending()
    spool.acknowledge(batch)
    spool.emit("adapter.session_closed", control_session_id="cs-1")

    reopened = _spool(tmp_path)
    pending = reopened.read_pending()
    assert pending is not None
    assert [event["event_type"] for event in pending.events] == ["adapter.session_closed"]


def test_details_are_bounded(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.emit("adapter.interaction_pending", details={"blob": "x" * 100_000})
    batch = spool.read_pending()
    details = batch.events[0]["details"]
    assert details["truncated"] is True
    assert len(details["preview"]) <= 1024


def test_fully_acknowledged_oversized_spool_rotates(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    filler = "y" * 2000
    while spool.spool_path.stat().st_size if spool.spool_path.exists() else 0 <= _ROTATE_AFTER_BYTES:
        spool.emit("adapter.turn_started", details={"filler": filler})
        if spool.spool_path.stat().st_size > _ROTATE_AFTER_BYTES:
            break
    while (batch := spool.read_pending()) is not None:
        spool.acknowledge(batch)
    assert spool.spool_path.stat().st_size == 0
    assert spool.read_pending() is None
    after = spool.emit("adapter.turn_completed")
    fresh = spool.read_pending()
    assert [event["event_id"] for event in fresh.events] == [after]


class _FakeClient:
    def __init__(self, status_codes: list[int]) -> None:
        self.status_codes = status_codes
        self.batches: list[list[dict]] = []

    def post(self, path: str, *, json: dict) -> httpx.Response:
        self.batches.append(json["events"])
        status = self.status_codes.pop(0) if self.status_codes else 200
        request = httpx.Request("POST", f"https://memento.invalid{path}")
        return httpx.Response(status, request=request, json={})


def _uploader(spool: ControlEventSpool, client: _FakeClient) -> ControlEventUploader:
    uploader = object.__new__(ControlEventUploader)
    uploader._spool = spool
    uploader._client = client
    uploader._collector_version = "test"
    uploader._poll_seconds = 0.01
    uploader._stop = SimpleNamespace(is_set=lambda: False, wait=lambda s: None)
    return uploader


def test_uploader_advances_only_after_server_accepts(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.emit("adapter.session_started", control_session_id="cs-9")

    failing = _uploader(spool, _FakeClient([503]))
    assert failing.sync_once() == "error"
    assert spool.read_pending() is not None  # cursor did not advance

    ok = _uploader(spool, _FakeClient([200]))
    assert ok.sync_once() == "sent"
    assert ok.sync_once() == "empty"
    assert spool.read_pending() is None


def test_uploader_stamps_collector_revision(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.emit("adapter.session_started")
    client = _FakeClient([200])
    uploader = _uploader(spool, client)
    uploader.sync_once()
    assert client.batches[0][0]["collector_revision"] == "test"


def test_malformed_spool_line_is_skipped(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.emit("adapter.session_started")
    with spool.spool_path.open("ab") as target:
        target.write(b"not json at all\n")
    spool.emit("adapter.session_closed")

    batch = spool.read_pending()
    types = [event["event_type"] for event in batch.events]
    assert types == ["adapter.session_started", "adapter.session_closed"]
    spool.acknowledge(batch)
    assert spool.read_pending() is None


def test_replayed_batch_is_idempotent_payload(tmp_path: Path) -> None:
    """A crash between POST and acknowledge re-sends identical event ids."""
    spool = _spool(tmp_path)
    spool.emit("adapter.session_started", control_session_id="cs-2")
    client = _FakeClient([200, 200])
    uploader = _uploader(spool, client)
    first_batch = spool.read_pending()
    uploader.sync_once()

    # Simulate crash-before-ack: reset cursor to the pre-ack offset.
    replay_spool = ControlEventSpool(spool.spool_path, tmp_path / "other-state.json")
    replay_uploader = _uploader(replay_spool, client)
    replay_uploader.sync_once()

    assert len(client.batches) == 2
    assert client.batches[0][0]["event_id"] == client.batches[1][0]["event_id"]
    assert first_batch is not None
