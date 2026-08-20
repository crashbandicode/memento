"""Durable Claw lifecycle outbox uploader.

The native tool transcripts remain on their existing collector paths. This
uploader only advances a byte cursor after the server durably accepts a bounded
batch of content-free orchestration identity/status events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import CollectorConfig
from .tls import SSL_CONTEXT

logger = logging.getLogger("collector.orchestration")

DEFAULT_POLL_SECONDS = 1.0
MAX_BATCH_EVENTS = 250
MAX_BATCH_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class PendingEventBatch:
    events: tuple[dict, ...]
    end_offset: int
    file_identity: str


def default_outbox_path() -> Path:
    configured = os.environ.get("CLAWO_MEMENTO_OUTBOX", "").strip()
    if configured:
        return Path(configured).expanduser()
    return (
        Path.home()
        / ".claw-orchestrator"
        / "memento-events"
        / "v1"
        / "events.jsonl"
    )


def default_state_path() -> Path:
    configured = os.environ.get("MEMENTO_ORCHESTRATION_STATE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".memento" / "orchestration-outbox-state.json"


def _file_identity(stat_result: os.stat_result) -> str:
    return f"{stat_result.st_dev}:{stat_result.st_ino}"


class OrchestrationOutboxReader:
    def __init__(self, outbox_path: Path, state_path: Path) -> None:
        self.outbox_path = outbox_path
        self.state_path = state_path
        self._offset = 0
        self._file_identity = ""
        self._tail_marker = ""
        self._load_state()

    @property
    def offset(self) -> int:
        return self._offset

    def _load_state(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._offset = max(0, int(payload.get("offset", 0)))
            self._file_identity = str(payload.get("file_identity") or "")
            self._tail_marker = str(payload.get("tail_marker") or "")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._offset = 0
            self._file_identity = ""
            self._tail_marker = ""

    def _current_tail_marker(self, offset: int) -> str:
        if offset <= 0:
            return ""
        try:
            with self.outbox_path.open("rb") as source:
                length = min(64, offset)
                source.seek(offset - length)
                return hashlib.sha256(source.read(length)).hexdigest()
        except OSError:
            return ""

    def read_pending(self) -> PendingEventBatch | None:
        try:
            stat_result = self.outbox_path.stat()
        except OSError:
            return None
        identity = _file_identity(stat_result)
        offset = self._offset
        if stat_result.st_size < offset or (
            self._file_identity and self._file_identity != identity
        ) or (
            offset > 0
            and self._tail_marker
            and self._current_tail_marker(offset) != self._tail_marker
        ):
            offset = 0

        events: list[dict] = []
        consumed = 0
        end_offset = offset
        try:
            with self.outbox_path.open("rb") as source:
                source.seek(offset)
                while len(events) < MAX_BATCH_EVENTS and consumed < MAX_BATCH_BYTES:
                    line_start = source.tell()
                    line = source.readline(MAX_BATCH_BYTES + 1)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        # Claw may still be appending the final line. Never
                        # acknowledge a partial JSON record.
                        break
                    consumed += len(line)
                    end_offset = source.tell()
                    try:
                        event = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        logger.warning(
                            "Skipping malformed Claw lifecycle line at byte %d",
                            line_start,
                        )
                        continue
                    if not isinstance(event, dict) or event.get("schema_version") != 1:
                        logger.warning(
                            "Skipping unsupported Claw lifecycle line at byte %d",
                            line_start,
                        )
                        continue
                    events.append(event)
        except OSError as exc:
            logger.debug("Unable to read Claw lifecycle outbox: %s", exc)
            return None

        if end_offset == offset:
            return None
        return PendingEventBatch(tuple(events), end_offset, identity)

    def acknowledge(self, batch: PendingEventBatch) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tail_marker = self._current_tail_marker(batch.end_offset)
        temporary.write_text(
            json.dumps(
                {
                    "offset": batch.end_offset,
                    "file_identity": batch.file_identity,
                    "tail_marker": tail_marker,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)
        self._offset = batch.end_offset
        self._file_identity = batch.file_identity
        self._tail_marker = tail_marker


class OrchestrationSync:
    def __init__(
        self,
        config: CollectorConfig,
        *,
        outbox_path: Path | None = None,
        state_path: Path | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._config = config
        self._reader = OrchestrationOutboxReader(
            outbox_path or default_outbox_path(),
            state_path or default_state_path(),
        )
        self._poll_seconds = max(0.2, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            from importlib.metadata import version

            collector_version = version("memento-brain-collector")
        except Exception:
            collector_version = "dev"
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
            target=self._run,
            daemon=True,
            name="orchestration-sync",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._client.close()

    def sync_once(self) -> bool:
        batch = self._reader.read_pending()
        if batch is None:
            return False
        if not batch.events:
            self._reader.acknowledge(batch)
            return True
        try:
            response = self._client.post(
                "/api/ingest/orchestration-events",
                json={"events": list(batch.events)},
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Claw lifecycle upload deferred: %s", exc)
            return False
        self._reader.acknowledge(batch)
        logger.info("Synced %d Claw lifecycle event(s)", len(batch.events))
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            # Drain a short burst without holding the main collector loop.
            for _ in range(4):
                if self._stop.is_set() or not self.sync_once():
                    break
            self._stop.wait(self._poll_seconds)
