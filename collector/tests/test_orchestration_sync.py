from __future__ import annotations

import json
from pathlib import Path

import httpx

from collector.orchestration_sync import (
    OrchestrationOutboxReader,
    OrchestrationSync,
)


def _event(event_id: str) -> dict:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "occurred_at": "2026-08-20T12:00:00Z",
    }


def test_reader_waits_for_partial_final_line(tmp_path: Path) -> None:
    outbox = tmp_path / "events.jsonl"
    state = tmp_path / "state.json"
    first = json.dumps(_event("one")) + "\n"
    partial = json.dumps(_event("two"))
    outbox.write_text(first + partial, encoding="utf-8")
    reader = OrchestrationOutboxReader(outbox, state)

    batch = reader.read_pending()

    assert batch is not None
    assert [item["event_id"] for item in batch.events] == ["one"]
    reader.acknowledge(batch)
    assert reader.read_pending() is None
    with outbox.open("a", encoding="utf-8") as target:
        target.write("\n")
    resumed = reader.read_pending()
    assert resumed is not None
    assert [item["event_id"] for item in resumed.events] == ["two"]


def test_reader_resets_cursor_after_truncation(tmp_path: Path) -> None:
    outbox = tmp_path / "events.jsonl"
    state = tmp_path / "state.json"
    outbox.write_text(json.dumps(_event("old")) + "\n", encoding="utf-8")
    reader = OrchestrationOutboxReader(outbox, state)
    first = reader.read_pending()
    assert first is not None
    reader.acknowledge(first)

    outbox.write_text(json.dumps(_event("new")) + "\n", encoding="utf-8")
    second = reader.read_pending()

    assert second is not None
    assert [item["event_id"] for item in second.events] == ["new"]


class _FakeReader:
    def __init__(self, batch) -> None:
        self.batch = batch
        self.acknowledged = False

    def read_pending(self):
        return self.batch

    def acknowledge(self, _batch) -> None:
        self.acknowledged = True


class _FakeClient:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def post(self, path: str, *, json: dict) -> httpx.Response:
        request = httpx.Request("POST", f"https://memento.invalid{path}")
        return httpx.Response(self.status_code, request=request, json={})


def test_sync_advances_only_after_server_accepts(tmp_path: Path) -> None:
    outbox = tmp_path / "events.jsonl"
    state = tmp_path / "state.json"
    outbox.write_text(json.dumps(_event("one")) + "\n", encoding="utf-8")
    batch = OrchestrationOutboxReader(outbox, state).read_pending()
    assert batch is not None
    sync = object.__new__(OrchestrationSync)
    failed_reader = _FakeReader(batch)
    sync._reader = failed_reader
    sync._client = _FakeClient(503)

    assert sync.sync_once() is False
    assert failed_reader.acknowledged is False

    accepted_reader = _FakeReader(batch)
    sync._reader = accepted_reader
    sync._client = _FakeClient(200)
    assert sync.sync_once() is True
    assert accepted_reader.acknowledged is True
