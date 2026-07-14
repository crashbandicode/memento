"""Durable, bounded-memory upload queue for the collector.

Large payloads live in immutable spool files rather than SQLite. Queue claims are
metadata-only and leased, so loading a batch cannot materialize several complete
conversation histories in RAM. Complete snapshots and adjacent pending DELTAs
coalesce while an immutable in-flight revision retains its lease.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, BinaryIO, Iterator


def _rollback_on_error(method):
    """Keep the shared connection usable if any SQLite write/commit fails."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        # RLock makes this an outer transaction-safety boundary while the
        # method's existing lock scopes remain valid and explicit.
        with self._lock:
            try:
                return method(self, *args, **kwargs)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
    return wrapped


@dataclass
class QueueItem:
    id: int
    tool_name: str
    category: str
    content_type: str
    relative_path: str
    content: str | None
    content_hash: str
    file_size: int
    sync_strategy: str
    is_partial: bool
    offset: int
    metadata: dict[str, Any]
    created_at: float
    source_modified_at: float | None = None
    base_hash: str | None = None
    base_offset: int = 0
    source_path: str | None = None
    retry_count: int = 0
    payload_path: str | None = None
    payload_bytes: int = 0
    lease_token: str | None = None


def _metadata_state_value(record: dict[str, Any], title: str) -> str:
    kind = str(record.get("title_kind") or "").strip().lower()
    if kind not in {"custom", "fallback"}:
        return title
    return json.dumps(
        {"title": title, "title_kind": kind},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_metadata_state_value(value: object) -> tuple[str, str]:
    raw = str(value or "")
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw, "unknown"
    if not isinstance(decoded, dict):
        return raw, "unknown"
    title = str(decoded.get("title") or "").strip()
    kind = str(decoded.get("title_kind") or "").strip().lower()
    if kind not in {"custom", "fallback"}:
        kind = "unknown"
    return title, kind


class SyncQueue:
    """Persistent SQLite metadata queue with immutable large-payload spooling."""

    SCHEMA_VERSION = 5

    def __init__(self, db_path: Path, spool_threshold: int = 4 * 1024 * 1024) -> None:
        self._db_path = db_path
        self._spool_threshold = max(64 * 1024, spool_threshold)
        self._spool_dir = db_path.parent / "spool"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._remove_orphaned_spool_files()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                category TEXT NOT NULL,
                content_type TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                sync_strategy TEXT NOT NULL,
                is_partial INTEGER NOT NULL DEFAULT 0,
                offset INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                source_modified_at REAL,
                base_hash TEXT,
                base_offset INTEGER NOT NULL DEFAULT 0,
                source_path TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                payload_path TEXT,
                payload_bytes INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT,
                lease_until REAL,
                available_at REAL NOT NULL DEFAULT 0,
                last_attempt_at REAL,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS file_state (
                tool_name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                last_hash TEXT,
                last_offset INTEGER NOT NULL DEFAULT 0,
                last_synced_at REAL,
                observed_hash TEXT,
                observed_offset INTEGER NOT NULL DEFAULT 0,
                observed_at REAL,
                synced_hash TEXT,
                synced_offset INTEGER NOT NULL DEFAULT 0,
                synced_at REAL,
                PRIMARY KEY (tool_name, relative_path)
            );
            CREATE TABLE IF NOT EXISTS metadata_state (
                namespace TEXT NOT NULL,
                item_key TEXT NOT NULL,
                observed_value TEXT NOT NULL,
                synced_value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (namespace, item_key)
            );
        """)

        # ALTER is intentionally additive so a v1 database remains readable by
        # this release. No payload-sized rewrite or startup VACUUM is performed.
        queue_columns = self._column_names("queue")
        queue_additions = {
            "payload_path": "TEXT",
            "payload_bytes": "INTEGER NOT NULL DEFAULT 0",
            "lease_token": "TEXT",
            "lease_until": "REAL",
            "available_at": "REAL NOT NULL DEFAULT 0",
            "last_attempt_at": "REAL",
            "last_error": "TEXT",
            # Kept nullable for queues created by older collectors. Those rows
            # retain their original enqueue-time fallback on upload.
            "source_modified_at": "REAL",
            "base_hash": "TEXT",
            "base_offset": "INTEGER NOT NULL DEFAULT 0",
            "source_path": "TEXT",
        }
        for name, definition in queue_additions.items():
            if name not in queue_columns:
                self._conn.execute(f"ALTER TABLE queue ADD COLUMN {name} {definition}")

        state_columns = self._column_names("file_state")
        state_additions = {
            "observed_hash": "TEXT",
            "observed_offset": "INTEGER NOT NULL DEFAULT 0",
            "observed_at": "REAL",
            "synced_hash": "TEXT",
            "synced_offset": "INTEGER NOT NULL DEFAULT 0",
            "synced_at": "REAL",
        }
        for name, definition in state_additions.items():
            if name not in state_columns:
                self._conn.execute(f"ALTER TABLE file_state ADD COLUMN {name} {definition}")

        self._conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_queue_status
                ON queue(status, available_at, created_at);
            CREATE INDEX IF NOT EXISTS idx_queue_path_status
                ON queue(tool_name, relative_path, status, id);
        """)
        # Reclaim only expired leases. A second collector process may be alive
        # against the same queue, so startup must not invalidate its work.
        self._conn.execute(
            """UPDATE queue SET status='pending', lease_token=NULL, lease_until=NULL
               WHERE status='uploading' AND COALESCE(lease_until, 0) <= ?""",
            (time.time(),),
        )
        # Older releases dead-lettered ordinary network failures after ten
        # attempts. Restore those rows: offline-resilient sync must keep trying.
        self._conn.execute(
            """UPDATE queue SET status='pending', retry_count=0, available_at=0,
                       lease_token=NULL, lease_until=NULL
               WHERE status='dead'"""
        )
        self._conn.execute(
            """UPDATE queue AS old SET status='superseded'
               WHERE old.status='pending' AND old.is_partial=0
                 AND old.sync_strategy IN ('full','delta')
                 AND EXISTS (
                    SELECT 1 FROM queue AS newer
                    WHERE newer.tool_name=old.tool_name
                      AND newer.relative_path=old.relative_path
                      AND newer.status='pending'
                      AND newer.is_partial=0
                      AND newer.sync_strategy IN ('full','delta')
                      AND newer.id > old.id
                 )"""
        )
        self._conn.execute(
            """UPDATE file_state
               SET observed_hash=COALESCE(observed_hash, last_hash),
                   observed_offset=CASE
                       WHEN observed_at IS NULL THEN last_offset
                       ELSE observed_offset
                   END,
                   observed_at=COALESCE(observed_at, last_synced_at)
               WHERE observed_hash IS NULL"""
        )
        self._conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
        self._conn.commit()

    def _protected_custom_title_locked(
        self,
        *,
        tool_name: str,
        relative_path: str,
        incoming_fallback: str,
        state_values: tuple[object, ...],
    ) -> str | None:
        """Recover the latest durable custom title before accepting a fallback."""
        for value in state_values:
            title, kind = _decode_metadata_state_value(value)
            if title and (
                kind == "custom"
                or (kind == "unknown" and title != incoming_fallback)
            ):
                return title

        # Upgrade recovery: pre-title-kind collectors retained queue metadata
        # for synced/superseded rows. This lets the first upgraded poll recover
        # a custom title even if an auto fallback was already acknowledged.
        rows = self._conn.execute(
            """SELECT metadata FROM queue
               WHERE tool_name=? AND relative_path=?
                 AND sync_strategy='metadata'
               ORDER BY id DESC LIMIT 50""",
            (tool_name, relative_path),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(str(row[0]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            title = str(metadata.get("title") or "").strip()
            kind = str(metadata.get("title_kind") or "unknown").strip().lower()
            if title and title != incoming_fallback and kind != "fallback":
                return title
        return None

    @_rollback_on_error
    def enqueue_metadata_changes(
        self,
        *,
        namespace: str,
        tool_name: str,
        records: dict[str, dict[str, Any]],
    ) -> int:
        """Durably coalesce changed lightweight metadata into the upload queue.

        The first observation is intentionally unsynced and therefore queued.
        Codex polling excludes subagents, while the server rejects injected
        wrapper titles, so this safe catch-up also repairs renames made before
        the collector was installed or while its queue database was absent.
        Once a custom Codex title is durable, an automatic first-prompt fallback
        is suppressed locally and cannot replace the queued/synced custom value.
        ``synced_value`` advances only after server acknowledgement, so restarts
        and force-resync cannot lose an update.
        """
        now = time.time()
        queued = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            for item_key, source_record in records.items():
                record = dict(source_record)
                current_title = str(record.get("title") or "").strip()
                if not current_title:
                    continue

                path_key = hashlib.sha256(item_key.encode("utf-8")).hexdigest()
                relative_path = f"__metadata__/{namespace}/{path_key}"

                state_row = self._conn.execute(
                    """SELECT observed_value, synced_value
                       FROM metadata_state
                       WHERE namespace=? AND item_key=?""",
                    (namespace, item_key),
                ).fetchone()
                state_values = tuple(state_row) if state_row is not None else ()
                if str(record.get("title_kind") or "").lower() == "fallback":
                    protected_title = self._protected_custom_title_locked(
                        tool_name=tool_name,
                        relative_path=relative_path,
                        incoming_fallback=current_title,
                        state_values=state_values,
                    )
                    if protected_title:
                        current_title = protected_title
                        record["title"] = protected_title
                        record["title_kind"] = "custom"

                current_value = _metadata_state_value(record, current_title)
                if state_row is None:
                    self._conn.execute(
                        """INSERT INTO metadata_state (
                               namespace, item_key, observed_value,
                               synced_value, updated_at
                           ) VALUES (?,?,?,?,?)""",
                        (namespace, item_key, current_value, "", now),
                    )
                    observed_value, synced_value = current_value, ""
                else:
                    observed_value, synced_value = (
                        str(state_row[0]),
                        str(state_row[1]),
                    )
                if state_row is not None and observed_value != current_value:
                    self._conn.execute(
                        """UPDATE metadata_state
                           SET observed_value=?, updated_at=?
                           WHERE namespace=? AND item_key=?""",
                        (current_value, now, namespace, item_key),
                    )

                active = self._conn.execute(
                    """SELECT 1 FROM queue
                       WHERE tool_name=? AND relative_path=?
                         AND status='uploading' LIMIT 1""",
                    (tool_name, relative_path),
                ).fetchone() is not None
                pending = self._conn.execute(
                    """SELECT id, metadata FROM queue
                       WHERE tool_name=? AND relative_path=?
                         AND status='pending'
                       ORDER BY id DESC LIMIT 1""",
                    (tool_name, relative_path),
                ).fetchone()

                # A change back to the last acknowledged value can cancel a
                # pending update. If an older value is already in flight, queue
                # the restoration so the server still converges correctly.
                needs_upload = current_value != synced_value or active
                if not needs_upload:
                    if pending:
                        self._conn.execute(
                            "UPDATE queue SET status='superseded' WHERE id=?",
                            (int(pending[0]),),
                        )
                    continue

                if pending:
                    try:
                        pending_metadata = json.loads(str(pending[1]))
                    except (TypeError, json.JSONDecodeError):
                        pending_metadata = {}
                    if pending_metadata.get("_queue_state_value") == current_value:
                        continue

                payload = dict(record)
                payload.update({
                    "_queue_state_namespace": namespace,
                    "_queue_state_key": item_key,
                    "_queue_state_value": current_value,
                })
                metadata_json = json.dumps(payload, default=str)
                content_hash = hashlib.sha256(
                    metadata_json.encode("utf-8")
                ).hexdigest()

                if pending:
                    self._conn.execute(
                        """UPDATE queue SET metadata=?, content_hash=?,
                                  created_at=?, retry_count=0, available_at=0,
                                  last_attempt_at=NULL, last_error=NULL
                           WHERE id=? AND status='pending'""",
                        (metadata_json, content_hash, now, int(pending[0])),
                    )
                else:
                    self._conn.execute(
                        """INSERT INTO queue (
                               tool_name, category, content_type, relative_path,
                               content, content_hash, file_size, sync_strategy,
                               is_partial, offset, metadata, created_at,
                               payload_bytes, available_at
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                        (
                            tool_name, "metadata", "json", relative_path, "",
                            content_hash, 0, "metadata", 0, 0, metadata_json,
                            now, 0,
                        ),
                    )
                queued += 1

            self._conn.commit()
        return queued

    def _column_names(self, table: str) -> set[str]:
        return {str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _remove_orphaned_spool_files(self) -> None:
        with self._lock:
            referenced = {
                str(Path(row[0]).resolve())
                for row in self._conn.execute(
                    "SELECT payload_path FROM queue WHERE payload_path IS NOT NULL"
                )
                if row[0]
            }
        stale_before = time.time() - 24 * 60 * 60
        for path in self._spool_dir.glob("*"):
            # A concurrent producer writes before inserting queue metadata.
            # The age guard keeps startup cleanup from racing that window.
            try:
                if (path.is_file() and str(path.resolve()) not in referenced
                        and path.stat().st_mtime < stale_before):
                    path.unlink()
            except OSError:
                pass

    def _write_spool_text(self, content: str) -> tuple[str, int]:
        stem = uuid.uuid4().hex
        temporary = self._spool_dir / f".{stem}.tmp"
        final = self._spool_dir / f"{stem}.payload"
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, final)
            return str(final), final.stat().st_size
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _write_spool_bytes(self, content: bytes) -> tuple[str, int]:
        stem = uuid.uuid4().hex
        temporary = self._spool_dir / f".{stem}.tmp"
        final = self._spool_dir / f"{stem}.payload"
        try:
            with temporary.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, final)
            return str(final), final.stat().st_size
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _store_payload(self, content: str) -> tuple[str, str | None, int]:
        # Character length is a safe fast lower bound for UTF-8 bytes. Only
        # encode potentially-small payloads; large strings stream to disk.
        if len(content) > self._spool_threshold:
            path, size = self._write_spool_text(content)
            return "", path, size
        encoded = content.encode("utf-8")
        if len(encoded) > self._spool_threshold:
            path, size = self._write_spool_bytes(encoded)
            return "", path, size
        return content, None, len(encoded)

    def _discard_payload(self, payload_path: str | None) -> None:
        if not payload_path:
            return
        try:
            path = Path(payload_path).resolve()
            if path.parent == self._spool_dir.resolve():
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _observed_hash_locked(self, tool_name: str, relative_path: str) -> str | None:
        row = self._conn.execute(
            """SELECT COALESCE(observed_hash, last_hash)
               FROM file_state WHERE tool_name=? AND relative_path=?""",
            (tool_name, relative_path),
        ).fetchone()
        return row[0] if row else None

    def get_delta_base(
        self,
        tool_name: str,
        relative_path: str,
    ) -> tuple[str | None, int]:
        """Return the earliest revision a new coalesced tail must extend."""
        with self._lock:
            # A queued complete snapshot is authoritative and should be
            # refreshed in place instead of accumulating a tail behind it.
            complete = self._conn.execute(
                """SELECT 1 FROM queue
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND is_partial=0 AND sync_strategy IN ('full','delta')
                   LIMIT 1""",
                (tool_name, relative_path),
            ).fetchone()
            if complete is not None:
                return None, 0

            # Re-read from the beginning of the one pending tail so repeated
            # filesystem events replace it with a single current tail.
            pending = self._conn.execute(
                """SELECT base_hash, base_offset FROM queue
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND is_partial=1 AND sync_strategy='delta'
                   ORDER BY id ASC LIMIT 1""",
                (tool_name, relative_path),
            ).fetchone()
            if pending is not None:
                return pending[0], int(pending[1] or 0)

            # A leased revision is immutable. A new tail may safely target its
            # end because the same-path FIFO barrier prevents overtaking it.
            uploading = self._conn.execute(
                """SELECT content_hash, offset FROM queue
                   WHERE tool_name=? AND relative_path=? AND status='uploading'
                     AND sync_strategy IN ('full','delta')
                   ORDER BY id DESC LIMIT 1""",
                (tool_name, relative_path),
            ).fetchone()
            if uploading is not None:
                return uploading[0], int(uploading[1] or 0)

            synced = self._conn.execute(
                """SELECT synced_hash, synced_offset FROM file_state
                   WHERE tool_name=? AND relative_path=?""",
                (tool_name, relative_path),
            ).fetchone()
            if synced is None or not synced[0]:
                return None, 0
            return str(synced[0]), int(synced[1] or 0)

    def enqueue(self, tool_name: str, category: str, content_type: str,
                relative_path: str, content: str, content_hash: str,
                file_size: int, sync_strategy: str, is_partial: bool = False,
                offset: int = 0, metadata: dict | None = None,
                source_modified_at: float | None = None,
                base_hash: str | None = None, base_offset: int = 0,
                source_path: str | None = None) -> int:
        del file_size  # payload byte size is measured after sanitization below
        is_complete_snapshot = (
            sync_strategy in {"full", "delta"} and not is_partial
        )
        is_coalescible_delta = (
            sync_strategy == "delta" and is_partial and bool(base_hash)
        )

        # Avoid writing another spool file for an identical complete observation.
        if is_complete_snapshot:
            with self._lock:
                if self._observed_hash_locked(tool_name, relative_path) == content_hash:
                    row = self._conn.execute(
                        """SELECT id FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN ('pending','uploading')
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                    return int(row[0]) if row else 0

        inline_content, payload_path, payload_bytes = self._store_payload(content)
        old_payload_path: str | None = None
        superseded_payload_paths: list[str] = []
        now = time.time()
        metadata_json = json.dumps(metadata or {}, default=str)

        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")

                # Re-check after the spool write closes the concurrent-enqueue race.
                if (
                    is_complete_snapshot
                    and self._observed_hash_locked(tool_name, relative_path) == content_hash
                ):
                    row = self._conn.execute(
                        """SELECT id FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN ('pending','uploading')
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                    self._conn.commit()
                    self._discard_payload(payload_path)
                    return int(row[0]) if row else 0

                existing = None
                if is_complete_snapshot:
                    existing = self._conn.execute(
                        """SELECT id, payload_path FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status='pending' AND is_partial=0
                             AND sync_strategy IN ('full','delta')
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                elif is_coalescible_delta:
                    existing = self._conn.execute(
                        """SELECT id, payload_path FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status='pending' AND sync_strategy='delta'
                             AND is_partial=1 AND base_hash=? AND base_offset=?
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path, base_hash, int(base_offset)),
                    ).fetchone()

                if existing:
                    item_id = int(existing[0])
                    old_payload_path = existing[1]
                    self._conn.execute(
                        """UPDATE queue SET category=?, content_type=?, content=?,
                           content_hash=?, file_size=?, sync_strategy=?, is_partial=?,
                           offset=?, metadata=?, source_modified_at=?, retry_count=0,
                           base_hash=?, base_offset=?, source_path=?,
                           status='pending', payload_path=?, payload_bytes=?,
                           lease_token=NULL, lease_until=NULL, available_at=0,
                           last_attempt_at=NULL, last_error=NULL
                           WHERE id=? AND status='pending'""",
                        (category, content_type, inline_content, content_hash,
                         payload_bytes, sync_strategy, int(is_partial), offset,
                         metadata_json, source_modified_at, base_hash,
                         int(base_offset), source_path, payload_path,
                         payload_bytes, item_id),
                    )
                else:
                    cursor = self._conn.execute(
                        """INSERT INTO queue (
                           tool_name, category, content_type, relative_path, content,
                           content_hash, file_size, sync_strategy, is_partial, offset,
                           metadata, created_at, source_modified_at, payload_path,
                           payload_bytes, available_at, base_hash, base_offset,
                           source_path
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                        (tool_name, category, content_type, relative_path,
                         inline_content, content_hash, payload_bytes, sync_strategy,
                         int(is_partial), offset, metadata_json, now,
                         source_modified_at, payload_path, payload_bytes,
                         base_hash, int(base_offset), source_path),
                    )
                    item_id = int(cursor.lastrowid)

                if is_complete_snapshot:
                    superseded = self._conn.execute(
                        """SELECT id, payload_path FROM queue
                           WHERE tool_name=? AND relative_path=? AND status='pending'
                             AND sync_strategy IN ('full','delta') AND id<>?""",
                        (tool_name, relative_path, item_id),
                    ).fetchall()
                    superseded_payload_paths.extend(
                        str(row[1]) for row in superseded if row[1]
                    )
                    self._conn.execute(
                        """UPDATE queue SET status='superseded', payload_path=NULL,
                                  content='', lease_token=NULL, lease_until=NULL
                           WHERE tool_name=? AND relative_path=? AND status='pending'
                             AND sync_strategy IN ('full','delta') AND id<>?""",
                        (tool_name, relative_path, item_id),
                    )

                self._conn.execute(
                    """INSERT INTO file_state (
                           tool_name, relative_path, last_hash, last_offset,
                           observed_hash, observed_offset, observed_at
                       ) VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(tool_name, relative_path) DO UPDATE SET
                           last_hash=excluded.last_hash,
                           last_offset=excluded.last_offset,
                           observed_hash=excluded.observed_hash,
                           observed_offset=excluded.observed_offset,
                           observed_at=excluded.observed_at""",
                    (tool_name, relative_path, content_hash, offset,
                     content_hash, offset, now),
                )
                self._conn.commit()
        except Exception:
            with self._lock:
                self._conn.rollback()
            self._discard_payload(payload_path)
            raise

        if old_payload_path and old_payload_path != payload_path:
            self._discard_payload(old_payload_path)
        for stale_payload_path in superseded_payload_paths:
            if stale_payload_path != payload_path:
                self._discard_payload(stale_payload_path)
        return item_id

    @_rollback_on_error
    def claim_batch(self, batch_size: int = 20,
                    max_bytes: int = 128 * 1024 * 1024,
                    lease_seconds: int = 300) -> list[QueueItem]:
        """Atomically lease a FIFO, byte-bounded, metadata-only batch."""
        now = time.time()
        selected: list[tuple[Any, ...]] = []
        tokens: dict[int, str] = {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """UPDATE queue SET status='pending', lease_token=NULL, lease_until=NULL
                   WHERE status='uploading' AND COALESCE(lease_until, 0) <= ?""",
                (now,),
            )
            in_flight_bytes = int(self._conn.execute(
                """SELECT COALESCE(SUM(
                       CASE WHEN payload_bytes > 0 THEN payload_bytes ELSE file_size END
                   ), 0) FROM queue
                   WHERE status='uploading' AND COALESCE(lease_until, 0) > ?""",
                (now,),
            ).fetchone()[0])
            candidates = self._conn.execute(
                """SELECT q.id, q.tool_name, q.category, q.content_type,
                          q.relative_path, q.content_hash, q.file_size,
                          q.sync_strategy, q.is_partial, q.offset, q.metadata,
                          q.created_at, q.source_modified_at, q.retry_count,
                          q.payload_path,
                          CASE WHEN q.payload_bytes > 0
                               THEN q.payload_bytes ELSE q.file_size END,
                          q.base_hash, q.base_offset, q.source_path
                   FROM queue AS q
                   WHERE q.status='pending' AND COALESCE(q.available_at, 0) <= ?
                     AND NOT EXISTS (
                        SELECT 1 FROM queue AS active
                        WHERE active.tool_name=q.tool_name
                          AND active.relative_path=q.relative_path
                          AND active.status='uploading'
                          AND COALESCE(active.lease_until, 0) > ?
                     )
                     AND (
                        q.sync_strategy='full' OR NOT EXISTS (
                            SELECT 1 FROM queue AS older
                            WHERE older.tool_name=q.tool_name
                              AND older.relative_path=q.relative_path
                              AND older.status='pending' AND older.id < q.id
                        )
                     )
                   ORDER BY CASE WHEN q.sync_strategy='metadata' THEN 0 ELSE 1 END,
                            q.created_at ASC, q.id ASC
                   LIMIT ?""",
                (now, now, max(batch_size * 8, batch_size)),
            ).fetchall()

            total_bytes = in_flight_bytes
            selected_paths: set[tuple[str, str]] = set()
            for row in candidates:
                path_key = (str(row[1]), str(row[4]))
                if path_key in selected_paths:
                    continue
                size = max(0, int(row[15] or 0))
                if len(selected) >= batch_size:
                    break
                # Metadata-only work has no payload and must remain claimable
                # while a large file consumes the byte budget. Payload rows
                # retain the existing FIFO barrier at the first item that does
                # not fit, so later files cannot leapfrog it.
                if size > 0 and total_bytes and total_bytes + size > max_bytes:
                    break
                selected.append(row)
                selected_paths.add(path_key)
                total_bytes += size
                # One oversize payload is legal, but no second payload may be
                # added. Zero-byte metadata selected before it is harmless.
                if in_flight_bytes == 0 and size > max_bytes:
                    break

            for row in selected:
                item_id = int(row[0])
                token = uuid.uuid4().hex
                cursor = self._conn.execute(
                    """UPDATE queue SET status='uploading', lease_token=?,
                              lease_until=?, last_attempt_at=?
                       WHERE id=? AND status='pending'""",
                    (token, now + lease_seconds, now, item_id),
                )
                if cursor.rowcount == 1:
                    tokens[item_id] = token
            self._conn.commit()

        items: list[QueueItem] = []
        for row in selected:
            item_id = int(row[0])
            token = tokens.get(item_id)
            if not token:
                continue
            try:
                metadata = json.loads(row[10])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            items.append(QueueItem(
                id=item_id, tool_name=row[1], category=row[2],
                content_type=row[3], relative_path=row[4], content=None,
                content_hash=row[5], file_size=int(row[6]),
                sync_strategy=row[7], is_partial=bool(row[8]), offset=int(row[9]),
                metadata=metadata, created_at=float(row[11]),
                source_modified_at=(
                    float(row[12]) if row[12] is not None else None
                ),
                retry_count=int(row[13]), payload_path=row[14],
                payload_bytes=int(row[15] or row[6]),
                base_hash=row[16], base_offset=int(row[17] or 0),
                source_path=row[18],
                lease_token=token,
            ))
        return items

    @_rollback_on_error
    def renew_lease(self, item: QueueItem, lease_seconds: int = 300) -> bool:
        if not item.lease_token:
            return False
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE queue SET lease_until=?
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (time.time() + lease_seconds, item.id, item.lease_token),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def _inline_content(self, item: QueueItem) -> str:
        with self._lock:
            row = self._conn.execute(
                """SELECT content FROM queue
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (item.id, item.lease_token),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"queue lease lost for item {item.id}")
        return str(row[0])

    def read_payload_text(self, item: QueueItem) -> str:
        if item.payload_path:
            return Path(item.payload_path).read_text(encoding="utf-8")
        return self._inline_content(item)

    @contextmanager
    def open_payload(self, item: QueueItem) -> Iterator[BinaryIO]:
        if item.payload_path:
            with Path(item.payload_path).open("rb") as stream:
                yield stream
            return
        stream = io.BytesIO(self._inline_content(item).encode("utf-8"))
        try:
            yield stream
        finally:
            stream.close()

    @_rollback_on_error
    def mark_synced(self, item: QueueItem) -> bool:
        """Acknowledge only the exact live lease and advance synced state."""
        if not item.lease_token:
            return False
        payload_path: str | None = None
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT tool_name, relative_path, content_hash, offset,
                          payload_path, metadata
                   FROM queue WHERE id=? AND status='uploading' AND lease_token=?""",
                (item.id, item.lease_token),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False
            payload_path = row[4]
            self._conn.execute(
                """UPDATE queue SET status='synced', lease_token=NULL,
                          lease_until=NULL, payload_path=NULL, content=''
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (item.id, item.lease_token),
            )
            self._conn.execute(
                """INSERT INTO file_state (
                       tool_name, relative_path, last_hash, last_offset,
                       last_synced_at, observed_hash, observed_offset, observed_at,
                       synced_hash, synced_offset, synced_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tool_name, relative_path) DO UPDATE SET
                       last_synced_at=excluded.last_synced_at,
                       synced_hash=excluded.synced_hash,
                       synced_offset=excluded.synced_offset,
                       synced_at=excluded.synced_at""",
                (row[0], row[1], row[2], int(row[3]), now,
                 row[2], int(row[3]), now, row[2], int(row[3]), now),
            )
            try:
                item_metadata = json.loads(str(row[5]))
            except (TypeError, json.JSONDecodeError):
                item_metadata = {}
            state_namespace = item_metadata.get("_queue_state_namespace")
            state_key = item_metadata.get("_queue_state_key")
            state_value = item_metadata.get("_queue_state_value")
            if all(isinstance(value, str) for value in (
                state_namespace, state_key, state_value,
            )):
                self._conn.execute(
                    """UPDATE metadata_state
                       SET synced_value=?, updated_at=?
                       WHERE namespace=? AND item_key=?""",
                    (state_value, now, state_namespace, state_key),
                )
            self._conn.commit()
        self._discard_payload(payload_path)
        return True

    @_rollback_on_error
    def mark_failed(self, item: QueueItem, error: str | None = None) -> bool:
        """Release a failed lease with backoff, or supersede an obsolete FULL."""
        if not item.lease_token:
            return False
        payload_path: str | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT tool_name, relative_path, content_hash, sync_strategy,
                          retry_count, payload_path
                   FROM queue WHERE id=? AND status='uploading' AND lease_token=?""",
                (item.id, item.lease_token),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False

            # A newer complete snapshot covers any older FULL or DELTA
            # payload for this path. Drop the failed predecessor so a legacy
            # strategy transition cannot wedge the authoritative snapshot.
            has_successor = self._conn.execute(
                """SELECT 1 FROM queue
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND sync_strategy IN ('full','delta')
                     AND is_partial=0 AND id > ? LIMIT 1""",
                (row[0], row[1], item.id),
            ).fetchone() is not None

            next_retry = int(row[4]) + 1
            if has_successor:
                status = "superseded"
                available_at = 0.0
                payload_path = row[5]
            else:
                status = "pending"
                available_at = time.time() + min(2 ** min(next_retry, 8), 300)

            self._conn.execute(
                """UPDATE queue SET retry_count=?, status=?, lease_token=NULL,
                          lease_until=NULL, available_at=?, last_error=?
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (next_retry, status, available_at, (error or "")[:1000],
                 item.id, item.lease_token),
            )
            if status == "superseded":
                self._conn.execute(
                    "UPDATE queue SET payload_path=NULL, content='' WHERE id=?", (item.id,)
                )
            self._conn.commit()
        self._discard_payload(payload_path)
        return True

    @_rollback_on_error
    def mark_delta_conflict(self, item: QueueItem) -> bool:
        """Discard a rejected delta chain and require one complete snapshot."""
        if not item.lease_token:
            return False
        payload_paths: list[str] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT tool_name, relative_path, payload_path FROM queue
                   WHERE id=? AND status='uploading' AND lease_token=?
                     AND sync_strategy='delta' AND is_partial=1""",
                (item.id, item.lease_token),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False

            if row[2]:
                payload_paths.append(str(row[2]))
            pending = self._conn.execute(
                """SELECT payload_path FROM queue
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND sync_strategy='delta' AND is_partial=1""",
                (row[0], row[1]),
            ).fetchall()
            payload_paths.extend(str(candidate[0]) for candidate in pending if candidate[0])

            self._conn.execute(
                """UPDATE queue SET status='superseded', payload_path=NULL,
                          content='', lease_token=NULL, lease_until=NULL,
                          last_error='delta base mismatch'
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (item.id, item.lease_token),
            )
            self._conn.execute(
                """UPDATE queue SET status='superseded', payload_path=NULL,
                          content='', lease_token=NULL, lease_until=NULL,
                          last_error='delta base mismatch'
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND sync_strategy='delta' AND is_partial=1""",
                (row[0], row[1]),
            )
            self._conn.execute(
                """UPDATE file_state
                   SET last_hash=synced_hash,
                       last_offset=COALESCE(synced_offset, 0),
                       observed_hash=synced_hash,
                       observed_offset=COALESCE(synced_offset, 0),
                       observed_at=synced_at
                   WHERE tool_name=? AND relative_path=?""",
                (row[0], row[1]),
            )
            self._conn.commit()

        for payload_path in payload_paths:
            self._discard_payload(payload_path)
        return True

    def get_file_state(self, tool_name: str, relative_path: str) -> tuple[str | None, int]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(observed_hash, last_hash),
                          COALESCE(observed_offset, last_offset, 0)
                   FROM file_state WHERE tool_name=? AND relative_path=?""",
                (tool_name, relative_path),
            ).fetchone()
            return (row[0], int(row[1])) if row else (None, 0)

    @_rollback_on_error
    def update_file_state(self, tool_name: str, relative_path: str,
                          content_hash: str, offset: int) -> None:
        """Compatibility helper: record observation, never claim upload success."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO file_state (
                       tool_name, relative_path, last_hash, last_offset,
                       observed_hash, observed_offset, observed_at
                   ) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(tool_name, relative_path) DO UPDATE SET
                       last_hash=excluded.last_hash,
                       last_offset=excluded.last_offset,
                       observed_hash=excluded.observed_hash,
                       observed_offset=excluded.observed_offset,
                       observed_at=excluded.observed_at""",
                (tool_name, relative_path, content_hash, offset,
                 content_hash, offset, now),
            )
            self._conn.commit()

    @_rollback_on_error
    def cleanup_synced(self, older_than_seconds: int = 3600) -> int:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT payload_path FROM queue
                   WHERE status IN ('synced','superseded') AND created_at < ?
                     AND payload_path IS NOT NULL""",
                (cutoff,),
            ).fetchall()
            cursor = self._conn.execute(
                """DELETE FROM queue
                   WHERE status IN ('synced','superseded') AND created_at < ?""",
                (cutoff,),
            )
            self._conn.commit()
        for row in rows:
            self._discard_payload(row[0])
        return cursor.rowcount

    def pending_count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM queue WHERE status IN ('pending','uploading')"
            ).fetchone()[0])

    def outstanding_bytes(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                """SELECT COALESCE(SUM(
                       CASE WHEN payload_bytes > 0 THEN payload_bytes ELSE file_size END
                   ), 0) FROM queue WHERE status IN ('pending','uploading')"""
            ).fetchone()[0])

    @_rollback_on_error
    def clear_all_state(self) -> None:
        """Invalidate leases and force a complete, safe rescan."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_path FROM queue WHERE payload_path IS NOT NULL"
            ).fetchall()
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("DELETE FROM file_state")
            self._conn.execute("DELETE FROM queue")
            self._conn.commit()
        for row in rows:
            self._discard_payload(row[0])

    @_rollback_on_error
    def clear_file_state(self, tool_name: str, relative_path: str) -> None:
        """Forget one observed revision so a force-full upload cannot dedupe away."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "DELETE FROM file_state WHERE tool_name=? AND relative_path=?",
                (tool_name, relative_path),
            )
            self._conn.commit()

    @_rollback_on_error
    def prioritize_file(self, tool_name: str, relative_path: str) -> int:
        """Move a server-requested repair ahead of ordinary backlog rows."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                """UPDATE queue
                   SET created_at=0, available_at=0
                   WHERE tool_name=? AND relative_path=? AND status='pending'""",
                (tool_name, relative_path),
            )
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
