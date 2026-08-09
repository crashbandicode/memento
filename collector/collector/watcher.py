"""File watcher — cross-platform file monitoring via watchdog with debouncing and event routing."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .compat import normalize_path, path_starts_with
from .config import CollectorConfig
from .interaction_signals import (
    extract_conversation_activity_updates,
    extract_conversation_interaction_updates,
)
from .parsers.base import BaseParser
from .parsers.json_parser import JsonParser
from .parsers.jsonl import JsonlParser
from .parsers.markdown import MarkdownParser
from .parsers.sqlite_parser import SqliteParser
from .parsers.toml_parser import TomlParser
from .queue import SyncQueue
from .sanitizer import (
    sanitize_json,
    sanitize_jsonl,
    sanitize_jsonl_line,
    sanitize_text,
)
from .tools.base import BaseTool, ContentType, SyncStrategy

logger = logging.getLogger("collector.watcher")

# watchdog exposes read-only filesystem activity through ``on_any_event`` on
# platforms that support it. Opening a transcript, reading it, and closing it
# without a write cannot change what the collector uploads, so letting those
# events enter the debounce queue creates needless stat/parse/hash work.
_NO_CONTENT_CHANGE_EVENT_TYPES = frozenset(
    {
        "accessed",
        "opened",
        "closed_no_write",
    }
)


_FAST_HASH_READ = 256 * 1024  # Read first 256KB for fast hashing
# Bump this whenever sanitization/parsing can change server-visible FULL
# content. A matching stat token may skip work only within the same epoch.
FULL_IDENTITY_VERSION = "sanitized-payload-v1"
# An acknowledged pre-canonical FULL hash can prove its historical source
# revision without pretending that hash describes the sanitized payload.
LEGACY_FULL_PROOF_VERSION = "legacy-full-source-v1"
LEGACY_RECONCILE_MAX_FILES = 16
LEGACY_RECONCILE_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
LEGACY_RECONCILE_MAX_SINGLE_SOURCE_BYTES = 256 * 1024 * 1024
LEGACY_RECONCILE_MAX_SECONDS = 30.0
LEGACY_RECONCILE_RETRY_SECONDS = 60.0


def _file_hash_revision(path: Path, *, size: int, mtime_ns: int) -> str:
    """Hash exact source bytes; size/mtime are observation tokens, not identity."""

    del mtime_ns
    try:
        h = hashlib.sha256()
        remaining = max(0, int(size))
        with open(path, "rb") as stream:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        if remaining:
            return ""
        return h.hexdigest()
    except OSError:
        return ""


def _legacy_full_hash_revision(path: Path, *, size: int, mtime_ns: int) -> str:
    """Reproduce the pre-canonical FULL identity for one-time adoption."""

    try:
        h = hashlib.sha256()
        h.update(f"{size}:{mtime_ns}".encode())
        with open(path, "rb") as stream:
            h.update(stream.read(min(_FAST_HASH_READ, max(0, int(size)))))
        return h.hexdigest()
    except OSError:
        return ""


def _delta_hash_revision(path: Path, *, size: int) -> str:
    """Return a reproducible append-prefix token for guarded JSONL deltas.

    A file mtime changes on every append, so the old token could not be
    reconstructed for a previously committed offset after queue recovery.
    Prefixing this deterministic scheme lets newer collectors verify and
    resume a server-advertised base without rereading the whole transcript.
    """
    try:
        h = hashlib.sha256()
        h.update(f"append-prefix:{size}:".encode())
        with open(path, "rb") as stream:
            h.update(stream.read(min(_FAST_HASH_READ, size)))
        return f"d2:{h.hexdigest()[:61]}"
    except OSError:
        return ""


def _file_hash(path: Path) -> str:
    """Compatibility helper returning exact source-byte identity."""
    try:
        stat = path.stat()
        return _file_hash_revision(
            path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
    except OSError:
        return ""


class _DebouncedHandler(FileSystemEventHandler):
    """Collects events and fires a debounced callback per unique path."""

    def __init__(
        self,
        callback: Callable[[Path], None],
        debounce_seconds: float,
        excluded_patterns: list[str],
    ) -> None:
        self._callback = callback
        self._debounce = max(0.0, debounce_seconds)
        self._excluded = excluded_patterns
        self._pending: dict[str, float] = {}
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._stopped = False

    def _is_excluded(self, path: str) -> bool:
        norm = normalize_path(path)
        for pattern in self._excluded:
            if fnmatch(norm, normalize_path(pattern)):
                return True
        return False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        event_type = getattr(event, "event_type", "")
        if event_type in _NO_CONTENT_CHANGE_EVENT_TYPES:
            return

        # A moved file no longer exists at src_path by the time the debounced
        # callback runs. Route the destination instead so archive/rename moves
        # still trigger a real content sync.
        path = (
            getattr(event, "dest_path", "") if event_type == "moved" else event.src_path
        )
        if not path:
            return
        if self._is_excluded(path):
            return

        with self._condition:
            if self._stopped:
                return
            was_idle = not self._pending
            self._pending[path] = time.monotonic() + self._debounce
            if self._worker is None:
                worker = threading.Thread(
                    target=self._run,
                    name=f"memento-debouncer-{id(self):x}",
                    daemon=True,
                )
                self._worker = worker
                try:
                    worker.start()
                except Exception:
                    self._worker = None
                    raise
            # Every new deadline is based on the same debounce interval and
            # therefore cannot precede an already-pending deadline. Wake only
            # an idle worker; otherwise it will dispatch each path when that
            # path's own deadline arrives without a condition-notify storm.
            if was_idle:
                self._condition.notify()

    def _run(self) -> None:
        """Wait for a quiet period, then dispatch one coalesced path batch."""
        while True:
            with self._condition:
                while True:
                    if self._stopped:
                        return
                    if not self._pending:
                        self._condition.wait()
                        continue

                    now = time.monotonic()
                    remaining = min(self._pending.values()) - now
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue

                    paths = [
                        path
                        for path, deadline in self._pending.items()
                        if deadline <= now
                    ]
                    for path in paths:
                        del self._pending[path]
                    break

            for path_str in paths:
                with self._condition:
                    if self._stopped:
                        return
                path = Path(path_str)
                if not path.exists() or not path.is_file():
                    continue
                with self._condition:
                    if self._stopped:
                        return
                try:
                    self._callback(path)
                except Exception:
                    logger.exception("Error processing %s", path)

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._pending.clear()
            worker = self._worker
            self._condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join()


class FileWatcher:
    """Orchestrates watching all tool directories and processing changes."""

    def __init__(
        self,
        tools: list[BaseTool],
        queue: SyncQueue,
        config: CollectorConfig,
    ) -> None:
        self._tools = tools
        self._queue = queue
        self._config = config
        self._observer = Observer()
        self._stop_event = threading.Event()
        self._scan_cancel_event = threading.Event()
        self._scan_lock = threading.Lock()
        self._legacy_reconcile_lock = threading.Lock()
        self._processing_lock = threading.Lock()
        self._resync_lock = threading.Lock()
        self._resyncing_paths: set[str] = set()
        self._catchup_lock = threading.Lock()
        self._catching_up_paths: set[str] = set()
        self._handlers: list[_DebouncedHandler] = []
        self._tool_map: dict[str, BaseTool] = {}  # root_path_str -> tool

        # Build parser registry
        self._parsers: list[BaseParser] = [
            MarkdownParser(),
            JsonlParser(),
            JsonParser(),
            TomlParser(),
            SqliteParser(),
        ]

        # Build excluded patterns from all tools
        all_excluded: list[str] = []
        for tool in tools:
            all_excluded.extend(tool.excluded_paths)

        # Register watches — collect all unique directories to watch per tool
        for tool in tools:
            if not tool.is_available():
                logger.info("Tool %s not available, skipping", tool.name)
                continue

            # Collect all unique root dirs from watch paths
            watch_dirs: set[str] = {str(tool.root_path)}
            for wp in tool.get_watch_paths():
                # Add parent directories that might be outside tool.root_path
                wp_str = str(wp.path)
                if not wp_str.startswith(str(tool.root_path)):
                    watch_dirs.add(wp_str)

            # Dedupe: drop any watch_dir that's already a subdirectory of another
            # (prevents duplicate events from nested recursive watches)
            normalized = sorted(watch_dirs, key=len)
            deduped: list[str] = []
            for d in normalized:
                if any(d.startswith(p + "/") or d == p for p in deduped):
                    continue
                deduped.append(d)
            watch_dirs = set(deduped)

            for watch_dir in watch_dirs:
                if not Path(watch_dir).exists():
                    continue
                self._tool_map[watch_dir] = tool

                handler = _DebouncedHandler(
                    callback=self._on_file_changed,
                    debounce_seconds=config.debounce_seconds,
                    excluded_patterns=all_excluded,
                )

                try:
                    self._observer.schedule(
                        handler,
                        watch_dir,
                        recursive=True,
                    )
                    self._handlers.append(handler)
                    logger.info(
                        "Watching %s (%s) at %s",
                        tool.display_name,
                        tool.name,
                        watch_dir,
                    )
                except OSError as e:
                    logger.error("Cannot watch %s: %s", watch_dir, e)

    def _find_tool(self, path: Path) -> BaseTool | None:
        """Find which tool owns a file path."""
        for root_str, tool in self._tool_map.items():
            if path_starts_with(str(path), root_str):
                return tool
        return None

    def request_full_resync(self, source_path: str) -> None:
        """Schedule one complete snapshot after the server rejects a delta base."""
        path = Path(source_path)
        path_key = normalize_path(str(path))
        with self._resync_lock:
            if path_key in self._resyncing_paths or self._stop_event.is_set():
                return
            self._resyncing_paths.add(path_key)

        def run() -> None:
            try:
                if self._stop_event.is_set() or not path.is_file():
                    return
                tool = self._find_tool(path)
                if tool is None:
                    return
                classification = tool.classify_file(path)
                if classification is None:
                    return
                for _attempt in range(3):
                    if self._stop_event.is_set() or not path.is_file():
                        return
                    self._on_file_changed(
                        path,
                        force_full=True,
                        emit_live_signals=False,
                    )
                    self._queue.prioritize_file(
                        classification.tool_name,
                        classification.relative_path,
                    )
                    observed_hash, observed_offset = self._queue.get_file_state(
                        classification.tool_name,
                        classification.relative_path,
                    )
                    if observed_hash and observed_offset > 0:
                        logger.info("Queued complete resync for %s", path)
                        return
                    time.sleep(0.5)
                logger.warning(
                    "Could not capture a stable complete resync for %s", path
                )
            finally:
                with self._resync_lock:
                    self._resyncing_paths.discard(path_key)

        threading.Thread(
            target=run,
            name="memento-delta-resync",
            daemon=True,
        ).start()

    def rebuild_terminal_source(
        self,
        source_path: str,
        *,
        tool_name: str,
        relative_path: str,
    ) -> bool:
        """Synchronously rebuild one explicitly selected pruned terminal row."""

        path = Path(source_path)
        if not path.is_file():
            return False
        tool = self._find_tool(path)
        if tool is None:
            return False
        classification = tool.classify_file(path)
        if (
            classification is None
            or classification.tool_name != tool_name
            or classification.relative_path != relative_path
        ):
            return False
        self._on_file_changed(path, force_full=True, emit_live_signals=False)
        self._queue.prioritize_file(tool_name, relative_path)
        return True

    def request_delta_catchup(self, source_path: str) -> None:
        """Queue the next bounded tail after the previous one is acknowledged.

        A transcript can grow by hundreds of megabytes while the collector is
        offline. Reading that whole append into one Python string multiplies
        memory during JSON decoding and sanitization. The watcher therefore
        captures at most one configured delta window at a time; this callback
        advances the durable chain once the server has accepted that window.
        """
        path = Path(source_path)
        path_key = normalize_path(str(path))
        with self._catchup_lock:
            if path_key in self._catching_up_paths or self._stop_event.is_set():
                return
            self._catching_up_paths.add(path_key)

        def run() -> None:
            try:
                if self._stop_event.is_set() or not path.is_file():
                    return
                self._on_file_changed(path, emit_live_signals=False)
            finally:
                with self._catchup_lock:
                    self._catching_up_paths.discard(path_key)

        threading.Thread(
            target=run,
            name="memento-delta-catchup",
            daemon=True,
        ).start()

    def request_relative_resync(self, tool_name: str, relative_path: str) -> bool:
        """Safely resolve and queue one server-selected conversation snapshot."""
        tool = next((item for item in self._tools if item.name == tool_name), None)
        if tool is None or not isinstance(relative_path, str):
            return False
        normalized_relative = relative_path.replace("\\", "/")
        parts = [
            part for part in normalized_relative.split("/") if part not in ("", ".")
        ]
        if not parts or ".." in parts or Path(normalized_relative).is_absolute():
            return False
        root = tool.root_path.resolve()
        source_path = root.joinpath(*parts).resolve()
        if (
            not path_starts_with(str(source_path), str(root))
            or not source_path.is_file()
        ):
            return False
        self.request_full_resync(str(source_path))
        return True

    def _get_parser(self, content_type: ContentType) -> BaseParser | None:
        ext_map = {
            ContentType.MARKDOWN: ".md",
            ContentType.JSONL: ".jsonl",
            ContentType.JSON: ".json",
            ContentType.TOML: ".toml",
            ContentType.SQLITE: ".sqlite",
        }
        dummy_ext = ext_map.get(content_type)
        if dummy_ext is None:
            return None
        dummy_path = Path(f"dummy{dummy_ext}")
        for parser in self._parsers:
            if parser.can_parse(dummy_path):
                return parser
        return None

    def _process_antigravity_pb(self, path: Path) -> None:
        """Decrypt+decode an Antigravity .pb file and enqueue it as a conversation."""
        try:
            from .parsers.antigravity_export import export_conversations
        except Exception:
            return

        try:
            convos = export_conversations(pb_files=[path])
        except Exception:
            logger.debug("Antigravity pb decode failed for %s", path)
            return

        for conv in convos:
            content = conv["content"]
            meta: dict = {"source": "aghistory", "doc_type": "full_conversation"}
            if conv.get("title"):
                meta["title"] = conv["title"]
            if conv.get("cascade_id"):
                meta["session_id"] = conv["cascade_id"]
            if conv.get("project_name"):
                meta["project_hash"] = conv["project_name"]
            if conv.get("workspace"):
                meta["project_path"] = conv["workspace"]
            if conv.get("export_diagnostics"):
                meta["export_diagnostics"] = conv["export_diagnostics"]
            self._queue.enqueue(
                tool_name="antigravity",
                category="conversation",
                content_type="jsonl",
                relative_path=f"conversations/{conv['cascade_id']}.jsonl",
                content=content,
                content_hash=conv.get(
                    "content_hash",
                    f"ag-{hash(content) & 0xFFFFFFFF:08x}",
                ),
                file_size=len(content),
                sync_strategy="full",
                metadata=meta,
                source_modified_at=conv.get("source_modified_at"),
            )
            logger.info(
                "Queued antigravity/conversations/%s.jsonl (conversation, jsonl)",
                conv["cascade_id"],
            )

    def _on_file_changed(
        self,
        path: Path,
        force_full: bool = False,
        emit_live_signals: bool = True,
    ) -> None:
        """Serialize parsing and make shutdown a hard callback boundary."""
        if self._stop_event.is_set():
            return
        with self._processing_lock:
            if self._stop_event.is_set():
                return
            self._process_file_changed(
                path,
                force_full=force_full,
                emit_live_signals=emit_live_signals,
            )

    def _process_file_changed(
        self,
        path: Path,
        force_full: bool = False,
        emit_live_signals: bool = True,
    ) -> None:
        tool = self._find_tool(path)
        if tool is None:
            return

        classification = tool.classify_file(path)
        if classification is None:
            return

        # Special handling for encrypted Antigravity .pb files
        if classification.metadata.get("__antigravity_pb__"):
            self._process_antigravity_pb(path)
            return

        # Skip POLL strategy files (SQLite)
        if classification.sync_strategy == SyncStrategy.POLL:
            return

        # A same-path delta already being uploaded is an ordering barrier for
        # canonical transcript content. Questions must still reach the inbox
        # immediately, so publish their tiny state records on the independent
        # metadata lane before honoring that barrier.
        if (
            emit_live_signals
            and classification.category.value == "conversation"
            and classification.content_type == ContentType.JSONL
            and classification.sync_strategy == SyncStrategy.DELTA
        ):
            try:
                interaction_updates = extract_conversation_interaction_updates(
                    path,
                    tool_name=classification.tool_name,
                    relative_path=classification.relative_path,
                )
                if interaction_updates:
                    self._queue.enqueue_metadata_changes(
                        namespace="conversation_interactions",
                        tool_name=classification.tool_name,
                        records=interaction_updates,
                    )
                activity_updates = extract_conversation_activity_updates(
                    path,
                    tool_name=classification.tool_name,
                    relative_path=classification.relative_path,
                )
                if activity_updates:
                    self._queue.enqueue_metadata_changes(
                        namespace="conversation_activities",
                        tool_name=classification.tool_name,
                        records=activity_updates,
                    )
            except Exception:
                logger.debug(
                    "Could not extract live interaction state from %s",
                    path,
                    exc_info=True,
                )

        if not force_full and classification.sync_strategy == SyncStrategy.DELTA:
            path_key = normalize_path(str(path))
            repair_is_being_captured = False
            resync_lock = getattr(self, "_resync_lock", None)
            if resync_lock is not None:
                with resync_lock:
                    repair_is_being_captured = path_key in self._resyncing_paths
            has_uncommitted = getattr(
                self._queue,
                "has_uncommitted_delta_revision",
                None,
            )
            if repair_is_being_captured or (
                has_uncommitted is not None
                and has_uncommitted(
                    classification.tool_name,
                    classification.relative_path,
                )
            ):
                # The acknowledgement callback captures the next bounded
                # window after the current one is committed. Do not build a
                # speculative tail on a merely pending or leased revision.
                return

        try:
            source_stat = path.stat()
            file_size = source_stat.st_size
        except OSError:
            return
        source_revision = (source_stat.st_size, source_stat.st_mtime_ns)
        get_source_revision = getattr(self._queue, "get_source_revision", None)
        if not force_full and callable(get_source_revision):
            observed_source_revision = get_source_revision(
                classification.tool_name,
                classification.relative_path,
                identity_version=(
                    FULL_IDENTITY_VERSION
                    if classification.sync_strategy == SyncStrategy.FULL
                    else None
                ),
            )
            if (
                observed_source_revision != source_revision
                and classification.sync_strategy == SyncStrategy.FULL
            ):
                observed_source_revision = get_source_revision(
                    classification.tool_name,
                    classification.relative_path,
                    identity_version=LEGACY_FULL_PROOF_VERSION,
                )
            if observed_source_revision == source_revision:
                # Startup/catch-up scans can contain thousands of durable,
                # unchanged files. A DELTA stat token proves only that the
                # source was observed, not that its entire length committed.
                # A base conflict intentionally preserves the current stat
                # while rewinding the committed offset, so skipping solely on
                # the stat would strand that source across every restart.
                source_is_committed = True
                if classification.sync_strategy == SyncStrategy.DELTA:
                    _, committed_offset = self._queue.get_delta_base(
                        classification.tool_name,
                        classification.relative_path,
                    )
                    source_is_committed = committed_offset == file_size
                if source_is_committed:
                    return

        legacy_adoption_hash = None
        legacy_source_hash = ""
        get_legacy_adoption = getattr(
            self._queue,
            "get_legacy_full_adoption_hash",
            None,
        )
        if (
            not force_full
            and classification.sync_strategy == SyncStrategy.FULL
            and callable(get_legacy_adoption)
        ):
            legacy_adoption_hash = get_legacy_adoption(
                classification.tool_name,
                classification.relative_path,
                identity_version=FULL_IDENTITY_VERSION,
            )
            if legacy_adoption_hash:
                legacy_source_hash = _legacy_full_hash_revision(
                    path,
                    size=file_size,
                    mtime_ns=source_stat.st_mtime_ns,
                )
                if legacy_source_hash == legacy_adoption_hash:
                    # Inactive acknowledged legacy rows need no canonical
                    # payload merely to make their unchanged source revision
                    # durable.  Keep the identity domains distinct so active
                    # transition rows still take the canonical reconciliation
                    # path below.
                    try:
                        proof_stat = path.stat()
                    except OSError:
                        return
                    proof_is_stable = (
                        source_stat.st_dev == proof_stat.st_dev
                        and source_stat.st_ino == proof_stat.st_ino
                        and (proof_stat.st_size, proof_stat.st_mtime_ns)
                        == source_revision
                    )
                    if not proof_is_stable:
                        return
                    record_legacy_source = getattr(
                        self._queue,
                        "record_unchanged_legacy_full_source",
                        None,
                    )
                    if callable(record_legacy_source) and record_legacy_source(
                        classification.tool_name,
                        classification.relative_path,
                        legacy_hash=legacy_adoption_hash,
                        source_size=file_size,
                        source_mtime_ns=source_stat.st_mtime_ns,
                        identity_version=LEGACY_FULL_PROOF_VERSION,
                    ):
                        logger.debug(
                            "Recorded unchanged legacy FULL source proof for %s",
                            path,
                        )
                        return

        # DELTA revisions deliberately retain the deterministic append-prefix
        # token introduced by d0d50a6. FULL identity is computed later from
        # the exact sanitized bytes while they are already being spooled.
        current_hash = ""
        if classification.sync_strategy == SyncStrategy.DELTA:
            current_hash = _delta_hash_revision(path, size=file_size)
            if not current_hash:
                return

        last_hash, _ = self._queue.get_file_state(
            classification.tool_name,
            classification.relative_path,
        )

        # Determine the byte-bounded capture window for delta sync.
        read_offset = 0
        read_end_offset = file_size
        base_hash: str | None = None
        base_offset = 0
        if classification.sync_strategy == SyncStrategy.DELTA:
            max_delta_bytes = getattr(
                self._config,
                "max_delta_upload_bytes",
                16 * 1024 * 1024,
            )
            if not force_full:
                base_hash, base_offset = self._queue.get_delta_base(
                    classification.tool_name,
                    classification.relative_path,
                )
                if file_size < base_offset:
                    # File was truncated, re-sync from beginning.
                    read_offset = 0
                    base_hash = None
                    base_offset = 0
                else:
                    read_offset = base_offset
                    if base_hash and base_hash.startswith("d2:"):
                        local_base_hash = _delta_hash_revision(
                            path,
                            size=base_offset,
                        )
                        if local_base_hash != base_hash:
                            logger.warning(
                                "Committed delta prefix no longer matches %s; "
                                "capturing a bounded authoritative base",
                                path,
                            )
                            force_full = True
                            read_offset = 0
                            base_hash = None
                            base_offset = 0
            if file_size - read_offset > max_delta_bytes:
                read_end_offset = read_offset + max_delta_bytes
                logger.info(
                    "Delta backlog exceeds %d bytes; queueing bounded %s for %s",
                    max_delta_bytes,
                    "base" if read_offset == 0 else "tail",
                    path,
                )

        # Parse and sanitize (with error protection). Production JSONL parsing
        # writes directly into the queue's thresholded spool writer, which
        # computes canonical identity without a second source read or a
        # payload-sized Python string.
        prepared_payload = None
        payload_writer = None
        try:
            parser = self._get_parser(classification.content_type)
            append_only_snapshot = (
                force_full
                and classification.sync_strategy == SyncStrategy.DELTA
                and classification.content_type == ContentType.JSONL
                and isinstance(parser, JsonlParser)
            )
            bounded_append_window = (
                classification.sync_strategy == SyncStrategy.DELTA
                and isinstance(parser, JsonlParser)
                and read_end_offset < file_size
            )
            can_stream_jsonl = (
                type(parser) is JsonlParser
                and callable(getattr(self._queue, "payload_writer", None))
            )
            if can_stream_jsonl:
                payload_writer = self._queue.payload_writer()
                result = parser.parse_to_writer(
                    path,
                    payload_writer.write,
                    offset=0 if append_only_snapshot else read_offset,
                    end_offset=read_end_offset,
                    transform_line=lambda line: sanitize_jsonl_line(line).content,
                )
                prepared_payload = payload_writer.finish()
                parsed_content = ""
                payload_has_content = prepared_payload.has_non_whitespace
                payload_bytes = prepared_payload.payload_bytes
            elif parser is None:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return
                parsed_content = content
                payload_has_content = bool(parsed_content.strip())
                payload_bytes = len(parsed_content.encode("utf-8"))
                new_offset = path.stat().st_size
                is_partial = read_offset > 0
            else:
                if append_only_snapshot:
                    result = parser.parse(
                        path,
                        offset=0,
                        end_offset=read_end_offset,
                    )
                elif isinstance(parser, JsonlParser):
                    result = parser.parse(
                        path,
                        offset=read_offset,
                        end_offset=read_end_offset,
                    )
                else:
                    result = parser.parse(path, offset=read_offset)
                parsed_content = result.content
                payload_has_content = bool(parsed_content.strip())
                payload_bytes = len(parsed_content.encode("utf-8"))
            if parser is not None:
                new_offset = result.offset if result.offset else path.stat().st_size
                is_partial = result.is_partial
                classification.metadata.update(result.metadata)
                if result.title:
                    classification.metadata["title"] = result.title
        except Exception:
            if payload_writer is not None:
                payload_writer.abort()
            if prepared_payload is not None:
                self._queue.discard_prepared_payload(prepared_payload)
            logger.debug("Parse error for %s, skipping", path)
            return

        if append_only_snapshot or read_end_offset < file_size:
            current_hash = _delta_hash_revision(path, size=new_offset)
            if not current_hash:
                if prepared_payload is not None:
                    self._queue.discard_prepared_payload(prepared_payload)
                return

        if not payload_has_content:
            if prepared_payload is not None:
                self._queue.discard_prepared_payload(prepared_payload)
            return

        if prepared_payload is None:
            # Sanitize before enqueue (defense-in-depth vs local SQLite leak).
            if classification.content_type == ContentType.JSONL:
                san = sanitize_jsonl(parsed_content)
            elif classification.content_type == ContentType.JSON:
                san = sanitize_json(parsed_content)
            else:
                san = sanitize_text(parsed_content)
            parsed_content = san.content
            payload_has_content = bool(parsed_content.strip())
            payload_bytes = len(parsed_content.encode("utf-8"))
            if not payload_has_content:
                return
        if classification.sync_strategy == SyncStrategy.FULL:
            current_hash = (
                prepared_payload.content_hash
                if prepared_payload is not None
                else hashlib.sha256(parsed_content.encode("utf-8")).hexdigest()
            )

        # Hash, parse, and timestamp must describe one stable source revision.
        # A concurrent append generates another watcher event; returning here
        # leaves file_state untouched so that event (or the next scan) retries
        # the complete newer revision rather than pairing old content with its
        # new mtime.
        try:
            final_stat = path.stat()
        except OSError:
            if prepared_payload is not None:
                self._queue.discard_prepared_payload(prepared_payload)
            return
        if append_only_snapshot or bounded_append_window:
            same_file = (
                source_stat.st_dev == final_stat.st_dev
                and source_stat.st_ino == final_stat.st_ino
            )
            same_revision = (
                final_stat.st_size,
                final_stat.st_mtime_ns,
            ) == source_revision
            appended_after_capture = final_stat.st_size > source_stat.st_size
            if (
                not same_file
                or final_stat.st_size < new_offset
                or not (same_revision or appended_after_capture)
            ):
                logger.debug("Source was replaced while processing %s; deferring", path)
                if prepared_payload is not None:
                    self._queue.discard_prepared_payload(prepared_payload)
                return
        elif (final_stat.st_size, final_stat.st_mtime_ns) != source_revision:
            logger.debug("Source changed while processing %s; deferring", path)
            if prepared_payload is not None:
                self._queue.discard_prepared_payload(prepared_payload)
            return
        source_modified_at = source_stat.st_mtime

        if (
            legacy_adoption_hash
            and legacy_source_hash == legacy_adoption_hash
            and classification.sync_strategy == SyncStrategy.FULL
        ):
            adopt_legacy_result = getattr(
                self._queue,
                "adopt_legacy_full_source_result",
                None,
            )
            if callable(adopt_legacy_result):
                adoption_result = adopt_legacy_result(
                    classification.tool_name,
                    classification.relative_path,
                    legacy_hash=legacy_adoption_hash,
                    canonical_hash=current_hash,
                    source_size=new_offset,
                    source_mtime_ns=source_stat.st_mtime_ns,
                    identity_version=FULL_IDENTITY_VERSION,
                )
            else:
                adopt_legacy = getattr(
                    self._queue,
                    "adopt_legacy_full_source",
                    None,
                )
                adoption_result = (
                    "adopted"
                    if callable(adopt_legacy)
                    and adopt_legacy(
                        classification.tool_name,
                        classification.relative_path,
                        legacy_hash=legacy_adoption_hash,
                        canonical_hash=current_hash,
                        source_size=new_offset,
                        source_mtime_ns=source_stat.st_mtime_ns,
                        identity_version=FULL_IDENTITY_VERSION,
                    )
                    else "state_changed"
                )
            if adoption_result == "adopted":
                if prepared_payload is not None:
                    self._queue.discard_prepared_payload(prepared_payload)
                logger.info("Adopted unchanged legacy FULL state for %s", path)
                return
            # A second collector may have leased a same-path row while this
            # source was being parsed. Keep the legacy state eligible for a
            # later proof rather than stamping it canonical and allowing the
            # duplicate transition row to escape reconciliation.
            if callable(get_legacy_adoption) and get_legacy_adoption(
                classification.tool_name,
                classification.relative_path,
                identity_version=FULL_IDENTITY_VERSION,
            ) and adoption_result != "canonical_mismatch":
                if prepared_payload is not None:
                    self._queue.discard_prepared_payload(prepared_payload)
                return

        if (
            not force_full
            and classification.sync_strategy == SyncStrategy.FULL
            and current_hash == last_hash
        ):
            record_unchanged = getattr(
                self._queue,
                "record_unchanged_source",
                None,
            )
            if callable(record_unchanged):
                record_unchanged(
                    classification.tool_name,
                    classification.relative_path,
                    current_hash,
                    source_size=new_offset,
                    source_mtime_ns=source_stat.st_mtime_ns,
                    identity_version=FULL_IDENTITY_VERSION,
                )
            if prepared_payload is not None:
                self._queue.discard_prepared_payload(prepared_payload)
            return

        queue_metadata = dict(classification.metadata)
        if force_full:
            # The collector's content hash is deliberately stable for an
            # unchanged source.  Tag an explicit repair snapshot so the
            # chunk uploader can give it a fresh upload identity and bypass a
            # completed server receipt without leaking queue-only state into
            # the document metadata.
            queue_metadata["_queue_force_reprocess_nonce"] = uuid.uuid4().hex

        self._queue.enqueue(
            tool_name=classification.tool_name,
            category=classification.category.value,
            content_type=classification.content_type.value,
            relative_path=classification.relative_path,
            content=parsed_content,
            content_hash=current_hash,
            file_size=payload_bytes,
            sync_strategy=classification.sync_strategy.value,
            is_partial=is_partial,
            offset=new_offset,
            metadata=queue_metadata,
            source_modified_at=source_modified_at,
            base_hash=base_hash if is_partial else None,
            base_offset=base_offset if is_partial else 0,
            source_path=str(path),
            source_size=new_offset,
            source_mtime_ns=source_stat.st_mtime_ns,
            identity_version=(
                FULL_IDENTITY_VERSION
                if classification.sync_strategy == SyncStrategy.FULL
                else None
            ),
            prepared_payload=prepared_payload,
        )

        logger.info(
            "Queued %s/%s (%s, %s%s)",
            classification.tool_name,
            classification.relative_path,
            classification.category.value,
            classification.content_type.value,
            " delta" if is_partial else "",
        )

    def reconcile_legacy_full_queue(
        self,
        *,
        max_files: int = LEGACY_RECONCILE_MAX_FILES,
        max_source_bytes: int = LEGACY_RECONCILE_MAX_SOURCE_BYTES,
        max_single_source_bytes: int = LEGACY_RECONCILE_MAX_SINGLE_SOURCE_BYTES,
        max_seconds: float = LEGACY_RECONCILE_MAX_SECONDS,
    ) -> dict[str, int]:
        """Reconcile a bounded batch before legacy-transition rows can upload.

        Candidate selection is deliberately narrow and durable in ``SyncQueue``.
        Exact legacy and canonical proofs still happen through the normal FULL
        processing path, including its final stat race check and atomic adoption.
        Negative or oversized cases are released unchanged; interrupted proof is
        durably backed off so it cannot create a startup or maintenance busy loop.
        """

        reconcile_lock = getattr(self, "_legacy_reconcile_lock", None)
        if reconcile_lock is None:
            reconcile_lock = threading.Lock()
            self._legacy_reconcile_lock = reconcile_lock
        if not reconcile_lock.acquire(blocking=False):
            return {
                "examined": 0,
                "resolved": 0,
                "released": 0,
                "deferred": 0,
                "remaining": 0,
            }

        result = {
            "examined": 0,
            "resolved": 0,
            "released": 0,
            "deferred": 0,
            "remaining": 0,
        }
        try:
            remaining = self._queue.begin_legacy_full_reconciliation()
            if remaining == 0:
                return result

            candidates = self._queue.legacy_full_reconciliation_candidates(
                limit=max(0, int(max_files)),
            )
            deadline = time.monotonic() + max(0.0, float(max_seconds))
            total_source_bytes = 0
            for candidate in candidates:
                if result["examined"] and time.monotonic() >= deadline:
                    break

                path = Path(candidate.source_path)
                try:
                    source_stat = path.stat()
                except FileNotFoundError:
                    if self._queue.release_legacy_full_reconciliation_candidate(
                        candidate.id
                    ):
                        result["released"] += 1
                    result["examined"] += 1
                    continue
                except OSError:
                    if self._queue.defer_legacy_full_reconciliation_candidate(
                        candidate.id,
                        delay_seconds=LEGACY_RECONCILE_RETRY_SECONDS,
                    ):
                        result["deferred"] += 1
                    result["examined"] += 1
                    continue

                source_bytes = max(
                    candidate.source_size,
                    max(0, int(source_stat.st_size)),
                )
                if source_bytes > max(0, int(max_single_source_bytes)):
                    if self._queue.release_legacy_full_reconciliation_candidate(
                        candidate.id
                    ):
                        result["released"] += 1
                    result["examined"] += 1
                    continue
                if (
                    result["examined"]
                    and total_source_bytes + source_bytes
                    > max(0, int(max_source_bytes))
                ):
                    break
                total_source_bytes += source_bytes

                try:
                    tool = self._find_tool(path)
                    classification = tool.classify_file(path) if tool else None
                except Exception:
                    if self._queue.defer_legacy_full_reconciliation_candidate(
                        candidate.id,
                        delay_seconds=LEGACY_RECONCILE_RETRY_SECONDS,
                    ):
                        result["deferred"] += 1
                    result["examined"] += 1
                    continue

                if (
                    classification is None
                    or classification.tool_name != candidate.tool_name
                    or classification.relative_path != candidate.relative_path
                    or classification.sync_strategy != SyncStrategy.FULL
                    or classification.metadata.get("__antigravity_pb__")
                ):
                    if self._queue.release_legacy_full_reconciliation_candidate(
                        candidate.id
                    ):
                        result["released"] += 1
                    result["examined"] += 1
                    continue

                source_revision_matches = (
                    int(source_stat.st_size) == candidate.source_size
                    and int(source_stat.st_mtime_ns) == candidate.source_mtime_ns
                )
                legacy_hash_matches = (
                    source_revision_matches
                    and _legacy_full_hash_revision(
                        path,
                        size=source_stat.st_size,
                        mtime_ns=source_stat.st_mtime_ns,
                    )
                    == candidate.legacy_hash
                )

                try:
                    self._on_file_changed(path, emit_live_signals=False)
                except Exception:
                    logger.debug(
                        "Legacy FULL startup reconciliation failed for %s",
                        path,
                        exc_info=True,
                    )

                if self._queue.is_legacy_full_reconciliation_candidate(
                    candidate.id
                ):
                    if legacy_hash_matches:
                        changed = (
                            self._queue.defer_legacy_full_reconciliation_candidate(
                                candidate.id,
                                delay_seconds=LEGACY_RECONCILE_RETRY_SECONDS,
                            )
                        )
                        if changed:
                            result["deferred"] += 1
                    else:
                        changed = (
                            self._queue.release_legacy_full_reconciliation_candidate(
                                candidate.id
                            )
                        )
                        if changed:
                            result["released"] += 1
                else:
                    result["resolved"] += 1
                result["examined"] += 1
        finally:
            try:
                result["remaining"] = (
                    self._queue.finish_legacy_full_reconciliation_pass()
                )
            finally:
                reconcile_lock.release()
        return result

    def initial_scan(self) -> int:
        """Scan newest files first while keeping the durable spool bounded."""
        if not self._scan_lock.acquire(blocking=False):
            logger.info("Initial scan already running; ignoring duplicate request")
            return 0
        try:
            return self._initial_scan()
        finally:
            self._scan_lock.release()

    def _initial_scan(self) -> int:
        if self._scan_cancel_event.is_set() or self._stop_event.is_set():
            return 0
        candidates: dict[str, tuple[float, Path]] = {}
        for tool in self._tools:
            if not tool.is_available():
                continue
            for wp in tool.get_watch_paths():
                if wp.sync_strategy == SyncStrategy.POLL:
                    continue  # SQLite handled by poller
                if wp.sync_strategy == SyncStrategy.IGNORE:
                    continue

                base = wp.path
                if not base.exists():
                    continue

                try:
                    if wp.recursive:
                        files_iter = base.rglob(wp.pattern)
                    else:
                        files_iter = base.glob(wp.pattern)

                    for f in files_iter:
                        if f.is_file():
                            try:
                                candidates[str(f)] = (f.stat().st_mtime, f)
                            except OSError:
                                logger.debug("Cannot stat %s", f)
                except OSError:
                    logger.debug("Cannot scan %s", base)

            # Special: Antigravity exports are deferred to periodic task (non-blocking)
            # See main.py AG_EXPORT_INTERVAL for aghistory + vscdb extraction

        count = 0
        for _mtime, path in sorted(
            candidates.values(), key=lambda item: item[0], reverse=True
        ):
            if self._stop_event.is_set() or self._scan_cancel_event.is_set():
                break
            high_water = self._config.queue_high_water_bytes
            while high_water > 0 and self._queue.outstanding_bytes() >= high_water:
                if self._stop_event.wait(0.5) or self._scan_cancel_event.is_set():
                    return count
            try:
                self._on_file_changed(path, emit_live_signals=False)
                count += 1
            except Exception:
                logger.debug("Error scanning %s", path)

        return count

    def cancel_scan(self, timeout: float = 60) -> bool:
        """Cancel and join the current scan without stopping file watching."""
        self._scan_cancel_event.set()
        if self._scan_lock.acquire(timeout=timeout):
            self._scan_lock.release()
            return True
        return False

    def allow_scan(self) -> None:
        self._scan_cancel_event.clear()

    def start(self) -> None:
        self._observer.start()
        logger.info("File watcher started")

    def stop(self) -> None:
        self._stop_event.set()
        self._scan_cancel_event.set()
        for handler in self._handlers:
            handler.stop()
        self._observer.stop()
        self._observer.join(timeout=5)
        # A timer may already have entered its callback when cancelled.
        with self._processing_lock:
            pass
        if self._scan_lock.acquire(timeout=60):
            self._scan_lock.release()
        else:
            logger.warning("Initial scan did not stop within 60 seconds")
        logger.info("File watcher stopped")
