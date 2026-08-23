"""Codex app-server adapter — the native control surface for managed Codex.

Spawns one ``codex app-server`` child per active managed session group,
performs the initialize handshake, and exposes the thread/turn verbs the
control plane needs: start, resume, send, steer, interrupt. Server-initiated
requests (exec/patch/permission approvals and ``request_user_input``
questions) are surfaced through pluggable handlers so the collector can
escalate them to Memento rather than deciding locally.

Event policy mirrors the audit design: low-volume lifecycle notifications
are forwarded whole; high-volume streaming deltas are coalesced into
per-item counters and never retained as content. Transcripts stay owned by
the file watcher — this adapter never writes conversation content anywhere.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .jsonl_rpc import JsonlRpcClient, RpcTransportClosed

logger = logging.getLogger("collector.agents.codex")

CLIENT_NAME = "memento-collector"
CLIENT_TITLE = "Memento Collector"

# Lifecycle notifications forwarded whole (bounded payloads by nature).
_FORWARDED_EVENTS = frozenset(
    {
        "thread/started",
        "turn/started",
        "turn/completed",
        "turn/plan/updated",
        "thread/tokenUsage/updated",
        "serverRequest/resolved",
        "error",
        "warning",
        "configWarning",
        "thread/status/changed",
    }
)
# Item lifecycle is forwarded as a bounded summary (never full content).
_ITEM_LIFECYCLE_EVENTS = frozenset({"item/started", "item/completed"})
# Streaming deltas are counted per item, never forwarded or retained.
_DELTA_EVENT_PREFIXES = (
    "item/agentMessage/",
    "item/reasoning/",
    "item/commandExecution/output",
    "item/plan/delta",
    "item/fileChange/patchUpdated",
)

# Handler contracts. Both may block while a human decides; they run on
# dedicated threads owned by the RPC client.
ApprovalHandler = Callable[[str, dict], dict]
UserInputHandler = Callable[[dict], dict]
EventSink = Callable[[str, dict], None]


class CodexAdapterError(Exception):
    pass


# The plugin runtime injects a custom `exec` tool whose tools.shell_command
# never reaches the exec-approval assessment, silently bypassing
# approvalPolicy (observed live: an `untrusted` thread ran shell writes with
# no approval request). Feature-level -c overrides lose to the plugin's own
# re-enabling, so managed sessions disable the plugin runtime entirely:
# a lean, predictable tool surface where every shell command goes through
# the native approval path.
MANAGED_CONFIG_OVERRIDES: tuple[str, ...] = (
    "features.plugins=false",
)


def resolve_codex_command() -> list[str]:
    """Locate the Codex CLI without a shell, honoring an explicit override."""
    override = os.environ.get("MEMENTO_CODEX_COMMAND", "").strip()
    if override:
        command = [override, "app-server"]
    else:
        for candidate in ("codex.exe", "codex.cmd", "codex"):
            found = shutil.which(candidate)
            if found:
                command = [found, "app-server"]
                break
        else:
            raise CodexAdapterError("codex CLI not found on PATH")
    for entry in MANAGED_CONFIG_OVERRIDES:
        command.extend(("-c", entry))
    return command


def _bounded_item_summary(item: dict) -> dict:
    """Type/status/id projection of a ThreadItem — no content fields."""
    summary = {
        key: item.get(key)
        for key in ("id", "type", "status", "exitCode", "durationMs", "delivery")
        if key in item
    }
    item_type = item.get("type") or item.get("itemType")
    if item_type is not None:
        summary["type"] = item_type
    return summary


@dataclass
class _DeltaCounters:
    events: int = 0
    characters: int = 0


@dataclass
class _TurnDeltas:
    by_item: dict[str, _DeltaCounters] = field(default_factory=dict)


class CodexAppServerAdapter:
    """One child ``codex app-server`` process and its RPC channel."""

    def __init__(
        self,
        *,
        on_event: EventSink,
        approval_handler: ApprovalHandler,
        user_input_handler: UserInputHandler,
        command: list[str] | None = None,
        cwd: str | None = None,
        client_version: str = "dev",
        request_timeout: float = 60.0,
    ) -> None:
        # Public and reassignable: the session manager rebinds these to
        # session-scoped closures after construction.
        self.on_event = on_event
        self.approval_handler = approval_handler
        self.user_input_handler = user_input_handler
        self._command = command
        self._cwd = cwd
        self._client_version = client_version
        self._request_timeout = request_timeout
        self._process: subprocess.Popen | None = None
        self._rpc: JsonlRpcClient | None = None
        self._deltas_lock = threading.Lock()
        self._deltas: dict[str, _TurnDeltas] = {}
        self.server_info: dict = {}
        self.last_thread_config: dict = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            raise CodexAdapterError("adapter already started")
        command = self._command or resolve_codex_command()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self._cwd,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CodexAdapterError(f"failed to spawn codex app-server: {exc}") from exc
        assert self._process.stdin is not None and self._process.stdout is not None
        self._rpc = JsonlRpcClient(
            self._process.stdin,
            self._process.stdout,
            on_notification=self._handle_notification,
            on_server_request=self._handle_server_request,
            on_closed=self._handle_closed,
            name="codex-app-server",
        )
        self._rpc.start()
        self.server_info = self._rpc.request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_TITLE,
                    "version": self._client_version,
                }
            },
            timeout=self._request_timeout,
        ) or {}
        self._rpc.notify("initialized")

    def stop(self) -> None:
        rpc, process = self._rpc, self._process
        self._rpc, self._process = None, None
        if rpc is not None:
            rpc.close()
        if process is not None:
            try:
                if sys.platform == "win32":
                    # codex.cmd spawns cmd -> node -> codex.exe; terminate()
                    # only kills the top of that tree and leaves an orphaned
                    # app-server running after close. Kill the whole tree.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=15,
                    )
                else:
                    process.terminate()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass

    @property
    def alive(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._rpc is not None
            and not self._rpc.closed
        )

    # -- thread/turn verbs -----------------------------------------------------

    def _request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> Any:
        if self._rpc is None:
            raise RpcTransportClosed("adapter is not started")
        return self._rpc.request(
            method, params, timeout=timeout or self._request_timeout
        )

    def thread_start(self, **options: Any) -> dict:
        """Create a new thread. Options pass through (model, cwd, ...)."""
        result = self._request("thread/start", options or {})
        result = result or {}
        # The response echoes the EFFECTIVE thread config; callers verify it
        # against what they requested instead of trusting silent defaults.
        self.last_thread_config = {
            key: result.get(key)
            for key in ("approvalPolicy", "sandbox", "model")
            if key in result
        }
        return result.get("thread") or {}

    def thread_resume(self, thread_id: str, **options: Any) -> dict:
        params = {"threadId": thread_id, **options}
        result = self._request("thread/resume", params)
        return (result or {}).get("thread") or {}

    def turn_start(
        self,
        thread_id: str,
        text: str,
        *,
        client_message_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        **options: Any,
    ) -> dict:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            **options,
        }
        if client_message_id is not None:
            params["clientUserMessageId"] = client_message_id
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        result = self._request("turn/start", params)
        return (result or {}).get("turn") or {}

    def turn_steer(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        *,
        client_message_id: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": [{"type": "text", "text": text}],
        }
        if client_message_id is not None:
            params["clientUserMessageId"] = client_message_id
        result = self._request("turn/steer", params)
        return str((result or {}).get("turnId") or expected_turn_id)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def delta_counters(self, turn_id: str) -> dict[str, dict[str, int]]:
        """Coalesced streaming counters for one turn (audit evidence)."""
        with self._deltas_lock:
            turn = self._deltas.get(turn_id)
            if turn is None:
                return {}
            return {
                item_id: {"events": c.events, "characters": c.characters}
                for item_id, c in turn.by_item.items()
            }

    # -- inbound dispatch ------------------------------------------------------

    def _handle_notification(self, method: str, params: dict) -> None:
        if method.startswith(_DELTA_EVENT_PREFIXES):
            self._count_delta(params)
            return
        if method in _ITEM_LIFECYCLE_EVENTS:
            self.on_event(
                method,
                {
                    "threadId": params.get("threadId"),
                    "turnId": params.get("turnId"),
                    "item": _bounded_item_summary(params.get("item") or {}),
                },
            )
            return
        if method in _FORWARDED_EVENTS:
            self.on_event(method, params)
            return
        # Unknown/low-value notifications become a type-only breadcrumb so
        # protocol drift is visible without storing unbounded payloads.
        self.on_event("codex.unhandled_notification", {"method": method})

    def _count_delta(self, params: dict) -> None:
        turn_id = str(params.get("turnId") or "")
        item_id = str(params.get("itemId") or "")
        delta = params.get("delta")
        with self._deltas_lock:
            turn = self._deltas.setdefault(turn_id, _TurnDeltas())
            counters = turn.by_item.setdefault(item_id, _DeltaCounters())
            counters.events += 1
            if isinstance(delta, str):
                counters.characters += len(delta)

    def _handle_server_request(self, method: str, params: dict) -> Any:
        if method == "item/tool/requestUserInput":
            return self.user_input_handler(params)
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        ):
            return self.approval_handler(method, params)
        logger.warning("Unsupported codex server request: %s", method)
        raise CodexAdapterError(f"unsupported server request: {method}")

    def _handle_closed(self) -> None:
        exit_code = None
        if self._process is not None:
            exit_code = self._process.poll()
        self.on_event("codex.process_closed", {"exit_code": exit_code})
