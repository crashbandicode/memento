"""HTTP queue drain with byte-bounded leases and streaming large uploads."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import BinaryIO, Callable

import httpx

from .config import CollectorConfig
from .queue import QueueItem, SyncQueue

logger = logging.getLogger("collector.sync")

CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB per chunk
CHUNK_UPLOAD_MAX_ATTEMPTS = 4
CHUNK_RETRY_BASE_SECONDS = 0.5
CHUNK_RETRY_MAX_SECONDS = 4.0


class DeltaBaseConflict(RuntimeError):
    """The server no longer has the exact revision a tail extends."""


class SyncClient:
    """Background worker that safely drains leased queue items."""

    def __init__(
        self,
        queue: SyncQueue,
        config: CollectorConfig,
        full_resync_callback: Callable[[str], None] | None = None,
        delta_catchup_callback: Callable[[str], None] | None = None,
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
        try:
            from importlib.metadata import version
            collector_version = version("memento-brain-collector")
        except Exception:
            collector_version = "dev"

        self._pool = ThreadPoolExecutor(max_workers=config.max_concurrent_uploads)
        from .tls import SSL_CONTEXT
        self._client = httpx.Client(
            base_url=config.server.url,
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
            },
        )

    def start(self) -> None:
        self._pause_requested.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="sync-worker")
        self._thread.start()
        logger.info("Sync client started (server: %s)", self._config.server.url)

    def stop(self) -> None:
        self._running = False
        self._pause_requested.clear()
        if self._thread:
            # An in-flight HTTP request has a 60-second timeout. Waiting here
            # prevents queue/client teardown from racing upload callbacks.
            self._thread.join(timeout=70)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._client.close()
        logger.info("Sync client stopped")

    def pause(self, timeout: float = 75) -> bool:
        """Stop claiming work and wait for the active upload batch to drain."""
        self._pause_requested.set()
        if not self._running:
            return True
        return self._idle.wait(timeout=timeout)

    def resume(self) -> None:
        self._pause_requested.clear()

    def _sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and not self._pause_requested.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.2))

    def _run(self) -> None:
        futures: dict[Future[bool], QueueItem] = {}
        poll_interval = max(0.05, self._config.sync_interval)

        # Keep polling while uploads are in flight. The old batch barrier waited
        # for a large archive to finish before it even looked for a newly queued
        # live delta, leaving otherwise available upload capacity unused.
        while self._running or futures:
            self._reap_completed(futures)

            if not self._running:
                if futures:
                    wait(tuple(futures), timeout=poll_interval,
                         return_when=FIRST_COMPLETED)
                continue

            if self._pause_requested.is_set():
                if futures:
                    wait(tuple(futures), timeout=poll_interval,
                         return_when=FIRST_COMPLETED)
                else:
                    self._idle.set()
                    time.sleep(0.05)
                continue

            available_slots = self._config.max_concurrent_uploads - len(futures)
            if available_slots > 0:
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
                for item in items:
                    futures[self._pool.submit(self._upload, item)] = item

            if futures:
                self._idle.clear()
                wait(tuple(futures), timeout=poll_interval,
                     return_when=FIRST_COMPLETED)
            else:
                self._idle.set()
                self._sleep_interruptibly(poll_interval)

        self._idle.set()

    def _reap_completed(
        self,
        futures: dict[Future[bool], QueueItem],
    ) -> None:
        """Acknowledge completed uploads without blocking queue polling."""
        completed = [future for future in futures if future.done()]
        synced = False
        for future in completed:
            item = futures.pop(future)
            try:
                if future.result():
                    if self._queue.mark_synced(item):
                        synced = True
                        if (
                            item.is_partial
                            and item.source_path
                            and self._delta_catchup_callback
                        ):
                            self._delta_catchup_callback(item.source_path)
                else:
                    self._queue.mark_failed(item, "upload returned false")
            except Exception as exc:
                logger.exception(
                    "Upload worker failed for %s/%s",
                    item.tool_name, item.relative_path,
                )
                self._queue.mark_failed(item, str(exc))
        if synced:
            self._queue.cleanup_synced()

    def _upload(self, item: QueueItem) -> bool:
        """Upload one leased item without materializing large payloads."""
        if not self._running or self._pause_requested.is_set():
            return False
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

        try:
            if item.sync_strategy == "metadata":
                # Roll out the server endpoint before this collector. Legacy
                # content endpoints intentionally reject synthetic metadata
                # rows so an older client cannot create bogus Documents.
                return self._upload_metadata(item)
            size = item.payload_bytes
            if size <= self._config.large_file_threshold:
                payload["content"] = self._queue.read_payload_text(item)
                return self._upload_json(payload)
            if (
                item.is_partial
                and size <= getattr(
                    self._config,
                    "max_delta_upload_bytes",
                    16 * 1024 * 1024,
                )
            ) or size <= CHUNK_SIZE:
                with self._queue.open_payload(item) as stream:
                    return self._upload_multipart(payload, stream)
            if force_reprocess_nonce:
                return self._upload_chunked(
                    payload,
                    item,
                    force_reprocess_nonce=force_reprocess_nonce,
                )
            return self._upload_chunked(payload, item)

        except DeltaBaseConflict:
            if not self._queue.mark_delta_conflict(item):
                logger.warning(
                    "Delta base conflict could not retire queue item %s/%s",
                    item.tool_name,
                    item.relative_path,
                )
                return False
            logger.warning(
                "Delta base changed for %s/%s; scheduling a complete snapshot",
                item.tool_name,
                item.relative_path,
            )
            if self._full_resync_callback and item.source_path:
                self._full_resync_callback(item.source_path)
            return True
        except httpx.ConnectError:
            logger.warning("Server unreachable, will retry later")
            return False
        except httpx.TimeoutException:
            logger.warning(
                "Upload timeout for %s/%s (%d bytes)",
                item.tool_name, item.relative_path, item.payload_bytes,
            )
            return False
        except Exception:
            logger.exception("Upload error for %s/%s", item.tool_name, item.relative_path)
            return False

    def _upload_metadata(self, item: QueueItem) -> bool:
        """Send a durable metadata-only update without reading file content."""
        payload = {
            key: value
            for key, value in item.metadata.items()
            if not key.startswith("_queue_")
        }
        resp = self._client.post("/api/ingest/metadata", json=payload)
        if resp.status_code in (200, 201):
            return True
        logger.warning(
            "Server %s for metadata %s/%s: %s",
            resp.status_code, item.tool_name, item.relative_path, resp.text[:200],
        )
        return False

    def _upload_json(self, payload: dict) -> bool:
        resp = self._client.post("/api/ingest/file", json=payload)
        self._raise_delta_conflict(resp, payload)
        if resp.status_code in (200, 201):
            return True
        logger.warning(
            "Server %s for %s/%s: %s",
            resp.status_code, payload["tool"], payload["relative_path"], resp.text[:200],
        )
        return False

    def _upload_multipart(self, payload: dict, content_stream: BinaryIO) -> bool:
        resp = self._client.post(
            "/api/ingest/file/upload",
            data={"metadata": json.dumps(payload)},
            files={"content": ("content.txt", content_stream, "text/plain")},
        )
        self._raise_delta_conflict(resp, payload)
        if resp.status_code in (200, 201):
            return True
        logger.warning(
            "Server %s for multipart %s/%s",
            resp.status_code, payload["tool"], payload["relative_path"],
        )
        return False

    def _upload_chunked(
        self,
        payload: dict,
        item: QueueItem,
        *,
        force_reprocess_nonce: str | None = None,
    ) -> bool:
        """Stream a large spool file in fixed-size chunks."""
        total_size = item.payload_bytes
        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        upload_id = f"{payload['tool']}/{payload['relative_path']}/{payload['hash'][:8]}"
        if force_reprocess_nonce:
            upload_id = f"{upload_id}/repair-{force_reprocess_nonce}"

        logger.info(
            "Chunked upload: %s (%d bytes, %d chunks)",
            payload["relative_path"], total_size, total_chunks,
        )

        with self._queue.open_payload(item) as stream:
            for index in range(total_chunks):
                if not self._running or self._pause_requested.is_set():
                    return False

                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    logger.warning("Payload ended early: %s", payload["relative_path"])
                    return False
                chunk_meta = {
                    **payload,
                    "chunk_index": index,
                    "total_chunks": total_chunks,
                    "upload_id": upload_id,
                }
                encoded_meta = json.dumps(chunk_meta)

                for attempt in range(1, CHUNK_UPLOAD_MAX_ATTEMPTS + 1):
                    if not self._running or self._pause_requested.is_set():
                        return False
                    if not self._queue.renew_lease(
                        item, lease_seconds=self._config.queue_lease_seconds,
                    ):
                        logger.warning(
                            "Lease lost during upload: %s", payload["relative_path"],
                        )
                        return False

                    retry_reason: str | None = None
                    try:
                        resp = self._client.post(
                            "/api/ingest/file/chunk",
                            data={"metadata": encoded_meta},
                            files={
                                "content": (
                                    f"chunk_{index}.txt", chunk, "text/plain",
                                ),
                            },
                        )
                    except httpx.TransportError as exc:
                        retry_reason = f"{type(exc).__name__}: {exc}"
                    else:
                        if resp.status_code in (200, 201):
                            break
                        if resp.status_code == 409 and payload.get("mode") == "delta":
                            raise DeltaBaseConflict(payload["relative_path"])
                        if resp.status_code == 429 or 500 <= resp.status_code < 600:
                            retry_reason = f"HTTP {resp.status_code}"
                        else:
                            logger.warning(
                                "Chunk %d/%d failed permanently (%s) for %s",
                                index + 1, total_chunks, resp.status_code,
                                payload["relative_path"],
                            )
                            return False

                    if attempt >= CHUNK_UPLOAD_MAX_ATTEMPTS:
                        logger.warning(
                            "Chunk %d/%d exhausted %d attempts (%s) for %s",
                            index + 1, total_chunks, CHUNK_UPLOAD_MAX_ATTEMPTS,
                            retry_reason, payload["relative_path"],
                        )
                        return False

                    delay = min(
                        CHUNK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                        CHUNK_RETRY_MAX_SECONDS,
                    )
                    logger.warning(
                        "Chunk %d/%d retry %d/%d in %.1fs (%s) for %s",
                        index + 1, total_chunks, attempt + 1,
                        CHUNK_UPLOAD_MAX_ATTEMPTS, delay, retry_reason,
                        payload["relative_path"],
                    )
                    self._sleep_interruptibly(delay)
                    if not self._running or self._pause_requested.is_set():
                        return False

        logger.info("Chunked upload complete: %s", payload["relative_path"])
        return True

    @staticmethod
    def _raise_delta_conflict(response, payload: dict) -> None:
        if response.status_code == 409 and payload.get("mode") == "delta":
            raise DeltaBaseConflict(payload["relative_path"])

    @property
    def is_connected(self) -> bool:
        try:
            resp = self._client.get("/api/ingest/status", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False
