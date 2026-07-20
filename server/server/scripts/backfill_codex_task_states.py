"""Overlay persisted task plans onto existing normalized messages.

Some preserved legacy transcripts cannot be replaced by a full reparse because
their verified raw blob is older than their normalized history. Codex stores
plans inside normalized ``exec`` rows, while Claude Code and Cursor retain
their native task tool calls. This repair feeds those already-persisted events
through the shared task tracker and projects only missing
``metadata.task_state`` values. It never inserts, deletes, renumbers, changes
message text, or overwrites an existing snapshot.

Dry-run is the default::

    python -m server.scripts.backfill_codex_task_states
    python -m server.scripts.backfill_codex_task_states --apply
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
from server.services.conversation_parser import NormalizedMessage, TaskStateTracker


@dataclass(frozen=True)
class CodexTaskRow:
    id: int
    document_id: uuid.UUID
    line_number: int
    metadata: dict[str, Any]
    tool_id: str = "codex"
    message_type: str | None = None
    content: str = ""


@dataclass(frozen=True)
class CodexTaskUpdate:
    id: int
    document_id: uuid.UUID
    line_number: int
    tool_input: str
    task_state: dict[str, Any]


def plan_task_state_overlays(
    rows: Iterable[CodexTaskRow],
) -> list[CodexTaskUpdate]:
    """Project nested plan replacements while preserving document order."""
    trackers: dict[tuple[uuid.UUID, str], TaskStateTracker] = {}
    updates: list[CodexTaskUpdate] = []
    for row in sorted(rows, key=lambda item: (item.document_id, item.line_number)):
        metadata = row.metadata
        tool_name = str(metadata.get("tool_name") or "")
        tool_input = str(metadata.get("tool_input") or "")
        message = NormalizedMessage(
            role="tool",
            content=row.content,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_call_id=str(
                metadata.get("tool_call_id")
                or metadata.get("source_id")
                or ""
            ),
            raw_type=row.message_type or "",
        )
        tracker = trackers.setdefault(
            (row.document_id, row.tool_id),
            TaskStateTracker(row.tool_id),
        )
        tracker.apply(message)
        if message.task_state is None:
            continue
        if metadata.get("task_state") is not None:
            continue
        updates.append(CodexTaskUpdate(
            id=row.id,
            document_id=row.document_id,
            line_number=row.line_number,
            tool_input=tool_input,
            task_state=message.task_state,
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
    tool_ids: tuple[str, ...],
) -> list[CodexTaskRow]:
    rows = await conn.fetch(
        """
        WITH task_documents AS (
            SELECT DISTINCT cm.document_id
            FROM conversation_messages cm
            JOIN documents d ON d.id=cm.document_id
            WHERE d.category='conversation'
              AND d.tool_id=ANY($2::text[])
              AND cm.role='tool'
              AND (
                lower(regexp_replace(
                  COALESCE(cm.metadata->>'tool_name', ''),
                  '[^a-zA-Z0-9]', '', 'g'
                )) ~ '(updateplan|todowrite|tasklist|taskcreate|taskupdate|taskstop|taskprogress)$'
                OR (
                  lower(regexp_replace(
                    COALESCE(cm.metadata->>'tool_name', ''),
                    '[^a-zA-Z0-9]', '', 'g'
                  )) LIKE '%exec'
                  AND COALESCE(cm.metadata->>'tool_input', '') ILIKE '%update_plan%'
                )
              )
              AND ($1::uuid[] IS NULL OR cm.document_id=ANY($1::uuid[]))
        )
        SELECT cm.id, cm.document_id, cm.line_number, cm.message_type,
               cm.content, cm.metadata, d.tool_id
        FROM conversation_messages cm
        JOIN documents d ON d.id=cm.document_id
        JOIN task_documents td ON td.document_id=cm.document_id
        WHERE d.category='conversation'
          AND d.tool_id=ANY($2::text[])
          AND cm.role='tool'
          AND (
            lower(regexp_replace(
              COALESCE(cm.metadata->>'tool_name', ''),
              '[^a-zA-Z0-9]', '', 'g'
            )) ~ '(updateplan|todowrite|tasklist|taskcreate|taskupdate|taskstop|taskprogress)$'
            OR (
              lower(regexp_replace(
                COALESCE(cm.metadata->>'tool_name', ''),
                '[^a-zA-Z0-9]', '', 'g'
              )) LIKE '%exec'
              AND COALESCE(cm.metadata->>'tool_input', '') ILIKE '%update_plan%'
            )
            OR (
              d.tool_id='claude_code'
              AND cm.message_type IN ('tool_result', 'tool_output')
            )
          )
          AND ($1::uuid[] IS NULL OR cm.document_id=ANY($1::uuid[]))
        ORDER BY cm.document_id, cm.line_number
        """,
        document_ids,
        list(tool_ids),
    )
    return [CodexTaskRow(
        id=int(row["id"]),
        document_id=row["document_id"],
        line_number=int(row["line_number"]),
        metadata=_metadata_dict(row["metadata"]),
        tool_id=str(row["tool_id"]),
        message_type=row["message_type"],
        content=str(row["content"] or ""),
    ) for row in rows]


async def _apply_updates(
    conn: asyncpg.Connection,
    updates: list[CodexTaskUpdate],
) -> int:
    applied = 0
    async with conn.transaction():
        for update in updates:
            result = await conn.execute(
                """
                UPDATE conversation_messages
                SET metadata=jsonb_set(
                    metadata, '{task_state}', $2::jsonb, true
                )
                WHERE id=$1
                  AND document_id=$3
                  AND line_number=$4
                  AND COALESCE(metadata->>'tool_input', '')=$5
                  AND NOT (metadata ? 'task_state')
                """,
                update.id,
                json.dumps(update.task_state),
                update.document_id,
                update.line_number,
                update.tool_input,
            )
            applied += int(result.rsplit(" ", 1)[-1])
    return applied


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
    tool_ids: tuple[str, ...] = ("codex",),
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        rows = await _candidate_rows(conn, document_ids, tool_ids)
        updates = plan_task_state_overlays(rows)
        documents = {row.document_id for row in rows}
        applied = await _apply_updates(conn, updates) if apply and updates else 0
        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_rows": len(rows),
            "documents": len(documents),
            "tools": sorted({row.tool_id for row in rows}),
            "planned": len(updates),
            "applied": applied,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit metadata-only task-state overlays",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        type=uuid.UUID,
        dest="document_ids",
        help="limit repair to one document (may be repeated)",
    )
    parser.add_argument(
        "--tool",
        action="append",
        choices=("all", "codex", "claude_code", "cursor"),
        dest="tools",
        help="repair one tool (repeatable); defaults to Codex for compatibility",
    )
    args = parser.parse_args()
    selected_tools = tuple(args.tools or ("codex",))
    if "all" in selected_tools:
        selected_tools = ("codex", "claude_code", "cursor")
    print(json.dumps(asyncio.run(run(
        apply=args.apply,
        document_ids=args.document_ids,
        tool_ids=selected_tools,
    )), indent=2))


if __name__ == "__main__":
    main()
