"""Clean stored transcript data after parser and presentation normalization changes.

This migration is intentionally idempotent: it converts useful Claude Code slash
commands into compact tool rows, removes only their repetitive caveat notices,
and replaces opaque generated document titles with a compact version of the
first meaningful user prompt.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete, func, or_, select, update

from server.db.models import ConversationMessage, Document
from server.db.session import async_session_factory, engine
from server.services.ingest_service import (
    _friendly_conversation_title,
    _has_generated_conversation_title,
)
from server.services.conversation_parser import _extract_local_command


LOCAL_COMMAND_PREFIXES = (
    "<command-name",
    "<command-message",
    "<command-args",
    "<local-command-caveat",
    "<local-command-stdout",
    "<local-command-stderr",
)


async def backfill(dry_run: bool) -> tuple[int, int, int]:
    async with async_session_factory() as db:
        claude_document_ids = select(Document.id).where(
            Document.tool_id == "claude_code"
        )
        local_messages = await db.execute(
            select(ConversationMessage).where(
                ConversationMessage.document_id.in_(claude_document_ids),
                ConversationMessage.role == "user",
                or_(*(
                    func.lower(func.ltrim(ConversationMessage.content)).like(f"{prefix}%")
                    for prefix in LOCAL_COMMAND_PREFIXES
                )),
            )
        )
        converted_commands = 0
        removed_caveats = 0
        for message in local_messages.scalars():
            command = _extract_local_command(message.content or "")
            if command is None:
                await db.execute(
                    delete(ConversationMessage).where(
                        ConversationMessage.id == message.id
                    )
                )
                removed_caveats += 1
                continue

            tool_name, tool_input, output = command
            message.role = "tool"
            message.message_type = "local_command"
            message.content = output or f"[{tool_name}]"
            message.metadata_ = {
                **(message.metadata_ or {}),
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            converted_commands += 1

        documents = await db.execute(
            select(Document.id, Document.title)
            .where(Document.category == "conversation")
            .order_by(Document.created_at.asc())
        )
        renamed_documents = 0
        for document_id, current_title in documents:
            if not _has_generated_conversation_title(current_title):
                continue

            messages = await db.execute(
                select(ConversationMessage.content)
                .where(
                    ConversationMessage.document_id == document_id,
                    ConversationMessage.role == "user",
                )
                .order_by(ConversationMessage.line_number.asc())
                .limit(25)
            )
            friendly_title = next(
                (
                    title
                    for content in messages.scalars()
                    if (title := _friendly_conversation_title(content or ""))
                ),
                None,
            )
            if not friendly_title:
                continue

            await db.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(title=friendly_title)
            )
            renamed_documents += 1

        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    await engine.dispose()
    return converted_commands, removed_caveats, renamed_documents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate changes and roll the transaction back",
    )
    args = parser.parse_args()

    converted, removed, renamed = asyncio.run(backfill(args.dry_run))
    mode = "dry-run" if args.dry_run else "applied"
    print(
        f"{mode}: converted_local_commands={converted} "
        f"removed_local_command_caveats={removed} "
        f"renamed_conversations={renamed}"
    )


if __name__ == "__main__":
    main()
