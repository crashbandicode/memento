"""HTTP queue drain with byte-bounded leases and streaming large uploads."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import BinaryIO, Callable

import httpx

from .config import CollectorConfig
from .outcomes import (
    EDGE_AVAILABILITY_HTTP_STATUSES,
    SourceRepairAction,
    UploadOutcome,
    UploadOutcomeState,
)
from .queue import QueueItem, SyncQueue

logger = logging.getLogger("collector.sync")

CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB per chunk
MAX_CHUNKED_UPLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_UPLOAD_MAX_ATTEMPTS = 4
CHUNK_RETRY_BASE_SECONDS = 0.5
CHUNK_RETRY_MAX_SECONDS = 4.0
CHUNK_COMMIT_POLL_SECONDS = 2.0
CHUNK_COMMIT_TIMEOUT_SECONDS = 30 * 60
ADMISSION_COMMIT_POLL_SECONDS = 0.25
ADMISSION_COMMIT_TIMEOUT_SECONDS = 30 * 60
TRANSIENT_HTTP_STATUSES = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
) | EDGE_AVAILABILITY_HTTP_STATUSES
SOURCE_REPAIR_HTTP_STATUSES = frozenset({400, 413, 422})


class DeltaBaseConflict(RuntimeError):
    """The server no longer has the exact revision a tail extends."""

    def __init__(
        self,
        relative_path: str,
        *,
        expected_hash: str | None = None,
        expected_offset: int = 0,
    ) -> None:
        super().__init__(relative_path)
        self.relative_path = relative_path
        self.expected_hash = expected_hash
        self.expected_offset = max(0, int(expected_offset or 0))


def _response_diagnostic(response, endpoint: str) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    detail = f": {text[:500]}" if text else ""
    return f"{endpoint} returned HTTP {int(response.status_code)}{detail}"


def _classify_http_response(
    response,
    *,
    endpoint: str,
    retry_not_found: bool = False,
) -> UploadOutcome:
    """Map an endpoint response to one exhaustive durable disposition."""

    status = int(response.status_code)
    diagnostic = _response_diagnostic(response, endpoint)
    if 200 <= status < 300:
        return UploadOutcome.success(diagnostic)
    if status in (401, 403):
        return UploadOutcome.authentication_blocked(
            diagnostic,
            http_status=status,
        )
    if status == 404 and retry_not_found:
        return UploadOutcome.transient(
            diagnostic,
            diagnostic_code="dependency_not_ready",
            http_status=status,
        )
    if status in TRANSIENT_HTTP_STATUSES:
        return UploadOutcome.transient(
            diagnostic,
            diagnostic_code=f"http_{status}",
            http_status=status,
        )
    if status in SOURCE_REPAIR_HTTP_STATUSES:
        return UploadOutcome.source_repair(
            diagnostic,
            diagnostic_code=f"http_{status}",
            http_status=status,
        )
    return UploadOutcome.quarantine(
        diagnostic,
        diagnostic_code=f"http_{status}",
        http_status=status,
    )


class SyncClient:
    """Background worker that safely drains leased queue items."""

    def __init__(
        self,
        queue: SyncQueue,
        config: CollectorConfig,
        full_resync_callback: Callable[[str], None] | None = None,
        delta_catchup_callback: Callable[[str], None] | None = None,
        upload_synced_callback: Callable[[QueueItem], None] | None = None,
    ) -> None:
        self._queue = queue
        self._config = config
        self._running = False
        self._thread: threading.Thread | None = None
        self._pause_requested = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._full_resync_callback = full_resync_callback
        self._delta_catchup_callback = delta_catchup_callback
        self._upload_synced_callback = upload_synced_callback
        auth_identity = json.dumps(
            {
                "server_url": config.server.url.rstrip("/"),
                "server_token": config.server.token,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._auth_fingerprint = hashlib.sha256(
            auth_identity.encode("utf-8")
        ).hexdigest()
        configure_auth = getattr(queue, "configure_auth", None)
        if callable(configure_auth):
            resumed = configure_auth(self._auth_fingerprint)
            if resumed:
                logger.info(
                    "Credentials changed; resumed %d authentication-blocked upload(s)",
                    resumed,
                )
        try:
            # Not importlib.metadata: frozen sidecars report whatever stale
            # distribution happened to be installed in the build Python.
            from collector._version import __version__ as collector_version
        except Exception:
            collector_version = "dev"

        self._pool = ThreadPoolExecutor(max_workers=config.max_concurrent_uploads)
        # Receipt polling is intentionally separate from upload capacity.  An
        # accepted head must not consume the slots needed to admit its bounded
        # speculative successors during the server quiet window.
        self._receipt_pool = ThreadPoolExecutor(
            max_workers=config.max_concurrent_uploads
        )
        from .tls import SSL_CONTEXT

        self._client = httpx.Client(
            base_url=config.server.url,
            http2=True,
            timeout=httpx.Timeout(60.0, connect=10.0),
            verify=SSL_CONTEXT,
            limits=httpx.Limits(
                max_connections=max(8, config.max_concurrent_uploads * 2),
                max_keepalive_connections=max(4, config.max_concurrent_uploads),
            ),
            headers={
                "X-Collector-Token": config.server.token,
                "X-Device-Id": config.device_id,
                "X-Device-Name": config.device_name,
                "X-Device-Platform": config.platform,
                "X-Collector-Version": collector_version,
                "X-Collector-Capabilities": "realtime_ingest_async_admission_v1",
            },
        )

    def start(self) -> None:
        self._pause_requested.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sync-worker"
        )
        self._thread.start()
        logger.info("Sync client started (server: %s)", self._config.server.url)

    def stop(self) -> None:
        self._running = False
        self._pause_requested.clear()
        wake_waiters = getattr(self._queue, "wake_waiters", None)
        if callable(wake_waiters):
            wake_waiters()
        if self._thread:
            # An in-flight HTTP request has a 60-second timeout. Waiting here
            # prevents queue/client teardown from racing upload callbacks.
            self._thread.join(timeout=70)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._receipt_pool.shutdown(wait=True, cancel_futures=True)
        self._client.close()
        logger.info("Sync client stopped")

    def pause(self, timeout: float = 75) -> bool:
        """Stop claiming work and wait for the active upload batch to drain."""
        self._pause_requested.set()
        wake_waiters = getattr(self._queue, "wake_waiters", None)
        if callable(wake_waiters):
            wake_waiters()
        if not self._running:
            return True
        return self._idle.wait(timeout=timeout)

    def resume(self) -> None:
        self._pause_requested.clear()
        wake_waiters = getattr(self._queue, "wake_waiters", None)
        if callable(wake_waiters):
            wake_waiters()

    def _sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and not self._pause_requested.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.2))

    def _run(self) -> None:
        futures: dict[Future[UploadOutcome | bool], QueueItem] = {}
        receipt_futures: dict[Future[UploadOutcome | bool], QueueItem] = {}
        poll_interval = max(0.05, self._config.sync_interval)
        queue_token = getattr(self._queue, "change_token", None)
        wait_for_change = getattr(self._queue, "wait_for_change", None)
        next_deferred_delay = getattr(self._queue, "next_deferred_delay", None)
        last_claim_token = -1
        next_retry_check = 0.0

        # Keep polling while uploads are in flight. The old batch barrier waited
        # for a large archive to finish before it even looked for a newly queued
        # live delta, leaving otherwise available upload capacity unused.
        receipt_scan_token: object = object()
        while self._running or futures or receipt_futures:
            self._reap_completed(futures, receipt_futures)
            self._reap_receipt_completions(receipt_futures)

            if self._running and not self._pause_requested.is_set():
                current_receipt_token = queue_token() if callable(queue_token) else None
                accepted_items = getattr(self._queue, "accepted_receipt_items", None)
                if (
                    callable(accepted_items)
                    and current_receipt_token != receipt_scan_token
                ):
                    active_receipt_ids = {item.id for item in receipt_futures.values()}
                    for item in accepted_items():
                        if item.id in active_receipt_ids:
                            continue
                        receipt_futures[
                            self._receipt_pool.submit(self._wait_for_admission_commit, item)
                        ] = item
                    receipt_scan_token = current_receipt_token

            if not self._running:
                if futures or receipt_futures:
                    wait(
                        tuple(futures) + tuple(receipt_futures),
                        timeout=poll_interval,
                        return_when=FIRST_COMPLETED,
                    )
                continue

            if self._pause_requested.is_set():
                if futures or receipt_futures:
                    wait(
                        tuple(futures) + tuple(receipt_futures),
                        timeout=poll_interval,
                        return_when=FIRST_COMPLETED,
                    )
                else:
                    self._idle.set()
                    time.sleep(0.05)
                continue

            available_slots = self._config.max_concurrent_uploads - len(futures)
            current_token = queue_token() if callable(queue_token) else None
            should_claim = (
                available_slots > 0
                and (
                    current_token is None
                    or current_token != last_claim_token
                    or time.monotonic() >= next_retry_check
                )
            )
            items: list[QueueItem] = []
            if should_claim:
                try:
                    items = self._queue.claim_batch(
                        batch_size=min(self._config.batch_size, available_slots),
                        max_bytes=self._config.max_in_flight_bytes,
                        lease_seconds=self._config.queue_lease_seconds,
                        live_delta_reserve_bytes=self._config.max_delta_upload_bytes,
                    )
                except Exception:
                    logger.exception("Sync worker claim error")
                    items = []
                if callable(queue_token):
                    last_claim_token = queue_token()
                    if items:
                        next_retry_check = float("inf")
                    else:
                        delay = (
                            next_deferred_delay()
                            if callable(next_deferred_delay)
                            else None
                        )
                        next_retry_check = (
                            time.monotonic() + max(0.05, delay)
                            if delay is not None
                            else float("inf")
                        )
                for item in items:
                    futures[self._pool.submit(self._upload, item)] = item

            if futures or receipt_futures:
                self._idle.clear()
                wait(
                    tuple(futures) + tuple(receipt_futures),
                    timeout=poll_interval,
                    return_when=FIRST_COMPLETED,
                )
            else:
                self._idle.set()
                if callable(queue_token) and callable(wait_for_change):
                    token_before_wait = queue_token()
                    deferred_wait = (
                        max(0.05, next_retry_check - time.monotonic())
                        if next_retry_check != float("inf")
                        else 60.0
                    )
                    observed_token = wait_for_change(
                        token_before_wait,
                        timeout=min(60.0, deferred_wait),
                    )
                    if observed_token == token_before_wait:
                        # Retry deadlines and a sparse safety check do not need
                        # a producer signal, but ordinary enqueues always do.
                        next_retry_check = time.monotonic()
                else:
                    self._sleep_interruptibly(poll_interval)

        self._idle.set()

    def _reap_completed(
        self,
        futures: dict[Future[UploadOutcome | bool], QueueItem],
        receipt_futures: dict[Future[UploadOutcome | bool], QueueItem] | None = None,
    ) -> None:
        """Acknowledge completed uploads without blocking queue polling."""
        completed = [future for future in futures if future.done()]
        synced = False
        for future in completed:
            item = futures.pop(future)
            try:
                result = future.result()
                outcome = (
                    result
                    if isinstance(result, UploadOutcome)
                    else (
                        UploadOutcome.success()
                        if result
                        else UploadOutcome.transient("upload returned false")
                    )
                )
                if outcome.state is UploadOutcomeState.ACCEPTED:
                    receipt_id = outcome.receipt_id
                    marker = getattr(self._queue, "mark_accepted", None)
                    if not receipt_id or not callable(marker) or not marker(
                        item,
                        receipt_id=receipt_id,
                    ):
                        self._queue.mark_failed(
                            item,
                            "accepted receipt could not be persisted locally",
                        )
                    elif receipt_futures is not None:
                        receipt_futures[
                            self._receipt_pool.submit(
                                self._wait_for_admission_commit,
                                item,
                            )
                        ] = item
                elif outcome.state is UploadOutcomeState.SUCCESS:
                    if self._queue.mark_synced(item):
                        synced = True
                        upload_synced_callback = getattr(
                            self,
                            "_upload_synced_callback",
                            None,
                        )
                        if callable(upload_synced_callback):
                            upload_synced_callback(item)
                        if (
                            item.sync_strategy == "delta"
                            and item.source_path
                            and self._delta_catchup_callback
                        ):
                            self._delta_catchup_callback(item.source_path)
                elif (
                    outcome.state is UploadOutcomeState.SOURCE_REPAIR_REQUIRED
                    and outcome.repair_action
                    is SourceRepairAction.DELTA_BASE_CONFLICT
                ):
                    if self._queue.mark_delta_conflict(
                        item,
                        expected_hash=outcome.expected_hash,
                        expected_offset=outcome.expected_offset,
                    ):
                        self._schedule_delta_repair(item, outcome)
                elif (
                    outcome.state is UploadOutcomeState.SOURCE_REPAIR_REQUIRED
                    and outcome.repair_action
                    is SourceRepairAction.REBUILD_BOUNDED_DELTA
                ):
                    marker = getattr(self._queue, "mark_repair_scheduled", None)
                    marked = (
                        marker(item, outcome)
                        if callable(marker)
                        else self._queue.mark_failed(item, outcome.diagnostic)
                    )
                    if marked and self._full_resync_callback and item.source_path:
                        self._full_resync_callback(item.source_path)
                else:
                    marker = getattr(self._queue, "mark_upload_outcome", None)
                    if callable(marker):
                        marker(
                            item,
                            outcome,
                            auth_fingerprint=getattr(
                                self,
                                "_auth_fingerprint",
                                None,
                            ),
                        )
                    else:
                        self._queue.mark_failed(item, outcome.diagnostic)
            except Exception as exc:
                logger.exception(
                    "Upload worker failed for %s/%s",
                    item.tool_name,
                    item.relative_path,
                )
                self._queue.mark_failed(item, str(exc))
        if synced:
            self._queue.cleanup_synced()

    def _reap_receipt_completions(
        self,
        receipt_futures: dict[Future[UploadOutcome | bool], QueueItem],
    ) -> None:
        """Advance the durable cursor only from COMMITTED receipt outcomes."""
        completed = [future for future in receipt_futures if future.done()]
        synced = False

        def receipt_is_live(candidate: QueueItem) -> bool:
            predicate = getattr(self._queue, "is_accepted_receipt", None)
            return not callable(predicate) or bool(predicate(candidate))

        for future in completed:
            item = receipt_futures.pop(future)
            try:
                result = future.result()
                outcome = result if isinstance(result, UploadOutcome) else UploadOutcome.transient(
                    "receipt poll returned false"
                )
                if outcome.state is UploadOutcomeState.SUCCESS:
                    if self._queue.mark_synced(item):
                        synced = True
                        callback = getattr(self, "_upload_synced_callback", None)
                        if callable(callback):
                            callback(item)
                        if (
                            item.sync_strategy == "delta"
                            and item.source_path
                            and self._delta_catchup_callback
                        ):
                            self._delta_catchup_callback(item.source_path)
                elif (
                    outcome.state is UploadOutcomeState.SOURCE_REPAIR_REQUIRED
                    and item.sync_strategy == "delta"
                ):
                    # A terminal head receipt invalidates every speculative
                    # accepted successor.  The queue drops that chain before
                    # requesting the existing authoritative FULL recovery.
                    if self._queue.mark_delta_conflict(
                        item,
                        expected_hash=outcome.expected_hash,
                        expected_offset=outcome.expected_offset,
                    ):
                        self._schedule_delta_repair(item, outcome)
                elif outcome.state is UploadOutcomeState.TRANSIENT_RETRY:
                    # Keep ACCEPTED durable locally; a retry cannot advance or
                    # discard the committed cursor.  Re-enter receipt polling
                    # without re-uploading the retained source bytes.
                    if (
                        self._running
                        and not self._pause_requested.is_set()
                        and receipt_is_live(item)
                    ):
                        receipt_futures[
                            self._receipt_pool.submit(
                                self._wait_for_admission_commit,
                                item,
                            )
                        ] = item
                else:
                    marker = getattr(self._queue, "mark_upload_outcome", None)
                    if callable(marker):
                        marker(
                            item,
                            outcome,
                            auth_fingerprint=getattr(self, "_auth_fingerprint", None),
                        )
            except Exception as exc:
                logger.exception(
                    "Commit receipt worker failed for %s/%s",
                    item.tool_name,
                    item.relative_path,
                )
                # Do not discard an accepted payload on a local polling
                # failure.  It is retried in-place and also resumed on restart.
                if (
                    self._running
                    and not self._pause_requested.is_set()
                    and receipt_is_live(item)
                ):
                    receipt_futures[
                        self._receipt_pool.submit(
                            self._wait_for_admission_commit,
                            item,
                        )
                    ] = item
        if synced:
            self._queue.cleanup_synced()

    def _schedule_delta_repair(
        self,
        item: QueueItem,
        outcome: UploadOutcome,
    ) -> None:
        resumable = (
            bool(outcome.expected_hash)
            and outcome.expected_hash.startswith("d2:")
            and outcome.expected_offset > 0
            and getattr(self, "_delta_catchup_callback", None)
        )
        if resumable and item.source_path:
            logger.warning(
                "Delta base advanced for %s/%s; resuming from server offset %d",
                item.tool_name,
                item.relative_path,
                outcome.expected_offset,
            )
            self._delta_catchup_callback(item.source_path)
            return
        logger.warning(
            "Delta base changed for %s/%s; scheduling a complete snapshot",
            item.tool_name,
            item.relative_path,
        )
        if self._full_resync_callback and item.source_path:
            self._full_resync_callback(item.source_path)

    def _upload(self, item: QueueItem) -> UploadOutcome:
        """Upload one leased item without materializing large payloads."""
        if not self._running or self._pause_requested.is_set():
            return UploadOutcome.transient(
                "upload paused before transmission",
                diagnostic_code="upload_paused",
            )
        item_metadata = dict(item.metadata)
        force_reprocess_nonce = item_metadata.pop(
            "_queue_force_reprocess_nonce",
            None,
        )
        payload = {
            "tool": item.tool_name,
            "category": item.category,
            "content_type": item.content_type,
            "relative_path": item.relative_path,
            "hash": item.content_hash,
            "mode": "delta" if item.is_partial else "full",
            "offset": item.offset,
            "file_size": item.payload_bytes,
            "sync_strategy": item.sync_strategy,
            "metadata": item_metadata,
            # New queue rows retain the filesystem's source mtime. Rows from
            # pre-v4 queues have no such value and keep the historical enqueue
            # time fallback instead of becoming unreadable after migration.
            "timestamp": (
                item.source_modified_at
                if item.source_modified_at is not None
                else item.created_at
            ),
        }
        if item.is_partial and item.base_hash:
            payload["base_hash"] = item.base_hash
            payload["base_offset"] = item.base_offset
        if force_reprocess_nonce:
            payload["authoritative_rebase"] = True

        try:
            if item.sync_strategy == "metadata":
                # Roll out the server endpoint before this collector. Legacy
                # content endpoints intentionally reject synthetic metadata
                # rows so an older client cannot create bogus Documents.
                return self._upload_metadata(item)
            size = item.payload_bytes
            if size > MAX_CHUNKED_UPLOAD_BYTES:
                if (
                    item.sync_strategy == "delta"
                    and not item.is_partial
                    and item.source_path
                    and self._full_resync_callback
                ):
                    logger.warning(
                        "Legacy snapshot exceeds the server's %d-byte upload cap; "
                        "rebuilding in bounded windows: %s",
                        MAX_CHUNKED_UPLOAD_BYTES,
                        item.relative_path,
                    )
                    return UploadOutcome.source_repair(
                        (
                            f"payload exceeds {MAX_CHUNKED_UPLOAD_BYTES} bytes; "
                            "rebuilding a bounded delta base"
                        ),
                        diagnostic_code="payload_too_large",
                        repair_action=SourceRepairAction.REBUILD_BOUNDED_DELTA,
                    )
                logger.warning(
                    "Payload exceeds the server's %d-byte upload cap: %s (%d bytes)",
                    MAX_CHUNKED_UPLOAD_BYTES,
                    item.relative_path,
                    size,
                )
                return UploadOutcome.source_repair(
                    f"payload is {size} bytes; server limit is "
                    f"{MAX_CHUNKED_UPLOAD_BYTES}",
                    diagnostic_code="payload_too_large",
                )
            if force_reprocess_nonce:
                return self._upload_chunked(
                    payload,
                    item,
                    force_reprocess_nonce=force_reprocess_nonce,
                )
            if size <= self._config.large_file_threshold:
                payload["content"] = self._queue.read_payload_text(item)
                return self._upload_json(payload)
            if (
                item.is_partial
                and size
                <= getattr(
                    self._config,
                    "max_delta_upload_bytes",
                    16 * 1024 * 1024,
                )
            ) or size <= CHUNK_SIZE:
                with self._queue.open_payload(item) as stream:
                    return self._upload_multipart(payload, stream)
            return self._upload_chunked(payload, item)

        except DeltaBaseConflict as conflict:
            return UploadOutcome.source_repair(
                "server rejected the guarded delta base",
                diagnostic_code="delta_base_mismatch",
                http_status=409,
                repair_action=SourceRepairAction.DELTA_BASE_CONFLICT,
                expected_hash=conflict.expected_hash,
                expected_offset=conflict.expected_offset,
            )
        except httpx.TransportError as exc:
            logger.warning("Server unreachable, will retry later")
            return UploadOutcome.transient(
                f"{type(exc).__name__}: {exc}",
                diagnostic_code="network_error",
            )
        except (OSError, UnicodeError) as exc:
            logger.error(
                "Local payload unavailable for %s/%s: %s",
                item.tool_name,
                item.relative_path,
                exc,
            )
            return UploadOutcome.source_repair(
                f"{type(exc).__name__}: {exc}",
                diagnostic_code="local_payload_unavailable",
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error(
                "Invalid upload metadata for %s/%s: %s",
                item.tool_name,
                item.relative_path,
                exc,
            )
            return UploadOutcome.source_repair(
                f"{type(exc).__name__}: {exc}",
                diagnostic_code="invalid_metadata",
            )
        except Exception as exc:
            logger.exception(
                "Upload error for %s/%s", item.tool_name, item.relative_path
            )
            return UploadOutcome.quarantine(
                f"{type(exc).__name__}: {exc}",
                diagnostic_code="unexpected_client_error",
            )

    def _upload_metadata(self, item: QueueItem) -> UploadOutcome:
        """Send a durable metadata-only update without reading file content."""
        payload = {
            key: value
            for key, value in item.metadata.items()
            if not key.startswith("_queue_")
        }
        resp = self._client.post("/api/ingest/metadata", json=payload)
        return _classify_http_response(
            resp,
            endpoint="/api/ingest/metadata",
            # A collector may be upgraded before the endpoint, and older
            # servers used 404 while the referenced transcript was not ready.
            retry_not_found=True,
        )

    def _upload_json(self, payload: dict) -> UploadOutcome:
        resp = self._client.post("/api/ingest/file", json=payload)
        self._raise_delta_conflict(resp, payload)
        admission = self._accepted_admission_outcome(resp, "/api/ingest/file")
        if admission is not None:
            return admission
        return _classify_http_response(
            resp,
            endpoint="/api/ingest/file",
        )

    def _upload_multipart(
        self,
        payload: dict,
        content_stream: BinaryIO,
    ) -> UploadOutcome:
        resp = self._client.post(
            "/api/ingest/file/upload",
            data={"metadata": json.dumps(payload)},
            files={"content": ("content.txt", content_stream, "text/plain")},
        )
        self._raise_delta_conflict(resp, payload)
        admission = self._accepted_admission_outcome(
            resp,
            "/api/ingest/file/upload",
        )
        if admission is not None:
            return admission
        return _classify_http_response(
            resp,
            endpoint="/api/ingest/file/upload",
        )

    @staticmethod
    def _accepted_admission_outcome(response, endpoint: str) -> UploadOutcome | None:
        """Decode the new ACCEPTED receipt without changing old-server success."""
        if not 200 <= int(response.status_code) < 300:
            return None
        try:
            payload = response.json()
        except (AttributeError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("status") != "accepted":
            return None
        receipt_id = payload.get("receipt_id")
        if not isinstance(receipt_id, str) or len(receipt_id) != 64:
            return UploadOutcome.quarantine(
                f"{endpoint} returned ACCEPTED without a valid receipt id",
                diagnostic_code="invalid_admission_receipt",
            )
        return UploadOutcome.accepted(receipt_id, "server durably accepted delta")

    def _wait_for_admission_commit(self, item: QueueItem) -> UploadOutcome:
        """Poll an ACCEPTED receipt without treating it as a committed base."""
        if not item.receipt_id:
            return UploadOutcome.quarantine(
                "accepted queue item has no receipt id",
                diagnostic_code="invalid_admission_receipt",
            )
        deadline = time.monotonic() + ADMISSION_COMMIT_TIMEOUT_SECONDS
        while self._running and not self._pause_requested.is_set():
            try:
                response = self._client.post(
                    "/api/ingest/file/receipt/status",
                    json={"receipt_id": item.receipt_id},
                )
            except httpx.TransportError as exc:
                logger.debug("Commit receipt unavailable for %s: %s", item.relative_path, exc)
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except (AttributeError, TypeError, ValueError):
                        return UploadOutcome.quarantine(
                            "receipt status endpoint returned invalid JSON",
                            diagnostic_code="invalid_commit_status",
                        )
                    status = payload.get("status") if isinstance(payload, dict) else None
                    returned_receipt = payload.get("receipt_id") if isinstance(payload, dict) else None
                    if returned_receipt not in (None, item.receipt_id):
                        return UploadOutcome.quarantine(
                            "receipt status identity changed",
                            diagnostic_code="commit_identity_mismatch",
                        )
                    if status == "committed":
                        return UploadOutcome.success("admitted delta committed")
                    if status in {"failed", "blocked", "missing"}:
                        error_type = str(payload.get("error_type") or "unknown")
                        return UploadOutcome.source_repair(
                            f"server receipt is {status} ({error_type})",
                            diagnostic_code=(
                                "delta_base_mismatch"
                                if error_type == "DeltaBaseMismatch"
                                else f"receipt_{status}"
                            ),
                            repair_action=SourceRepairAction.DELTA_BASE_CONFLICT,
                        )
                    if status not in {"accepted", "receiving"}:
                        return UploadOutcome.quarantine(
                            f"unknown receipt status: {status!r}",
                            diagnostic_code="invalid_commit_status",
                        )
                else:
                    outcome = _classify_http_response(
                        response,
                        endpoint="/api/ingest/file/receipt/status",
                    )
                    if outcome.state is not UploadOutcomeState.TRANSIENT_RETRY:
                        return outcome
            if time.monotonic() >= deadline:
                return UploadOutcome.transient(
                    "timed out awaiting admitted delta commit",
                    diagnostic_code="commit_timeout",
                )
            self._sleep_interruptibly(ADMISSION_COMMIT_POLL_SECONDS)
        return UploadOutcome.transient(
            "admitted delta commit polling paused",
            diagnostic_code="upload_paused",
        )

    def _upload_chunked(
        self,
        payload: dict,
        item: QueueItem,
        *,
        force_reprocess_nonce: str | None = None,
    ) -> UploadOutcome:
        """Stream a large spool file in fixed-size chunks."""
        total_size = item.payload_bytes
        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        upload_id = (
            f"{payload['tool']}/{payload['relative_path']}/{payload['hash'][:8]}"
        )
        if force_reprocess_nonce:
            upload_id = f"{upload_id}/repair-{force_reprocess_nonce}"

        logger.info(
            "Chunked upload: %s (%d bytes, %d chunks)",
            payload["relative_path"],
            total_size,
            total_chunks,
        )

        commit_job_id: str | None = None
        with self._queue.open_payload(item) as stream:
            for index in range(total_chunks):
                if not self._running or self._pause_requested.is_set():
                    return UploadOutcome.transient(
                        "chunk upload paused",
                        diagnostic_code="upload_paused",
                    )

                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    logger.warning("Payload ended early: %s", payload["relative_path"])
                    return UploadOutcome.source_repair(
                        "spooled payload ended before its recorded byte length",
                        diagnostic_code="payload_truncated",
                    )
                chunk_meta = {
                    **payload,
                    "chunk_index": index,
                    "total_chunks": total_chunks,
                    "upload_id": upload_id,
                }
                encoded_meta = json.dumps(chunk_meta)

                for attempt in range(1, CHUNK_UPLOAD_MAX_ATTEMPTS + 1):
                    if not self._running or self._pause_requested.is_set():
                        return UploadOutcome.transient(
                            "chunk upload paused",
                            diagnostic_code="upload_paused",
                        )
                    if not self._queue.renew_lease(
                        item,
                        lease_seconds=self._config.queue_lease_seconds,
                    ):
                        logger.warning(
                            "Lease lost during upload: %s",
                            payload["relative_path"],
                        )
                        return UploadOutcome.transient(
                            "queue lease was lost during chunk upload",
                            diagnostic_code="lease_lost",
                        )

                    retry_outcome: UploadOutcome | None = None
                    try:
                        resp = self._client.post(
                            "/api/ingest/file/chunk",
                            data={"metadata": encoded_meta},
                            files={
                                "content": (
                                    f"chunk_{index}.txt",
                                    chunk,
                                    "text/plain",
                                ),
                            },
                        )
                    except httpx.TransportError as exc:
                        retry_outcome = UploadOutcome.transient(
                            f"{type(exc).__name__}: {exc}",
                            diagnostic_code="network_error",
                        )
                    else:
                        if resp.status_code == 409 and payload.get("mode") == "delta":
                            raise DeltaBaseConflict(payload["relative_path"])
                        response_outcome = _classify_http_response(
                            resp,
                            endpoint="/api/ingest/file/chunk",
                        )
                        if response_outcome.succeeded:
                            if index == total_chunks - 1:
                                try:
                                    document_id = resp.json().get("document_id", "")
                                except (AttributeError, TypeError, ValueError):
                                    document_id = ""
                                if isinstance(
                                    document_id, str
                                ) and document_id.startswith("queued:"):
                                    commit_job_id = document_id.removeprefix("queued:")
                            break
                        if (
                            response_outcome.state
                            is UploadOutcomeState.TRANSIENT_RETRY
                        ):
                            retry_outcome = response_outcome
                        else:
                            logger.warning(
                                "Chunk %d/%d stopped (%s) for %s",
                                index + 1,
                                total_chunks,
                                response_outcome.state.value,
                                payload["relative_path"],
                            )
                            return response_outcome

                    if attempt >= CHUNK_UPLOAD_MAX_ATTEMPTS:
                        logger.warning(
                            "Chunk %d/%d exhausted %d attempts (%s) for %s",
                            index + 1,
                            total_chunks,
                            CHUNK_UPLOAD_MAX_ATTEMPTS,
                            (
                                retry_outcome.diagnostic
                                if retry_outcome is not None
                                else "transient failure"
                            ),
                            payload["relative_path"],
                        )
                        return retry_outcome or UploadOutcome.transient(
                            "chunk attempts exhausted",
                            diagnostic_code="chunk_attempts_exhausted",
                        )

                    delay = min(
                        CHUNK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                        CHUNK_RETRY_MAX_SECONDS,
                    )
                    logger.warning(
                        "Chunk %d/%d retry %d/%d in %.1fs (%s) for %s",
                        index + 1,
                        total_chunks,
                        attempt + 1,
                        CHUNK_UPLOAD_MAX_ATTEMPTS,
                        delay,
                        (
                            retry_outcome.diagnostic
                            if retry_outcome is not None
                            else "transient failure"
                        ),
                        payload["relative_path"],
                    )
                    self._sleep_interruptibly(delay)
                    if not self._running or self._pause_requested.is_set():
                        return UploadOutcome.transient(
                            "chunk upload paused during retry backoff",
                            diagnostic_code="upload_paused",
                        )

        if commit_job_id is not None:
            return self._wait_for_chunk_commit(
                payload,
                item,
                upload_id=upload_id,
                job_id=commit_job_id,
            )

        # Compatibility with servers predating explicit commit status. Their
        # final response has no queued job identifier, so retain the historical
        # durable-acceptance behavior until the server is upgraded.
        logger.info("Chunked upload accepted: %s", payload["relative_path"])
        return UploadOutcome.success("legacy server durably accepted all chunks")

    def _wait_for_chunk_commit(
        self,
        payload: dict,
        item: QueueItem,
        *,
        upload_id: str,
        job_id: str,
    ) -> UploadOutcome:
        """Poll a durably accepted upload until its database commit is known."""
        deadline = time.monotonic() + CHUNK_COMMIT_TIMEOUT_SECONDS
        while self._running and not self._pause_requested.is_set():
            if not self._queue.renew_lease(
                item,
                lease_seconds=self._config.queue_lease_seconds,
            ):
                logger.warning(
                    "Lease lost while awaiting commit: %s",
                    payload["relative_path"],
                )
                return UploadOutcome.transient(
                    "queue lease was lost while awaiting chunk commit",
                    diagnostic_code="lease_lost",
                )
            try:
                response = self._client.post(
                    "/api/ingest/file/chunk/status",
                    json={"upload_id": upload_id, "hash": payload["hash"]},
                )
            except httpx.TransportError as exc:
                logger.warning(
                    "Commit status unavailable for %s: %s",
                    payload["relative_path"],
                    exc,
                )
            else:
                if response.status_code == 200:
                    try:
                        status_payload = response.json()
                    except (AttributeError, TypeError, ValueError):
                        return UploadOutcome.quarantine(
                            "chunk status endpoint returned invalid JSON",
                            diagnostic_code="invalid_commit_status",
                        )
                    if not isinstance(status_payload, dict):
                        return UploadOutcome.quarantine(
                            "chunk status endpoint returned a non-object payload",
                            diagnostic_code="invalid_commit_status",
                        )
                    status = status_payload.get("status")
                    returned_job_id = status_payload.get("job_id")
                    if returned_job_id not in (None, job_id):
                        logger.warning(
                            "Commit status identity changed for %s",
                            payload["relative_path"],
                        )
                        return UploadOutcome.quarantine(
                            "chunk commit job identity changed",
                            diagnostic_code="commit_identity_mismatch",
                        )
                    if status == "completed":
                        logger.info(
                            "Chunked upload committed: %s",
                            payload["relative_path"],
                        )
                        return UploadOutcome.success("chunk upload committed")
                    if status in {"failed", "blocked"}:
                        error_type = str(
                            status_payload.get("error_type") or "unknown"
                        )
                        logger.warning(
                            "Chunked upload %s on the server (%s): %s",
                            status,
                            error_type,
                            payload["relative_path"],
                        )
                        if (
                            payload.get("mode") == "delta"
                            and error_type == "DeltaBaseMismatch"
                        ):
                            raise DeltaBaseConflict(payload["relative_path"])
                        return UploadOutcome.quarantine(
                            f"server chunk commit is {status} ({error_type})",
                            diagnostic_code=f"commit_{status}",
                        )
                    if status == "missing":
                        logger.warning(
                            "Server lost accepted chunk upload %s: %s",
                            job_id,
                            payload["relative_path"],
                        )
                        return UploadOutcome.source_repair(
                            "server lost the durably accepted chunk upload",
                            diagnostic_code="commit_missing",
                        )
                    if status not in {"pending", "receiving"}:
                        return UploadOutcome.quarantine(
                            f"unknown chunk commit status: {status!r}",
                            diagnostic_code="invalid_commit_status",
                        )
                else:
                    if response.status_code == 404:
                        return UploadOutcome.source_repair(
                            _response_diagnostic(
                                response,
                                "/api/ingest/file/chunk/status",
                            ),
                            diagnostic_code="commit_status_missing",
                            http_status=404,
                        )
                    response_outcome = _classify_http_response(
                        response,
                        endpoint="/api/ingest/file/chunk/status",
                    )
                    if (
                        response_outcome.state
                        is not UploadOutcomeState.TRANSIENT_RETRY
                    ):
                        return response_outcome

            if time.monotonic() >= deadline:
                logger.warning(
                    "Timed out awaiting database commit for %s",
                    payload["relative_path"],
                )
                return UploadOutcome.transient(
                    "timed out awaiting server chunk commit",
                    diagnostic_code="commit_timeout",
                )
            self._sleep_interruptibly(CHUNK_COMMIT_POLL_SECONDS)
        return UploadOutcome.transient(
            "chunk commit polling paused",
            diagnostic_code="upload_paused",
        )

    @staticmethod
    def _raise_delta_conflict(response, payload: dict) -> None:
        if response.status_code == 409 and payload.get("mode") == "delta":
            expected_hash = None
            expected_offset = 0
            try:
                body = response.json()
                detail = body.get("detail") if isinstance(body, dict) else None
                if isinstance(detail, dict):
                    raw_hash = detail.get("expected_hash")
                    if isinstance(raw_hash, str) and raw_hash:
                        expected_hash = raw_hash
                    expected_offset = max(
                        0,
                        int(detail.get("expected_offset") or 0),
                    )
            except (TypeError, ValueError, AttributeError):
                pass
            raise DeltaBaseConflict(
                payload["relative_path"],
                expected_hash=expected_hash,
                expected_offset=expected_offset,
            )

    @property
    def is_connected(self) -> bool:
        try:
            resp = self._client.get("/api/ingest/status", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False
