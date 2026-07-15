"""Consolidate duplicate Cursor paths that represent one stable session.

Run with ``--dry-run`` first.  The write pass preserves version/audit/knowledge
references, keeps the preferred monotonic source revision, deletes superseded
document aliases, and installs the database uniqueness guard used by ingest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import defaultdict

import asyncpg

from server.services.conversation_identity import (
    CURSOR_SESSION_UNIQUE_INDEX,
    CURSOR_SESSION_UNIQUE_INDEX_SQL,
    select_canonical_cursor_document,
)
from server.services.ingest_service import _source_lock_id
from server.scripts.reparse_conversations import _connect


def build_cursor_consolidation_plan(rows: list[object]) -> list[dict[str, object]]:
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
        canonical = select_canonical_cursor_document(candidates, session_id)
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


async def _candidate_rows(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        WITH duplicate_identities AS (
            SELECT machine_id, metadata->>'session_id' AS session_id
            FROM documents
            WHERE category='conversation'
              AND tool_id='cursor'
              AND coalesce(metadata->>'session_id', '') <> ''
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
        WHERE d.category='conversation' AND d.tool_id='cursor'
        ORDER BY d.machine_id, session_id, d.id
        """
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


async def _replace_daily_summary_reference(
    conn: asyncpg.Connection,
    alias_id: uuid.UUID,
    canonical_id: uuid.UUID,
) -> int:
    status = await conn.execute(
        """
        UPDATE daily_summaries ds
        SET source_document_ids = ARRAY(
            SELECT mapped_id
            FROM (
                SELECT CASE WHEN source_id=$1 THEN $2 ELSE source_id END AS mapped_id,
                       min(ordinality) AS first_ordinality
                FROM unnest(ds.source_document_ids) WITH ORDINALITY
                     AS source(source_id, ordinality)
                GROUP BY mapped_id
            ) mapped
            ORDER BY first_ordinality
        )
        WHERE $1=ANY(ds.source_document_ids)
        """,
        alias_id,
        canonical_id,
    )
    return int(status.rsplit(" ", 1)[-1])


async def consolidate(*, dry_run: bool) -> dict[str, object]:
    conn = await _connect()
    try:
        if dry_run:
            plan = build_cursor_consolidation_plan(await _candidate_rows(conn))
            result = {
                "dry_run": True,
                "identity_groups": len(plan),
                "aliases": sum(len(item["aliases"]) for item in plan),
                "plan": _public_plan(plan),
            }
            print(json.dumps(result, default=str, indent=2), flush=True)
            return result

        async with conn.transaction(isolation="serializable"):
            await conn.execute("SET LOCAL statement_timeout = '10min'")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('memento-cursor-consolidation'))"
            )
            plan = build_cursor_consolidation_plan(await _candidate_rows(conn))
            moved_versions = 0
            moved_access_logs = 0
            moved_observations = 0
            changed_summaries = 0
            deleted_aliases = 0

            for item in plan:
                machine_id = item["machine_id"]
                session_id = str(item["session_id"])
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    _source_lock_id(
                        machine_id=str(machine_id) if machine_id else None,
                        user_id=None,
                        tool_id="cursor",
                        relative_path="",
                        source_identity=session_id,
                    ),
                )
                canonical = item["canonical"]
                canonical_id = canonical["id"]  # type: ignore[index]
                alias_paths: list[str] = []
                earliest_created = canonical["created_at"]  # type: ignore[index]

                for alias in item["aliases"]:  # type: ignore[assignment]
                    alias_id = alias["id"]
                    alias_paths.append(str(alias["relative_path"]))
                    alias_created = alias["created_at"]
                    if alias_created and (
                        earliest_created is None or alias_created < earliest_created
                    ):
                        earliest_created = alias_created

                    await conn.execute(
                        """
                        INSERT INTO document_versions (
                            document_id, content_hash, file_size_bytes, synced_at
                        )
                        SELECT $2, content_hash, file_size_bytes,
                               coalesce(synced_at, now())
                        FROM documents WHERE id=$1
                        """,
                        alias_id,
                        canonical_id,
                    )
                    moved_versions += int((await conn.execute(
                        "UPDATE document_versions SET document_id=$2 WHERE document_id=$1",
                        alias_id,
                        canonical_id,
                    )).rsplit(" ", 1)[-1])
                    moved_access_logs += int((await conn.execute(
                        "UPDATE access_logs SET document_id=$2 WHERE document_id=$1",
                        alias_id,
                        canonical_id,
                    )).rsplit(" ", 1)[-1])
                    moved_observations += int((await conn.execute(
                        """
                        UPDATE knowledge_observations
                        SET source_document_id=$2 WHERE source_document_id=$1
                        """,
                        alias_id,
                        canonical_id,
                    )).rsplit(" ", 1)[-1])
                    changed_summaries += await _replace_daily_summary_reference(
                        conn,
                        alias_id,
                        canonical_id,
                    )
                    await conn.execute(
                        "DELETE FROM conversation_messages_reparse_stage WHERE document_id=$1",
                        alias_id,
                    )
                    await conn.execute(
                        "DELETE FROM conversation_reparse_manifest WHERE document_id=$1",
                        alias_id,
                    )
                    deleted_aliases += int((await conn.execute(
                        "DELETE FROM documents WHERE id=$1",
                        alias_id,
                    )).rsplit(" ", 1)[-1])

                await conn.execute(
                    """
                    UPDATE documents
                    SET created_at=least(created_at, $2),
                        metadata=jsonb_set(
                            coalesce(metadata, '{}'::jsonb),
                            '{cursor_alias_paths}',
                            to_jsonb($3::text[]),
                            true
                        )
                    WHERE id=$1
                    """,
                    canonical_id,
                    earliest_created,
                    alias_paths,
                )

            await conn.execute(
                """
                UPDATE tools
                SET total_files=(
                        SELECT count(*) FROM documents WHERE tool_id='cursor'
                    ),
                    total_size_bytes=(
                        SELECT coalesce(sum(file_size_bytes), 0)
                        FROM documents WHERE tool_id='cursor'
                    )
                WHERE id='cursor'
                """
            )
            await conn.execute(CURSOR_SESSION_UNIQUE_INDEX_SQL)

        result = {
            "dry_run": False,
            "identity_groups": len(plan),
            "deleted_aliases": deleted_aliases,
            "moved_versions": moved_versions,
            "moved_access_logs": moved_access_logs,
            "moved_observations": moved_observations,
            "changed_summaries": changed_summaries,
            "unique_index": CURSOR_SESSION_UNIQUE_INDEX,
            "plan": _public_plan(plan),
        }
        print(json.dumps(result, default=str, indent=2), flush=True)
        return result
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(consolidate(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
