"""Forward-only agent-session lifecycle and execution-privilege observations.

Hooks run inside the owning agent process token, which is the only reliable
place to distinguish an elevated Windows agent from an ordinary collector
service.  They write tiny local markers; the collector polls and uploads the
latest state through its durable metadata queue.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,512}$")
_TOOLS = frozenset({"claude_code", "codex", "cursor"})
_RUNNING_EVENTS = frozenset({"sessionstart", "posttooluse"})
_ENDED_EVENTS = frozenset({"sessionend"})
_PRIVILEGES = frozenset({"standard", "administrator", "root", "unknown"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _runtime_directory() -> Path:
    return Path.home() / ".memento" / "session-runtime"


def _platform_name() -> str:
    return os.name


def _windows_process_is_elevated() -> bool | None:
    """Return the current process token's elevation state, failing unknown."""

    try:
        from ctypes import wintypes

        token_query = 0x0008
        token_elevation_class = 20

        class TokenElevation(ctypes.Structure):
            _fields_ = [("TokenIsElevated", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return None
        try:
            elevation = TokenElevation()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token,
                token_elevation_class,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            ):
                return None
            return bool(elevation.TokenIsElevated)
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def execution_privilege() -> str:
    """Describe only the current hook process' observed OS privilege."""

    if _platform_name() == "nt":
        elevated = _windows_process_is_elevated()
        if elevated is None:
            return "unknown"
        return "administrator" if elevated else "standard"
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        try:
            return "root" if geteuid() == 0 else "standard"
        except OSError:
            return "unknown"
    return "unknown"


def _event_name(payload: dict[str, Any]) -> str:
    value = payload.get("hook_event_name", payload.get("hook_event", ""))
    return re.sub(r"[^a-z]", "", str(value).casefold())


def _session_id(payload: dict[str, Any]) -> str | None:
    for field in ("conversation_id", "session_id"):
        value = payload.get(field)
        if isinstance(value, str) and _SAFE_SESSION_ID.fullmatch(value):
            return value
    return None


def _tool_name(payload: dict[str, Any], session_id: str) -> str | None:
    explicit = str(payload.get("tool") or "").strip().casefold()
    if explicit in _TOOLS:
        return explicit
    if isinstance(payload.get("conversation_id"), str):
        return "cursor"
    if os.environ.get("CODEX_THREAD_ID", "").strip() == session_id:
        return "codex"
    transcript = str(payload.get("transcript_path") or "").replace("\\", "/").casefold()
    if "/.codex/" in transcript or Path(transcript).name.startswith("rollout-"):
        return "codex"
    if "/.claude/" in transcript or "/projects/" in transcript:
        return "claude_code"
    return None


def _timestamp(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_atomic(path: Path, record: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_hook_observation(payload: object) -> bool:
    """Record one supported lifecycle hook without ever blocking the agent."""

    if not isinstance(payload, dict):
        return False
    event = _event_name(payload)
    if event in _RUNNING_EVENTS:
        state = "running"
    elif event in _ENDED_EVENTS:
        state = "ended"
    else:
        return False
    session_id = _session_id(payload)
    if session_id is None:
        return False
    tool = _tool_name(payload, session_id)
    if tool is None:
        return False
    privilege = execution_privilege()
    if privilege not in _PRIVILEGES:
        privilege = "unknown"
    record = {
        "metadata_type": "conversation_runtime",
        "tool": tool,
        "session_id": session_id,
        "runtime_state": state,
        "runtime_privilege": privilege,
        "timestamp": _timestamp(_utc_now()),
    }
    try:
        _write_atomic(_runtime_directory() / tool / f"{session_id}.json", record)
    except OSError:
        return False
    return True


class SessionRuntimePoller:
    """Change-driven reader for bounded hook-produced runtime markers."""

    def __init__(self, *, directory: Path | None = None) -> None:
        self._directory = directory
        self._last_token: tuple[tuple[str, int, int], ...] | None = None

    @property
    def directory(self) -> Path:
        return self._directory or _runtime_directory()

    def _token(self) -> tuple[tuple[str, int, int], ...]:
        try:
            paths = sorted(self.directory.glob("*/*.json"))
        except OSError:
            return ()
        token = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            token.append((path.as_posix(), stat.st_mtime_ns, stat.st_size))
        return tuple(token)

    def needs_poll(self) -> bool:
        return self._token() != self._last_token

    def invalidate(self) -> None:
        self._last_token = None

    def poll(self) -> dict[str, dict[str, dict[str, str]]]:
        token = self._token()
        grouped: dict[str, dict[str, dict[str, str]]] = {}
        for raw_path, _mtime, _size in token:
            path = Path(raw_path)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            tool = str(raw.get("tool") or "").strip()
            session_id = str(raw.get("session_id") or "").strip()
            state = str(raw.get("runtime_state") or "").strip()
            privilege = str(raw.get("runtime_privilege") or "").strip()
            timestamp = str(raw.get("timestamp") or "").strip()[:128]
            if (
                tool not in _TOOLS
                or not _SAFE_SESSION_ID.fullmatch(session_id)
                or state not in {"running", "ended"}
                or privilege not in _PRIVILEGES
                or not timestamp
            ):
                continue
            record = {
                "metadata_type": "conversation_runtime",
                "tool": tool,
                "session_id": session_id,
                "runtime_state": state,
                "runtime_privilege": privilege,
                "timestamp": timestamp,
            }
            grouped.setdefault(tool, {})[f"{tool}:{session_id}"] = record
        self._last_token = token
        return grouped


def _read_stdin_payload() -> object:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    """Fail-open standalone hook entry point."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print("{}")
        return 0
    try:
        record_hook_observation(_read_stdin_payload())
    except Exception:  # noqa: BLE001, S110 -- telemetry must never block an agent
        pass
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
