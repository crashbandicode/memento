"""Repair recovered Codex history prompts in normalized conversations."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import distinct, select

from server.db.models import ConversationMessage
from server.db.session import async_session_factory
from server.services.ingest_service import _reconcile_recovered_history_rows


async def repair(
    document_id: uuid.UUID | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """Remove transport duplicates and chronologically place true gaps."""
    async with async_session_factory() as db:
        query = select(distinct(ConversationMessage.document_id)).where(
            ConversationMessage.message_type == "history_user_message"
        )
        if document_id is not None:
            query = query.where(ConversationMessage.document_id == document_id)
        document_ids = list((await db.execute(query)).scalars().all())

    details: list[dict[str, object]] = []
    removed = 0
    placed = 0
    for current_document_id in document_ids:
        async with async_session_factory() as db:
            transaction = await db.begin()
            current_removed, current_placed = (
                await _reconcile_recovered_history_rows(
                    db,
                    current_document_id,
                )
            )
            if dry_run:
                await transaction.rollback()
            else:
                await transaction.commit()
        removed += current_removed
        placed += current_placed
        details.append(
            {
                "document_id": str(current_document_id),
                "duplicates_removed": current_removed,
                "missing_prompts_placed": current_placed,
            }
        )
    return {
        "documents": len(document_ids),
        "dry_run": dry_run,
        "duplicates_removed": removed,
        "missing_prompts_placed": placed,
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", type=uuid.UUID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(repair(args.document_id, dry_run=args.dry_run)),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
