"""Consolidate duplicate movable paths that represent one stable session.

Run with ``--dry-run`` first.  The write pass preserves version/audit/knowledge
references, keeps the preferred monotonic source revision, deletes superseded
document aliases, and installs the database uniqueness guard used by ingest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict

import asyncpg

from server.services.conversation_identity import (
    CODEX_SESSION_UNIQUE_INDEX,
    CODEX_SESSION_UNIQUE_INDEX_SQL,
    CURSOR_SESSION_UNIQUE_INDEX,
    CURSOR_SESSION_UNIQUE_INDEX_SQL,
    select_canonical_conversation_document,
)
from server.services.ingest_service import _source_lock_id
from server.scripts.reparse_conversations import _connect


IDENTITY_INDEXES = {
    "codex": (CODEX_SESSION_UNIQUE_INDEX, CODEX_SESSION_UNIQUE_INDEX_SQL),
    "cursor": (CURSOR_SESSION_UNIQUE_INDEX, CURSOR_SESSION_UNIQUE_INDEX_SQL),
}


def build_session_consolidation_plan(
    rows: list[object],
    *,
    tool_id: str,
) -> list[dict[str, object]]:
    """Group candidate rows and select one canonical revision per identity."""
    grouped: dict[tuple[object, str], list[object]] = defaultdict(list)
    for row in rows:
        machine_id = row["machine_id"]  # type: ignore[index]
        session_id = str(row["session_id"])  # type: ignore[index]
        grouped[(machine_id, session_id)].append(row)

    plan: list[dict[str, object]] = []
    for (machine_id, session_id), candidates in sorted(
        grouped.items(),
        key=lambda item: (str(item[0][0]), item[0][1]),
    ):
        if len(candidates) < 2:
            continue
        canonical = select_canonical_conversation_document(
            candidates,
            tool_id=tool_id,
            session_id=session_id,
        )
        assert canonical is not None
        canonical_id = canonical["id"]  # type: ignore[index]
        aliases = [
            candidate
            for candidate in candidates
            if candidate["id"] != canonical_id  # type: ignore[index]
        ]
        plan.append({
            "machine_id": machine_id,
            "session_id": session_id,
            "canonical": canonical,
            "aliases": aliases,
        })
    return plan


def build_cursor_consolidation_plan(rows: list[object]) -> list[dict[str, object]]:
    """Backward-compatible Cursor plan helper used by existing tests/tools."""
    return build_session_consolidation_plan(rows, tool_id="cursor")


async def _candidate_rows(
    conn: asyncpg.Connection,
    *,
    tool_id: str,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        WITH duplicate_identities AS (
            SELECT machine_id, metadata->>'session_id' AS session_id
            FROM documents
            WHERE category='conversation'
              AND tool_id=$1
              AND coalesce(metadata->>'session_id', '') <> ''
              AND ($1 <> 'codex' OR metadata->>'session_id'=metadata->>'thread_id')
            GROUP BY machine_id, metadata->>'session_id'
            HAVING count(*) > 1
        )
        SELECT d.id, d.machine_id, d.relative_path, d.content_hash,
               d.file_size_bytes, d.source_modified_at, d.synced_at,
               d.created_at, d.metadata->>'session_id' AS session_id
        FROM documents d
        JOIN duplicate_identities i
          ON i.machine_id=d.machine_id
         AND i.session_id=d.metadata->>'session_id'
        WHERE d.category='conversation' AND d.tool_id=$1
        ORDER BY d.machine_id, session_id, d.id
        """,
        tool_id,
    )


def _public_plan(plan: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "machine_id": str(item["machine_id"]),
            "session_id": item["session_id"],
            "canonical_id": str(item["canonical"]["id"]),  # type: ignore[index]
            "canonical_path": item["canonical"]["relative_path"],  # type: ignore[index]
            "alias_ids": [
                str(alias["id"]) for alias in item["aliases"]  # type: ignore[index]
            ],
            "alias_paths": [
                alias["relative_path"] for alias in item["aliases"]  # type: ignore[index]
            ],
        }
        for item in plan
    ]


