"""Streaming, atomic sanitization for oversized transcript payloads.

The normal ingest path sanitizes an in-memory string.  Large transcript files
need the same protection before they are copied to object storage, without
using ``read_text()`` (or even ``readline()``) on an attacker-controlled file.
This module therefore scans bytes in fixed-size chunks and keeps only bounded
pattern prefixes in memory.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


_API_PREFIX = b"sk-"
_GITHUB_PREFIX = b"ghp_"
_TELEGRAM_PREFIX = b"bot"
_PEM_BEGIN_PREFIX = b"-----BEGIN"
_PEM_END_PREFIX = b"-----END"

_NORMAL_PREFIXES = (
    _API_PREFIX,
    _GITHUB_PREFIX,
    _TELEGRAM_PREFIX,
    _PEM_BEGIN_PREFIX,
)
_PEM_SUFFIXES = (
    b"PRIVATE KEY-----",
    b"RSA PRIVATE KEY-----",
    b"EC PRIVATE KEY-----",
    b"DSA PRIVATE KEY-----",
    b"OPENSSH PRIVATE KEY-----",
)
_ASCII_WHITESPACE = frozenset(b" \t\n\r\v\f")
_NORMAL_START_RE = re.compile(rb"[\x00\x2dbgs]")
_DIGIT_RUN_RE = re.compile(rb"[0-9]+")
_OUTPUT_BUFFER_BYTES = 1024 * 1024

_API_REPLACEMENT = b"[API_KEY_REDACTED]"
_GITHUB_REPLACEMENT = b"[GITHUB_TOKEN_REDACTED]"
_TELEGRAM_REPLACEMENT = b"[TELEGRAM_BOT_TOKEN_REDACTED]"
_PRIVATE_KEY_REPLACEMENT = b"[PRIVATE_KEY_REDACTED]"


@dataclass(frozen=True, slots=True)
class SanitizedContent:
    """Result of an atomically completed sanitization pass."""

    path: Path
    size: int
    had_sensitive: bool


def _is_ascii_alnum(value: int) -> bool:
    return 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122


def _is_telegram_token_char(value: int) -> bool:
    return _is_ascii_alnum(value) or value in (45, 95)  # ``-`` and ``_``


class _StreamingSanitizer:
    """Small byte-oriented state machine with bounded in-memory candidates."""

    def __init__(self, sink: BinaryIO) -> None:
        self._sink = sink
        self._state = "normal"
        self._pending = bytearray()
        self._candidate = bytearray()
        self._candidate_start = 0
        self._count = 0
        self._suffix = bytearray()
        self._pem_end_pending = bytearray()
        self.had_sensitive = False

    def feed(self, chunk: bytes) -> None:
        offset = 0
        view = memoryview(chunk)
        while offset < len(chunk):
            # Most transcript bytes cannot begin a sensitive pattern.  Copy
            # those spans in bulk; the state machine still receives every
            # possible marker byte and anything adjacent to a chunk boundary.
            if self._state == "normal" and not self._pending:
                match = _NORMAL_START_RE.search(chunk, offset)
                if match is None:
                    self._sink.write(view[offset:])
                    return
                if match.start() > offset:
                    self._sink.write(view[offset : match.start()])
                offset = match.start()

            # Telegram bot ids are unbounded by the source regex.  Copy a
            # digit run in bulk to the rewindable temp file instead of issuing
            # one output write for every id byte.
            if self._state == "telegram_digits":
                match = _DIGIT_RUN_RE.match(chunk, offset)
                if match is not None:
                    self._sink.write(view[offset : match.end()])
                    self._count += match.end() - offset
                    offset = match.end()
                    if offset == len(chunk):
                        return

            value = chunk[offset]
            offset += 1
            # PostgreSQL text cannot contain NUL.  Removing it before matching
            # also prevents a NUL from being used to split an otherwise valid
            # token or PEM delimiter.
            if value != 0:
                self._consume(value)

    def finish(self) -> None:
        """Flush non-matching prefixes while preserving completed redactions."""
        state = self._state
        if state == "normal":
            self._sink.write(self._pending)
            self._pending.clear()
            return
        if state in {"api", "github"}:
            candidate = bytes(self._candidate)
            self._candidate.clear()
            self._state = "normal"
            self._replay_literal(candidate)
            self.finish()
            return
        if state == "telegram_body":
            body = bytes(self._candidate)
            self._candidate.clear()
            self._state = "normal"
            self._replay_literal(body)
            self.finish()
            return
        if state == "pem_begin_suffix":
            suffix = bytes(self._suffix)
            self._suffix.clear()
            self._state = "normal"
            self._replay_literal(suffix)
            self.finish()
            return

        # api_tail has already emitted its replacement.  Telegram digits and
        # PEM begin whitespace were written as they arrived.  A recognized PEM
        # begin is conservatively redacted even if its closing marker is absent.
        self._state = "normal"
        self._pending.clear()

    def _consume(self, value: int) -> None:
        handler = getattr(self, f"_consume_{self._state}")
        handler(value)

    def _consume_normal(self, value: int) -> None:
        self._pending.append(value)
        while self._pending:
            pending = bytes(self._pending)
            if any(marker.startswith(pending) for marker in _NORMAL_PREFIXES):
                if pending == _API_PREFIX:
                    self._candidate = bytearray(_API_PREFIX)
                    self._count = 0
                    self._pending.clear()
                    self._state = "api"
                elif pending == _GITHUB_PREFIX:
                    self._candidate = bytearray(_GITHUB_PREFIX)
                    self._count = 0
                    self._pending.clear()
                    self._state = "github"
                elif pending == _TELEGRAM_PREFIX:
                    self._candidate_start = self._sink.tell()
                    self._sink.write(_TELEGRAM_PREFIX)
                    self._count = 0
                    self._pending.clear()
                    self._state = "telegram_digits"
                elif pending == _PEM_BEGIN_PREFIX:
                    self._candidate_start = self._sink.tell()
                    self._sink.write(_PEM_BEGIN_PREFIX)
                    self._count = 0
                    self._pending.clear()
                    self._state = "pem_begin_ws"
                return

            self._sink.write(self._pending[:1])
            del self._pending[:1]

    def _consume_api(self, value: int) -> None:
        if _is_ascii_alnum(value):
            self._candidate.append(value)
            self._count += 1
            if self._count == 20:
                self._sink.write(_API_REPLACEMENT)
                self.had_sensitive = True
                self._candidate.clear()
                self._state = "api_tail"
            return
        self._fail_buffered_candidate(value)

    def _consume_api_tail(self, value: int) -> None:
        if _is_ascii_alnum(value):
            return
        self._state = "normal"
        self._consume(value)

    def _consume_github(self, value: int) -> None:
        if _is_ascii_alnum(value):
            self._candidate.append(value)
            self._count += 1
            if self._count == 36:
                self._sink.write(_GITHUB_REPLACEMENT)
                self.had_sensitive = True
                self._candidate.clear()
                self._state = "normal"
            return
        self._fail_buffered_candidate(value)

    def _consume_telegram_digits(self, value: int) -> None:
        if 48 <= value <= 57:
            # The bot id has no regex length bound.  Write it to the seekable
            # temporary output and rewind only after the full token is proven.
            self._sink.write(bytes((value,)))
            self._count += 1
            return
        if value == 58 and self._count > 0:  # ``:``
            self._sink.write(b":")
            self._candidate.clear()
            self._state = "telegram_body"
            return
        self._state = "normal"
        self._consume(value)

    def _consume_telegram_body(self, value: int) -> None:
        if _is_telegram_token_char(value):
            self._candidate.append(value)
            if len(self._candidate) == 35:
                self._rewind_candidate()
                self._sink.write(_TELEGRAM_REPLACEMENT)
                self.had_sensitive = True
                self._candidate.clear()
                self._state = "normal"
            return

        body = bytes(self._candidate)
        self._candidate.clear()
        self._state = "normal"
        self._replay_literal(body)
        self._consume(value)

    def _consume_pem_begin_ws(self, value: int) -> None:
        if value in _ASCII_WHITESPACE:
            self._sink.write(bytes((value,)))
            self._count = 1
            return
        if self._count == 0:
            self._state = "normal"
            self._consume(value)
            return
        self._suffix = bytearray((value,))
        self._state = "pem_begin_suffix"
        self._evaluate_pem_suffix(begin=True)

    def _consume_pem_begin_suffix(self, value: int) -> None:
        self._suffix.append(value)
        self._evaluate_pem_suffix(begin=True)

    def _consume_pem_body(self, value: int) -> None:
        self._pem_end_pending.append(value)
        while self._pem_end_pending:
            pending = bytes(self._pem_end_pending)
            if _PEM_END_PREFIX.startswith(pending):
                if pending == _PEM_END_PREFIX:
                    self._pem_end_pending.clear()
                    self._count = 0
                    self._state = "pem_end_ws"
                return
            del self._pem_end_pending[:1]

    def _consume_pem_end_ws(self, value: int) -> None:
        if value in _ASCII_WHITESPACE:
            self._count = 1
            return
        if self._count == 0:
            self._state = "pem_body"
            self._consume(value)
            return
        self._suffix = bytearray((value,))
        self._state = "pem_end_suffix"
        self._evaluate_pem_suffix(begin=False)

    def _consume_pem_end_suffix(self, value: int) -> None:
        self._suffix.append(value)
        self._evaluate_pem_suffix(begin=False)

    def _evaluate_pem_suffix(self, *, begin: bool) -> None:
        suffix = bytes(self._suffix)
        candidates = [
            candidate for candidate in _PEM_SUFFIXES if candidate.startswith(suffix)
        ]
        if any(candidate == suffix for candidate in candidates):
            self._suffix.clear()
            if begin:
                self._rewind_candidate()
                self._sink.write(_PRIVATE_KEY_REPLACEMENT)
                self.had_sensitive = True
                self._pem_end_pending.clear()
                self._state = "pem_body"
            else:
                self._state = "normal"
            return
        if candidates:
            return

        failed = bytes(self._suffix)
        self._suffix.clear()
        self._state = "normal" if begin else "pem_body"
        for value in failed:
            self._consume(value)

    def _fail_buffered_candidate(self, current: int) -> None:
        candidate = bytes(self._candidate)
        self._candidate.clear()
        self._state = "normal"
        # Emitting one byte guarantees progress while replaying the remainder
        # catches a valid token that overlaps a failed prefix.
        self._sink.write(candidate[:1])
        self._replay_literal(candidate[1:])
        self._consume(current)

    def _replay_literal(self, content: bytes) -> None:
        for value in content:
            self._consume(value)

    def _rewind_candidate(self) -> None:
        self._sink.seek(self._candidate_start)
        self._sink.truncate()


def sanitize_content_file(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    chunk_size: int = 64 * 1024,
) -> SanitizedContent:
    """Sanitize ``source_path`` into an atomically replaced ``output_path``.

    Memory use is bounded by ``chunk_size`` plus small fixed pattern prefixes;
    physical lines are never accumulated.  The temporary file is created next
    to the destination so ``os.replace`` remains atomic.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    source = Path(source_path)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with source.open("rb", buffering=0) as source_file:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                os.chmod(temporary_path, 0o600)
                with os.fdopen(
                    descriptor,
                    "w+b",
                    buffering=_OUTPUT_BUFFER_BYTES,
                ) as output_file:
                    sanitizer = _StreamingSanitizer(output_file)
                    while chunk := source_file.read(chunk_size):
                        sanitizer.feed(chunk)
                    sanitizer.finish()
                    output_file.flush()
                    os.fsync(output_file.fileno())
                    size = output_file.tell()
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise

        os.replace(temporary_path, target)
        temporary_path = None
        _fsync_directory(target.parent)
        return SanitizedContent(
            path=target,
            size=size,
            had_sensitive=sanitizer.had_sensitive,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Persist the atomic rename on platforms that support directory fsync."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
