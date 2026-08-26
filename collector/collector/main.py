"""Collector daemon — fully async, non-blocking on all platforms."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

from .canvas_sync import sync_pending_canvases
from .claude_pending_hook import install_claude_pending_hooks
from .claude_pending_questions import (
    ClaudePendingPoller,
)
from .agents.control_event_spool import ControlEventSpool, ControlEventUploader
from .agents.session_manager import AgentSessionManager
from .config import SYSTEM, CollectorConfig, _default_data_dir
from .control_channel import ControlChannel, capability_snapshot
from .cursor_state_export import (
    CursorStateExporter,
    enqueue_cursor_state_snapshots,
)
from .orchestration_sync import OrchestrationSync
from .queue import SyncQueue
from .sync_client import SyncClient
from .tools.antigravity import AntigravityTool
from .tools.claude_code import ClaudeCodeTool
from .tools.codex import CodexTool
from .tools.cursor import CursorTool
from .tools.hermes import HermesTool
from .tools.obsidian import ObsidianTool
from .tools.openclaw import OpenClawTool
from .watcher import FileWatcher

HEARTBEAT_INTERVAL = 30       # Log heartbeat every 30s
AUTO_UPDATE_INTERVAL = 3600   # Check for updates every 1 hour
QUEUE_MAINTENANCE_INTERVAL = 3600
LEGACY_RECONCILIATION_INTERVAL = 60
PACKAGE_NAME = "memento-brain-collector"
DISCOVERY_TIMEOUT = 10        # Discovery HTTP timeout
SOURCE_CHANGE_CHECK_INTERVAL = 1  # Cheap stat tokens; expensive work is change-driven
COLLECTOR_LOG_MAX_BYTES = 5 * 1024 * 1024
COLLECTOR_LOG_BACKUP_COUNT = 3
_COLLECTOR_LOG_HANDLER_MARKER = "_memento_collector_managed"
_canvas_sync_lock = threading.Lock()
_logging_setup_lock = threading.Lock()


class _CanvasPollSchedule:
    """Back off empty polls and wake promptly only for Canvas-bearing signals."""

    def __init__(self, *, minimum: float = 5.0, maximum: float = 300.0) -> None:
        self._minimum = minimum
        self._maximum = maximum
        self._delay = minimum
        self._next_due = 0.0
        self._active = False
        self._generation = 0
        self._lock = threading.Lock()

    def _notify_canvas_signal(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._generation += 1
            self._delay = self._minimum
            self._next_due = min(self._next_due, now + 2.0)

    def notify_upload(self, item, now: float | None = None) -> None:
        """Wake for an uploaded Canvas path, never for ordinary conversations."""
        relative_path = str(getattr(item, "relative_path", ""))
        if ".canvas.tsx" not in relative_path.casefold():
            return
        self._notify_canvas_signal(now)

    def notify_control_notification(self, now: float | None = None) -> None:
        """Wake after the server projects a newly discovered Canvas reference."""
        self._notify_canvas_signal(now)

    def claim_due(self, now: float | None = None) -> int | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._active or now < self._next_due:
                return None
            self._active = True
            return self._generation

    def complete(
        self,
        generation: int,
        counts: dict[str, int],
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._active = False
            if generation != self._generation:
                return
            if counts.get("requested") or counts.get("failed"):
                self._delay = self._minimum
            else:
                self._delay = min(self._maximum, max(self._minimum, self._delay * 2))
            self._next_due = now + self._delay


def _load_saved_config() -> CollectorConfig:
    config = CollectorConfig()
    saved_path = _default_data_dir() / "config.json"
    if saved_path.exists():
        try:
            saved = json.loads(saved_path.read_text())
            if saved.get("server_url"):
                os.environ.setdefault("MEMENTO_SERVER_URL", saved["server_url"])
            if saved.get("server_token"):
                os.environ.setdefault("MEMENTO_SERVER_TOKEN", saved["server_token"])
            if saved.get("obsidian_vault_path"):
                os.environ.setdefault("MEMENTO_OBSIDIAN_VAULT_PATH", saved["obsidian_vault_path"])
            config = CollectorConfig()
        except Exception:
            pass
    return config


def _setup_logging(config: CollectorConfig) -> None:
    """Configure bounded collector logging without leaking handlers.

    ``ConcurrentRotatingFileHandler`` serializes rollover across both threads
    and processes. This matters during service restarts/upgrades, when an old
    collector can briefly overlap its replacement.
    """
    with _logging_setup_lock:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = config.log_dir / "collector.log"
        formatter = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        )

        # Force UTF-8 on the log file — the platform locale may otherwise be
        # cp936/GBK on Windows. Three 5 MiB backups plus the active file cap
        # normal retained collector logs at about 20 MiB.
        file_handler = ConcurrentRotatingFileHandler(
            log_file,
            mode="a",
            maxBytes=COLLECTOR_LOG_MAX_BYTES,
            backupCount=COLLECTOR_LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _COLLECTOR_LOG_HANDLER_MARKER, True)
        handlers: list[logging.Handler] = [file_handler]

        # Console: same encoding issue as the file on Windows. Best-effort;
        # pythonw and replaced streams may not provide a usable stdout.
        try:
            sys.stdout.write("")
            sys.stdout.flush()
            try:
                sys.stdout.reconfigure(  # type: ignore[attr-defined]
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                pass
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            setattr(console_handler, _COLLECTOR_LOG_HANDLER_MARKER, True)
            handlers.append(console_handler)
        except Exception:
            pass

        root = logging.getLogger()
        previous_handlers = [
            handler
            for handler in root.handlers
            if getattr(handler, _COLLECTOR_LOG_HANDLER_MARKER, False)
        ]
        for handler in previous_handlers:
            root.removeHandler(handler)
        root.setLevel(logging.INFO)
        for handler in handlers:
            root.addHandler(handler)
        for handler in previous_handlers:
            handler.close()

        # httpx logs every successful request at INFO. Polling makes those
        # entries high-volume but low-signal; warnings and errors still flow.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _send_discovery(config: CollectorConfig, logger: logging.Logger) -> None:
    """Send tool discovery to server (runs in background thread)."""
    try:
        import httpx

        from .discovery import discover_all_tools
        from .tls import SSL_CONTEXT
        discovery = discover_all_tools()
        if discovery:
            logger.info("Discovered tools: %s", ", ".join(discovery.keys()))
            httpx.post(
                f"{config.server.url}/api/ingest/discovery",
                json={"device_id": config.device_id, "device_name": config.device_name,
                      "platform": config.platform, "tools": discovery},
                headers={"X-Collector-Token": config.server.token},
                timeout=DISCOVERY_TIMEOUT,
                verify=SSL_CONTEXT,
            )
    except Exception:
        pass


def _run_initial_scan(watcher: FileWatcher, logger: logging.Logger) -> None:
    """Run initial scan in background thread."""
    try:
        count = watcher.initial_scan()
        logger.info("Initial scan complete: %d files queued", count)
    except Exception:
        logger.exception("Initial scan failed")


def _run_legacy_full_reconciliation(
    watcher: FileWatcher,
    logger: logging.Logger,
) -> dict[str, int]:
    """Run and report one bounded legacy-transition reconciliation pass."""

    try:
        result = watcher.reconcile_legacy_full_queue()
        if result["examined"] or result["remaining"]:
            logger.info(
                "Legacy FULL reconciliation: examined=%d resolved=%d "
                "preserved=%d deferred=%d remaining=%d",
                result["examined"],
                result["resolved"],
                result["released"],
                result["deferred"],
                result["remaining"],
            )
        return result
    except Exception:
        logger.exception("Legacy FULL reconciliation failed")
        return {
            "examined": 0,
            "resolved": 0,
            "released": 0,
            "deferred": 0,
            "remaining": 0,
        }


def _poll_canvas_artifacts(
    config: CollectorConfig,
    logger: logging.Logger,
    schedule: _CanvasPollSchedule | None = None,
    generation: int = 0,
) -> None:
    """Run one bounded artifact batch without overlapping a prior batch."""
    if not _canvas_sync_lock.acquire(blocking=False):
        if schedule is not None:
            schedule.complete(generation, {"requested": 0, "failed": 1})
        return
    counts = {
        "requested": 0,
        "renderable": 0,
        "static_only": 0,
        "missing": 0,
        "rejected": 0,
        "unchanged": 0,
        "updated": 0,
        "failed": 0,
    }
    try:
        counts = sync_pending_canvases(config, logger)
        if counts["requested"]:
            logger.info(
                "Canvas backfill batch: requested=%d renderable=%d static=%d "
                "missing=%d rejected=%d unchanged=%d updated=%d failed=%d",
                counts["requested"],
                counts["renderable"],
                counts["static_only"],
                counts["missing"],
                counts["rejected"],
                counts["unchanged"],
                counts["updated"],
                counts["failed"],
            )
    except Exception:
        counts["failed"] += 1
        logger.exception("Canvas backfill poll failed")
    finally:
        _canvas_sync_lock.release()
        if schedule is not None:
            schedule.complete(generation, counts)


def execute_control_command(
    kind: str,
    payload: dict,
    *,
    config: CollectorConfig,
    queue: SyncQueue,
    watcher: FileWatcher,
    sync_client: SyncClient,
    logger: logging.Logger,
    on_resync: Callable[[], None] | None = None,
    on_canvas_sync: Callable[[], None] | None = None,
) -> tuple[str, str | None, dict]:
    """Execute one durable control command and return its terminal outcome.

    Returns ``(status, error_code, detail)`` with ``status`` in
    ``completed``/``failed``/``cancelled``. Previously these actions ran
    fire-and-forget after a legacy ack; every result is now reported so the
    server's command row records what actually happened.
    """
    if kind in ("device.resync", "resync"):
        logger.info("Received resync — draining uploads before full re-scan")
        if not watcher.cancel_scan(timeout=60):
            logger.error("Resync aborted: current scan did not stop")
            watcher.allow_scan()
            return "failed", "command.execution_failed", {"reason": "scan_did_not_stop"}
        if not sync_client.pause(timeout=75):
            logger.error("Resync aborted: upload batch did not drain")
            watcher.allow_scan()
            sync_client.resume()
            return (
                "failed",
                "command.execution_failed",
                {"reason": "uploads_did_not_drain"},
            )
        try:
            queue.clear_all_state()
            if on_resync is not None:
                on_resync()
            try:
                from .parsers import antigravity_export as _ag
                _ag._last_hashes.clear()
                _ag._title_map_cache = None  # Force re-read on next export
            except Exception:
                pass
        finally:
            watcher.allow_scan()
            sync_client.resume()
        threading.Thread(target=_run_initial_scan, args=(watcher, logger), daemon=True).start()
        logger.info("Resync triggered — cache cleared, re-scan started")
        return "completed", None, {"rescan_started": True}

    if kind in ("conversation.repair", "repair-conversations"):
        targets = [
            target
            for target in (payload.get("paths") or [])[:2]
            if isinstance(target, dict)
        ]
        queued = sum(
            watcher.request_relative_resync(
                str(target.get("tool_name", "")),
                str(target.get("relative_path", "")),
            )
            for target in targets
        )
        logger.info(
            "Received targeted conversation repair — %d snapshots queued",
            queued,
        )
        return "completed", None, {"targets": len(targets), "queued": queued}

    if kind in ("collector.update", "update"):
        if not config.auto_update_enabled:
            logger.info("Ignoring update command: auto-update is disabled")
            return "completed", None, {"skipped": "auto_update_disabled"}
        logger.info("Received update command from server")
        threading.Thread(target=_check_and_update, args=(logger,), daemon=True).start()
        return "completed", None, {"initiated": True}

    if kind == "canvas.sync":
        if on_canvas_sync is not None:
            on_canvas_sync()
        logger.info("Received Canvas notification; scheduling pending-artifact sync")
        return "completed", None, {"scheduled": True}

    logger.warning("Unsupported control command kind: %s", kind)
    return "failed", "capability.unsupported", {"kind": kind}


def _get_pypi_latest(package: str) -> str | None:
    """Query PyPI for latest version of a package."""
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{package}/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())["info"]["version"]
    except Exception:
        return None


def _upgrade_package(package: str, version: str, logger: logging.Logger) -> bool:
    """Pip upgrade a single package to a specific version."""
    import subprocess
    pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
               f"{package}=={version}", "--quiet"]
    if SYSTEM == "Windows":
        pip_cmd.insert(-1, "--user")
    result = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logger.warning("Upgrade %s failed: %s", package, result.stderr[:300])
        return False
    return True


def _is_newer_version(candidate: str, installed: str) -> bool:
    """Return true only for an actual upgrade, never for a downgrade."""
    try:
        from packaging.version import Version
        return Version(candidate) > Version(installed)
    except Exception:
        return False


def _check_and_update(logger: logging.Logger) -> None:
    """Check PyPI for a newer version and auto-upgrade + restart if found.

    Upgrades both memento-collector and memento-memory (MCP server).
    """
    # Frozen desktop sidecar — `sys.executable` is the PyInstaller-built
    # sidecar binary, not a real Python interpreter, so `[sys.executable,
    # "-m", "pip", "install", ...]` ends up calling
    # `memento-sidecar -m pip install ...` which the entry.py wrapper
    # rejects ("only 'run' is supported"). Even if pip could run, the
    # collector code is bundled inside the .exe — pip can't replace it.
    # The user has to install a new desktop release to upgrade.
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        logger.info(
            "Running as bundled desktop sidecar — auto-upgrade not applicable. "
            "Install a new desktop release to upgrade collector + MCP."
        )
        return

    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as get_version

        # Log starting state up front so the user sees the check fired even
        # if nothing needs upgrading. Guard the mcp version lookup with
        # try/except so this initial log itself never crashes.
        current = get_version(PACKAGE_NAME)
        try:
            _mcp_current_for_log = get_version("memento-brain-memory")
        except PackageNotFoundError:
            _mcp_current_for_log = "not installed"
        logger.info(
            "Checking PyPI for updates (collector=%s, mcp=%s)",
            current, _mcp_current_for_log,
        )

        # Check collector update
        latest = _get_pypi_latest(PACKAGE_NAME)
        needs_restart = False
        any_upgrade = False

        if latest is None:
            logger.warning(
                "PyPI lookup failed for memento-brain-collector "
                "(network/proxy/timeout) — try again later"
            )
        elif latest == current:
            logger.info("Collector already up to date (%s)", current)
        elif _is_newer_version(latest, current):
            logger.info("Collector update available: %s → %s", current, latest)
            if _upgrade_package(PACKAGE_NAME, latest, logger):
                logger.info("Collector upgraded to %s", latest)
                needs_restart = True
                any_upgrade = True
        else:
            logger.info(
                "Installed collector %s is newer than PyPI %s; keeping local build",
                current, latest,
            )

        # Also check MCP server update (installed as dependency)
        try:
            mcp_current = get_version("memento-brain-memory")
            mcp_latest = _get_pypi_latest("memento-brain-memory")
            if mcp_latest is None:
                logger.warning("PyPI lookup failed for memento-brain-memory")
            elif mcp_latest == mcp_current:
                logger.info("MCP server already up to date (%s)", mcp_current)
            elif _is_newer_version(mcp_latest, mcp_current):
                logger.info("MCP server update available: %s → %s", mcp_current, mcp_latest)
                if _upgrade_package("memento-brain-memory", mcp_latest, logger):
                    logger.info("MCP server upgraded to %s (restart AI IDE to activate)", mcp_latest)
                    any_upgrade = True
            else:
                logger.info(
                    "Installed MCP server %s is newer than PyPI %s; keeping local build",
                    mcp_current, mcp_latest,
                )
        except PackageNotFoundError:
            logger.info("memento-brain-memory not installed, skipping MCP upgrade")

        if not any_upgrade:
            logger.info("Update check complete — no upgrades needed")

        # Restart collector if it was upgraded
        if needs_restart:
            if SYSTEM == "Windows":
                # `os.execv` on Windows is *not* a true exec — Python spawns a
                # new process and exits the original. Task Scheduler then sees
                # the original PID terminate and marks the task "completed",
                # detaching the new process from the schedule. The new process
                # keeps running for now, but the moment it dies (any reason)
                # nothing brings it back until the user logs off and on.
                # Instead: exit non-zero so Task Scheduler's RestartOnFailure
                # (configured in the XML task definition) brings us back fresh
                # within ~1 minute, with the schedule association intact.
                logger.info("Collector upgraded — exiting; Task Scheduler will restart in ~1m")
                sys.exit(1)
            logger.info("Restarting collector...")
            os.execv(sys.executable, [sys.executable, "-m", "collector.main"])

    except Exception as e:
        logger.debug("Auto-update check failed: %s", e)


_ag_export_lock = threading.Lock()
_claude_pending_poll_lock = threading.Lock()
_codex_metadata_poll_lock = threading.Lock()
_cursor_state_poll_lock = threading.Lock()


def _poll_claude_pending_questions(
    tool: ClaudeCodeTool,
    queue: SyncQueue,
    logger: logging.Logger,
    poller: ClaudePendingPoller,
) -> None:
    """Queue live prompt state captured by the Claude Code hook."""
    if not _claude_pending_poll_lock.acquire(blocking=False):
        return
    try:
        records, activity_records = poller.poll(tool)
        queued = (
            queue.enqueue_metadata_changes(
                namespace="conversation_interactions",
                tool_name="claude_code",
                records=records,
            )
            if records
            else 0
        )
        if queued:
            logger.info("Queued %d Claude prompt interaction update(s)", queued)
        activity_queued = (
            queue.enqueue_metadata_changes(
                namespace="conversation_activities",
                tool_name="claude_code",
                records=activity_records,
            )
            if activity_records
            else 0
        )
        if activity_queued:
            logger.info(
                "Queued %d Claude shell activity update(s)",
                activity_queued,
            )
    except Exception:
        logger.exception("Claude prompt interaction poll failed")
    finally:
        _claude_pending_poll_lock.release()


def _poll_codex_thread_titles(
    tool: CodexTool,
    queue: SyncQueue,
    logger: logging.Logger,
) -> None:
    """Queue explicit Codex title transitions as tiny durable updates."""
    if not _codex_metadata_poll_lock.acquire(blocking=False):
        return
    try:
        records = tool.thread_title_records(changed_only=True)
        if not records:
            return
        valid_records: dict[str, dict] = {}
        for thread_id, record in records.items():
            try:
                parsed_thread_id = uuid.UUID(thread_id)
                revision = int(record.get("revision") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if str(parsed_thread_id) != thread_id.lower() or revision <= 0:
                continue
            valid_records[thread_id] = record
        queued = queue.enqueue_metadata_changes(
            namespace="codex_thread_titles",
            tool_name="codex",
            records=valid_records,
        )
        if queued:
            logger.info("Queued %d Codex thread title update(s)", queued)
    except Exception:
        logger.exception("Codex thread title poll failed")
    finally:
        _codex_metadata_poll_lock.release()


def _poll_cursor_state(
    exporter: CursorStateExporter,
    queue: SyncQueue,
    logger: logging.Logger,
) -> None:
    """Queue changed Cursor composers from the authoritative live database."""
    if not _cursor_state_poll_lock.acquire(blocking=False):
        return
    try:
        queued = enqueue_cursor_state_snapshots(exporter, queue)
        if queued:
            logger.info("Queued %d Cursor live-state conversation(s)", queued)
    except Exception:
        logger.exception("Cursor live-state projection failed")
    finally:
        _cursor_state_poll_lock.release()


def _invalidate_cursor_state(exporter: CursorStateExporter) -> None:
    """Reset projector state without racing an in-flight read."""
    with _cursor_state_poll_lock:
        exporter.invalidate()


def _log_queue_heartbeat(
    queue: SyncQueue,
    logger: logging.Logger,
    last_token: int,
) -> int:
    """Log only queue transitions; unchanged idle state performs no SQL."""
    token = queue.change_token()
    if token == last_token:
        return last_token
    pending = queue.pending_count()
    if pending > 0:
        logger.info("Heartbeat: %d items pending sync", pending)
    else:
        logger.info("Heartbeat: idle, watching for changes")
    return token


def _run_antigravity_export(queue: SyncQueue, logger: logging.Logger) -> None:
    """Run Antigravity export in background thread (non-blocking)."""
    # Prevent concurrent exports from overlapping
    if not _ag_export_lock.acquire(blocking=False):
        return
    try:
        from .parsers.antigravity_export import export_conversations
        convos = export_conversations()
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
            queue.enqueue(
                tool_name="antigravity",
                category="conversation",
                content_type="jsonl",
                relative_path=f"conversations/{conv['cascade_id']}.jsonl",
                content=content,
                content_hash=conv.get("content_hash", f"ag-{hash(content) & 0xFFFFFFFF:08x}"),
                file_size=len(content),
                sync_strategy="full",
                metadata=meta,
                source_modified_at=conv.get("source_modified_at"),
            )
    except Exception:
        logger.exception("Antigravity export error")
    finally:
        _ag_export_lock.release()




_devnull_file = None  # Module-level ref to keep devnull fd alive


def _ensure_stdio() -> None:
    """Ensure stdout/stderr are writable (pythonw.exe on Windows sets them to None)."""
    global _devnull_file
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        try:
            if stream is not None:
                stream.write("")
                stream.flush()
                continue
        except Exception:
            pass
        # Stream is None or broken — redirect to devnull
        if _devnull_file is None:
            _devnull_file = open(os.devnull, "w")
        setattr(sys, stream_name, _devnull_file)


def _check_windows_task_health(logger: logging.Logger) -> None:
    """Warn if the scheduled task is missing the hardening settings.

    Old installs (before XML-based registration) were created with the
    shorthand `schtasks /Create /SC ONLOGON ...` form, which inherits
    Windows defaults that kill long-running daemons (3-day time limit,
    stop-on-battery, no auto-restart). New installs ship a proper XML
    definition; this helper detects the old form and tells the user to
    re-run setup once.
    """
    if SYSTEM != "Windows":
        return
    try:
        import subprocess as _sp
        r = _sp.run(
            ["schtasks", "/Query", "/TN", "MementoCollector", "/XML"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return
        xml = r.stdout
        # Heuristic: hardened tasks declare RestartOnFailure + PT0S limit.
        if "<RestartOnFailure>" not in xml or "PT0S" not in xml:
            logger.warning(
                "Scheduled task is using legacy settings (no auto-restart, "
                "3-day execution limit, stops on battery). Re-run "
                "`memento-collector setup` once to apply the hardened XML "
                "definition — this is the most common cause of the collector "
                "appearing to stop on its own."
            )
    except Exception:
        pass  # best-effort, don't block startup


def main() -> None:
    _ensure_stdio()

    config = _load_saved_config()
    config.ensure_dirs()
    _setup_logging(config)

    logger = logging.getLogger("collector")
    logger.info(
        "Starting Memento Collector [%s] on %s (%s)",
        config.device_id[:8], config.device_name, config.platform,
    )
    _check_windows_task_health(logger)
    try:
        hook_settings, hooks_changed = install_claude_pending_hooks()
        if hooks_changed:
            logger.info("Installed Claude prompt hooks in %s", hook_settings)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not install Claude prompt hooks: %s", exc)

    # Initialize tools
    claude_tool = ClaudeCodeTool()
    codex_tool = CodexTool()
    cursor_tool = CursorTool()
    cursor_exporter = CursorStateExporter(cursor_tool)
    claude_pending_poller = ClaudePendingPoller()
    canvas_schedule = _CanvasPollSchedule()
    tools = [
        claude_tool, OpenClawTool(), codex_tool,
        AntigravityTool(), ObsidianTool(vault_path=config.obsidian_vault_path),
        cursor_tool,
        HermesTool(),
    ]
    available = [t for t in tools if t.is_available()]
    logger.info("Available tools (%d): %s", len(available),
                ", ".join(t.display_name for t in available))

    if not available:
        logger.warning("No AI tools found on this device!")

    # Initialize queue + sync client + watcher
    queue = SyncQueue(
        config.queue_db_path,
        spool_threshold=config.queue_spool_threshold,
        terminal_spool_max_age_seconds=config.terminal_spool_max_age_seconds,
        terminal_spool_max_bytes=config.terminal_spool_max_bytes,
    )
    watcher = FileWatcher(available, queue, config)

    def _request_full_resync(source_path: str) -> None:
        try:
            is_cursor_state = (
                Path(source_path).resolve()
                == cursor_tool.state_database_path.resolve()
            )
        except OSError:
            is_cursor_state = False
        if not is_cursor_state:
            watcher.request_full_resync(source_path)
            return
        # state.vscdb is a POLL source rather than a normal watched transcript.
        # A rejected projection delta must forget its in-memory row baseline and
        # immediately capture a complete authoritative snapshot.
        _invalidate_cursor_state(cursor_exporter)
        _poll_cursor_state(cursor_exporter, queue, logger)

    sync_client = SyncClient(
        queue,
        config,
        full_resync_callback=_request_full_resync,
        delta_catchup_callback=watcher.request_delta_catchup,
        upload_synced_callback=canvas_schedule.notify_upload,
    )
    orchestration_sync = OrchestrationSync(config)

    def _invalidate_source_pollers() -> None:
        _invalidate_cursor_state(cursor_exporter)
        with _codex_metadata_poll_lock:
            codex_tool.invalidate_thread_title_poll()
        with _claude_pending_poll_lock:
            claude_pending_poller.invalidate()

    control_spool = ControlEventSpool()
    control_uploader = ControlEventUploader(config, control_spool)
    agent_sessions = AgentSessionManager(config, control_spool)

    def _execute_control_command(kind: str, payload: dict) -> tuple[str, str | None, dict]:
        if kind.startswith("agent."):
            return agent_sessions.execute(kind, payload)
        return execute_control_command(
            kind,
            payload,
            config=config,
            queue=queue,
            watcher=watcher,
            sync_client=sync_client,
            logger=logger,
            on_resync=_invalidate_source_pollers,
            on_canvas_sync=canvas_schedule.notify_control_notification,
        )

    def _control_capabilities() -> dict:
        return capability_snapshot(
            config,
            extra_commands=agent_sessions.supported_commands(),
            agents=agent_sessions.agents_capabilities(),
        )

    control_channel = ControlChannel(
        config,
        _execute_control_command,
        capabilities_provider=_control_capabilities,
    )

    # Graceful shutdown
    shutdown = False

    def _signal_handler(signum: int, frame: object) -> None:
        nonlocal shutdown
        logger.info("Received signal %s, shutting down...", signum)
        shutdown = True

    signal.signal(signal.SIGINT, _signal_handler)
    if SYSTEM != "Windows":
        signal.signal(signal.SIGTERM, _signal_handler)

    # --- All blocking operations run in background threads ---

    # 1. Start watching before proof so a source change in the reconciliation
    # window cannot fall between the startup scan and observer activation.
    # Upgrade-only canonical FULL rows are gated durably while this synchronous
    # bounded pass runs; unfinished candidates remain gated for later passes.
    watcher.start()
    _run_legacy_full_reconciliation(watcher, logger)

    # 2. Discovery (non-blocking)
    threading.Thread(target=_send_discovery, args=(config, logger), daemon=True).start()

    # 3. Initial scan (non-blocking)
    threading.Thread(target=_run_initial_scan, args=(watcher, logger), daemon=True).start()

    # Establish the durable state_5.sqlite title baseline without uploading the
    # database or any transcript. Future title transitions become tiny queue
    # items and survive collector/server restarts.
    if codex_tool in available:
        threading.Thread(
            target=_poll_codex_thread_titles,
            args=(codex_tool, queue, logger),
            daemon=True,
        ).start()

    if claude_tool in available:
        threading.Thread(
            target=_poll_claude_pending_questions,
            args=(claude_tool, queue, logger, claude_pending_poller),
            daemon=True,
        ).start()

    if cursor_tool in available and cursor_tool.state_database_path.is_file():
        threading.Thread(
            target=_poll_cursor_state,
            args=(cursor_exporter, queue, logger),
            daemon=True,
        ).start()

    # 4. Start uploader only after the startup reconciliation pass.
    sync_client.start()
    orchestration_sync.start()
    control_uploader.start()
    control_channel.start()

    logger.info("Collector running. Watching for file changes...")

    # 5. Auto-update check on startup (non-blocking)
    if config.auto_update_enabled:
        threading.Thread(target=_check_and_update, args=(logger,), daemon=True).start()

    # 6. Antigravity export on startup (real-time updates handled by main FileWatcher)
    has_antigravity = any(t.name == "antigravity" for t in available)
    if has_antigravity:
        threading.Thread(
            target=_run_antigravity_export, args=(queue, logger), daemon=True,
        ).start()

    # --- Main loop: heartbeat + periodic tasks ---
    last_heartbeat = time.monotonic()
    last_heartbeat_token = -1
    last_update_check = time.monotonic()
    last_queue_maintenance = time.monotonic()
    last_legacy_reconciliation = time.monotonic()
    last_codex_metadata_poll = time.monotonic()
    last_source_change_check = time.monotonic()

    try:
        while not shutdown:
            time.sleep(1)

            now = time.monotonic()

            # Heartbeat log every 30s
            if now - last_heartbeat > HEARTBEAT_INTERVAL:
                last_heartbeat = now
                last_heartbeat_token = _log_queue_heartbeat(
                    queue,
                    logger,
                    last_heartbeat_token,
                )

            canvas_generation = canvas_schedule.claim_due(now)
            if canvas_generation is not None:
                threading.Thread(
                    target=_poll_canvas_artifacts,
                    args=(config, logger, canvas_schedule, canvas_generation),
                    daemon=True,
                ).start()

            # Auto-update check every hour
            if config.auto_update_enabled and now - last_update_check > AUTO_UPDATE_INTERVAL:
                last_update_check = now
                threading.Thread(target=_check_and_update, args=(logger,), daemon=True).start()

            if now - last_queue_maintenance > QUEUE_MAINTENANCE_INTERVAL:
                last_queue_maintenance = now
                discarded = queue.cleanup_terminal_spool()
                if discarded:
                    logger.info(
                        "Released %d rebuildable terminal spool payload(s)",
                        discarded,
                    )

            if (
                now - last_legacy_reconciliation
                > LEGACY_RECONCILIATION_INTERVAL
            ):
                last_legacy_reconciliation = now
                threading.Thread(
                    target=_run_legacy_full_reconciliation,
                    args=(watcher, logger),
                    daemon=True,
                    name="legacy-full-reconciliation",
                ).start()

            if (
                codex_tool in available
                and now - last_codex_metadata_poll > config.sqlite_poll_interval
                and codex_tool.thread_titles_changed()
            ):
                last_codex_metadata_poll = now
                threading.Thread(
                    target=_poll_codex_thread_titles,
                    args=(codex_tool, queue, logger),
                    daemon=True,
                ).start()

            if now - last_source_change_check > SOURCE_CHANGE_CHECK_INTERVAL:
                last_source_change_check = now
                if (
                    claude_tool in available
                    and not _claude_pending_poll_lock.locked()
                    and claude_pending_poller.needs_poll(claude_tool)
                ):
                    threading.Thread(
                        target=_poll_claude_pending_questions,
                        args=(
                            claude_tool,
                            queue,
                            logger,
                            claude_pending_poller,
                        ),
                        daemon=True,
                    ).start()

                if (
                    cursor_tool in available
                    and not _cursor_state_poll_lock.locked()
                    and cursor_exporter.needs_export()
                ):
                    threading.Thread(
                        target=_poll_cursor_state,
                        args=(cursor_exporter, queue, logger),
                        daemon=True,
                    ).start()

    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        watcher.stop()
        control_channel.stop()
        agent_sessions.shutdown()
        control_uploader.stop()
        orchestration_sync.stop()
        sync_client.stop()
        queue.close()
        logger.info("Collector stopped.")


if __name__ == "__main__":
    main()
