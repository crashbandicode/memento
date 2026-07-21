"""Backfill shared agent lifecycle events for Cursor Task spawns.

Historical Cursor parents store ``task_v2`` / ``Task`` rows without the shared
``agent_event`` payload that badge merge expects. Path-linked children also
often lack ``agent_depth`` / ``root_session_id``. This repair overlays those
fields from already-stored tool input/content and transcript paths.

Dry-run is the default::

    python -m server.scripts.backfill_cursor_subagent_events
    python -m server.scripts.backfill_cursor_subagent_events --apply
    python -m server.scripts.backfill_cursor_subagent_events --apply --document-id UUID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from server.scripts.reparse_conversations import _database_dsn
from server.services.conversation_hierarchy import path_linked_subagent_identity
from server.services.conversation_parser import normalize_task_spawn_agent_event


@dataclass(frozen=True)
class MessageUpdate:
    id: uuid.UUID
    document_id: uuid.UUID
    line_number: int
    content: str
    metadata: dict[str, Any]
    original_content: str
    original_message_type: str


@dataclass(frozen=True)
class DocumentUpdate:
    id: uuid.UUID
    metadata: dict[str, Any]


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value or {})


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError):
        return getattr(row, key, None)


def plan_message_updates(rows: list[Any]) -> list[MessageUpdate]:
    updates: list[MessageUpdate] = []
    for row in rows:
        metadata = _metadata_dict(_row_get(row, "metadata"))
        if metadata.get("agent_event"):
            continue
        tool_name = str(metadata.get("tool_name") or "")
        tool_input = metadata.get("tool_input") or ""
        content = str(_row_get(row, "content") or "")
        agent_event = normalize_task_spawn_agent_event(
            tool_name,
            tool_input,
            content,
            tool_status=metadata.get("tool_status"),
        )
        if agent_event is None:
            continue
        next_metadata = dict(metadata)
        next_metadata["agent_event"] = agent_event
        next_metadata["tool_name"] = "Agent activity"
        updates.append(MessageUpdate(
            id=_row_get(row, "id"),
            document_id=_row_get(row, "document_id"),
            line_number=int(_row_get(row, "line_number")),
            content=f"{agent_event['label']} {agent_event['kind']}",
            metadata=next_metadata,
            original_content=content,
            original_message_type=str(_row_get(row, "message_type") or ""),
        ))
    return updates


def plan_document_updates(
    rows: list[Any],
    *,
    agent_paths_by_session: dict[str, str],
) -> list[DocumentUpdate]:
    updates: list[DocumentUpdate] = []
    for row in rows:
        metadata = _metadata_dict(_row_get(row, "metadata"))
        relative_path = str(_row_get(row, "relative_path") or "")
        identity = path_linked_subagent_identity(relative_path)
        changed = False
        next_metadata = dict(metadata)
        for key, value in identity.items():
            if next_metadata.get(key) != value:
                next_metadata[key] = value
                changed = True
        session_id = str(
            next_metadata.get("session_id")
            or next_metadata.get("thread_id")
            or ""
        ).strip()
        agent_path = agent_paths_by_session.get(session_id)
        if agent_path and not next_metadata.get("agent_path"):
            next_metadata["agent_path"] = agent_path
            changed = True
        if changed:
            updates.append(DocumentUpdate(
                id=_row_get(row, "id"),
                metadata=next_metadata,
            ))
    return updates


async def _candidate_task_rows(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
) -> list[asyncpg.Record]:
    clauses = [
        "d.tool_id = 'cursor'",
        "d.category = 'conversation'",
        "m.role = 'tool'",
        "(m.metadata ? 'tool_name')",
        """
        lower(regexp_replace(coalesce(m.metadata->>'tool_name', ''), '[^a-z0-9]', '', 'g'))
        IN ('task', 'taskv2')
        """,
        "NOT (m.metadata ? 'agent_event')",
    ]
    args: list[Any] = []
    if document_ids:
        args.append(document_ids)
        clauses.append(f"m.document_id = ANY(${len(args)}::uuid[])")
    where_sql = " AND ".join(clauses)
    return await conn.fetch(
        f"""
        SELECT m.id, m.document_id, m.line_number, m.message_type, m.content, m.metadata
        FROM conversation_messages m
        JOIN documents d ON d.id = m.document_id
        WHERE {where_sql}
        ORDER BY m.document_id, m.line_number
        """,
        *args,
    )


async def _candidate_child_rows(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
) -> list[asyncpg.Record]:
    if not document_ids:
        return await conn.fetch(
            """
            SELECT id, relative_path, metadata
            FROM documents
            WHERE tool_id IN ('cursor', 'claude_code')
              AND category = 'conversation'
              AND relative_path LIKE '%/subagents/%'
            ORDER BY id
            """
        )
    return await conn.fetch(
        """
        WITH roots AS (
            SELECT id,
                   coalesce(metadata->>'session_id', metadata->>'thread_id') AS session_id,
                   relative_path
            FROM documents
            WHERE id = ANY($1::uuid[])
        )
        SELECT d.id, d.relative_path, d.metadata
        FROM documents d
        WHERE d.tool_id IN ('cursor', 'claude_code')
          AND d.category = 'conversation'
          AND d.relative_path LIKE '%/subagents/%'
          AND (
            d.id = ANY($1::uuid[])
            OR EXISTS (
                SELECT 1
                FROM roots r
                WHERE r.session_id IS NOT NULL
                  AND r.session_id <> ''
                  AND d.relative_path LIKE '%/' || r.session_id || '/subagents/%'
            )
          )
        ORDER BY d.id
        """,
        document_ids,
    )


async def _agent_paths_by_session(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
) -> dict[str, str]:
    clauses = [
        "d.tool_id = 'cursor'",
        "m.metadata ? 'agent_event'",
    ]
    args: list[Any] = []
    if document_ids:
        args.append(document_ids)
        clauses.append(f"m.document_id = ANY(${len(args)}::uuid[])")
    where_sql = " AND ".join(clauses)
    rows = await conn.fetch(
        f"""
        SELECT m.metadata->'agent_event'->>'agent_thread_id' AS thread_id,
               m.metadata->'agent_event'->>'agent_path' AS agent_path
        FROM conversation_messages m
        JOIN documents d ON d.id = m.document_id
        WHERE {where_sql}
        """,
        *args,
    )
    mapping: dict[str, str] = {}
    for row in rows:
        thread_id = str(row["thread_id"] or "").strip()
        agent_path = str(row["agent_path"] or "").strip()
        if thread_id and agent_path:
            mapping[thread_id] = agent_path
    return mapping


async def _apply_message_updates(
    conn: asyncpg.Connection,
    updates: list[MessageUpdate],
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
                  AND message_type=$6
                  AND content=$7
                  AND NOT (metadata ? 'agent_event')
                """,
                update.id,
                update.content,
                json.dumps(update.metadata),
                update.document_id,
                update.line_number,
                update.original_message_type,
                update.original_content,
            )
            applied += int(result.rsplit(" ", 1)[-1])
    return applied


