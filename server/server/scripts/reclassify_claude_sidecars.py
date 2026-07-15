"""Reclassify Claude subagent metadata sidecars as state documents.

Claude writes ``agent-*.meta.json`` next to each subagent JSONL transcript.
Legacy collectors labeled both files as conversations, creating empty thread
cards and unnecessary embeddings for the metadata-only sidecars.  The raw
sidecar remains useful state, so this repair preserves the document and its
version history while removing conversation-only derived data.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from .reparse_conversations import _connect


def _predicate(alias: str) -> str:
    prefix = f"{alias}." if alias else ""
    return f"""
        {prefix}tool_id = 'claude_code'
        AND {prefix}category = 'conversation'
        AND {prefix}relative_path LIKE '%/subagents/%.meta.json'
        AND {prefix}metadata->>'is_subagent_meta' = 'true'
    """


async def reclassify(*, apply: bool) -> dict[str, int | str]:
    connection = await _connect()
    try:
        async with connection.transaction(isolation="serializable"):
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('memento-claude-sidecars'))"
            )
            counts = await connection.fetchrow(
                f"""
                SELECT
                    count(*) AS documents,
                    count(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM conversation_messages cm
                        WHERE cm.document_id = d.id
                    )) AS documents_with_messages,
                    count(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM document_embeddings de
                        WHERE de.document_id = d.id
                    )) AS documents_with_embeddings
                FROM documents d
                WHERE {_predicate("d")}
                """
            )
            result: dict[str, int | str] = {
                "mode": "apply" if apply else "dry-run",
                "documents": int(counts["documents"]),
                "documents_with_messages": int(counts["documents_with_messages"]),
                "documents_with_embeddings": int(
                    counts["documents_with_embeddings"]
                ),
            }
            if not apply or not counts["documents"]:
                print(json.dumps(result))
                return result

            deleted_messages = await connection.fetchval(
                f"""
                WITH removed AS (
                    DELETE FROM conversation_messages cm
                    USING documents d
                    WHERE cm.document_id = d.id AND {_predicate("d")}
                    RETURNING cm.id
                )
                SELECT count(*) FROM removed
                """
            )
            deleted_embeddings = await connection.fetchval(
                f"""
                WITH removed AS (
                    DELETE FROM document_embeddings de
                    USING documents d
                    WHERE de.document_id = d.id AND {_predicate("d")}
                    RETURNING de.id
                )
                SELECT count(*) FROM removed
                """
            )
            updated = await connection.fetchval(
                f"""
                WITH changed AS (
                    UPDATE documents AS d
                    SET category = 'state',
                        activity_at = NULL,
                        embedding_status = 'skipped',
                        embedding_attempts = 0,
                        embedding_claim_token = NULL,
                        embedding_claimed_at = NULL,
                        embedding_content_hash = NULL,
                        knowledge_status = 'skipped',
                        knowledge_attempts = 0,
                        updated_at = now()
                    WHERE {_predicate("d")}
                    RETURNING id
                )
                SELECT count(*) FROM changed
                """
            )
            result.update({
                "reclassified": int(updated),
                "deleted_messages": int(deleted_messages),
                "deleted_embeddings": int(deleted_embeddings),
            })
            print(json.dumps(result))
            return result
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(reclassify(apply=args.apply))


if __name__ == "__main__":
    main()
