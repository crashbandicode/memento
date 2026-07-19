"""Convert persisted Codex ``list_agents`` results into semantic snapshots.

This is a bounded metadata-only repair: it scans only Codex tool-result rows
whose stored JSON mentions an ``agents`` array.  It never reparses transcript
blobs or retains agent completion payloads.  Dry-run is the default::

    python -m server.scripts.backfill_codex_agent_snapshots
    python -m server.scripts.backfill_codex_agent_snapshots --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import asyncpg

from server.scripts.reparse_conversations import _database_dsn
from server.services.conversation_parser import (
    codex_agent_snapshot_summary,
    normalize_codex_agent_snapshot,
)


@dataclass(frozen=True)
class CodexAgentResultRow:
    id: int
    document_id: uuid.UUID
    line_number: int
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CodexAgentSnapshotUpdate:
    id: int
    document_id: uuid.UUID
    line_number: int
    original_content: str
    content: str
    metadata: dict[str, Any]


def plan_agent_snapshot_updates(
    rows: Iterable[CodexAgentResultRow],
) -> list[CodexAgentSnapshotUpdate]:
    updates: list[CodexAgentSnapshotUpdate] = []
    for row in rows:
        snapshot = normalize_codex_agent_snapshot(row.content)
        if snapshot is None:
            continue
        metadata = {
            **row.metadata,
            "tool_name": "Subagent status",
            "agent_event": snapshot,
        }
        updates.append(CodexAgentSnapshotUpdate(
            id=row.id,
            document_id=row.document_id,
            line_number=row.line_number,
            original_content=row.content,
            content=codex_agent_snapshot_summary(snapshot),
            metadata=metadata,
        ))
    return updates


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value or {})


async def _candidate_rows(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
) -> list[CodexAgentResultRow]:
    rows = await conn.fetch(
        """
        SELECT cm.id, cm.document_id, cm.line_number, cm.content, cm.metadata
        FROM conversation_messages cm
        JOIN documents d ON d.id=cm.document_id
        WHERE d.category='conversation'
          AND d.tool_id='codex'
          AND cm.role='tool'
          AND cm.message_type='tool_output'
          AND cm.content ILIKE '%"agents"%'
          AND ($1::uuid[] IS NULL OR cm.document_id=ANY($1::uuid[]))
        ORDER BY cm.document_id, cm.line_number
        """,
        document_ids,
    )
    return [CodexAgentResultRow(
        id=int(row["id"]),
        document_id=row["document_id"],
        line_number=int(row["line_number"]),
        content=str(row["content"] or ""),
        metadata=_metadata_dict(row["metadata"]),
    ) for row in rows]


async def _apply_updates(
    conn: asyncpg.Connection,
    updates: list[CodexAgentSnapshotUpdate],
) -> int:
    applied = 0
    async with conn.transaction():
        for update in updates:
            result = await conn.execute(
                """
                UPDATE conversation_messages
                SET message_type='agent_event', content=$2, metadata=$3::jsonb
                WHERE id=$1
                  AND document_id=$4
                  AND line_number=$5
                  AND message_type='tool_output'
                  AND content=$6
                """,
                update.id,
                update.content,
                json.dumps(update.metadata),
                update.document_id,
                update.line_number,
                update.original_content,
            )
            applied += int(result.rsplit(" ", 1)[-1])
    return applied


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        rows = await _candidate_rows(conn, document_ids)
        updates = plan_agent_snapshot_updates(rows)
        applied = await _apply_updates(conn, updates) if apply and updates else 0
        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_rows": len(rows),
            "documents": len({row.document_id for row in rows}),
            "planned": len(updates),
            "applied": applied,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit semantic snapshot overlays")
    parser.add_argument(
        "--document-id",
        action="append",
        type=uuid.UUID,
        dest="document_ids",
        help="limit repair to one document (may be repeated)",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(
        apply=args.apply,
        document_ids=args.document_ids,
    )), indent=2))


if __name__ == "__main__":
    main()
