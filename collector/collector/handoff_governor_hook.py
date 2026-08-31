"""Fail-open Claude Code hooks that govern long-running session handoffs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_HYGIENE_TOKENS = 200_000
DEFAULT_HANDOFF_TOKENS = 350_000
DEFAULT_BLOCK_TOKENS = 400_000
_TRANSCRIPT_TAIL_BYTES = 512 * 1024
_HANDOFF_DOCUMENT_MAX_BYTES = 512 * 1024
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_SECTION_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+.*$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class GovernorThresholds:
    hygiene: int
    handoff: int
    block: int


@dataclass(frozen=True)
class ContextUsage:
    """One engine's current prompt-prefix usage and optional window size."""

    tokens: int
    context_window: int | None = None


def governor_enabled() -> bool:
    """Return whether the governor was explicitly enabled for this process."""

    return os.environ.get("MEMENTO_GOVERNOR_ENABLED", "").strip().casefold() in _TRUE_VALUES


def _configured_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _default_thresholds(context_window: int | None) -> GovernorThresholds:
    if context_window is None:
        return GovernorThresholds(
            hygiene=DEFAULT_HYGIENE_TOKENS,
            handoff=DEFAULT_HANDOFF_TOKENS,
            block=DEFAULT_BLOCK_TOKENS,
        )
    return GovernorThresholds(
        hygiene=min(DEFAULT_HYGIENE_TOKENS, int(context_window * 0.75)),
        handoff=min(DEFAULT_HANDOFF_TOKENS, int(context_window * 0.85)),
        block=min(DEFAULT_BLOCK_TOKENS, int(context_window * 0.90)),
    )


def configured_thresholds(
    context_window: int | None = None,
) -> GovernorThresholds | None:
    """Read a coherent threshold set, or fail open for invalid configuration."""

    defaults = _default_thresholds(context_window)
    try:
        thresholds = GovernorThresholds(
            hygiene=_configured_positive_int(
                "MEMENTO_GOVERNOR_HYGIENE_TOKENS",
                defaults.hygiene,
            ),
            handoff=_configured_positive_int(
                "MEMENTO_GOVERNOR_HANDOFF_TOKENS",
                defaults.handoff,
            ),
            block=_configured_positive_int(
                "MEMENTO_GOVERNOR_BLOCK_TOKENS",
                defaults.block,
            ),
        )
    except (TypeError, ValueError):
        return None
    if not thresholds.hygiene <= thresholds.handoff <= thresholds.block:
        return None
    return thresholds


def _transcript_tail(path: Path) -> list[bytes] | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    if start:
        first_newline = data.find(b"\n")
        if first_newline < 0:
            return []
        data = data[first_newline + 1 :]
    return data.splitlines()


def _usage_integer(usage: dict[str, Any], field: str) -> int | None:
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_from_transcript(transcript_path: object) -> ContextUsage | None:
    """Read current Claude or Codex usage from a bounded transcript tail."""

    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return None
    path = Path(transcript_path).expanduser()
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    lines = _transcript_tail(path)
    if lines is None:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            continue
        if record.get("type") == "event_msg":
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                return None
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                return None
            tokens = _usage_integer(usage, "input_tokens")
            if tokens is None:
                return None
            context_window = _usage_integer(info, "model_context_window")
            return ContextUsage(tokens=tokens, context_window=context_window)
        if record.get("type") == "assistant":
            if record.get("isSidechain") is True:
                continue
            message = record.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                return None
            values = (
                _usage_integer(usage, "cache_read_input_tokens"),
                _usage_integer(usage, "cache_creation_input_tokens"),
                _usage_integer(usage, "input_tokens"),
            )
            if any(value is None for value in values):
                return None
            return ContextUsage(
                tokens=sum(value for value in values if value is not None)
            )
    return None


def prefix_tokens_from_transcript(transcript_path: object) -> int | None:
    """Backward-compatible token-only view used by existing callers/tests."""

    usage = _usage_from_transcript(transcript_path)
    return usage.tokens if usage is not None else None


