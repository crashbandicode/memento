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

from .outcomes import UploadOutcome, UploadOutcomeState


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


@dataclass(frozen=True)
class PreparedPayload:
    """A sanitized payload already hashed and optionally spooled to disk."""

    content: str
    content_hash: str
    payload_path: str | None
    payload_bytes: int
    has_non_whitespace: bool


class PayloadWriter:
    """Hash sanitized UTF-8 while spilling large payloads exactly once."""

    def __init__(self, spool_dir: Path, threshold: int) -> None:
        self._spool_dir = spool_dir
        self._threshold = threshold
        self._buffer = bytearray()
        self._hash = hashlib.sha256()
        self._has_non_whitespace = False
        self._temporary: Path | None = None
        self._final: Path | None = None
        self._stream: BinaryIO | None = None
        self._bytes = 0
        self._finished = False

    def _spill(self) -> None:
        if self._stream is not None:
            return
        stem = uuid.uuid4().hex
        self._temporary = self._spool_dir / f".{stem}.tmp"
        self._final = self._spool_dir / f"{stem}.payload"
        self._stream = self._temporary.open("wb")
        if self._buffer:
            self._stream.write(self._buffer)
            self._buffer.clear()

    def write(self, text: str) -> None:
        if self._finished:
            raise RuntimeError("payload writer is already finished")
        if not text:
            return
        encoded = text.encode("utf-8")
        self._hash.update(encoded)
        self._bytes += len(encoded)
        self._has_non_whitespace = self._has_non_whitespace or bool(text.strip())
        if self._stream is None and len(self._buffer) + len(encoded) <= self._threshold:
            self._buffer.extend(encoded)
            return
        self._spill()
        assert self._stream is not None
        self._stream.write(encoded)

    def finish(self) -> PreparedPayload:
        if self._finished:
            raise RuntimeError("payload writer is already finished")
        self._finished = True
        if self._stream is None:
            return PreparedPayload(
                content=bytes(self._buffer).decode("utf-8"),
                content_hash=self._hash.hexdigest(),
                payload_path=None,
                payload_bytes=self._bytes,
                has_non_whitespace=self._has_non_whitespace,
            )
        assert self._temporary is not None and self._final is not None
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
            os.replace(self._temporary, self._final)
            return PreparedPayload(
                content="",
                content_hash=self._hash.hexdigest(),
                payload_path=str(self._final),
                payload_bytes=self._bytes,
                has_non_whitespace=self._has_non_whitespace,
            )
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        for path in (self._temporary, self._final):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._finished = True


