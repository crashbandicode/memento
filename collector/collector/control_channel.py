"""Durable server→collector control channel.

Long-polls ``POST /api/control/poll`` for leased commands, acknowledges
delivery, executes, and reports an explicit terminal outcome. This replaces
the legacy 10-second short poll whose ack both deleted the command and stood
in for a result. Invariants mirrored from the server state machine:

- Acknowledge before executing: a failed ack skips execution and lets the
  server lease expire and redeliver (or fail closed for destructive kinds).
- Every executed command reports ``completed``/``failed``/``cancelled`` with
  a stable machine-readable error code; outcomes are fenced by the lease id,
  so a stale duplicate can never overwrite the recorded result.
- The poll also carries the collector's bounded capability snapshot, which
  the server uses to route only supported command kinds.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import httpx

from .config import CollectorConfig
from .tls import SSL_CONTEXT

logger = logging.getLogger("collector.control")

DEFAULT_WAIT_SECONDS = 20
DEFAULT_MAX_COMMANDS = 4
# Must cover the slowest command execution (a resync drains uploads and can
# take a couple of minutes) so the lease cannot expire mid-execution.
DEFAULT_LEASE_SECONDS = 300
_ERROR_BACKOFF_INITIAL = 5.0
_ERROR_BACKOFF_MAX = 120.0
_UNSUPPORTED_SERVER_BACKOFF = 60.0
_COMPLETE_RETRY_DELAYS = (2.0, 5.0, 10.0)

SUPPORTED_COMMANDS = (
    "device.resync",
    "conversation.repair",
    "collector.update",
)

# (status, error_code, detail) — status must be completed/failed/cancelled.
CommandOutcome = tuple[str, str | None, dict]
CommandExecutor = Callable[[str, dict], CommandOutcome]


def collector_version() -> str:
    try:
        from importlib.metadata import version

        return version("memento-brain-collector")
    except Exception:
        return "dev"


def capability_snapshot(config: CollectorConfig) -> dict:
    """Bounded, schema-versioned capability report for the server."""
    return {
        "schema_version": 1,
        "collector_version": collector_version(),
        "platform": config.platform,
        "control": {
            "long_poll": True,
            "outcome_reporting": True,
            "commands": list(SUPPORTED_COMMANDS),
        },
        "agents": {},
    }


class ControlChannel:
    """One daemon thread owning the durable command long-poll."""

    def __init__(
        self,
        config: CollectorConfig,
        execute: CommandExecutor,
        *,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_commands: int = DEFAULT_MAX_COMMANDS,
    ) -> None:
        self._config = config
        self._execute = execute
        self._wait_seconds = max(0, min(int(wait_seconds), 25))
        self._lease_seconds = max(5, min(int(lease_seconds), 300))
        self._max_commands = max(1, min(int(max_commands), 16))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._version = collector_version()
        self._client = httpx.Client(
            base_url=config.server.url,
            # Read timeout must exceed the server-side long-poll hold.
            timeout=httpx.Timeout(self._wait_seconds + 15.0, connect=10.0),
            verify=SSL_CONTEXT,
            headers={
                "X-Collector-Token": config.server.token,
                "X-Device-Id": config.device_id,
                "X-Device-Name": config.device_name,
                "X-Device-Platform": config.platform,
                "X-Collector-Version": self._version,
            },
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="control-channel",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._client.close()

    # -- one poll cycle ----------------------------------------------------

    def poll_once(self) -> int:
        """Long-poll once and process every delivered command. Returns count."""
        response = self._client.post(
            "/api/control/poll",
            json={
                "wait_seconds": self._wait_seconds,
                "max_commands": self._max_commands,
                "lease_seconds": self._lease_seconds,
                "collector_version": self._version,
                "capabilities": capability_snapshot(self._config),
            },
        )
        if response.status_code in (404, 405):
            raise UnsupportedServerError(response.status_code)
        response.raise_for_status()
        commands = response.json().get("commands", [])
        processed = 0
        for command in commands:
            if self._stop.is_set():
                break
            if self._process(command):
                processed += 1
        return processed

    def _process(self, command: dict) -> bool:
        command_id = str(command.get("id") or "")
        lease_id = str(command.get("lease_id") or "")
        kind = str(command.get("kind") or command.get("action") or "")
        payload = command.get("payload") or {}
        if not command_id or not lease_id or not kind:
            logger.warning("Skipping malformed control command: %r", command_id)
            return False

        # Acknowledge delivery before executing. On failure, defer to the
        # server's lease-expiry semantics instead of executing unrecorded.
        try:
            ack = self._client.post(
                f"/api/control/commands/{command_id}/ack",
                json={"lease_id": lease_id},
            )
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Deferring command %s: ack failed (%s)", command_id, exc)
            return False
        if ack.status_code != 200:
            logger.warning(
                "Deferring command %s: ack rejected (%d)", command_id, ack.status_code
            )
            return False

        started = time.monotonic()
        try:
            status, error_code, detail = self._execute(kind, dict(payload))
        except Exception as exc:  # noqa: BLE001 — report, never crash the channel
            logger.exception("Command %s (%s) raised", command_id, kind)
            status, error_code, detail = (
                "failed",
                "command.execution_failed",
                {"error": str(exc)[:512]},
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._report_outcome(
            command_id, lease_id, status, error_code, detail, elapsed_ms
        )
        return True

    def _report_outcome(
        self,
        command_id: str,
        lease_id: str,
        status: str,
        error_code: str | None,
        detail: dict,
        elapsed_ms: int,
    ) -> None:
        body = {
            "lease_id": lease_id,
            "status": status,
            "error_code": error_code,
            "detail": detail,
            "elapsed_ms": elapsed_ms,
        }
        for attempt, delay in enumerate((0.0,) + _COMPLETE_RETRY_DELAYS):
            if delay and self._stop.wait(delay):
                return
            try:
                response = self._client.post(
                    f"/api/control/commands/{command_id}/complete",
                    json=body,
                )
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "Outcome report attempt %d for %s failed: %s",
                    attempt + 1,
                    command_id,
                    exc,
                )
                continue
            if response.status_code == 200:
                return
            if response.status_code == 409:
                # First-writer-wins on the server; nothing left to report.
                logger.warning(
                    "Outcome for %s superseded by an earlier terminal state",
                    command_id,
                )
                return
            logger.warning(
                "Outcome report attempt %d for %s rejected (%d)",
                attempt + 1,
                command_id,
                response.status_code,
            )
        logger.error(
            "Giving up reporting outcome for %s; server lease policy decides",
            command_id,
        )

    # -- thread body ---------------------------------------------------------

    def _run(self) -> None:
        backoff = _ERROR_BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = _ERROR_BACKOFF_INITIAL
            except UnsupportedServerError:
                logger.info(
                    "Server has no control channel yet; retrying in %.0fs",
                    _UNSUPPORTED_SERVER_BACKOFF,
                )
                self._stop.wait(_UNSUPPORTED_SERVER_BACKOFF)
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Control poll failed: %s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _ERROR_BACKOFF_MAX)
            except Exception:  # noqa: BLE001 — the channel must outlive any cycle
                # This thread is the machine's only heartbeat/command path; a
                # dead thread looks identical to a healthy idle one from the
                # server side. Log loudly and keep polling.
                logger.exception("Control poll cycle failed; channel continues")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _ERROR_BACKOFF_MAX)


class UnsupportedServerError(Exception):
    """The server does not expose /api/control yet (pre-rollout)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"control channel unsupported: HTTP {status_code}")
        self.status_code = status_code
