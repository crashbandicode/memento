"""Celery task: backfill content_tsv on documents created before the FTS
index landed, and refresh any rows where it's NULL.

Idempotent — only touches rows whose content_tsv is NULL. Batches to keep
memory flat and commit frequency high.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select, update

from ..db.models import ConversationMessage, Document
from ..db.session import async_session_factory
from ..services.tokenize import tokenize_for_index
from .celery_app import celery_app

logger = logging.getLogger("tsvector_backfill")

BATCH_SIZE = 100
MAX_SEARCH_TEXT_CHARS = 200 * 1024
MAX_SEARCH_MESSAGE_CHARS = 2_048
MAX_SEARCH_MESSAGES = 200


async def _run() -> dict:
    total = 0
    touched = 0
    async with async_session_factory() as db:
        while True:
            rows = (
                await db.execute(
                    select(
                        Document.id,
                        Document.title,
                        Document.category,
                        func.left(Document.content, MAX_SEARCH_TEXT_CHARS),
                    )
                    .where(Document.content_tsv.is_(None))
                    .limit(BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            for did, title, category, content in rows:
                total += 1
                if category == "conversation":
                    message_rows = (
                        (
                            await db.execute(
                                select(
                                    func.left(
                                        ConversationMessage.content,
                                        MAX_SEARCH_MESSAGE_CHARS,
                                    )
                                )
                                .where(
                                    ConversationMessage.document_id == did,
                                    ConversationMessage.role.in_(("user", "assistant")),
                                )
                                .order_by(ConversationMessage.line_number.desc())
                                .limit(MAX_SEARCH_MESSAGES)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    normalized_content = "\n".join(
                        row for row in reversed(message_rows) if row
                    )[:MAX_SEARCH_TEXT_CHARS]
                    if normalized_content:
                        content = normalized_content
                tsv_input = tokenize_for_index(f"{title or ''} {content or ''}")
                await db.execute(
                    update(Document)
                    .where(Document.id == did)
                    .values(content_tsv=func.to_tsvector("simple", tsv_input))
                )
                touched += 1
            await db.commit()
            logger.info("tsvector backfill: %d done", total)

    return {"scanned": total, "updated": touched}


@celery_app.task(
    name="server.tasks.tsvector_backfill.backfill_content_tsv",
    acks_late=True,
)
def backfill_content_tsv() -> dict:
    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.exception("backfill errored")
        return {"scanned": 0, "updated": 0, "error": str(e)[:200]}
