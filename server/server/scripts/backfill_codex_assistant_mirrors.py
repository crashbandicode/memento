"""Remove exact historical Codex assistant transport mirrors.

Codex writes the same assistant text through a small, known transport family.
Current ingestion folds those copies in ``iter_conversation_messages``. Older
normalized rows can retain both copies, and many of their source snapshots are
only historical prefixes, so a destructive source reparse is not safe.

This repair applies the same conservative identity boundary to stored rows:
same document, exact non-empty text, a different known transport, at most four
normalized rows apart, and within the parser's exact-mirror time window. Only
the lower-priority transport is deleted. Line numbers are deliberately left
stable so existing prompt, task, and agent-event anchors do not move.

Dry-run is the default::

    python -m server.scripts.backfill_codex_assistant_mirrors
    python -m server.scripts.backfill_codex_assistant_mirrors --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

import asyncpg

from server.scripts.reparse_conversations import _database_dsn
from server.services.conversation_parser import (
    CODEX_ASSISTANT_EXACT_MIRROR_MAX_SECONDS,
    CODEX_ASSISTANT_TRANSPORT_PRIORITY,
)


_CANDIDATE_SQL = """
WITH transport_priority AS (
    SELECT *
    FROM unnest($2::text[], $3::int[]) AS priority(message_type, rank)
), candidates AS (
    SELECT DISTINCT ON (lower_message.id)
           lower_message.id,
           lower_message.document_id,
           lower_message.message_type,
           lower_priority.rank,
           preferred.message_type AS preferred_type,
           preferred.line_number AS preferred_line_number
    FROM conversation_messages lower_message
    JOIN documents document ON document.id=lower_message.document_id
    JOIN transport_priority lower_priority
      ON lower_priority.message_type=lower_message.message_type
    JOIN conversation_messages preferred
      ON preferred.document_id=lower_message.document_id
     AND preferred.id<>lower_message.id
     AND preferred.role='assistant'
     AND preferred.content=lower_message.content
     AND abs(preferred.line_number-lower_message.line_number)<=4
    JOIN transport_priority preferred_priority
      ON preferred_priority.message_type=preferred.message_type
     AND preferred_priority.rank>lower_priority.rank
    WHERE document.category='conversation'
      AND document.tool_id='codex'
      AND lower_message.role='assistant'
      AND btrim(lower_message.content)<>''
      AND preferred.timestamp IS NOT NULL
      AND lower_message.timestamp IS NOT NULL
      AND abs(extract(epoch FROM (
          preferred.timestamp-lower_message.timestamp
      )))<=$4
      AND ($1::uuid[] IS NULL OR lower_message.document_id=ANY($1::uuid[]))
    ORDER BY lower_message.id,
             preferred_priority.rank DESC,
             abs(preferred.line_number-lower_message.line_number),
             preferred.id
)
SELECT * FROM candidates
"""


def _priority_arrays() -> tuple[list[str], list[int]]:
    ordered = sorted(CODEX_ASSISTANT_TRANSPORT_PRIORITY.items())
    return [item[0] for item in ordered], [item[1] for item in ordered]


async def _candidate_rows(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
) -> list[asyncpg.Record]:
    names, ranks = _priority_arrays()
    return await conn.fetch(
        _CANDIDATE_SQL,
        document_ids,
        names,
        ranks,
        CODEX_ASSISTANT_EXACT_MIRROR_MAX_SECONDS,
    )


def _summary(rows: list[asyncpg.Record], *, mode: str) -> dict[str, Any]:
    pair_counts: dict[str, int] = {}
    for row in rows:
        pair = f"{row['message_type']}->{row['preferred_type']}"
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    return {
        "mode": mode,
        "documents": len({row["document_id"] for row in rows}),
        "mirrors": len(rows),
        "pairs": dict(sorted(pair_counts.items())),
    }


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        await conn.execute("SET statement_timeout = '15min'")
        rows = await _candidate_rows(conn, document_ids)
        result = _summary(rows, mode="apply" if apply else "dry-run")
        if not apply or not rows:
            print(json.dumps(result), flush=True)
            return result

        names, ranks = _priority_arrays()
        async with conn.transaction(isolation="serializable"):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                "memento-codex-assistant-mirror-backfill",
            )
            await conn.execute("SET LOCAL statement_timeout = '15min'")
            await conn.execute(
                """
                CREATE TEMP TABLE codex_assistant_mirror_delete
                (id bigint PRIMARY KEY, document_id uuid NOT NULL)
                ON COMMIT DROP
                """
            )
            await conn.execute(
                """
                INSERT INTO codex_assistant_mirror_delete (id, document_id)
                SELECT id, document_id FROM (
                """
                + _CANDIDATE_SQL
                + ") candidate",
                document_ids,
                names,
                ranks,
                CODEX_ASSISTANT_EXACT_MIRROR_MAX_SECONDS,
            )
            current = await conn.fetchval(
                "SELECT count(*) FROM codex_assistant_mirror_delete"
            )
            if current != len(rows):
                raise RuntimeError(
                    "candidate set changed during repair; retry after live ingest settles"
                )
            await conn.execute(
                """
                DELETE FROM document_embeddings embedding
                USING (
                    SELECT DISTINCT document_id
                    FROM codex_assistant_mirror_delete
                ) affected
                WHERE embedding.document_id=affected.document_id
                """
            )
            await conn.execute(
                """
                UPDATE documents document SET
                    embedding_status='pending',
                    embedding_attempts=0,
                    embedding_claim_token=NULL,
                    embedding_claimed_at=NULL
                FROM (
                    SELECT DISTINCT document_id
                    FROM codex_assistant_mirror_delete
                ) affected
                WHERE document.id=affected.document_id
                """
            )
            deleted = await conn.fetchval(
                """
                WITH removed AS (
                    DELETE FROM conversation_messages message
                    USING codex_assistant_mirror_delete target
                    WHERE message.id=target.id
                    RETURNING 1
                )
                SELECT count(*) FROM removed
                """
            )
            if deleted != len(rows):
                raise RuntimeError("not every guarded mirror row was deleted")

        result["deleted"] = deleted
        print(json.dumps(result), flush=True)
        return result
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--document-id", action="append", default=[])
    args = parser.parse_args()
    document_ids = [uuid.UUID(value) for value in args.document_id] or None
    asyncio.run(run(apply=args.apply, document_ids=document_ids))


if __name__ == "__main__":
    main()