async def consolidate(
    *,
    dry_run: bool,
    tool_id: str = "cursor",
    include_plan: bool = True,
) -> dict[str, object]:
    if tool_id not in IDENTITY_INDEXES:
        raise ValueError(f"unsupported stable-identity tool: {tool_id}")
    unique_index, unique_index_sql = IDENTITY_INDEXES[tool_id]
    conn = await _connect()
    try:
        if dry_run:
            plan = build_session_consolidation_plan(
                await _candidate_rows(conn, tool_id=tool_id),
                tool_id=tool_id,
            )
            result = {
                "dry_run": True,
                "tool_id": tool_id,
                "identity_groups": len(plan),
                "aliases": sum(len(item["aliases"]) for item in plan),
            }
            if include_plan:
                result["plan"] = _public_plan(plan)
            print(json.dumps(result, default=str, indent=2), flush=True)
            return result

        async with conn.transaction(isolation="serializable"):
            await conn.execute("SET LOCAL statement_timeout = '10min'")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"memento-{tool_id}-consolidation",
            )
            plan = build_session_consolidation_plan(
                await _candidate_rows(conn, tool_id=tool_id),
                tool_id=tool_id,
            )
            moved_versions = 0
            moved_access_logs = 0
            moved_observations = 0
            changed_summaries = 0
            deleted_aliases = 0
            mapping_rows: list[tuple[object, object]] = []

            for item in plan:
                machine_id = item["machine_id"]
                session_id = str(item["session_id"])
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _source_lock_id(
                        machine_id=str(machine_id) if machine_id else None,
                        user_id=None,
                        tool_id=tool_id,
                        relative_path="",
                        source_identity=session_id,
                    ),
                )
                canonical = item["canonical"]
                canonical_id = canonical["id"]  # type: ignore[index]
                for alias in item["aliases"]:  # type: ignore[assignment]
                    mapping_rows.append((alias["id"], canonical_id))

            if mapping_rows:
                await conn.execute(
                    """
                    CREATE TEMP TABLE conversation_session_alias_map (
                        alias_id uuid PRIMARY KEY,
                        canonical_id uuid NOT NULL
                    ) ON COMMIT DROP
                    """
                )
                await conn.executemany(
                    """
                    INSERT INTO conversation_session_alias_map
                        (alias_id, canonical_id)
                    VALUES ($1, $2)
                    """,
                    mapping_rows,
                )
                await conn.execute(
                    """
                    INSERT INTO document_versions (
                        document_id, content_hash, file_size_bytes, synced_at
                    )
                    SELECT m.canonical_id, d.content_hash, d.file_size_bytes,
                           coalesce(d.synced_at, now())
                    FROM conversation_session_alias_map m
                    JOIN documents d ON d.id=m.alias_id
                    """
                )
                moved_versions = int((await conn.execute(
                    """
                    UPDATE document_versions v
                    SET document_id=m.canonical_id
                    FROM conversation_session_alias_map m
                    WHERE v.document_id=m.alias_id
                    """
                )).rsplit(" ", 1)[-1])
                moved_access_logs = int((await conn.execute(
                    """
                    UPDATE access_logs a
                    SET document_id=m.canonical_id
                    FROM conversation_session_alias_map m
                    WHERE a.document_id=m.alias_id
                    """
                )).rsplit(" ", 1)[-1])
                moved_observations = int((await conn.execute(
                    """
                    UPDATE knowledge_observations o
                    SET source_document_id=m.canonical_id
                    FROM conversation_session_alias_map m
                    WHERE o.source_document_id=m.alias_id
                    """
                )).rsplit(" ", 1)[-1])
                changed_summaries = int((await conn.execute(
                    """
                    UPDATE daily_summaries ds
                    SET source_document_ids=(
                        SELECT array_agg(mapped_id ORDER BY first_ordinality)
                        FROM (
                            SELECT coalesce(m.canonical_id, source_id) AS mapped_id,
                                   min(ordinality) AS first_ordinality
                            FROM unnest(ds.source_document_ids) WITH ORDINALITY
                                 AS source(source_id, ordinality)
                            LEFT JOIN conversation_session_alias_map m
                              ON m.alias_id=source_id
                            GROUP BY coalesce(m.canonical_id, source_id)
                        ) mapped
                    )
                    WHERE EXISTS (
                        SELECT 1 FROM conversation_session_alias_map m
                        WHERE m.alias_id=ANY(ds.source_document_ids)
                    )
                    """
                )).rsplit(" ", 1)[-1])
                await conn.execute(
                    """
                    DELETE FROM conversation_messages_reparse_stage s
                    USING conversation_session_alias_map m
                    WHERE s.document_id=m.alias_id
                    """
                )
                await conn.execute(
                    """
                    DELETE FROM conversation_reparse_manifest r
                    USING conversation_session_alias_map m
                    WHERE r.document_id=m.alias_id
                    """
                )
                deleted_aliases = int((await conn.execute(
                    """
                    DELETE FROM documents d
                    USING conversation_session_alias_map m
                    WHERE d.id=m.alias_id
                    """
                )).rsplit(" ", 1)[-1])

            await conn.execute(
                """
                UPDATE tools
                SET total_files=(
                        SELECT count(*) FROM documents WHERE tool_id=$1
                    ),
                    total_size_bytes=(
                        SELECT coalesce(sum(file_size_bytes), 0)
                        FROM documents WHERE tool_id=$1
                    )
                WHERE id=$1
                """,
                tool_id,
            )
            await conn.execute(unique_index_sql)

        result = {
            "dry_run": False,
            "tool_id": tool_id,
            "identity_groups": len(plan),
            "deleted_aliases": deleted_aliases,
            "moved_versions": moved_versions,
            "moved_access_logs": moved_access_logs,
            "moved_observations": moved_observations,
            "changed_summaries": changed_summaries,
            "unique_index": unique_index,
        }
        if include_plan:
            result["plan"] = _public_plan(plan)
        print(json.dumps(result, default=str, indent=2), flush=True)
        return result
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tool", choices=sorted(IDENTITY_INDEXES), default="cursor")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(consolidate(
        dry_run=args.dry_run,
        tool_id=args.tool,
        include_plan=not args.summary_only,
    ))


if __name__ == "__main__":
    main()
