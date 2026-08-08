"""Bounded, verified streaming access to conversation JSONL files."""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import orjson


DEFAULT_STREAM_CHUNK_BYTES = 64 * 1024
DEFAULT_PREFIX_BYTES = 1024 * 1024
MAX_JSON_RECORD_CHARS = 16 * 1024 * 1024


class ConversationSourceChanged(OSError):
    """Raised when a parsed file no longer matches its sanitized proof."""


def iter_decoded_json_objects(
    byte_chunks: Iterable[bytes],
    *,
    max_record_chars: int = MAX_JSON_RECORD_CHARS,
) -> Iterator[object]:
    """Decode and frame JSON values without materializing the whole transcript.

    JSONL produced by the supported collectors is usually one compact object per
    line, but Claude can also emit indented multi-line objects.  A string-aware
    bracket scanner therefore frames complete top-level objects while an
    incremental decoder preserves UTF-8 characters split across read chunks.
    Memory is proportional to one bounded source record, not the source file.
    """
    if max_record_chars < 1:
        raise ValueError("max_record_chars must be positive")

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pieces: list[str] = []
    record_chars = 0
    depth = 0
    started = False
    discarding = False
    in_string = False
    escaped = False

    def consume(text: str) -> Iterator[object]:
        nonlocal pieces
        nonlocal record_chars
        nonlocal depth
        nonlocal started
        nonlocal discarding
        nonlocal in_string
        nonlocal escaped

        piece_start: int | None = 0 if started and not discarding else None
        for index, char in enumerate(text):
            if not started:
                if char in " \t\r\n":
                    continue
                if char not in "[{":
                    # Conversation streams contain object records.  Preserve the
                    # old parser's malformed-fragment tolerance by ignoring
                    # non-JSON noise through the next physical line.
                    continue
                started = True
                discarding = False
                in_string = False
                escaped = False
                depth = 1
                record_chars = 1
                pieces = []
                piece_start = index
                continue

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1

            if not discarding:
                record_chars += 1
                if record_chars > max_record_chars:
                    pieces = []
                    discarding = True
                    piece_start = None

            if depth != 0:
                continue

            if not discarding:
                assert piece_start is not None
                pieces.append(text[piece_start : index + 1])
                serialized = "".join(pieces)
                try:
                    yield orjson.loads(serialized)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            started = False
            discarding = False
            in_string = False
            escaped = False
            depth = 0
            record_chars = 0
            pieces = []
            piece_start = None

        if started and not discarding and piece_start is not None:
            fragment = text[piece_start:]
            pieces.append(fragment)

    for chunk in byte_chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("conversation stream chunks must be bytes")
        if chunk:
            yield from consume(decoder.decode(chunk, final=False))
    yield from consume(decoder.decode(b"", final=True))


@dataclass(frozen=True, slots=True)
class ConversationFileSource:
    """A sanitized file plus immutable byte-level proof used during ingest."""

    path: Path
    size: int
    sha256: str
    prefix: str
    chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES

    @classmethod
    def inspect(
        cls,
        path: str | Path,
        *,
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
        prefix_bytes: int = DEFAULT_PREFIX_BYTES,
    ) -> "ConversationFileSource":
        """Build a proof using fixed-size reads and a bounded decoded prefix."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if prefix_bytes < 0:
            raise ValueError("prefix_bytes must be non-negative")

        source_path = Path(path)
        digest = hashlib.sha256()
        total = 0
        prefix = bytearray()
        with source_path.open("rb", buffering=0) as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
                total += len(chunk)
                if len(prefix) < prefix_bytes:
                    prefix.extend(chunk[: prefix_bytes - len(prefix)])
        return cls(
            path=source_path,
            size=total,
            sha256=digest.hexdigest(),
            prefix=bytes(prefix).decode("utf-8", errors="replace"),
            chunk_size=chunk_size,
        )

    def iter_objects(self) -> Iterator[object]:
        """Parse the file and verify the exact bytes as they are consumed."""
        digest = hashlib.sha256()
        total = 0

        def chunks() -> Iterator[bytes]:
            nonlocal total
            with self.path.open("rb", buffering=0) as stream:
                while chunk := stream.read(self.chunk_size):
                    digest.update(chunk)
                    total += len(chunk)
                    yield chunk

        yield from iter_decoded_json_objects(chunks())
        if total != self.size or digest.hexdigest() != self.sha256:
            raise ConversationSourceChanged(
                "conversation source changed after its sanitized proof was created"
            )
