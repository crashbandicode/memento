"""Ask a live collector to upload authoritative conversation snapshots.

Run this inside the API container. The command resolves each document to its
owning collector and calls the authenticated device API, so the request enters
the live API process's in-memory command queue instead of mutating it from a
separate process.

Example::

    python -m server.scripts.request_conversation_repair DOCUMENT_ID
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

import asyncpg
import httpx

from server.middleware.auth import create_access_token
from server.scripts.reparse_conversations import _database_dsn


async def request_repair(document_ids: list[uuid.UUID]) -> list[dict[str, str]]:
    conn = await asyncpg.connect(_database_dsn())
    results: list[dict[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for document_id in document_ids:
                row = await conn.fetchrow(
                    """
                    SELECT d.machine_id, u.id AS user_id, u.role
                    FROM documents d
                    JOIN machines m ON m.id=d.machine_id
                    JOIN users u ON u.id=m.user_id
                    WHERE d.id=$1
                      AND d.category='conversation'
                      AND d.tool_id=ANY($2::text[])
                    """,
                    document_id,
                    ["codex", "claude_code", "cursor"],
                )
                if row is None:
                    results.append(
                        {
                            "document_id": str(document_id),
                            "status": "not_found",
                        }
                    )
                    continue
                token = create_access_token(str(row["user_id"]), row["role"])
                response = await client.post(
                    f"http://127.0.0.1:8000/api/devices/{row['machine_id']}/command",
                    params={
                        "action": "repair-conversations",
                        "document_id": str(document_id),
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                results.append(
                    {
                        "document_id": str(document_id),
                        "status": "queued",
                    }
                )
    finally:
        await conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document_ids", nargs="+", type=uuid.UUID)
    args = parser.parse_args()
    for result in asyncio.run(request_repair(args.document_ids)):
        print(f"{result['document_id']} {result['status']}")


if __name__ == "__main__":
    main()