def _cursor_state_db_path() -> Path | None:
    configured = os.environ.get("MEMENTO_CURSOR_STATE_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA", "").strip()
        if not app_data:
            return None
        return Path(app_data) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _cursor_usage(session_id: str) -> ContextUsage | None:
    """Read Cursor's exact per-composer context counters without message text."""

    path = _cursor_state_db_path()
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        try:
            row = connection.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{session_id}",),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    if row is None or not isinstance(row[0], (str, bytes)):
        return None
    try:
        state = json.loads(row[0])
    except (UnicodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    tokens = _usage_integer(state, "contextTokensUsed")
    context_window = _usage_integer(state, "contextTokenLimit")
    if tokens is None:
        return None
    return ContextUsage(tokens=tokens, context_window=context_window)


def _payload_usage(payload: dict[str, Any]) -> ContextUsage | None:
    """Use engine-native hook counters when no durable state source exists."""

    context_tokens = _usage_integer(payload, "context_tokens")
    if context_tokens is not None:
        return ContextUsage(
            tokens=context_tokens,
            context_window=_usage_integer(payload, "context_window_size"),
        )
    values = (
        _usage_integer(payload, "input_tokens"),
        _usage_integer(payload, "cache_read_tokens"),
        _usage_integer(payload, "cache_write_tokens"),
    )
    present = [value for value in values if value is not None]
    if not present:
        return None
    return ContextUsage(tokens=sum(present))


def _governor_directory() -> Path:
    return Path.home() / ".memento" / "governor"


def _state_path(session_id: str) -> Path | None:
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    return _governor_directory() / f"{session_id}.json"


def _load_latches(path: Path, session_id: str) -> set[int] | None:
    if not path.exists():
        return set()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or state.get("session_id") != session_id:
        return None
    notified = state.get("notified_thresholds")
    if not isinstance(notified, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in notified
    ):
        return None
    return set(notified)


def _write_latches(path: Path, session_id: str, notified: set[int]) -> bool:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "notified_thresholds": sorted(notified),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def consume_threshold_latch(session_id: str, threshold: int) -> bool:
    """Atomically latch one session/threshold pair and report first delivery."""

    path = _state_path(session_id)
    if path is None or isinstance(threshold, bool) or threshold <= 0:
        return False
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        notified = _load_latches(path, session_id)
        if notified is None or threshold in notified:
            return False
        notified.add(threshold)
        return _write_latches(path, session_id, notified)
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _event_name(payload: dict[str, Any]) -> str:
    return str(payload.get("hook_event_name") or payload.get("hook_event") or "").casefold()


def _session_id(payload: dict[str, Any]) -> str | None:
    for field in ("session_id", "conversation_id"):
        session_id = payload.get(field)
        if isinstance(session_id, str) and _SAFE_SESSION_ID.fullmatch(session_id):
            return session_id
    return None


def _context_usage(payload: dict[str, Any], session_id: str) -> ContextUsage | None:
    transcript_usage = _usage_from_transcript(payload.get("transcript_path"))
    if transcript_usage is not None:
        return transcript_usage
    cursor_usage = _cursor_usage(session_id)
    if cursor_usage is not None:
        return cursor_usage
    return _payload_usage(payload)


def _nudge_output(session_id: str, tokens: int, thresholds: GovernorThresholds) -> dict[str, Any] | None:
    messages: list[str] = []
    if tokens >= thresholds.hygiene and consume_threshold_latch(
        session_id,
        thresholds.hygiene,
    ):
        messages.append(
            "Context hygiene threshold reached: stop quoting large outputs, "
            "persist state to disk, and aim the next milestone at a handoff."
        )
    if tokens >= thresholds.handoff and consume_threshold_latch(
        session_id,
        thresholds.handoff,
    ):
        messages.append(
            "Author the milestone handoff NOW and spawn the successor "
            "(spawn-prime-stop-resume), then print the cd-prefixed resume command."
        )
    if not messages:
        return None
    message = "\n\n".join(messages)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        },
        # Cursor's native hook adapter consumes snake_case directly. Claude
        # Code and Codex ignore the extra field and use hookSpecificOutput.
        "additional_context": message,
    }


def _handoff_path(payload: dict[str, Any]) -> Path | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    project = Path(cwd).expanduser()
    configured = os.environ.get("MEMENTO_GOVERNOR_HANDOFF_PATH", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_absolute() else project / candidate
    for directory in (project, *project.parents):
        candidate = directory / "MEMENTO_HANDOFF.md"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            return None
    return project / "MEMENTO_HANDOFF.md"


def _document_has_session_section(path: Path, session_id: str) -> bool | None:
    try:
        if not path.exists():
            return False
        if path.stat().st_size > _HANDOFF_DOCUMENT_MAX_BYTES:
            return None
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    headings = list(_SECTION_HEADING.finditer(document))
    if not headings:
        return False
    mention = re.compile(
        rf"(?<![A-Za-z0-9._-]){re.escape(session_id)}(?![A-Za-z0-9._-])"
    )
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(document)
        if mention.search(document[heading.start() : end]):
            return True
    return False


def _stop_output(payload: dict[str, Any], session_id: str, tokens: int, thresholds: GovernorThresholds) -> dict[str, str] | None:
    loop_count = payload.get("loop_count")
    already_continued = payload.get("stop_hook_active") is True or (
        isinstance(loop_count, int)
        and not isinstance(loop_count, bool)
        and loop_count > 0
    )
    if tokens < thresholds.block or already_continued:
        return None
    handoff_path = _handoff_path(payload)
    if handoff_path is None:
        return None
    handoff_is_real = _document_has_session_section(handoff_path, session_id)
    if handoff_is_real is not False:
        return None
    reason = (
        "Write a handoff section naming this session_id, spawn the successor "
        "(spawn-prime-stop-resume), and print the cd-prefixed resume command."
    )
    return {
        "decision": "block",
        "reason": reason,
        # Cursor's native Stop response uses this name. Claude Code and Codex
        # consume decision/reason and ignore the compatibility field.
        "followup_message": reason,
    }


def process_hook_payload(
    payload: object,
    *,
    force_enabled: bool = False,
) -> dict[str, Any] | None:
    """Produce a Claude hook response, with every unusable input failing open."""

    if not force_enabled and not governor_enabled():
        return None
    if not isinstance(payload, dict):
        return None
    event_name = _event_name(payload)
    if event_name not in {"posttooluse", "stop"}:
        return None
    session_id = _session_id(payload)
    if session_id is None:
        return None
    usage = _context_usage(payload, session_id)
    if usage is None:
        return None
    thresholds = configured_thresholds(usage.context_window)
    if thresholds is None:
        return None
    if event_name == "posttooluse":
        return _nudge_output(session_id, usage.tokens, thresholds)
    return _stop_output(payload, session_id, usage.tokens, thresholds)


def _read_stdin_payload() -> object:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--enabled"]):
        print("{}")
        return 0
    try:
        response = process_hook_payload(
            _read_stdin_payload(),
            force_enabled=arguments == ["--enabled"],
        )
    except Exception:  # noqa: BLE001, S110 -- a hook must never block work by crashing
        response = None
    # Codex Stop hooks parse successful stdout as JSON, including neutral no-ops.
    print(
        json.dumps(
            response if response is not None else {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
