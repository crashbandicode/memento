"""JSONL parser — supports incremental/delta reading. Memory-efficient for large files."""

from __future__ import annotations

import json
from pathlib import Path

from .base import BaseParser, ParseResult

# No content size limit — DELTA mode only reads new lines (small).
# Full resync reads entire file, relying on chunked upload for large files.
MAX_CONTENT_SIZE = 0  # unlimited


class JsonlParser(BaseParser):

    def can_parse(self, path: Path) -> bool:
        return path.suffix.lower() == ".jsonl"

    def parse(
        self,
        path: Path,
        offset: int = 0,
        *,
        end_offset: int | None = None,
    ) -> ParseResult:
        """Parse a byte-bounded JSONL revision.

        ``end_offset`` lets the watcher capture an immutable prefix of an
        append-only transcript while the writer continues adding later
        records.  Reading in binary mode keeps offsets exact for UTF-8 and
        avoids consuming a half-written final record.
        """
        line_count = 0
        title = ""
        first_timestamp = ""
        last_timestamp = ""
        message_types: dict[str, int] = {}
        content_parts: list[str] = []
        content_size = 0

        file_size = path.stat().st_size
        bounded_end = file_size if end_offset is None else min(file_size, end_offset)
        is_partial = offset > 0

        if offset > bounded_end:
            offset = 0
            is_partial = False

        new_offset = offset
        with open(path, "rb") as f:
            if offset > 0:
                f.seek(offset)

            while f.tell() < bounded_end:
                line_start = f.tell()
                raw_line = f.readline(bounded_end - line_start)
                if not raw_line:
                    break
                terminated = raw_line.endswith(b"\n")
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

                # A bounded capture can intersect a record that is still
                # being appended.  Leave that tail for the next guarded delta
                # unless it is already a complete JSON value.
                parsed_object = None
                if not terminated:
                    try:
                        parsed_object = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        new_offset = line_start
                        break

                new_offset = f.tell()
                if not line:
                    continue

                # Accumulate all content (no size limit)
                if MAX_CONTENT_SIZE == 0 or content_size < MAX_CONTENT_SIZE:
                    content_parts.append(line)
                    content_size += len(line) + 1
                line_count += 1

                # Lightweight metadata extraction (only parse first 100 chars for type/timestamp)
                try:
                    obj = parsed_object if parsed_object is not None else json.loads(line)
                    if not isinstance(obj, dict):
                        continue
                    msg_type = str(obj.get("type") or "unknown")
                    message_types[msg_type] = message_types.get(msg_type, 0) + 1

                    if msg_type == "ai-title" and not title:
                        title = str(obj.get("title") or "")

                    ts = obj.get("timestamp", "")
                    if ts:
                        if not first_timestamp:
                            first_timestamp = ts
                        last_timestamp = ts
                except (json.JSONDecodeError, TypeError):
                    continue

        content = "\n".join(content_parts)

        metadata: dict = {
            "message_types": message_types,
            "total_lines": line_count,
        }
        if first_timestamp:
            metadata["first_timestamp"] = first_timestamp
        if last_timestamp:
            metadata["last_timestamp"] = last_timestamp
        if MAX_CONTENT_SIZE and content_size >= MAX_CONTENT_SIZE:
            metadata["truncated"] = True

        return ParseResult(
            content=content,
            title=title or path.stem,
            metadata=metadata,
            line_count=line_count,
            is_partial=is_partial,
            offset=new_offset,
        )
