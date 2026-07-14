"""Verify complete legacy conversation blobs and record their source identity.

Older installs persisted sanitized conversation content while retaining the
collector's raw-file revision hash.  That is intentional, but those rows did
not record which revision the persisted blob represented.  A later delta may
also have left a tail or an older externalized object behind, so blindly
trusting every legacy blob is unsafe.

This migration adopts only snapshots whose content-derived JSONL statistics
exactly match the cumulative metadata recorded during ingest.  Full-strategy
Claude ``.meta.json`` sidecars are safe to adopt directly.  Rows that fail the
proof remain untouched and must be repaired by a collector full resync.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..services.ingest_service import (
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
)
from ..services.large_content_store import read_large_content
from .reparse_conversations import SUPPORTED_TOOLS, _connect


MAX_LEGACY_SOURCE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class SnapshotEvidence:
    total_lines: int
    message_types: dict[str, int]
    first_timestamp: str
    last_timestamp: str


def collect_jsonl_evidence(payload: str) -> SnapshotEvidence:
    """Reproduce the collector JSONL parser's cumulative metadata."""
    total_lines = 0
    message_types: Counter[str] = Counter()
    first_timestamp = ""
    last_timestamp = ""
    for line in payload.splitlines():
        if not line.strip():
            continue
        total_lines += 1
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        message_types[str(item.get("type", "unknown"))] += 1
        timestamp = item.get("timestamp", "")
        if timestamp:
            timestamp = str(timestamp)
            if not first_timestamp:
                first_timestamp = timestamp
            last_timestamp = timestamp
    return SnapshotEvidence(
        total_lines=total_lines,
        message_types=dict(message_types),
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
    )


def legacy_snapshot_proof(
    *,
    content_type: str,
    metadata: dict[str, Any],
    payload: str,
) -> tuple[bool, str]:
    """Return whether a persisted legacy blob proves it is a full snapshot."""
    if not payload.strip():
        return False, "empty payload"
    if content_type != "jsonl":
        # The only supported non-JSONL conversation documents are Claude
        # .meta.json sidecars, classified by the collector as FULL strategy.
        return True, "full-strategy sidecar"

    evidence = collect_jsonl_evidence(payload)
    expected_lines = metadata.get("total_lines")
    if not isinstance(expected_lines, int) or evidence.total_lines != expected_lines:
        return False, "total_lines mismatch"

    expected_types = metadata.get("message_types")
    if not isinstance(expected_types, dict):
        return False, "message_types missing"
    normalized_types = {
        str(key): int(value)
        for key, value in expected_types.items()
        if isinstance(value, int)
    }
    if evidence.message_types != normalized_types:
        return False, "message_types mismatch"

    for field, actual in (
        ("first_timestamp", evidence.first_timestamp),
        ("last_timestamp", evidence.last_timestamp),
    ):
        expected = str(metadata.get(field) or "")
        if actual != expected:
            return False, f"{field} mismatch"
    return True, "metadata matches stored snapshot"


async def _read_locked_payload(conn, row) -> str:
    if row["content_s3_key"]:
        return await asyncio.to_thread(
            read_large_content,
            row["content_s3_key"],
            max_bytes=MAX_LEGACY_SOURCE_BYTES,
        )
    return row["content"] or ""


async def backfill(
    *,
    apply: bool,
    limit: int | None = None,
    reverse: bool = False,
) -> dict[str, Any]:
    conn = await _connect()
    totals: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    try:
        order = "DESC" if reverse else "ASC"
        document_ids = await conn.fetch(
            f"""
            SELECT id
            FROM documents
            WHERE category='conversation'
              AND tool_id=ANY($1::text[])
              AND COALESCE(metadata->>$2, '') <> content_hash
            ORDER BY id {order}
            LIMIT $3
            """,
            list(SUPPORTED_TOOLS),
            STORED_SOURCE_REVISION_KEY,
            limit,
        )
        for index, item in enumerate(document_ids, start=1):
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, content_type, content, content_s3_key,
                           content_hash, metadata
                    FROM documents
                    WHERE id=$1
                    FOR UPDATE
                    """,
                    item["id"],
                )
                if row is None:
                    totals["deleted"] += 1
                    continue
                raw_metadata = row["metadata"] or {}
                metadata = (
                    json.loads(raw_metadata)
                    if isinstance(raw_metadata, str)
                    else dict(raw_metadata)
                )
                if metadata.get(STORED_SOURCE_REVISION_KEY) == row["content_hash"]:
                    totals["already_verified"] += 1
                    continue
                try:
                    payload = await _read_locked_payload(conn, row)
                except Exception as exc:
                    reason = f"read failed: {type(exc).__name__}"
                    totals["rejected"] += 1
                    rejections[reason] += 1
                    continue
                proved, reason = legacy_snapshot_proof(
                    content_type=row["content_type"],
                    metadata=metadata,
                    payload=payload,
                )
                if not proved:
                    totals["rejected"] += 1
                    rejections[reason] += 1
                    continue
                encoded = payload.encode("utf-8")
                if apply:
                    await conn.execute(
                        """
                        UPDATE documents
                        SET metadata = metadata || jsonb_build_object(
                            $2::text, content_hash,
                            $3::text, $4::text,
                            $5::text, $6::bigint
                        )
                        WHERE id=$1
                        """,
                        row["id"],
                        STORED_SOURCE_REVISION_KEY,
                        STORED_SOURCE_HASH_KEY,
                        hashlib.sha256(encoded).hexdigest(),
                        STORED_SOURCE_SIZE_KEY,
                        len(encoded),
                    )
                totals["verified"] += 1
            if index % 100 == 0:
                print(json.dumps({
                    "progress": f"{index}/{len(document_ids)}",
                    **totals,
                }))
        result = {
            "mode": "apply" if apply else "dry-run",
            "documents_considered": len(document_ids),
            **totals,
            "rejections": dict(rejections),
        }
        print(json.dumps(result))
        return result
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="process newest UUID ordering first (safe for a parallel catch-up pass)",
    )
    args = parser.parse_args()
    asyncio.run(backfill(apply=args.apply, limit=args.limit, reverse=args.reverse))


if __name__ == "__main__":
    main()
