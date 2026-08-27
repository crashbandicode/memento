"""Long-lived Phase 3 drain for accepted realtime conversation DELTAs.

Ready markers are the source of truth.  This process deliberately polls the
shared spool as well as accepting optional wake-ups, so a Redis/Celery loss or
a process restart cannot strand an accepted collector receipt.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable

from ..config import settings
from ..services.ingest_service import DeltaBaseMismatch
from ..services.ingest_spool import (
    DEFAULT_SPOOL_ROOT,
    ChunkValidationError,
    complete_and_remove_job,
    mark_job_failed,
    next_ready_source_head,
    ready_delta_chain_job_ids,
    ready_job_ids,
    record_job_attempt,
    source_identity,
    spool_job_lock,
    spool_realtime_drain_lock,
    spool_source_lock,
    try_ready_manifest_metadata,
)

logger = logging.getLogger("realtime_ingest_drain")
QUIET_WINDOW_SECONDS = 1.25
MAX_WINDOW_SECONDS = 2.0
MAX_FINALIZE_RETRIES = 12


class RealtimeTerminalDisposition(ValueError):
    """The raw transaction did not commit the admitted final revision."""


def _realtime_ready_sources() -> dict[tuple[str, str, str, str], set[str]]:
    """Scan markers, returning only Phase 3 source groups.

    The scan is intentionally cheap metadata I/O.  It doubles as crash
    recovery; no queue broker contains payload bytes or completion authority.
    """
    groups: dict[tuple[str, str, str, str], set[str]] = {}
    for job_id in ready_job_ids(DEFAULT_SPOOL_ROOT):
        manifest = try_ready_manifest_metadata(job_id, DEFAULT_SPOOL_ROOT)
        if manifest is None or manifest["meta"].get("realtime_admission") is not True:
            continue
        groups.setdefault(source_identity(manifest), set()).add(job_id)
    return groups


async def _drain_source(identity: tuple[str, str, str, str]) -> dict | None:
    """Claim one source head, coalesce its contiguous DELTAs, and receipt all."""
    head = next_ready_source_head(identity, DEFAULT_SPOOL_ROOT)
    if head is None:
        return None
    manifest = try_ready_manifest_metadata(head, DEFAULT_SPOOL_ROOT)
    if manifest is None or manifest["meta"].get("realtime_admission") is not True:
        return None
    with spool_source_lock(identity, root=DEFAULT_SPOOL_ROOT, blocking=False) as source_locked:
        if not source_locked:
            return {"status": "source_busy", "job_id": head}
        with spool_job_lock(
            head,
            root=DEFAULT_SPOOL_ROOT,
            purpose="realtime-drain",
            blocking=False,
        ) as job_locked:
            if not job_locked:
                return {"status": "already_processing", "job_id": head}
            if head not in ready_job_ids(DEFAULT_SPOOL_ROOT):
                return None
            chain: list[tuple[str, dict]] = []
            for candidate_id in ready_delta_chain_job_ids(head, DEFAULT_SPOOL_ROOT):
                candidate = try_ready_manifest_metadata(candidate_id, DEFAULT_SPOOL_ROOT)
                if candidate is None or candidate["meta"].get("realtime_admission") is not True:
                    break
                chain.append((candidate_id, candidate))
            if not chain:
                return None
            attempts = record_job_attempt(head, root=DEFAULT_SPOOL_ROOT)
            try:
                # Import lazily to keep this service's module import light in
                # API/pytest processes that do not start the entrypoint.
                from ..tasks.ingest_spool import _ingest_ready_job

                result = await _ingest_ready_job(
                    head,
                    manifest,
                    delta_chain=tuple(chain),
                )
                if result.get("status") not in {"committed", "idempotent"}:
                    raise RealtimeTerminalDisposition(
                        f"raw chain ended as {result.get('status')!r}"
                    )
                document_id = result["document_id"]
                completed = 0
                for candidate_id, _ in chain:
                    if complete_and_remove_job(
                        candidate_id,
                        document_id=document_id,
                        root=DEFAULT_SPOOL_ROOT,
                    ):
                        completed += 1
                result["completed_receipts"] = completed
                result["coalesced_frames"] = len(chain)
                return result
            except (ChunkValidationError, DeltaBaseMismatch, KeyError, TypeError, ValueError) as exc:
                # A failed head is a chain barrier.  Collectors observing this
                # terminal receipt discard every accepted successor and use
                # their existing authoritative FULL repair path.
                logger.warning("Realtime ingest head failed for %s: %s", head, exc)
                # The one raw transaction rolled back every constituent. Mark
                # every known successor terminal so independent receipt polls
                # cannot remain ACCEPTED forever behind a failed head.
                terminal_ids = dict.fromkeys(
                    (*ready_delta_chain_job_ids(head, DEFAULT_SPOOL_ROOT), *(item[0] for item in chain))
                )
                for candidate_id in terminal_ids:
                    try:
                        mark_job_failed(
                            candidate_id,
                            error_type=type(exc).__name__,
                            attempts=attempts,
                            root=DEFAULT_SPOOL_ROOT,
                        )
                    except FileNotFoundError:
                        pass
                return {"status": "failed", "job_id": head, "error_type": type(exc).__name__}
            except Exception as exc:
                if attempts >= MAX_FINALIZE_RETRIES:
                    logger.exception("Realtime ingest exhausted retries for %s", head)
                    terminal_ids = dict.fromkeys(
                        (*ready_delta_chain_job_ids(head, DEFAULT_SPOOL_ROOT), *(item[0] for item in chain))
                    )
                    for candidate_id in terminal_ids:
                        try:
                            mark_job_failed(
                                candidate_id,
                                error_type=type(exc).__name__,
                                attempts=attempts,
                                root=DEFAULT_SPOOL_ROOT,
                            )
                        except FileNotFoundError:
                            pass
                    return {"status": "failed", "job_id": head, "error_type": type(exc).__name__}
                raise


class RealtimeIngestDrain:
    """One serialized drain lifecycle with trailing-quiet coalescing."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], asyncio.Future] | None = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._stopping = asyncio.Event()
        self._windows: dict[tuple[str, str, str, str], tuple[frozenset[str], float, float]] = {}
        self._retry_after: dict[tuple[str, str, str, str], float] = {}

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self) -> list[dict]:
        now = self._clock()
        groups = _realtime_ready_sources()
        ready: list[tuple[str, str, str, str]] = []
        for identity, job_ids in groups.items():
            fingerprint = frozenset(job_ids)
            previous = self._windows.get(identity)
            if previous is None:
                self._windows[identity] = (fingerprint, now, now)
                continue
            last_fingerprint, first_seen, last_arrival = previous
            if fingerprint != last_fingerprint:
                last_arrival = now
            self._windows[identity] = (fingerprint, first_seen, last_arrival)
            retry_after = self._retry_after.get(identity, 0.0)
            if now < retry_after:
                continue
            if (
                now - last_arrival >= QUIET_WINDOW_SECONDS
                or now - first_seen >= MAX_WINDOW_SECONDS
            ):
                ready.append(identity)
        for identity in tuple(self._windows):
            if identity not in groups:
                self._windows.pop(identity, None)
                self._retry_after.pop(identity, None)

        results: list[dict] = []
        for identity in sorted(ready):
            try:
                result = await _drain_source(identity)
            except Exception:
                # Retry a transient head after a bounded delay rather than
                # incrementing its durable attempt counter on every scan.
                logger.exception("Realtime drain retrying source %s", identity)
                self._retry_after[identity] = self._clock() + 1.0
                continue
            self._windows.pop(identity, None)
            self._retry_after.pop(identity, None)
            if result is not None:
                results.append(result)
        return results

    async def run(self) -> None:
        poll_seconds = max(0.02, float(settings.realtime_ingest_drain_poll_seconds))
        with spool_realtime_drain_lock(
            root=DEFAULT_SPOOL_ROOT,
            blocking=False,
        ) as acquired:
            if not acquired:
                logger.error("Another realtime ingest drain owns the spool lock")
                return
            logger.info("Realtime ingest drain started (marker recovery authoritative)")
            while not self._stopping.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
            logger.info("Realtime ingest drain stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    drain = RealtimeIngestDrain()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, drain.stop)
        except NotImplementedError:
            signal.signal(signum, lambda *_args: drain.stop())
    try:
        loop.run_until_complete(drain.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
