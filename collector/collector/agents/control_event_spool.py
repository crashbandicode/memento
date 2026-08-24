"""Durable outbound spool for adapter lifecycle events.

Modeled on the proven Claw outbox (`orchestration_sync.py`) but owned end to
end by the collector: the session manager appends schema-v1 JSONL events,
and an uploader thread batches them to ``POST /api/control/events``, which
is idempotent by ``(machine_id, event_id)``. The byte cursor advances only
after the server accepts a batch (2xx), so restarts replay safely. When the
file is fully acknowledged and oversized, it is truncated under the shared
lock — the spool never grows without bound.

Events carry identity and bounded detail only — never prompt, response, or
tool content.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import CollectorConfig
from ..tls import SSL_CONTEXT

logger = logging.getLogger("collector.agents.spool")

SCHEMA_VERSION = 1
MAX_BATCH_EVENTS = 200
MAX_BATCH_BYTES = 512 * 1024
MAX_DETAILS_BYTES = 4096
_ROTATE_AFTER_BYTES = 8 * 1024 * 1024
_DEFAULT_POLL_SECONDS = 1.0
_ERROR_BACKOFF_MAX = 60.0


def default_spool_path() -> Path:
    configured = os.environ.get("MEMENTO_CONTROL_EVENT_SPOOL", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".memento" / "control-events" / "v1" / "events.jsonl"


def default_state_path() -> Path:
    configured = os.environ.get("MEMENTO_CONTROL_EVENT_STATE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".memento" / "control-events-state.json"


def _bounded_details(details: dict | None) -> dict:
    if not details:
        return {}
    encoded = json.dumps(details, default=str)
    if len(encoded) <= MAX_DETAILS_BYTES:
        return details
    return {"truncated": True, "preview": encoded[:1024]}


@dataclass(frozen=True, slots=True)
class PendingBatch:
    events: tuple[dict, ...]
    end_offset: int


class ControlEventSpool:
    """Append-only writer plus cursor state for one collector process."""

    def __init__(
        self,
        spool_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.spool_path = spool_path or default_spool_path()
        self.state_path = state_path or default_state_path()
        self._lock = threading.Lock()
        self._session_seq: dict[str, int] = {}
        self._offset = 0
        self._load_state()

    # -- writer ----------------------------------------------------------------

    def emit(
        self,
        event_type: str,
        *,
        control_session_id: str | None = None,
        command_id: str | None = None,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
        interaction_id: str | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
        elapsed_ms: int | None = None,
        adapter: str | None = None,
        details: dict | None = None,
    ) -> str:
        """Durably append one event; returns its event id."""
        event_id = str(uuid.uuid4())
        event: dict = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at_device": datetime.now(timezone.utc).isoformat(),
            "details": _bounded_details(details),
        }
        if control_session_id:
            event["control_session_id"] = control_session_id
            with self._lock:
                seq = self._session_seq.get(control_session_id, 0) + 1
                self._session_seq[control_session_id] = seq
            event["details"] = {**event["details"], "session_seq": seq}
        for key, value in (
            ("command_id", command_id),
            ("native_session_id", native_session_id),
            ("native_turn_id", native_turn_id),
            ("interaction_id", interaction_id),
            ("outcome", outcome),
            ("error_code", error_code),
            ("elapsed_ms", elapsed_ms),
            ("adapter", adapter),
        ):
            if value is not None:
                event[key] = value
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with self._lock:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            with self.spool_path.open("ab") as target:
                target.write(line.encode("utf-8"))
        return event_id

    # -- cursor / reader ---------------------------------------------------------

    def _load_state(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._offset = max(0, int(payload.get("offset", 0)))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._offset = 0

    def read_pending(self) -> PendingBatch | None:
        with self._lock:
            try:
                size = self.spool_path.stat().st_size
            except OSError:
                return None
            offset = self._offset if self._offset <= size else 0
            events: list[dict] = []
            consumed = 0
            end_offset = offset
            try:
                with self.spool_path.open("rb") as source:
                    source.seek(offset)
                    while len(events) < MAX_BATCH_EVENTS and consumed < MAX_BATCH_BYTES:
                        line = source.readline(MAX_BATCH_BYTES + 1)
                        if not line or not line.endswith(b"\n"):
                            break
                        consumed += len(line)
                        end_offset = source.tell()
                        try:
                            event = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            logger.warning("Skipping malformed control spool line")
                            continue
                        if isinstance(event, dict) and event.get("schema_version") == SCHEMA_VERSION:
                            events.append(event)
            except OSError:
                return None
        if end_offset == offset:
            return None
        return PendingBatch(tuple(events), end_offset)

    def acknowledge(self, batch: PendingBatch) -> None:
        with self._lock:
            self._offset = batch.end_offset
            self._persist_state()
            self._maybe_rotate()

    def _persist_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        temporary.write_text(json.dumps({"offset": self._offset}), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _maybe_rotate(self) -> None:
        """Truncate a fully-acknowledged oversized spool. Caller holds lock."""
        try:
            size = self.spool_path.stat().st_size
        except OSError:
            return
        if size == self._offset and size > _ROTATE_AFTER_BYTES:
            with self.spool_path.open("wb"):
                pass
            self._offset = 0
            self._persist_state()


class ControlEventUploader:
    """Background thread draining the spool into /api/control/events."""

    def __init__(
        self,
        config: CollectorConfig,
        spool: ControlEventSpool,
        *,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        self._spool = spool
        self._poll_seconds = max(0.2, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            # Not importlib.metadata: frozen sidecars report whatever stale
            # distribution happened to be installed in the build Python.
            from collector._version import __version__ as collector_version
        except Exception:
            collector_version = "dev"
        self._collector_version = collector_version
        self._client = httpx.Client(
            base_url=config.server.url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=SSL_CONTEXT,
            headers={
                "X-Collector-Token": config.server.token,
                "X-Device-Id": config.device_id,
                "X-Device-Name": config.device_name,
                "X-Device-Platform": config.platform,
                "X-Collector-Version": collector_version,
            },
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="control-event-uploader"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._client.close()

    def sync_once(self) -> str:
        """One upload attempt. Returns ``sent``, ``empty``, or ``error``."""
        batch = self._spool.read_pending()
        if batch is None:
            return "empty"
        if not batch.events:
            self._spool.acknowledge(batch)
            return "sent"
        events = [
            {**event, "collector_revision": self._collector_version}
            for event in batch.events
        ]
        try:
            response = self._client.post("/api/control/events", json={"events": events})
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Control event upload deferred: %s", exc)
            return "error"
        self._spool.acknowledge(batch)
        return "sent"

    def _run(self) -> None:
        # Pending-question latency rides this loop: an empty spool polls at
        # the base cadence, and only transport errors back off.
        backoff = self._poll_seconds
        while not self._stop.is_set():
            try:
                outcome = "empty"
                for _ in range(4):
                    if self._stop.is_set():
                        break
                    outcome = self.sync_once()
                    if outcome != "sent":
                        break
                backoff = (
                    min(backoff * 2, _ERROR_BACKOFF_MAX)
                    if outcome == "error"
                    else self._poll_seconds
                )
            except Exception:  # noqa: BLE001 — uploader must outlive any cycle
                logger.exception("Control event upload cycle failed; continuing")
                backoff = min(backoff * 2, _ERROR_BACKOFF_MAX)
            self._stop.wait(backoff)
