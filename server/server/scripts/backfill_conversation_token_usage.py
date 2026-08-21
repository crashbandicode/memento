"""Backfill exact native token totals without reparsing conversation rows.

The default is a read-only scan. Pass ``--apply`` to persist confirmed Codex
or Claude totals into document metadata and the conversation read projection.
Cursor transcripts currently contain no exact token accounting and are not
assigned an invented value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
import orjson

from server.config import settings
from server.services.conversation_usage import (
    TOKEN_USAGE_METADATA_KEY,
    normalize_token_usage,
    observe_token_usage,
)
from server.services.large_content_store import iter_large_content_lines


@dataclass(frozen=True)
class TokenUsageUpdate:
    document_id: UUID
    content_hash: str
    usage: dict[str, object]


def _database_dsn() -> str:
    return str(settings.database_url).replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )


def extract_token_usage_from_lines(
    lines: Iterable[str],
    tool_id: str,
) -> dict[str, object]:
    usage: dict[str, object] = {}
    seen_source_ids: set[str] = set()
    for line in lines:
        try:
            record = orjson.loads(line)
        except (orjson.JSONDecodeError, TypeError, ValueError):
            continue
        usage = observe_token_usage(
            usage,
            seen_source_ids,
            record,
            tool_id,
        )
    return normalize_token_usage(usage)


def _scan_document(row: asyncpg.Record) -> TokenUsageUpdate | None:
    content_s3_key = row["content_s3_key"]
    if content_s3_key:
        lines = iter_large_content_lines(content_s3_key)
    else:
        lines = str(row["content"] or "").splitlines()
    usage = extract_token_usage_from_lines(lines, row["tool_id"])
    if not usage:
        return None
    return TokenUsageUpdate(
        document_id=row["id"],
        content_hash=row["content_hash"],
        usage=usage,
    )


async def _apply_updates(
    conn: asyncpg.Connection,
    updates: list[TokenUsageUpdate],
) -> int:
    changed = 0
    async with conn.transaction():
        for update in updates:
            serialized = json.dumps(update.usage, separators=(",", ":"))
            result = await conn.execute(
                """
                UPDATE documents
                SET metadata=jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        ARRAY[$3::text],
                        $4::jsonb,
                        true
                    ),
                    updated_at=now()
                WHERE id=$1
                  AND content_hash=$2
                  AND COALESCE(metadata->$3, 'null'::jsonb)
                      IS DISTINCT FROM $4::jsonb
                """,
                update.document_id,
                update.content_hash,
                TOKEN_USAGE_METADATA_KEY,
                serialized,
            )
            if result != "UPDATE 1":
                continue
            changed += 1
            await conn.execute(
                """
                UPDATE conversation_read_models
                SET runtime=jsonb_set(
                        COALESCE(runtime, '{}'::jsonb),
                        '{token_usage}',
                        $2::jsonb,
                        true
                    ),
                    updated_at=now()
                WHERE document_id=$1
                """,
                update.document_id,
                serialized,
            )
    return changed


async def run(*, apply: bool, limit: int | None) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        total = int(
            await conn.fetchval(
                """
                SELECT count(*)
                FROM documents
                WHERE category='conversation'
                  AND tool_id=ANY($1::text[])
                """,
                ["codex", "claude_code"],
            )
            or 0
        )
        if limit is not None:
            total = min(total, limit)

        statement = await conn.prepare(
            """
            SELECT id, tool_id, content, content_s3_key, content_hash
            FROM documents
            WHERE category='conversation'
              AND tool_id=ANY($1::text[])
            ORDER BY id
            LIMIT $2
            """
        )
        updates: list[TokenUsageUpdate] = []
        scanned = 0
        # asyncpg cursors require a transaction.  Keep only a very small row
        # window resident: an inline transcript can itself be large, and a
        # bulk ``fetch`` would otherwise materialize the entire corpus.
        async with conn.transaction(readonly=True):
            async for row in statement.cursor(
                ["codex", "claude_code"],
                limit,
                prefetch=4,
            ):
                update = await asyncio.to_thread(_scan_document, row)
                scanned += 1
                if update is not None:
                    updates.append(update)
                if scanned % 100 == 0:
                    print(
                        json.dumps(
                            {
                                "scanned": scanned,
                                "eligible": len(updates),
                                "total": total,
                            }
                        ),
                        flush=True,
                    )
        changed = await _apply_updates(conn, updates) if apply else 0
        return {
            "mode": "apply" if apply else "dry_run",
            "scanned": scanned,
            "eligible": len(updates),
            "changed": changed,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    print(
        json.dumps(asyncio.run(run(apply=args.apply, limit=args.limit))),
        flush=True,
    )


if __name__ == "__main__":
    main()
