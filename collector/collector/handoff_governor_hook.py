"""Fail-open Claude Code hooks that govern long-running session handoffs."""

from __future__ import annotations

import json
import os
import re
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


def configured_thresholds() -> GovernorThresholds | None:
    """Read a coherent threshold set, or fail open for invalid configuration."""

    try:
        thresholds = GovernorThresholds(
            hygiene=_configured_positive_int(
                "MEMENTO_GOVERNOR_HYGIENE_TOKENS",
                DEFAULT_HYGIENE_TOKENS,
            ),
            handoff=_configured_positive_int(
                "MEMENTO_GOVERNOR_HANDOFF_TOKENS",
                DEFAULT_HANDOFF_TOKENS,
            ),
            block=_configured_positive_int(
                "MEMENTO_GOVERNOR_BLOCK_TOKENS",
                DEFAULT_BLOCK_TOKENS,
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


def prefix_tokens_from_transcript(transcript_path: object) -> int | None:
    """Return the latest primary assistant prompt prefix from a bounded tail."""

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
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
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
        return sum(value for value in values if value is not None)
    return None


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
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    return session_id


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
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(messages),
        }
    }


def _handoff_path(payload: dict[str, Any]) -> Path | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    project = Path(cwd).expanduser()
    configured = os.environ.get("MEMENTO_GOVERNOR_HANDOFF_PATH", "").strip()
    candidate = Path(configured).expanduser() if configured else Path("MEMENTO_HANDOFF.md")
    return candidate if candidate.is_absolute() else project / candidate


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
    if tokens < thresholds.block or payload.get("stop_hook_active") is True:
        return None
    handoff_path = _handoff_path(payload)
    if handoff_path is None:
        return None
    handoff_is_real = _document_has_session_section(handoff_path, session_id)
    if handoff_is_real is not False:
        return None
    return {
        "decision": "block",
        "reason": (
            "Write a handoff section naming this session_id, spawn the successor "
            "(spawn-prime-stop-resume), and print the cd-prefixed resume command."
        ),
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
    thresholds = configured_thresholds()
    if thresholds is None:
        return None
    tokens = prefix_tokens_from_transcript(payload.get("transcript_path"))
    if tokens is None:
        return None
    if event_name == "posttooluse":
        return _nudge_output(session_id, tokens, thresholds)
    return _stop_output(payload, session_id, tokens, thresholds)


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
        return 0
    try:
        response = process_hook_payload(
            _read_stdin_payload(),
            force_enabled=arguments == ["--enabled"],
        )
    except Exception:  # noqa: BLE001, S110 -- a hook must never block work by crashing
        return 0
    if response is not None:
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
