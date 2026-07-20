"""Backfill source-verifiable interactive question metadata.

Older normalized Cursor state rows retained the complete question input and
answer output but predated Memento's shared interaction schema.  This repair
normalizes only those persisted values and overlays missing ``interaction`` /
``interaction_response`` metadata.  Existing semantic metadata and message
text/order are never changed.

Dry-run is the default::

    python -m server.scripts.backfill_question_interactions
    python -m server.scripts.backfill_question_interactions --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

import asyncpg

from server.scripts.reparse_conversations import _database_dsn
from server.services.conversation_parser import (
    build_question_response,
    normalize_question_interaction,
)


@dataclass(frozen=True)
class QuestionRow:
    id: int
    document_id: uuid.UUID
    line_number: int
    tool_id: str
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class QuestionUpdate:
    id: int
    document_id: uuid.UUID
    line_number: int
    tool_id: str
    tool_input: str
    content: str
    metadata_patch: dict[str, Any]


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value or {})


def _response_evidence(content: str) -> bool:
    if "cancel" in content.casefold():
        return True
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and "answers" in parsed


def plan_question_overlays(
    rows: Iterable[QuestionRow],
) -> list[QuestionUpdate]:
    """Return conservative metadata-only question/answer updates."""
    updates: list[QuestionUpdate] = []
    for row in rows:
        metadata = row.metadata
        tool_name = str(metadata.get("tool_name") or "")
        tool_input = str(metadata.get("tool_input") or "")
        interaction = metadata.get("interaction")
        if not isinstance(interaction, dict):
            interaction = normalize_question_interaction(
                tool_name,
                tool_input,
                source=row.tool_id,
                interaction_id=(
                    metadata.get("source_id")
                    or f"{row.tool_id}:{row.document_id}:{row.line_number}"
                ),
            )
        if not isinstance(interaction, dict):
            continue

        patch: dict[str, Any] = {}
        if not isinstance(metadata.get("interaction"), dict):
            patch["interaction"] = interaction
        if (
            not isinstance(metadata.get("interaction_response"), dict)
            and _response_evidence(row.content)
        ):
            patch["interaction_response"] = build_question_response(
                interaction,
                row.content,
            )
        if patch:
            updates.append(QuestionUpdate(
                id=row.id,
                document_id=row.document_id,
                line_number=row.line_number,
                tool_id=row.tool_id,
                tool_input=tool_input,
                content=row.content,
                metadata_patch=patch,
            ))
    return updates


async def _candidate_rows(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
    tool_ids: tuple[str, ...],
) -> list[QuestionRow]:
    rows = await conn.fetch(
        """
        SELECT cm.id, cm.document_id, cm.line_number, cm.content,
               cm.metadata, d.tool_id
        FROM conversation_messages cm
        JOIN documents d ON d.id=cm.document_id
        WHERE d.category='conversation'
          AND d.tool_id=ANY($2::text[])
          AND cm.role='tool'
          AND lower(regexp_replace(
                COALESCE(cm.metadata->>'tool_name', ''),
                '[^a-zA-Z0-9]', '', 'g'
              )) IN ('askquestion', 'askuserquestion', 'requestuserinput')
          AND (
            NOT (cm.metadata ? 'interaction')
            OR NOT (cm.metadata ? 'interaction_response')
          )
          AND ($1::uuid[] IS NULL OR cm.document_id=ANY($1::uuid[]))
        ORDER BY cm.document_id, cm.line_number
        """,
        document_ids,
        list(tool_ids),
    )
    return [QuestionRow(
        id=int(row["id"]),
        document_id=row["document_id"],
        line_number=int(row["line_number"]),
        tool_id=str(row["tool_id"]),
        content=str(row["content"] or ""),
        metadata=_metadata_dict(row["metadata"]),
    ) for row in rows]


async def _apply_updates(
    conn: asyncpg.Connection,
    updates: list[QuestionUpdate],
) -> int:
    applied = 0
    async with conn.transaction():
        for update in updates:
            result = await conn.execute(
                """
                UPDATE conversation_messages
                SET metadata=metadata || $2::jsonb
                WHERE id=$1
                  AND document_id=$3
                  AND line_number=$4
                  AND content=$5
                  AND COALESCE(metadata->>'tool_input', '')=$6
                  AND (
                    ($2::jsonb ? 'interaction' AND NOT metadata ? 'interaction')
                    OR (
                      $2::jsonb ? 'interaction_response'
                      AND NOT metadata ? 'interaction_response'
                    )
                  )
                """,
                update.id,
                json.dumps(update.metadata_patch),
                update.document_id,
                update.line_number,
                update.content,
                update.tool_input,
            )
            applied += int(result.rsplit(" ", 1)[-1])
    return applied


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
    tool_ids: tuple[str, ...] = ("codex", "claude_code", "cursor"),
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        rows = await _candidate_rows(conn, document_ids, tool_ids)
        updates = plan_question_overlays(rows)
        planned_by_tool = Counter(update.tool_id for update in updates)
        applied = await _apply_updates(conn, updates) if apply and updates else 0
        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_rows": len(rows),
            "documents": len({row.document_id for row in rows}),
            "planned": len(updates),
            "planned_by_tool": dict(sorted(planned_by_tool.items())),
            "applied": applied,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit conservative interaction metadata overlays",
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
        help="limit to one tool (repeatable); defaults to all supported tools",
    )
    args = parser.parse_args()
    selected_tools = tuple(args.tools or ("codex", "claude_code", "cursor"))
    if "all" in selected_tools:
        selected_tools = ("codex", "claude_code", "cursor")
    print(json.dumps(asyncio.run(run(
        apply=args.apply,
        document_ids=args.document_ids,
        tool_ids=selected_tools,
    )), indent=2))


if __name__ == "__main__":
    main()
