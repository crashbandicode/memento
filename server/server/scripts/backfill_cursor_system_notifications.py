"""Reclassify Cursor ``<system_notification>`` rows as session context.

Cursor injects shell/await completion notices as user bubbles. Those must not
render as human "You" turns. Forward parsing treats leading
``system_notification`` envelopes as session context; this repair rewrites
already-stored rows that are notification-only.

Dry-run is the default::

    python -m server.scripts.backfill_cursor_system_notifications
    python -m server.scripts.backfill_cursor_system_notifications --apply
    python -m server.scripts.backfill_cursor_system_notifications --apply --document-id UUID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

import asyncpg

from server.scripts.reparse_conversations import _database_dsn
from server.services.conversation_parser import parse_cursor_user_payload


def plan_notification_context_update(
    *,
    content: str,
    role: str,
    message_type: str,
    metadata: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any]] | None:
    """Return rewritten role/type/content/metadata, or None when unchanged."""
    if role != "user":
        return None
    text = content or ""
    if "<system_notification" not in text.casefold():
        return None
    payload = parse_cursor_user_payload(text)
    if payload.content.strip():
        return None
    context = (payload.session_context or "").strip()
    if not context:
        return None
    next_metadata = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key != "session_context"
    }
    if (
        message_type == "cursor_context"
        and content == context
        and role == "system"
    ):
        return None
    return "system", "cursor_context", context, next_metadata


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        clauses = [
            "d.tool_id = 'cursor'",
            "d.category = 'conversation'",
            "m.role = 'user'",
            "m.content ILIKE '%<system_notification>%'",
        ]
        args: list[Any] = []
        if document_ids:
            args.append(document_ids)
            clauses.append(f"m.document_id = ANY(${len(args)}::uuid[])")
        where_sql = " AND ".join(clauses)
        rows = await conn.fetch(
            f"""
            SELECT m.id, m.document_id, m.line_number, m.role, m.message_type,
                   m.content, m.metadata
            FROM conversation_messages m
            JOIN documents d ON d.id = m.document_id
            WHERE {where_sql}
            ORDER BY m.document_id, m.line_number
            """,
            *args,
        )
        planned = 0
        applied = 0
        async with conn.transaction():
            for row in rows:
                metadata = row["metadata"]
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                metadata = dict(metadata or {})
                update = plan_notification_context_update(
                    content=str(row["content"] or ""),
                    role=str(row["role"] or ""),
                    message_type=str(row["message_type"] or ""),
                    metadata=metadata,
                )
                if update is None:
                    continue
                planned += 1
                if not apply:
                    continue
                role, message_type, content, next_metadata = update
                result = await conn.execute(
                    """
                    UPDATE conversation_messages
                    SET role=$2, message_type=$3, content=$4, metadata=$5::jsonb
                    WHERE id=$1
                      AND role='user'
                      AND content=$6
                    """,
                    row["id"],
                    role,
                    message_type,
                    content,
                    json.dumps(next_metadata),
                    row["content"],
                )
                applied += int(result.rsplit(" ", 1)[-1])
        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_rows": len(rows),
            "planned": planned,
            "applied": applied,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--document-id",
        action="append",
        type=uuid.UUID,
        dest="document_ids",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(
        apply=args.apply,
        document_ids=args.document_ids,
    )), indent=2))


if __name__ == "__main__":
    main()