async def _apply_document_updates(
    conn: asyncpg.Connection,
    updates: list[DocumentUpdate],
) -> int:
    applied = 0
    async with conn.transaction():
        for update in updates:
            result = await conn.execute(
                """
                UPDATE documents
                SET metadata = $2::jsonb
                WHERE id = $1
                """,
                update.id,
                json.dumps(update.metadata),
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
        task_rows = await _candidate_task_rows(conn, document_ids)
        message_updates = plan_message_updates(task_rows)
        applied_messages = 0
        if apply and message_updates:
            applied_messages = await _apply_message_updates(conn, message_updates)

        agent_paths = await _agent_paths_by_session(conn, document_ids)
        # Include freshly planned events in dry-run path mapping.
        for update in message_updates:
            event = update.metadata.get("agent_event") or {}
            thread_id = str(event.get("agent_thread_id") or "").strip()
            agent_path = str(event.get("agent_path") or "").strip()
            if thread_id and agent_path:
                agent_paths[thread_id] = agent_path

        child_rows = await _candidate_child_rows(conn, document_ids)
        document_updates = plan_document_updates(
            child_rows,
            agent_paths_by_session=agent_paths,
        )
        applied_documents = 0
        if apply and document_updates:
            applied_documents = await _apply_document_updates(conn, document_updates)

        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_task_rows": len(task_rows),
            "planned_message_updates": len(message_updates),
            "applied_message_updates": applied_messages,
            "candidate_child_documents": len(child_rows),
            "planned_document_updates": len(document_updates),
            "applied_document_updates": applied_documents,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit overlays")
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
