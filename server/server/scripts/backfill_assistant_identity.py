"""Safely overlay assistant identity onto preserved legacy transcripts.

Some historical documents cannot be replaced by the normal reparse because
their stored raw blob is an older, verified prefix of the current revision.
Discarding the newer normalized rows would lose history.  This repair parses
only the verified stored prefix and copies model/reasoning metadata onto an
existing assistant row when its identity is unambiguous.  It never inserts,
deletes, renumbers, or replaces conversation messages.

Dry-run is the default::

    python -m server.scripts.backfill_assistant_identity
    python -m server.scripts.backfill_assistant_identity --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

import asyncpg

from server.scripts.reparse_conversations import (
    SourceRevision,
    _database_dsn,
    _source_payload,
    _source_revision,
    source_payload_error,
)
from server.services.ingest_service import iter_stored_conversation_messages


@dataclass(frozen=True)
class AssistantIdentityRow:
    line_number: int
    message_type: str | None
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class IdentityUpdate:
    line_number: int
    message_type: str | None
    content: str
    metadata_patch: dict[str, str]
    match_kind: str


def _signature(row: AssistantIdentityRow) -> tuple[str, bytes]:
    return (
        row.message_type or "",
        hashlib.sha256(row.content.encode("utf-8")).digest(),
    )


def _identity_patch(
    existing: AssistantIdentityRow,
    parsed: AssistantIdentityRow,
) -> dict[str, str]:
    patch: dict[str, str] = {}
    for key in ("model", "reasoning_effort"):
        value = parsed.metadata.get(key)
        if value and not existing.metadata.get(key):
            patch[key] = str(value)
    return patch


def plan_identity_overlay(
    existing_rows: Iterable[AssistantIdentityRow],
    parsed_rows: Iterable[AssistantIdentityRow],
) -> list[IdentityUpdate]:
    """Return conservative metadata-only updates for unambiguous rows."""
    existing = list(existing_rows)
    parsed = list(parsed_rows)
    existing_counts = Counter(_signature(row) for row in existing)
    parsed_counts = Counter(_signature(row) for row in parsed)
    parsed_by_line = {row.line_number: row for row in parsed}
    parsed_by_signature: dict[
        tuple[str, bytes], list[AssistantIdentityRow]
    ] = defaultdict(list)
    for row in parsed:
        parsed_by_signature[_signature(row)].append(row)

    updates: list[IdentityUpdate] = []
    for row in existing:
        signature = _signature(row)
        candidate = parsed_by_line.get(row.line_number)
        match_kind = "line"
        if candidate is None or _signature(candidate) != signature:
            candidates = parsed_by_signature.get(signature, [])
            if existing_counts[signature] != 1 or parsed_counts[signature] != 1:
                continue
            candidate = candidates[0]
            match_kind = "unique_content"
        if candidate.content != row.content:
            continue
        patch = _identity_patch(row, candidate)
        if patch:
            updates.append(
                IdentityUpdate(
                    line_number=row.line_number,
                    message_type=row.message_type,
                    content=row.content,
                    metadata_patch=patch,
                    match_kind=match_kind,
                )
            )
    return updates


def _parsed_identity_rows(payload: str, tool_id: str) -> list[AssistantIdentityRow]:
    rows: list[AssistantIdentityRow] = []
    for line_number, (normalized, content, metadata, _timestamp) in enumerate(
        iter_stored_conversation_messages(payload, tool_id),
        start=1,
    ):
        if normalized.role != "assistant" or not metadata.get("model"):
            continue
        rows.append(
            AssistantIdentityRow(
                line_number=line_number,
                message_type=normalized.raw_type or normalized.role,
                content=content,
                metadata=dict(metadata),
            )
        )
    return rows


async def _legacy_candidates(conn: asyncpg.Connection) -> list[uuid.UUID]:
    rows = await conn.fetch(
        """
        SELECT d.id
        FROM documents d
        WHERE d.category='conversation'
          AND d.tool_id='codex'
          AND EXISTS (
            SELECT 1
            FROM conversation_messages cm
            WHERE cm.document_id=d.id
              AND cm.role='assistant'
              AND COALESCE(cm.metadata->>'model', '')=''
          )
        ORDER BY d.file_size_bytes, d.id
        """
    )
    return [row["id"] for row in rows]


async def _existing_rows(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
) -> list[AssistantIdentityRow]:
    rows = await conn.fetch(
        """
        SELECT line_number, message_type, content, metadata
        FROM conversation_messages
        WHERE document_id=$1 AND role='assistant'
        ORDER BY line_number
        """,
        document_id,
    )
    def metadata_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        return dict(value or {})

    return [
        AssistantIdentityRow(
            line_number=int(row["line_number"]),
            message_type=row["message_type"],
            content=row["content"],
            metadata=metadata_dict(row["metadata"]),
        )
        for row in rows
    ]


def _stored_prefix_error(source: SourceRevision, payload: str) -> str | None:
    if not source.stored_source_hash or source.stored_source_size is None:
        return "stored source identity is unavailable"
    return source_payload_error(
        payload,
        expected_hash=source.stored_source_hash,
        expected_size=source.stored_source_size,
    )


async def _apply_updates(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    updates: list[IdentityUpdate],
) -> int:
    applied = 0
    async with conn.transaction():
        for update in updates:
            result = await conn.execute(
                """
                UPDATE conversation_messages
                SET metadata = metadata || $4::jsonb
                WHERE document_id=$1
                  AND line_number=$2
                  AND role='assistant'
                  AND message_type IS NOT DISTINCT FROM $3
                  AND content=$5
                  AND (
                    ($4::jsonb ? 'model' AND COALESCE(metadata->>'model', '')='')
                    OR ($4::jsonb ? 'reasoning_effort'
                        AND COALESCE(metadata->>'reasoning_effort', '')='')
                  )
                """,
                document_id,
                update.line_number,
                update.message_type,
                json.dumps(update.metadata_patch),
                update.content,
            )
            applied += int(result.rsplit(" ", 1)[-1])
    return applied


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "documents": 0,
        "source_verified": 0,
        "planned": 0,
        "applied": 0,
        "line_matches": 0,
        "unique_content_matches": 0,
        "skipped_sources": [],
    }
    try:
        candidates = document_ids or await _legacy_candidates(conn)
        for document_id in candidates:
            summary["documents"] += 1
            source = await _source_revision(conn, document_id)
            if source is None:
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
            parsed = _parsed_identity_rows(payload, source.tool_id)
            existing = await _existing_rows(conn, document_id)
            updates = plan_identity_overlay(existing, parsed)
            summary["planned"] += len(updates)
            summary["line_matches"] += sum(
                update.match_kind == "line" for update in updates
            )
            summary["unique_content_matches"] += sum(
                update.match_kind == "unique_content" for update in updates
            )
            if apply and updates:
                summary["applied"] += await _apply_updates(
                    conn,
                    document_id,
                    updates,
                )
            del payload, parsed, existing, updates
        return summary
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit conservative metadata-only identity updates",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        type=uuid.UUID,
        dest="document_ids",
        help="limit repair to one document (may be repeated)",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(apply=args.apply, document_ids=args.document_ids)
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
