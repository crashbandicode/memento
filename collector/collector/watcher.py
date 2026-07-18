"""File watcher — cross-platform file monitoring via watchdog with debouncing and event routing."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .compat import normalize_path, path_starts_with
from .config import CollectorConfig
from .parsers.base import BaseParser
from .parsers.json_parser import JsonParser
from .parsers.jsonl import JsonlParser
from .parsers.markdown import MarkdownParser
from .parsers.sqlite_parser import SqliteParser
from .parsers.toml_parser import TomlParser
from .queue import SyncQueue
from .sanitizer import sanitize_json, sanitize_jsonl, sanitize_text
from .tools.base import BaseTool, ContentType, SyncStrategy

logger = logging.getLogger("collector.watcher")




_FAST_HASH_READ = 256 * 1024  # Read first 256KB for fast hashing


def _file_hash_revision(path: Path, *, size: int, mtime_ns: int) -> str:
    """Hash one observed source revision without restatting a growing file."""
    try:
        h = hashlib.sha256()
        h.update(f"{size}:{mtime_ns}".encode())
        with open(path, "rb") as f:
            h.update(f.read(min(_FAST_HASH_READ, size)))
        return h.hexdigest()
    except OSError:
        return ""


def _file_hash(path: Path) -> str:
    """Fast file change detection: size + mtime + hash of first 256KB.

    Full SHA-256 is too slow for frequent file changes on large JSONL files.
    The first 256KB + file size + mtime catches virtually all real changes.
    """
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
        path = event.src_path
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
        self._processing_lock = threading.Lock()
        self._resync_lock = threading.Lock()
        self._resyncing_paths: set[str] = set()
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
                        handler, watch_dir, recursive=True,
                    )
                    self._handlers.append(handler)
                    logger.info(
                        "Watching %s (%s) at %s",
                        tool.display_name, tool.name, watch_dir,
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
                # Force-full means the server explicitly needs the complete
                # payload even when the local source revision has not changed.
                # Without clearing this observation, SyncQueue.enqueue treats
                # the snapshot as an identical no-op and uploads nothing.
                self._queue.clear_file_state(
                    classification.tool_name,
                    classification.relative_path,
                )
                for _attempt in range(3):
                    if self._stop_event.is_set() or not path.is_file():
                        return
                    self._on_file_changed(path, force_full=True)
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
                logger.warning("Could not capture a stable complete resync for %s", path)
            finally:
                with self._resync_lock:
                    self._resyncing_paths.discard(path_key)

        threading.Thread(
            target=run,
            name="memento-delta-resync",
            daemon=True,
        ).start()

    def request_relative_resync(self, tool_name: str, relative_path: str) -> bool:
        """Safely resolve and queue one server-selected conversation snapshot."""
        tool = next((item for item in self._tools if item.name == tool_name), None)
        if tool is None or not isinstance(relative_path, str):
            return False
        normalized_relative = relative_path.replace("\\", "/")
        parts = [part for part in normalized_relative.split("/") if part not in ("", ".")]
        if not parts or ".." in parts or Path(normalized_relative).is_absolute():
            return False
        root = tool.root_path.resolve()
        source_path = root.joinpath(*parts).resolve()
        if not path_starts_with(str(source_path), str(root)) or not source_path.is_file():
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
                    "content_hash", f"ag-{hash(content) & 0xFFFFFFFF:08x}",
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

    def _on_file_changed(self, path: Path, force_full: bool = False) -> None:
        """Serialize parsing and make shutdown a hard callback boundary."""
        if self._stop_event.is_set():
            return
        with self._processing_lock:
            if self._stop_event.is_set():
                return
            self._process_file_changed(path, force_full=force_full)

    def _process_file_changed(self, path: Path, force_full: bool = False) -> None:
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

        try:
            source_stat = path.stat()
            file_size = source_stat.st_size
        except OSError:
            return
        source_revision = (source_stat.st_size, source_stat.st_mtime_ns)

        # Check if file content actually changed
        current_hash = _file_hash_revision(
            path,
            size=file_size,
            mtime_ns=source_stat.st_mtime_ns,
        )
        if not current_hash:
            return

        last_hash, _ = self._queue.get_file_state(
            classification.tool_name, classification.relative_path,
        )

        # For FULL sync, skip if hash unchanged
        if (
            not force_full
            and classification.sync_strategy == SyncStrategy.FULL
            and current_hash == last_hash
        ):
            return

        # Determine read offset for delta sync
        read_offset = 0
        base_hash: str | None = None
        base_offset = 0
        if classification.sync_strategy == SyncStrategy.DELTA and not force_full:
            base_hash, base_offset = self._queue.get_delta_base(
                classification.tool_name,
                classification.relative_path,
            )
            if file_size < base_offset:
                # File was truncated, re-sync from beginning
                read_offset = 0
                base_hash = None
                base_offset = 0
            else:
                read_offset = base_offset
            max_delta_bytes = getattr(
                self._config,
                "max_delta_upload_bytes",
                16 * 1024 * 1024,
            )
            if (
                read_offset > 0
                and file_size - read_offset
                > max_delta_bytes
            ):
                logger.info(
                    "Delta burst exceeds %d bytes; queueing complete snapshot for %s",
                    max_delta_bytes,
                    path,
                )
                read_offset = 0
                base_hash = None
                base_offset = 0

        # Parse (with error protection)
        try:
            parser = self._get_parser(classification.content_type)
            append_only_snapshot = (
                force_full
                and classification.sync_strategy == SyncStrategy.DELTA
                and classification.content_type == ContentType.JSONL
                and isinstance(parser, JsonlParser)
            )
            if parser is None:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return
                parsed_content = content
                new_offset = path.stat().st_size
                is_partial = read_offset > 0
            else:
                if append_only_snapshot:
                    result = parser.parse(
                        path,
                        offset=0,
                        end_offset=file_size,
                    )
                else:
                    result = parser.parse(path, offset=read_offset)
                parsed_content = result.content
                new_offset = result.offset if result.offset else path.stat().st_size
                is_partial = result.is_partial
                classification.metadata.update(result.metadata)
                if result.title:
                    classification.metadata["title"] = result.title
        except Exception:
            logger.debug("Parse error for %s, skipping", path)
            return

        if append_only_snapshot:
            current_hash = _file_hash_revision(
                path,
                size=new_offset,
                mtime_ns=source_stat.st_mtime_ns,
            )
            if not current_hash:
                return

        if not parsed_content.strip():
            return

        # Sanitize before enqueue (defense-in-depth vs local SQLite leak)
        if classification.content_type == ContentType.JSONL:
            san = sanitize_jsonl(parsed_content)
        elif classification.content_type == ContentType.JSON:
            san = sanitize_json(parsed_content)
        else:
            san = sanitize_text(parsed_content)
        parsed_content = san.content

        # Hash, parse, and timestamp must describe one stable source revision.
        # A concurrent append generates another watcher event; returning here
        # leaves file_state untouched so that event (or the next scan) retries
        # the complete newer revision rather than pairing old content with its
        # new mtime.
        try:
            final_stat = path.stat()
        except OSError:
            return
        if append_only_snapshot:
            same_file = (
                source_stat.st_dev == final_stat.st_dev
                and source_stat.st_ino == final_stat.st_ino
            )
            if not same_file or final_stat.st_size < source_stat.st_size:
                logger.debug("Source was replaced while processing %s; deferring", path)
                return
        elif (final_stat.st_size, final_stat.st_mtime_ns) != source_revision:
            logger.debug("Source changed while processing %s; deferring", path)
            return
        source_modified_at = source_stat.st_mtime

        self._queue.enqueue(
            tool_name=classification.tool_name,
            category=classification.category.value,
            content_type=classification.content_type.value,
            relative_path=classification.relative_path,
            content=parsed_content,
            content_hash=current_hash,
            file_size=len(parsed_content),
            sync_strategy=classification.sync_strategy.value,
            is_partial=is_partial,
            offset=new_offset,
            metadata=classification.metadata,
            source_modified_at=source_modified_at,
            base_hash=base_hash if is_partial else None,
            base_offset=base_offset if is_partial else 0,
            source_path=str(path),
        )

        logger.info(
            "Queued %s/%s (%s, %s%s)",
            classification.tool_name,
            classification.relative_path,
            classification.category.value,
            classification.content_type.value,
            " delta" if is_partial else "",
        )

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
        for _mtime, path in sorted(candidates.values(), key=lambda item: item[0], reverse=True):
            if self._stop_event.is_set() or self._scan_cancel_event.is_set():
                break
            high_water = self._config.queue_high_water_bytes
            while high_water > 0 and self._queue.outstanding_bytes() >= high_water:
                if (self._stop_event.wait(0.5)
                        or self._scan_cancel_event.is_set()):
                    return count
            try:
                self._on_file_changed(path)
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
