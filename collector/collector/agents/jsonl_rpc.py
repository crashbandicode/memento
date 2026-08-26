"""JSON-RPC 2.0 over newline-delimited JSON for agent child processes.

Codex's app-server speaks JSON-RPC with the ``"jsonrpc"`` header omitted on
the wire, one message per line, over stdio. This client owns the reader
thread and correlation state:

- Client requests block on a per-id event until the response line arrives.
- Server notifications dispatch to a non-blocking callback (exceptions are
  logged, never fatal).
- Server-initiated requests (approvals, user-input questions) dispatch on
  their own daemon threads because answering can take minutes; the reader
  must keep draining notifications while a human decides.
- A malformed line is skipped; only EOF/process death closes the channel,
  failing every pending request with :class:`RpcTransportClosed`.

Payload contents are never logged — method names only.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import IO, Any

import orjson

logger = logging.getLogger("collector.agents.rpc")

_MAX_LINE_BYTES = 8 * 1024 * 1024  # generous; app-server lines are bounded


class RpcTransportClosed(Exception):
    """The child process closed its stdout (exit or crash)."""


class RpcResponseError(Exception):
    """The peer answered a request with a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    @property
    def retryable(self) -> bool:
        # -32001 is app-server backpressure: "Server overloaded; retry later."
        return self.code == -32001


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None


NotificationHandler = Callable[[str, dict], None]
# Returns the result payload for the server-initiated request, or raises to
# produce a JSON-RPC error response.
ServerRequestHandler = Callable[[str, dict], Any]


class JsonlRpcClient:
    def __init__(
        self,
        stdin: IO[bytes],
        stdout: IO[bytes],
        *,
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
        on_closed: Callable[[], None] | None = None,
        name: str = "agent",
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_closed = on_closed
        self._name = name
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(
            target=self._read_loop,
            daemon=True,
            name=f"{self._name}-rpc-reader",
        )
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._stdin.close()
        except OSError:
            pass
        self._fail_all_pending(RpcTransportClosed("client closed"))

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- outbound ------------------------------------------------------------

    def request(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> Any:
        """Send one request and block for its correlated response."""
        if self._closed.is_set():
            raise RpcTransportClosed(f"{self._name} channel is closed")
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            pending = _Pending()
            self._pending[request_id] = pending
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._write(message)
            if not pending.event.wait(timeout):
                raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if pending.error is not None:
            raise pending.error
        return pending.result

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _write(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                self._stdin.write(line.encode("utf-8"))
                self._stdin.flush()
            except (OSError, ValueError) as exc:
                raise RpcTransportClosed(f"{self._name} stdin write failed: {exc}") from exc

    # -- inbound -------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                line = self._stdout.readline(_MAX_LINE_BYTES)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = orjson.loads(line)
                except orjson.JSONDecodeError:
                    logger.warning("%s: skipping malformed protocol line", self._name)
                    continue
                if not isinstance(message, dict):
                    logger.warning("%s: skipping non-object protocol line", self._name)
                    continue
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            self._fail_all_pending(
                RpcTransportClosed(f"{self._name} process closed its stdout")
            )
            if self._on_closed is not None:
                try:
                    self._on_closed()
                except Exception:
                    logger.exception("%s: on_closed handler failed", self._name)

    def _dispatch(self, message: dict) -> None:
        has_id = "id" in message and message["id"] is not None
        method = message.get("method")
        if has_id and method:
            # Server-initiated request: answering may take minutes (a human
            # approval), so never occupy the reader thread with it.
            threading.Thread(
                target=self._answer_server_request,
                args=(message["id"], str(method), message.get("params") or {}),
                daemon=True,
                name=f"{self._name}-server-request",
            ).start()
            return
        if method:
            try:
                self._on_notification(str(method), message.get("params") or {})
            except Exception:
                logger.exception("%s: notification handler failed for %s", self._name, method)
            return
        if has_id:
            self._resolve_response(message)
            return
        logger.warning("%s: skipping protocol message with no id or method", self._name)

    def _answer_server_request(self, request_id: Any, method: str, params: dict) -> None:
        response: dict[str, Any] = {"id": request_id}
        try:
            response["result"] = self._on_server_request(method, params)
        except Exception as exc:
            logger.exception("%s: server request %s failed", self._name, method)
            response["error"] = {"code": -32603, "message": str(exc)[:512]}
        try:
            self._write(response)
        except RpcTransportClosed:
            pass

    def _resolve_response(self, message: dict) -> None:
        with self._pending_lock:
            pending = self._pending.get(message["id"])
        if pending is None:
            logger.warning("%s: response for unknown request id", self._name)
            return
        error = message.get("error")
        if error is not None:
            pending.error = RpcResponseError(
                int(error.get("code", -32603)),
                str(error.get("message", "unknown error")),
                error.get("data"),
            )
        else:
            pending.result = message.get("result")
        pending.event.set()

    def _fail_all_pending(self, error: Exception) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.error = error
            pending.event.set()