def _metadata_state_value(record: dict[str, Any], title: str = "") -> str:
    if str(record.get("metadata_type") or "") != "codex_thread_title":
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
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

    SCHEMA_VERSION = 7

    def __init__(self, db_path: Path, spool_threshold: int = 4 * 1024 * 1024) -> None:
        self._db_path = db_path
        self._spool_threshold = max(64 * 1024, spool_threshold)
        self._spool_dir = db_path.parent / "spool"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._spool_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._change_condition = threading.Condition()
        self._change_token = 0
        self._fair_lane_cursor = 0
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._remove_orphaned_spool_files()

    def _signal_change(self) -> None:
        """Wake consumers without requiring them to poll SQLite."""
        with self._change_condition:
            self._change_token += 1
            self._change_condition.notify_all()

    def change_token(self) -> int:
        with self._change_condition:
            return self._change_token

    def wait_for_change(self, token: int, timeout: float | None = None) -> int:
        """Wait until queue ownership state changes or a retry timer expires."""
        with self._change_condition:
            if self._change_token == token:
                self._change_condition.wait(timeout=timeout)
            return self._change_token

    def wake_waiters(self) -> None:
        """Interrupt queue waits during pause and shutdown."""
        self._signal_change()

    def next_deferred_delay(self, maximum: float = 60.0) -> float | None:
        """Return the next future retry deadline, excluding ready work."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                """SELECT MIN(available_at) FROM queue
                   WHERE status='pending' AND available_at > ?""",
                (now,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return min(maximum, max(0.0, float(row[0]) - now))

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
                last_error TEXT,
                outcome_state TEXT,
                diagnostic_code TEXT,
                http_status INTEGER,
                terminal_at REAL,
                blocked_config_fingerprint TEXT
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
                source_size INTEGER,
                source_mtime_ns INTEGER,
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
            CREATE TABLE IF NOT EXISTS queue_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
            "outcome_state": "TEXT",
            "diagnostic_code": "TEXT",
            "http_status": "INTEGER",
            "terminal_at": "REAL",
            "blocked_config_fingerprint": "TEXT",
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
            "source_size": "INTEGER",
            "source_mtime_ns": "INTEGER",
        }
        for name, definition in state_additions.items():
            if name not in state_columns:
                self._conn.execute(
                    f"ALTER TABLE file_state ADD COLUMN {name} {definition}"
                )

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
                kind == "custom" or (kind == "unknown" and title != incoming_fallback)
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
        """Durably coalesce lightweight source state into the upload queue.

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
        queue_changed = False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            auth_blocked = self._auth_gate_active_locked()
            target_status = "auth_blocked" if auth_blocked else "pending"
            auth_fingerprint = (
                self._meta_value_locked("current_auth_fingerprint")
                if auth_blocked
                else None
            )
            for item_key, source_record in records.items():
                record = dict(source_record)
                metadata_type = str(record.get("metadata_type") or "").strip()
                is_title_update = metadata_type == "codex_thread_title"
                is_terminal_activity = (
                    metadata_type == "conversation_activity"
                    and str(record.get("activity_status") or "").casefold()
                    in {"completed", "failed", "cancelled"}
                )
                current_title = str(record.get("title") or "").strip()
                if not metadata_type or (is_title_update and not current_title):
                    continue

                path_key = hashlib.sha256(item_key.encode("utf-8")).hexdigest()
                relative_path = f"__metadata__/{namespace}/{path_key}"

                state_row = self._conn.execute(
                    """SELECT observed_value, synced_value
                       FROM metadata_state
                       WHERE namespace=? AND item_key=?""",
                    (namespace, item_key),
                ).fetchone()
                if state_row is None and is_terminal_activity:
                    # Generated snapshots contain historical completed shell
                    # calls. Only publish a terminal transition after this
                    # queue has observed the matching running state.
                    continue
                state_values = tuple(state_row) if state_row is not None else ()
                if (
                    is_title_update
                    and str(record.get("title_kind") or "").lower() == "fallback"
                ):
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

                active = (
                    self._conn.execute(
                        """SELECT 1 FROM queue
                       WHERE tool_name=? AND relative_path=?
                         AND status='uploading' LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                    is not None
                )
                pending = self._conn.execute(
                    """SELECT id, metadata FROM queue
                       WHERE tool_name=? AND relative_path=?
                         AND status IN (
                             'pending','auth_blocked',
                             'repair_required','quarantined'
                         )
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
                        queue_changed = True
                    continue

                if pending:
                    try:
                        pending_metadata = json.loads(str(pending[1]))
                    except (TypeError, json.JSONDecodeError):
                        pending_metadata = {}
                    if pending_metadata.get("_queue_state_value") == current_value:
                        continue

                payload = dict(record)
                payload.update(
                    {
                        "_queue_state_namespace": namespace,
                        "_queue_state_key": item_key,
                        "_queue_state_value": current_value,
                    }
                )
                metadata_json = json.dumps(payload, default=str)
                content_hash = hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()

                if pending:
                    self._conn.execute(
                        """UPDATE queue SET metadata=?, content_hash=?,
                                  created_at=?, retry_count=0, available_at=0,
                                  last_attempt_at=NULL, status=?,
                                  last_error=CASE WHEN ?='pending' THEN NULL
                                      ELSE 'credentials rejected by server' END,
                                  outcome_state=CASE WHEN ?='pending' THEN NULL
                                      ELSE 'authentication_blocked' END,
                                  diagnostic_code=CASE WHEN ?='pending' THEN NULL
                                      ELSE 'authentication_rejected' END,
                                  http_status=CASE WHEN ?='pending' THEN NULL
                                      ELSE http_status END,
                                  terminal_at=CASE WHEN ?='pending' THEN NULL ELSE ? END,
                                  blocked_config_fingerprint=?
                           WHERE id=? AND status IN (
                               'pending','auth_blocked',
                               'repair_required','quarantined'
                           )""",
                        (
                            metadata_json,
                            content_hash,
                            now,
                            target_status,
                            target_status,
                            target_status,
                            target_status,
                            target_status,
                            target_status,
                            now,
                            auth_fingerprint,
                            int(pending[0]),
                        ),
                    )
                else:
                    self._conn.execute(
                        """INSERT INTO queue (
                               tool_name, category, content_type, relative_path,
                               content, content_hash, file_size, sync_strategy,
                               is_partial, offset, metadata, created_at,
                               payload_bytes, available_at, status,
                               outcome_state, diagnostic_code, terminal_at,
                               blocked_config_fingerprint, last_error
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
                        (
                            tool_name,
                            "metadata",
                            "json",
                            relative_path,
                            "",
                            content_hash,
                            0,
                            "metadata",
                            0,
                            0,
                            metadata_json,
                            now,
                            0,
                            target_status,
                            (
                                UploadOutcomeState.AUTHENTICATION_BLOCKED.value
                                if auth_blocked
                                else None
                            ),
                            "authentication_rejected" if auth_blocked else None,
                            now if auth_blocked else None,
                            auth_fingerprint,
                            (
                                "credentials rejected by server"
                                if auth_blocked
                                else None
                            ),
                        ),
                    )
                queue_changed = True
                queued += 1

            self._conn.commit()
        if queue_changed:
            self._signal_change()
        return queued

    def _column_names(self, table: str) -> set[str]:
        return {
            str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({table})")
        }

    def payload_writer(self) -> PayloadWriter:
        """Create a one-pass sanitized payload writer owned by this queue."""

        return PayloadWriter(self._spool_dir, self._spool_threshold)

    def discard_prepared_payload(self, payload: PreparedPayload) -> None:
        self._discard_payload(payload.payload_path)

    def _meta_value_locked(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM queue_meta WHERE key=?",
            (key,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _set_meta_value_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO queue_meta (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )

    def _auth_gate_active_locked(self) -> bool:
        current = self._meta_value_locked("current_auth_fingerprint")
        blocked = self._meta_value_locked("blocked_auth_fingerprint")
        return bool(current and blocked and current == blocked)

    @_rollback_on_error
    def configure_auth(self, fingerprint: str) -> int:
        """Resume auth-blocked rows only after endpoint/token identity changes."""

        if not fingerprint:
            raise ValueError("auth fingerprint must not be empty")
        resumed = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            previous = self._meta_value_locked("current_auth_fingerprint")
            blocked = self._meta_value_locked("blocked_auth_fingerprint")
            changed = previous is not None and previous != fingerprint
            if changed or (blocked is not None and blocked != fingerprint):
                cursor = self._conn.execute(
                    """UPDATE queue
                       SET status='pending', available_at=0, retry_count=0,
                           outcome_state=NULL, diagnostic_code=NULL,
                           http_status=NULL, terminal_at=NULL,
                           blocked_config_fingerprint=NULL, last_error=NULL
                       WHERE status='auth_blocked'"""
                )
                resumed = int(cursor.rowcount)
                self._conn.execute(
                    "DELETE FROM queue_meta WHERE key='blocked_auth_fingerprint'"
                )
            self._set_meta_value_locked("current_auth_fingerprint", fingerprint)
            self._conn.commit()
        if resumed:
            self._signal_change()
        return resumed

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
                if (
                    path.is_file()
                    and str(path.resolve()) not in referenced
                    and path.stat().st_mtime < stale_before
                ):
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

    def _record_source_revision_locked(
        self,
        tool_name: str,
        relative_path: str,
        source_size: int | None,
        source_mtime_ns: int | None,
    ) -> None:
        if source_size is None or source_mtime_ns is None:
            return
        self._conn.execute(
            """UPDATE file_state SET source_size=?, source_mtime_ns=?
               WHERE tool_name=? AND relative_path=?""",
            (
                max(0, int(source_size)),
                max(0, int(source_mtime_ns)),
                tool_name,
                relative_path,
            ),
        )

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

    def has_uncommitted_delta_revision(
        self,
        tool_name: str,
        relative_path: str,
    ) -> bool:
        """Return whether this source already has a staged delta revision.

        A leased upload is not a committed base. Capturing another tail while
        it is pending or uploading makes that tail speculative: a restart,
        receipt failure, or server-side base conflict leaves it anchored to a
        revision that never became authoritative. The sync callback captures
        the next window immediately after ``mark_synced``, so filesystem events
        can safely defer while one revision is uncommitted.
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM queue
                   WHERE tool_name=? AND relative_path=?
                     AND status IN ('pending','uploading','auth_blocked')
                     AND sync_strategy='delta'
                   LIMIT 1""",
                (tool_name, relative_path),
            ).fetchone()
            return row is not None

    def enqueue(
        self,
        tool_name: str,
        category: str,
        content_type: str,
        relative_path: str,
        content: str,
        content_hash: str,
        file_size: int,
        sync_strategy: str,
        is_partial: bool = False,
        offset: int = 0,
        metadata: dict | None = None,
        source_modified_at: float | None = None,
        base_hash: str | None = None,
        base_offset: int = 0,
        source_path: str | None = None,
        source_size: int | None = None,
        source_mtime_ns: int | None = None,
        prepared_payload: PreparedPayload | None = None,
    ) -> int:
        del file_size  # payload byte size is measured after sanitization below
        is_complete_snapshot = sync_strategy in {"full", "delta"} and not is_partial
        is_coalescible_delta = (
            sync_strategy == "delta" and is_partial and bool(base_hash)
        )
        force_reprocess = bool(
            isinstance(metadata, dict) and metadata.get("_queue_force_reprocess_nonce")
        )

        # Avoid writing another spool file for an identical complete observation.
        if is_complete_snapshot and not force_reprocess:
            with self._lock:
                if self._observed_hash_locked(tool_name, relative_path) == content_hash:
                    row = self._conn.execute(
                        """SELECT id FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN (
                                 'pending','uploading','auth_blocked',
                                 'repair_required','quarantined'
                             )
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                    self._record_source_revision_locked(
                        tool_name,
                        relative_path,
                        source_size,
                        source_mtime_ns,
                    )
                    self._conn.commit()
                    if prepared_payload is not None:
                        self.discard_prepared_payload(prepared_payload)
                    return int(row[0]) if row else 0

        if prepared_payload is None:
            inline_content, payload_path, payload_bytes = self._store_payload(content)
        else:
            inline_content = prepared_payload.content
            payload_path = prepared_payload.payload_path
            payload_bytes = prepared_payload.payload_bytes
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
                    and not force_reprocess
                    and self._observed_hash_locked(tool_name, relative_path)
                    == content_hash
                ):
                    row = self._conn.execute(
                        """SELECT id FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN (
                                 'pending','uploading','auth_blocked',
                                 'repair_required','quarantined'
                             )
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                    self._record_source_revision_locked(
                        tool_name,
                        relative_path,
                        source_size,
                        source_mtime_ns,
                    )
                    self._conn.commit()
                    self._discard_payload(payload_path)
                    return int(row[0]) if row else 0

                auth_blocked = self._auth_gate_active_locked()
                target_status = "auth_blocked" if auth_blocked else "pending"
                auth_fingerprint = (
                    self._meta_value_locked("current_auth_fingerprint")
                    if auth_blocked
                    else None
                )
                existing = None
                if is_complete_snapshot:
                    existing = self._conn.execute(
                        """SELECT id, payload_path FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN (
                                 'pending','auth_blocked',
                                 'repair_required','quarantined'
                             ) AND is_partial=0
                             AND sync_strategy IN ('full','delta')
                           ORDER BY id DESC LIMIT 1""",
                        (tool_name, relative_path),
                    ).fetchone()
                elif is_coalescible_delta:
                    existing = self._conn.execute(
                        """SELECT id, payload_path FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN (
                                 'pending','auth_blocked',
                                 'repair_required','quarantined'
                             ) AND sync_strategy='delta'
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
                           status=?, payload_path=?, payload_bytes=?,
                           lease_token=NULL, lease_until=NULL, available_at=0,
                           last_attempt_at=NULL,
                           last_error=CASE WHEN ?='pending' THEN NULL
                                           ELSE 'credentials rejected by server' END,
                           outcome_state=CASE WHEN ?='pending' THEN NULL
                                              ELSE 'authentication_blocked' END,
                           diagnostic_code=CASE WHEN ?='pending' THEN NULL
                                                ELSE 'authentication_rejected' END,
                           http_status=CASE WHEN ?='pending' THEN NULL ELSE http_status END,
                           terminal_at=CASE WHEN ?='pending' THEN NULL ELSE ? END,
                           blocked_config_fingerprint=?
                           WHERE id=? AND status IN (
                               'pending','auth_blocked',
                               'repair_required','quarantined'
                           )""",
                        (
                            category,
                            content_type,
                            inline_content,
                            content_hash,
                            payload_bytes,
                            sync_strategy,
                            int(is_partial),
                            offset,
                            metadata_json,
                            source_modified_at,
                            base_hash,
                            int(base_offset),
                            source_path,
                            target_status,
                            payload_path,
                            payload_bytes,
                            target_status,
                            target_status,
                            target_status,
                            target_status,
                            target_status,
                            now,
                            auth_fingerprint,
                            item_id,
                        ),
                    )
                else:
                    cursor = self._conn.execute(
                        """INSERT INTO queue (
                           tool_name, category, content_type, relative_path, content,
                           content_hash, file_size, sync_strategy, is_partial, offset,
                           metadata, created_at, source_modified_at, payload_path,
                           payload_bytes, available_at, base_hash, base_offset,
                           source_path, status, outcome_state, diagnostic_code,
                           terminal_at, blocked_config_fingerprint, last_error
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)""",
                        (
                            tool_name,
                            category,
                            content_type,
                            relative_path,
                            inline_content,
                            content_hash,
                            payload_bytes,
                            sync_strategy,
                            int(is_partial),
                            offset,
                            metadata_json,
                            now,
                            source_modified_at,
                            payload_path,
                            payload_bytes,
                            base_hash,
                            int(base_offset),
                            source_path,
                            target_status,
                            (
                                UploadOutcomeState.AUTHENTICATION_BLOCKED.value
                                if auth_blocked
                                else None
                            ),
                            "authentication_rejected" if auth_blocked else None,
                            now if auth_blocked else None,
                            auth_fingerprint,
                            (
                                "credentials rejected by server"
                                if auth_blocked
                                else None
                            ),
                        ),
                    )
                    item_id = int(cursor.lastrowid)

                if is_complete_snapshot:
                    superseded = self._conn.execute(
                        """SELECT id, payload_path FROM queue
                           WHERE tool_name=? AND relative_path=?
                             AND status IN (
                                 'pending','auth_blocked',
                                 'repair_required','quarantined'
                             )
                             AND sync_strategy IN ('full','delta') AND id<>?""",
                        (tool_name, relative_path, item_id),
                    ).fetchall()
                    superseded_payload_paths.extend(
                        str(row[1]) for row in superseded if row[1]
                    )
                    self._conn.execute(
                        """UPDATE queue SET status='superseded', payload_path=NULL,
                                  content='', lease_token=NULL, lease_until=NULL
                           WHERE tool_name=? AND relative_path=?
                             AND status IN (
                                 'pending','auth_blocked',
                                 'repair_required','quarantined'
                             )
                             AND sync_strategy IN ('full','delta') AND id<>?""",
                        (tool_name, relative_path, item_id),
                    )

                self._conn.execute(
                    """INSERT INTO file_state (
                           tool_name, relative_path, last_hash, last_offset,
                           observed_hash, observed_offset, observed_at,
                           source_size, source_mtime_ns
                       ) VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(tool_name, relative_path) DO UPDATE SET
                           last_hash=excluded.last_hash,
                           last_offset=excluded.last_offset,
                           observed_hash=excluded.observed_hash,
                           observed_offset=excluded.observed_offset,
                           observed_at=excluded.observed_at,
                           source_size=COALESCE(
                               excluded.source_size, file_state.source_size
                           ),
                           source_mtime_ns=COALESCE(
                               excluded.source_mtime_ns, file_state.source_mtime_ns
                           )""",
                    (
                        tool_name,
                        relative_path,
                        content_hash,
                        offset,
                        content_hash,
                        offset,
                        now,
                        source_size,
                        source_mtime_ns,
                    ),
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
        self._signal_change()
        return item_id

    @_rollback_on_error
    def claim_batch(
        self,
        batch_size: int = 20,
        max_bytes: int = 128 * 1024 * 1024,
        lease_seconds: int = 300,
        live_delta_reserve_bytes: int = 16 * 1024 * 1024,
    ) -> list[QueueItem]:
        """Atomically lease a priority-aware, byte-bounded metadata batch.

        Lightweight metadata and guarded tails from files that are actively
        growing stay responsive while complete historical snapshots drain.
        Same-path barriers still prevent a tail from overtaking its base.
        """
        now = time.time()
        selected: list[tuple[Any, ...]] = []
        tokens: dict[int, str] = {}
        with self._lock:
            actionable = self._conn.execute(
                """SELECT 1 FROM queue
                   WHERE (status='pending' AND COALESCE(available_at, 0) <= ?)
                      OR (status='uploading' AND COALESCE(lease_until, 0) <= ?)
                   LIMIT 1""",
                (now, now),
            ).fetchone()
            if actionable is None:
                return []
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """UPDATE queue SET status='pending', lease_token=NULL, lease_until=NULL
                   WHERE status='uploading' AND COALESCE(lease_until, 0) <= ?""",
                (now,),
            )
            in_flight_bytes = int(
                self._conn.execute(
                    """SELECT COALESCE(SUM(
                       CASE WHEN payload_bytes > 0 THEN payload_bytes ELSE file_size END
                   ), 0) FROM queue
                   WHERE status='uploading' AND COALESCE(lease_until, 0) > ?""",
                    (now,),
                ).fetchone()[0]
            )
            candidate_sql = """
                   SELECT q.id, q.tool_name, q.category, q.content_type,
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
                     AND ({lane_predicate})
                   ORDER BY q.created_at ASC, q.id ASC
                   LIMIT ?"""
            lane_limit = max(batch_size * 4, 8)

            def lane_rows(predicate: str) -> list[tuple[Any, ...]]:
                return self._conn.execute(
                    candidate_sql.format(lane_predicate=predicate),
                    (now, now, lane_limit),
                ).fetchall()

            urgent = lane_rows("q.created_at <= 0")
            lanes = {
                "metadata": lane_rows(
                    "q.created_at > 0 AND q.sync_strategy='metadata'"
                ),
                "live": lane_rows(
                    "q.created_at > 0 AND q.sync_strategy='delta' "
                    "AND q.is_partial=1"
                ),
                "canonical": lane_rows(
                    "q.created_at > 0 AND q.sync_strategy<>'metadata' "
                    "AND NOT (q.sync_strategy='delta' AND q.is_partial=1)"
                ),
            }
            # Two prompt/metadata turns, one live tail, and one canonical turn
            # keep interactive state quick without allowing metadata catch-up
            # to hide durable conversation content indefinitely.
            lane_cycle = ("metadata", "live", "metadata", "canonical")
            lane_indices = {name: 0 for name in lanes}
            fair_cursor = self._fair_lane_cursor % len(lane_cycle)
            ordered_candidates: list[tuple[tuple[Any, ...], int | None]] = [
                (row, None) for row in urgent
            ]
            while any(lane_indices[name] < len(rows) for name, rows in lanes.items()):
                appended = False
                for _attempt in range(len(lane_cycle)):
                    lane_name = lane_cycle[fair_cursor]
                    fair_cursor = (fair_cursor + 1) % len(lane_cycle)
                    lane_index = lane_indices[lane_name]
                    if lane_index >= len(lanes[lane_name]):
                        continue
                    ordered_candidates.append(
                        (lanes[lane_name][lane_index], fair_cursor)
                    )
                    lane_indices[lane_name] += 1
                    appended = True
                    break
                if not appended:
                    break

            total_bytes = in_flight_bytes
            live_delta_bytes = 0
            selected_paths: set[tuple[str, str]] = set()
            selected_fair_cursor: int | None = None
            for row, next_fair_cursor in ordered_candidates:
                path_key = (str(row[1]), str(row[4]))
                if path_key in selected_paths:
                    continue
                size = max(0, int(row[15] or 0))
                is_live_delta = str(row[7]) == "delta" and bool(row[8])
                if len(selected) >= batch_size:
                    break
                # Metadata-only work has no payload and must remain claimable
                # while a large file consumes the byte budget. Same-path FIFO
                # is enforced by the candidate query; a large row from another
                # path must not hide a later small live tail such as an
                # interactive question.
                if size > 0 and total_bytes and total_bytes + size > max_bytes:
                    # An oversized historical upload may consume the ordinary
                    # byte budget by itself. Keep a small, explicit reserve for
                    # append-only live tails so the second upload worker is not
                    # left idle while the visible conversation falls behind.
                    if not (
                        is_live_delta
                        and live_delta_bytes + size <= live_delta_reserve_bytes
                    ):
                        continue
                selected.append(row)
                if next_fair_cursor is not None:
                    selected_fair_cursor = next_fair_cursor
                selected_paths.add(path_key)
                total_bytes += size
                if is_live_delta:
                    live_delta_bytes += size
                # One oversize payload is legal, but no second payload may be
                # added. Zero-byte metadata selected before it is harmless.
                if in_flight_bytes == 0 and size > max_bytes:
                    break
            if selected_fair_cursor is not None:
                self._fair_lane_cursor = selected_fair_cursor

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
        if tokens:
            self._signal_change()

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
            items.append(
                QueueItem(
                    id=item_id,
                    tool_name=row[1],
                    category=row[2],
                    content_type=row[3],
                    relative_path=row[4],
                    content=None,
                    content_hash=row[5],
                    file_size=int(row[6]),
                    sync_strategy=row[7],
                    is_partial=bool(row[8]),
                    offset=int(row[9]),
                    metadata=metadata,
                    created_at=float(row[11]),
                    source_modified_at=(
                        float(row[12]) if row[12] is not None else None
                    ),
                    retry_count=int(row[13]),
                    payload_path=row[14],
                    payload_bytes=int(row[15] or row[6]),
                    base_hash=row[16],
                    base_offset=int(row[17] or 0),
                    source_path=row[18],
                    lease_token=token,
                )
            )
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
                          lease_until=NULL, payload_path=NULL, content='',
                          outcome_state='success', diagnostic_code=NULL,
                          http_status=NULL, terminal_at=NULL, last_error=NULL,
                          blocked_config_fingerprint=NULL
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
                (
                    row[0],
                    row[1],
                    row[2],
                    int(row[3]),
                    now,
                    row[2],
                    int(row[3]),
                    now,
                    row[2],
                    int(row[3]),
                    now,
                ),
            )
            try:
                item_metadata = json.loads(str(row[5]))
            except (TypeError, json.JSONDecodeError):
                item_metadata = {}
            state_namespace = item_metadata.get("_queue_state_namespace")
            state_key = item_metadata.get("_queue_state_key")
            state_value = item_metadata.get("_queue_state_value")
            if all(
                isinstance(value, str)
                for value in (
                    state_namespace,
                    state_key,
                    state_value,
                )
            ):
                self._conn.execute(
                    """UPDATE metadata_state
                       SET synced_value=?, updated_at=?
                       WHERE namespace=? AND item_key=?""",
                    (state_value, now, state_namespace, state_key),
                )
            self._conn.commit()
        self._discard_payload(payload_path)
        self._signal_change()
        return True

    def mark_failed(self, item: QueueItem, error: str | None = None) -> bool:
        """Compatibility wrapper for an explicitly transient failure."""

        return self.mark_upload_outcome(
            item,
            UploadOutcome.transient(error or "upload failed"),
        )

    @_rollback_on_error
    def mark_upload_outcome(
        self,
        item: QueueItem,
        outcome: UploadOutcome,
        *,
        auth_fingerprint: str | None = None,
    ) -> bool:
        """Persist a typed failure without retrying terminal dispositions."""

        if outcome.state is UploadOutcomeState.SUCCESS:
            return self.mark_synced(item)
        if not item.lease_token:
            return False
        payload_path: str | None = None
        now = time.time()
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
            has_successor = (
                self._conn.execute(
                    """SELECT 1 FROM queue
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND sync_strategy IN ('full','delta')
                     AND is_partial=0 AND id > ? LIMIT 1""",
                    (row[0], row[1], item.id),
                ).fetchone()
                is not None
            )

            next_retry = int(row[4]) + 1
            if has_successor:
                status = "superseded"
                available_at = 0.0
                payload_path = row[5]
            elif outcome.state is UploadOutcomeState.TRANSIENT_RETRY:
                status = "pending"
                available_at = now + min(2 ** min(next_retry, 8), 300)
            elif outcome.state is UploadOutcomeState.AUTHENTICATION_BLOCKED:
                status = "auth_blocked"
                available_at = 0.0
            elif outcome.state is UploadOutcomeState.SOURCE_REPAIR_REQUIRED:
                status = "repair_required"
                available_at = 0.0
            else:
                status = "quarantined"
                available_at = 0.0

            self._conn.execute(
                """UPDATE queue SET retry_count=?, status=?, lease_token=NULL,
                          lease_until=NULL, available_at=?, last_error=?,
                          outcome_state=?, diagnostic_code=?, http_status=?,
                          terminal_at=?, blocked_config_fingerprint=?
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (
                    next_retry,
                    status,
                    available_at,
                    outcome.diagnostic[:1000],
                    outcome.state.value,
                    outcome.diagnostic_code[:128],
                    outcome.http_status,
                    (
                        None
                        if outcome.state is UploadOutcomeState.TRANSIENT_RETRY
                        else now
                    ),
                    (
                        auth_fingerprint
                        if outcome.state
                        is UploadOutcomeState.AUTHENTICATION_BLOCKED
                        else None
                    ),
                    item.id,
                    item.lease_token,
                ),
            )
            if status == "superseded":
                self._conn.execute(
                    "UPDATE queue SET payload_path=NULL, content='' WHERE id=?",
                    (item.id,),
                )
            if (
                auth_fingerprint
                and outcome.state is UploadOutcomeState.AUTHENTICATION_BLOCKED
            ):
                self._set_meta_value_locked(
                    "current_auth_fingerprint",
                    auth_fingerprint,
                )
                self._set_meta_value_locked(
                    "blocked_auth_fingerprint",
                    auth_fingerprint,
                )
                self._conn.execute(
                    """UPDATE queue
                       SET status='auth_blocked', available_at=0,
                           outcome_state='authentication_blocked',
                           diagnostic_code='authentication_rejected',
                           http_status=?, terminal_at=?,
                           blocked_config_fingerprint=?, last_error=?
                       WHERE status='pending'""",
                    (
                        outcome.http_status,
                        now,
                        auth_fingerprint,
                        outcome.diagnostic[:1000],
                    ),
                )
            self._conn.commit()
        self._discard_payload(payload_path)
        self._signal_change()
        return True

    @_rollback_on_error
    def mark_repair_scheduled(
        self,
        item: QueueItem,
        outcome: UploadOutcome,
    ) -> bool:
        """Retire a revision that an automatic bounded repair will replace."""

        if not item.lease_token:
            return False
        payload_path: str | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """SELECT payload_path FROM queue
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (item.id, item.lease_token),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return False
            payload_path = row[0]
            self._conn.execute(
                """UPDATE queue
                   SET status='superseded', payload_path=NULL, content='',
                       lease_token=NULL, lease_until=NULL, available_at=0,
                       outcome_state=?, diagnostic_code=?, http_status=?,
                       terminal_at=?, last_error=?
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (
                    outcome.state.value,
                    outcome.diagnostic_code[:128],
                    outcome.http_status,
                    time.time(),
                    outcome.diagnostic[:1000],
                    item.id,
                    item.lease_token,
                ),
            )
            self._conn.commit()
        self._discard_payload(payload_path)
        self._signal_change()
        return True

    @_rollback_on_error
    def mark_delta_conflict(
        self,
        item: QueueItem,
        *,
        expected_hash: str | None = None,
        expected_offset: int = 0,
    ) -> bool:
        """Discard a rejected tail and adopt the server's committed base."""
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
            payload_paths.extend(
                str(candidate[0]) for candidate in pending if candidate[0]
            )

            self._conn.execute(
                """UPDATE queue SET status='superseded', payload_path=NULL,
                          content='', lease_token=NULL, lease_until=NULL,
                          last_error='delta base mismatch',
                          outcome_state='source_repair_required',
                          diagnostic_code='delta_base_mismatch',
                          terminal_at=?
                   WHERE id=? AND status='uploading' AND lease_token=?""",
                (time.time(), item.id, item.lease_token),
            )
            self._conn.execute(
                """UPDATE queue SET status='superseded', payload_path=NULL,
                          content='', lease_token=NULL, lease_until=NULL,
                          last_error='delta base mismatch',
                          outcome_state='source_repair_required',
                          diagnostic_code='delta_base_mismatch',
                          terminal_at=?
                   WHERE tool_name=? AND relative_path=? AND status='pending'
                     AND sync_strategy='delta' AND is_partial=1""",
                (time.time(), row[0], row[1]),
            )
            if expected_hash:
                now = time.time()
                self._conn.execute(
                    """INSERT INTO file_state (
                           tool_name, relative_path, last_hash, last_offset,
                           last_synced_at, observed_hash, observed_offset,
                           observed_at, synced_hash, synced_offset, synced_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(tool_name, relative_path) DO UPDATE SET
                           last_hash=excluded.last_hash,
                           last_offset=excluded.last_offset,
                           last_synced_at=excluded.last_synced_at,
                           observed_hash=excluded.observed_hash,
                           observed_offset=excluded.observed_offset,
                           observed_at=excluded.observed_at,
                           synced_hash=excluded.synced_hash,
                           synced_offset=excluded.synced_offset,
                           synced_at=excluded.synced_at""",
                    (
                        row[0],
                        row[1],
                        expected_hash,
                        max(0, int(expected_offset or 0)),
                        now,
                        expected_hash,
                        max(0, int(expected_offset or 0)),
                        now,
                        expected_hash,
                        max(0, int(expected_offset or 0)),
                        now,
                    ),
                )
            else:
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
        self._signal_change()
        return True

    def get_file_state(
        self, tool_name: str, relative_path: str
    ) -> tuple[str | None, int]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(observed_hash, last_hash),
                          COALESCE(observed_offset, last_offset, 0)
                   FROM file_state WHERE tool_name=? AND relative_path=?""",
                (tool_name, relative_path),
            ).fetchone()
            return (row[0], int(row[1])) if row else (None, 0)

    def get_source_revision(
        self,
        tool_name: str,
        relative_path: str,
    ) -> tuple[int | None, int | None]:
        """Return the last fully observed filesystem revision."""
        with self._lock:
            row = self._conn.execute(
                """SELECT source_size, source_mtime_ns FROM file_state
                   WHERE tool_name=? AND relative_path=?""",
                (tool_name, relative_path),
            ).fetchone()
        if row is None:
            return None, None
        return (
            int(row[0]) if row[0] is not None else None,
            int(row[1]) if row[1] is not None else None,
        )

    @_rollback_on_error
    def record_unchanged_source(
        self,
        tool_name: str,
        relative_path: str,
        content_hash: str,
        *,
        source_size: int,
        source_mtime_ns: int,
    ) -> bool:
        """Advance only the cheap observation token for identical content."""

        with self._lock:
            cursor = self._conn.execute(
                """UPDATE file_state
                   SET source_size=?, source_mtime_ns=?
                   WHERE tool_name=? AND relative_path=?
                     AND COALESCE(observed_hash, last_hash)=?""",
                (
                    max(0, int(source_size)),
                    max(0, int(source_mtime_ns)),
                    tool_name,
                    relative_path,
                    content_hash,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    @_rollback_on_error
    def update_file_state(
        self, tool_name: str, relative_path: str, content_hash: str, offset: int
    ) -> None:
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
                (
                    tool_name,
                    relative_path,
                    content_hash,
                    offset,
                    content_hash,
                    offset,
                    now,
                ),
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
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM queue WHERE status IN ('pending','uploading')"
                ).fetchone()[0]
            )

    def health_status(self, diagnostic_limit: int = 10) -> dict[str, Any]:
        """Return queue counts and safe terminal diagnostics for CLI health."""

        terminal_statuses = ("auth_blocked", "repair_required", "quarantined")
        with self._lock:
            counts = {
                str(status): int(count)
                for status, count in self._conn.execute(
                    """SELECT status, COUNT(*) FROM queue
                       GROUP BY status ORDER BY status"""
                ).fetchall()
            }
            rows = self._conn.execute(
                """SELECT id, tool_name, relative_path, status, outcome_state,
                          diagnostic_code, http_status, last_error, terminal_at
                   FROM queue
                   WHERE status IN (?,?,?)
                   ORDER BY COALESCE(terminal_at, created_at) DESC, id DESC
                   LIMIT ?""",
                (*terminal_statuses, max(0, int(diagnostic_limit))),
            ).fetchall()
        return {
            "counts": counts,
            "actionable": counts.get("pending", 0) + counts.get("uploading", 0),
            "terminal": sum(counts.get(status, 0) for status in terminal_statuses),
            "diagnostics": [
                {
                    "id": int(row[0]),
                    "tool": str(row[1]),
                    "relative_path": str(row[2]),
                    "status": str(row[3]),
                    "outcome": str(row[4] or ""),
                    "code": str(row[5] or ""),
                    "http_status": (
                        int(row[6]) if row[6] is not None else None
                    ),
                    "diagnostic": str(row[7] or ""),
                    "terminal_at": (
                        float(row[8]) if row[8] is not None else None
                    ),
                }
                for row in rows
            ],
        }

    @_rollback_on_error
    def requeue_terminal(self, item_id: int) -> bool:
        """Explicitly retry one inspected terminal row if its payload is intact."""

        with self._lock:
            row = self._conn.execute(
                """SELECT payload_path FROM queue
                   WHERE id=? AND status IN (
                       'auth_blocked','repair_required','quarantined'
                   )""",
                (int(item_id),),
            ).fetchone()
            if row is None:
                return False
            if row[0] and not Path(str(row[0])).is_file():
                return False
            cursor = self._conn.execute(
                """UPDATE queue
                   SET status='pending', retry_count=0, available_at=0,
                       lease_token=NULL, lease_until=NULL, last_attempt_at=NULL,
                       last_error=NULL, outcome_state=NULL, diagnostic_code=NULL,
                       http_status=NULL, terminal_at=NULL,
                       blocked_config_fingerprint=NULL
                   WHERE id=? AND status IN (
                       'auth_blocked','repair_required','quarantined'
                   )""",
                (int(item_id),),
            )
            self._conn.commit()
            changed = cursor.rowcount == 1
        if changed:
            self._signal_change()
        return changed

    def outstanding_bytes(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    """SELECT COALESCE(SUM(
                       CASE WHEN payload_bytes > 0 THEN payload_bytes ELSE file_size END
                   ), 0) FROM queue WHERE status IN ('pending','uploading')"""
                ).fetchone()[0]
            )

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
        self._signal_change()

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
        self._signal_change()

    @_rollback_on_error
    def prioritize_file(self, tool_name: str, relative_path: str) -> int:
        """Move a server-requested repair ahead of every ordinary backlog row.

        ``created_at=0`` is an explicit priority marker consumed by
        ``claim_batch`` before its metadata/live-tail classes. Merely making a
        full repair older is insufficient because a continuously growing
        transcript can otherwise keep producing class-1 deltas and starve the
        class-2 repair forever.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                """UPDATE queue
                   SET created_at=0, available_at=0
                   WHERE tool_name=? AND relative_path=? AND status='pending'""",
                (tool_name, relative_path),
            )
            self._conn.commit()
            changed = cursor.rowcount
        if changed:
            self._signal_change()
        return changed

    def close(self) -> None:
        with self._lock:
            self._conn.close()
