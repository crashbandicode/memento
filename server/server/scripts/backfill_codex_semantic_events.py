"""Insert missing safe Codex thought summaries and agent lifecycle events.

Preserved legacy conversations can have a verified raw object that is only a
prefix of the live normalized timeline.  A destructive reparse would lose the
newer suffix.  This repair parses the verified prefix, aligns it to existing
rows, and inserts only semantic rows that older parsers skipped.  Existing
messages are never replaced or deleted.

Dry-run is the default::

    python -m server.scripts.backfill_codex_semantic_events --document-id UUID
    python -m server.scripts.backfill_codex_semantic_events --apply --document-id UUID
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import asyncpg

from server.scripts.backfill_assistant_identity import _stored_prefix_error
from server.scripts.reparse_conversations import (
    _database_dsn,
    _source_payload,
    _source_revision,
)
from server.services.ingest_service import iter_stored_conversation_messages


SEMANTIC_MESSAGE_TYPES = {"agent_event", "reasoning"}


@dataclass(frozen=True)
class TimelineRow:
    position: int
    line_number: int | None
    role: str
    message_type: str
    content: str
    metadata: dict[str, Any]
    timestamp: datetime | None


@dataclass(frozen=True)
class SemanticInsert:
    anchor: int
    row: TimelineRow


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value or {})


def _source_key(row: TimelineRow) -> tuple[str, str, str] | None:
    source_id = str(row.metadata.get("source_id") or "").strip()
    if not source_id:
        return None
    return row.role, row.message_type, source_id


def _content_key(row: TimelineRow) -> tuple[str, str, bytes]:
    return (
        row.role,
        row.message_type,
        hashlib.sha256(row.content.encode("utf-8")).digest(),
    )


def _semantic_key(row: TimelineRow) -> tuple[str, str]:
    source_key = _source_key(row)
    if source_key:
        return row.message_type, source_key[-1]
    stable = json.dumps(
        {
            "content": row.content,
            "timestamp": row.timestamp.isoformat() if row.timestamp else "",
            "agent_event": row.metadata.get("agent_event"),
            "thinking": row.metadata.get("thinking"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return row.message_type, hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _unique_matches(
    parsed: list[TimelineRow],
    existing: list[TimelineRow],
) -> dict[int, int]:
    """Map parsed positions to existing line numbers using safe unique keys."""
    matches: dict[int, int] = {}
    matched_lines: set[int] = set()

    for key_fn in (_source_key, _content_key):
        parsed_groups: dict[object, list[TimelineRow]] = defaultdict(list)
        existing_groups: dict[object, list[TimelineRow]] = defaultdict(list)
        for row in parsed:
            if row.message_type in SEMANTIC_MESSAGE_TYPES or row.position in matches:
                continue
            key = key_fn(row)
            if key is not None:
                parsed_groups[key].append(row)
        for row in existing:
            if row.message_type in SEMANTIC_MESSAGE_TYPES or row.line_number in matched_lines:
                continue
            key = key_fn(row)
            if key is not None:
                existing_groups[key].append(row)
        for key, parsed_rows in parsed_groups.items():
            existing_rows = existing_groups.get(key, [])
            if len(parsed_rows) != 1 or len(existing_rows) != 1:
                continue
            line_number = existing_rows[0].line_number
            if line_number is None or line_number < 1:
                continue
            matches[parsed_rows[0].position] = line_number
            matched_lines.add(line_number)
    return matches


def plan_semantic_inserts(
    parsed_rows: Iterable[TimelineRow],
    existing_rows: Iterable[TimelineRow],
) -> list[SemanticInsert]:
    """Plan missing semantic rows at the next unambiguous source anchor."""
    parsed = list(parsed_rows)
    existing = list(existing_rows)
    existing_keys = {
        _semantic_key(row)
        for row in existing
        if row.message_type in SEMANTIC_MESSAGE_TYPES
    }
    candidates = [
        row
        for row in parsed
        if row.message_type in SEMANTIC_MESSAGE_TYPES
        and _semantic_key(row) not in existing_keys
    ]
    if not candidates:
        return []

    matches = _unique_matches(parsed, existing)
    positions = sorted(matches)
    max_line = max((row.line_number or 0 for row in existing), default=0)
    timestamp_anchors = sorted(
        (row.timestamp, row.line_number)
        for row in existing
        if row.timestamp is not None and row.line_number is not None
    )
    timestamps = [item[0] for item in timestamp_anchors]
    planned: list[SemanticInsert] = []
    for row in candidates:
        index = bisect_right(positions, row.position)
        if index < len(positions):
            anchor = matches[positions[index]]
        elif row.timestamp is not None and timestamp_anchors:
            timestamp_index = bisect_right(timestamps, row.timestamp)
            anchor = (
                timestamp_anchors[timestamp_index][1]
                if timestamp_index < len(timestamp_anchors)
                else max_line + 1
            )
        else:
            anchor = max_line + 1
        planned.append(SemanticInsert(anchor=anchor, row=row))
    return planned


def _parsed_rows(
    payload: str,
    tool_id: str,
    supplemental_payloads: Iterable[str] = (),
) -> list[TimelineRow]:
    """Parse a verified base plus server-retained append deltas.

    Supplemental deltas may overlap the base.  Stable source IDs make the
    merge idempotent; a later cumulative record replaces the earlier value at
    the same timeline position.  Unkeyed supplemental rows are ignored so an
    old full-version delta cannot duplicate the base timeline.
    """
    rows: list[TimelineRow] = []
    source_indexes: dict[tuple[str, str, str], int] = {}
    for payload_index, current_payload in enumerate((payload, *supplemental_payloads)):
        for normalized, content, metadata, timestamp in iter_stored_conversation_messages(
            current_payload,
            tool_id,
        ):
            parsed = TimelineRow(
                position=len(rows) + 1,
                line_number=None,
                role=normalized.role,
                message_type=normalized.raw_type or normalized.role,
                content=content,
                metadata=dict(metadata),
                timestamp=timestamp,
            )
            source_key = _source_key(parsed)
            if payload_index > 0 and source_key is None:
                continue
            if source_key is not None and source_key in source_indexes:
                existing_index = source_indexes[source_key]
                parsed = TimelineRow(
                    position=rows[existing_index].position,
                    line_number=None,
                    role=parsed.role,
                    message_type=parsed.message_type,
                    content=parsed.content,
                    metadata=parsed.metadata,
                    timestamp=parsed.timestamp,
                )
                rows[existing_index] = parsed
                continue
            if source_key is not None:
                source_indexes[source_key] = len(rows)
            rows.append(parsed)
    return rows


async def _existing_rows(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
) -> list[TimelineRow]:
    records = await conn.fetch(
        """
        SELECT line_number, role, message_type, content, metadata, timestamp
        FROM conversation_messages
        WHERE document_id=$1 AND line_number >= 1
        ORDER BY line_number
        """,
        document_id,
    )
    return [
        TimelineRow(
            position=0,
            line_number=int(record["line_number"]),
            role=record["role"] or "",
            message_type=record["message_type"] or record["role"] or "",
            content=record["content"],
            metadata=_metadata_dict(record["metadata"]),
            timestamp=record["timestamp"],
        )
        for record in records
    ]


async def _apply_inserts(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    inserts: list[SemanticInsert],
) -> int:
    groups: dict[int, list[TimelineRow]] = defaultdict(list)
    for planned in inserts:
        groups[planned.anchor].append(planned.row)

    async with conn.transaction():
        existing = await conn.fetch(
            """
            SELECT id, line_number
            FROM conversation_messages
            WHERE document_id=$1 AND line_number >= 1
            ORDER BY line_number
            """,
            document_id,
        )
        line_map: list[tuple[int, int]] = []
        new_rows: list[tuple[int, TimelineRow]] = []
        next_line = 1
        for record in existing:
            original_line = int(record["line_number"])
            for row in groups.pop(original_line, []):
                new_rows.append((next_line, row))
                next_line += 1
            line_map.append((int(record["id"]), next_line))
            next_line += 1
        for anchor in sorted(groups):
            for row in groups[anchor]:
                new_rows.append((next_line, row))
                next_line += 1

        minimum_line = await conn.fetchval(
            """
            SELECT COALESCE(MIN(line_number), 0)
            FROM conversation_messages
            WHERE document_id=$1
            """,
            document_id,
        )
        max_positive = int(existing[-1]["line_number"]) if existing else 0
        temporary_offset = max_positive + abs(min(0, int(minimum_line))) + 1
        await conn.execute(
            """
            UPDATE conversation_messages
            SET line_number=-line_number - $2
            WHERE document_id=$1 AND line_number >= 1
            """,
            document_id,
            temporary_offset,
        )
        await conn.execute(
            """
            CREATE TEMP TABLE semantic_line_map (
                message_id bigint PRIMARY KEY,
                new_line integer NOT NULL
            ) ON COMMIT DROP
            """
        )
        await conn.copy_records_to_table(
            "semantic_line_map",
            records=line_map,
            columns=("message_id", "new_line"),
        )
        await conn.execute(
            """
            UPDATE conversation_messages AS message
            SET line_number=line_map.new_line
            FROM semantic_line_map AS line_map
            WHERE message.id=line_map.message_id
            """
        )
        await conn.executemany(
            """
            INSERT INTO conversation_messages (
                document_id, line_number, message_type, role,
                content, metadata, timestamp
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            [
                (
                    document_id,
                    line_number,
                    row.message_type,
                    row.role,
                    row.content,
                    json.dumps(row.metadata),
                    row.timestamp,
                )
                for line_number, row in new_rows
            ],
        )
    return len(new_rows)


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID],
    supplemental_payloads: list[str] | None = None,
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "documents": 0,
        "source_verified": 0,
        "planned": 0,
        "planned_reasoning": 0,
        "planned_agent_events": 0,
        "delta_versions": 0,
        "supplemental_payloads": len(supplemental_payloads or []),
        "append_planned": 0,
        "anchor_min": None,
        "anchor_max": None,
        "applied": 0,
        "skipped_sources": [],
    }
    try:
        for document_id in document_ids:
            summary["documents"] += 1
            source = await _source_revision(conn, document_id)
            if source is None or source.tool_id != "codex":
                summary["skipped_sources"].append(
                    {"document_id": str(document_id), "reason": "not a Codex source"}
                )
                continue
            try:
                payload = await _source_payload(conn, source)
            except Exception as exc:
                summary["skipped_sources"].append(
                    {"document_id": str(document_id), "reason": str(exc)[:200]}
                )
                continue
            error = _stored_prefix_error(source, payload)
            if error:
                summary["skipped_sources"].append(
                    {"document_id": str(document_id), "reason": error}
                )
                continue
            summary["source_verified"] += 1
            version_rows = await conn.fetch(
                """
                SELECT content_delta
                FROM document_versions
                WHERE document_id=$1 AND content_delta IS NOT NULL
                ORDER BY id
                """,
                document_id,
            )
            all_supplemental_payloads = [
                row["content_delta"] for row in version_rows if row["content_delta"]
            ] + list(supplemental_payloads or [])
            summary["delta_versions"] += len(version_rows)
            parsed = _parsed_rows(
                payload,
                source.tool_id,
                all_supplemental_payloads,
            )
            existing = await _existing_rows(conn, document_id)
            inserts = plan_semantic_inserts(parsed, existing)
            max_existing_line = max(
                (row.line_number or 0 for row in existing),
                default=0,
            )
            summary["planned"] += len(inserts)
            summary["append_planned"] += sum(
                item.anchor > max_existing_line for item in inserts
            )
            if inserts:
                minimum = min(item.anchor for item in inserts)
                maximum = max(item.anchor for item in inserts)
                summary["anchor_min"] = (
                    minimum
                    if summary["anchor_min"] is None
                    else min(summary["anchor_min"], minimum)
                )
                summary["anchor_max"] = (
                    maximum
                    if summary["anchor_max"] is None
                    else max(summary["anchor_max"], maximum)
                )
            summary["planned_reasoning"] += sum(
                item.row.message_type == "reasoning" for item in inserts
            )
            summary["planned_agent_events"] += sum(
                item.row.message_type == "agent_event" for item in inserts
            )
            if apply and inserts:
                summary["applied"] += await _apply_inserts(
                    conn, document_id, inserts
                )
            del payload, all_supplemental_payloads, parsed, existing, inserts
        return summary
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--document-id",
        action="append",
        type=uuid.UUID,
        dest="document_ids",
        required=True,
    )
    parser.add_argument(
        "--supplemental-jsonl",
        action="append",
        type=Path,
        default=[],
        help="safe semantic-only JSONL to merge after the verified source",
    )
    args = parser.parse_args()
    supplemental_payloads = [
        path.read_text(encoding="utf-8") for path in args.supplemental_jsonl
    ]
    print(
        json.dumps(
            asyncio.run(
                run(
                    apply=args.apply,
                    document_ids=args.document_ids,
                    supplemental_payloads=supplemental_payloads,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
