"""Embedding generation pipeline — calls external embedding HTTP server.

The embedding model (BGE-M3) runs on the host machine as a separate process,
not inside the Docker container. This avoids OOM issues.

Host server: python -m server.services.embedding_server --port 8002
API container calls: http://host.docker.internal:8002/embed
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, DocumentEmbedding

logger = logging.getLogger("embedding_service")

EMBEDDING_DIM = int(os.environ.get("MEMENTO_EMBEDDING_DIM", "1024"))
# URL of the embedding server (host machine)
EMBEDDING_SERVER_URL = os.environ.get(
    "MEMENTO_EMBEDDING_SERVER_URL",
    "http://host.docker.internal:8002",
)
CHUNK_SIZE = 2000  # chars per chunk
CHUNK_OVERLAP = 200

_server_available: bool | None = None  # None = not checked yet
_last_check_time: float = 0  # Retry every 60s after failure
EMBEDDING_PROCESSING_STALE_AFTER = timedelta(minutes=25)


class EmbeddingServerBusy(RuntimeError):
    """The healthy embedding server is already processing another request."""


def _chunk_text(
    text: str, chunk_chars: int = CHUNK_SIZE, overlap_chars: int = CHUNK_OVERLAP
) -> list[str]:
    """Split text into overlapping chunks with smart boundary detection."""
    if len(text) <= chunk_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_chars
        if end < len(text):
            for sep in ("\n\n", "\n", ". ", "。", "；"):
                break_pos = text.rfind(sep, start + chunk_chars // 2, end)
                if break_pos != -1:
                    end = break_pos + len(sep)
                    break
        chunks.append(text[start:end].strip())
        start = end - overlap_chars
    return [c for c in chunks if len(c) > 50]


async def _call_embedding_server(
    texts: list[str],
    timeout: float = 900.0,
    *,
    raise_on_busy: bool = False,
) -> list[list[float]] | None:
    """Call the external embedding HTTP server.

    timeout: request timeout. Default 900s covers the complete background
    document (up to 50 chunks); real CPU-only BGE-M3 batches can exceed two
    minutes. Interactive callers pass 30s instead. When ``raise_on_busy`` is
    true, an admission 503 is surfaced separately so durable background work
    can remain pending without consuming its finite failure budget.
    """
    global _server_available, _last_check_time
    import time

    if _server_available is False and (time.time() - _last_check_time) < 60:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            # One request reserves server admission for the whole document.
            # The server still encodes in bounded model batches, but another
            # caller cannot interleave and force us to discard partial work.
            resp = await client.post(
                f"{EMBEDDING_SERVER_URL}/embed",
                json={"texts": texts},
            )
            if resp.status_code == 503:
                _server_available = True
                if raise_on_busy:
                    raise EmbeddingServerBusy("embedding server is busy")
                return None
            if resp.status_code != 200:
                logger.warning("Embedding server returned %d", resp.status_code)
                return None
            data = resp.json()
        _server_available = True
        return data.get("embeddings", [])
    except EmbeddingServerBusy:
        raise
    except Exception as e:
        import time

        _last_check_time = time.time()
        if _server_available is not True:
            logger.info(
                "Embedding server not available at %s: %s", EMBEDDING_SERVER_URL, e
            )
        else:
            logger.warning("Embedding call failed: %s", e)
        _server_available = False
        return None


async def generate_document_embeddings(db: AsyncSession, doc: Document) -> int:
    """Generate and store embeddings for a document. Returns count of chunks created.

    Writes ``doc.embedding_status`` via raw UPDATE statements (not ORM attribute
    assignment) so concurrent ingests of the same file don't trigger
    SQLAlchemy's stale-row detection — under load every collector resend used
    to roll back the whole transaction and lose the embeddings.
    """
    revision_hash = doc.content_hash
    claim_token = str(uuid4())

    async def _claim_revision() -> bool:
        """Claim one exact revision, including abandoned processing work."""
        stale_before = datetime.now(timezone.utc) - EMBEDDING_PROCESSING_STALE_AFTER
        result = await db.execute(
            update(Document)
            .where(
                Document.id == doc.id,
                Document.content_hash == revision_hash,
                or_(
                    Document.embedding_status.in_(("pending", "failed")),
                    and_(
                        Document.embedding_status == "processing",
                        or_(
                            Document.embedding_claimed_at.is_(None),
                            Document.embedding_claimed_at < stale_before,
                        ),
                    ),
                ),
            )
            .values(
                embedding_status="processing",
                embedding_claim_token=claim_token,
                embedding_claimed_at=func.now(),
                updated_at=func.now(),
            )
        )
        await db.commit()
        return result.rowcount == 1

    async def _set_status(status: str, *, bump_attempts: bool = False) -> bool:
        """Update embedding_status in its own short transaction.

        Critical: commits IMMEDIATELY so the documents-row write lock is
        released before any long-running await (BGE-M3 call can take 10+s).
        Without this, the doc row stays locked the whole time, heartbeat /
        ingest contention piles up and the connection pool dies.
        """
        values: dict = {
            "embedding_status": status,
            "embedding_claim_token": None,
            "embedding_claimed_at": None,
            "updated_at": func.now(),
        }
        if bump_attempts:
            values["embedding_attempts"] = (
                func.coalesce(Document.embedding_attempts, 0) + 1
            )
        result = await db.execute(
            update(Document)
            .where(
                Document.id == doc.id,
                Document.content_hash == revision_hash,
                Document.embedding_status == "processing",
                Document.embedding_claim_token == claim_token,
            )
            .values(**values)
        )
        await db.commit()
        return result.rowcount == 1

    if not await _claim_revision():
        return 0

    if doc.content_type in ("sqlite", "sqlite_export", "binary"):
        await _set_status("skipped", bump_attempts=True)
        return 0

    embedding_content = doc.content or ""
    if not embedding_content and doc.category == "conversation":
        rows = (
            (
                await db.execute(
                    select(func.left(ConversationMessage.content, 4_000))
                    .where(
                        ConversationMessage.document_id == doc.id,
                        ConversationMessage.role.in_(("user", "assistant")),
                    )
                    .order_by(ConversationMessage.line_number)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        parts: list[str] = []
        used = 0
        for message in rows:
            fragment = (message or "").strip()
            if not fragment:
                continue
            remaining = 100_000 - used
            if remaining <= 0:
                break
            fragment = fragment[:remaining]
            parts.append(fragment)
            used += len(fragment)
        embedding_content = "\n\n".join(parts)

    if len(embedding_content) < 100:
        await _set_status("skipped", bump_attempts=True)
        return 0

    chunks = _chunk_text(embedding_content)
    if not chunks:
        await _set_status("skipped", bump_attempts=True)
        return 0

    # Cap at 50 chunks per document (~100KB) to avoid overloading embedding server
    if len(chunks) > 50:
        chunks = chunks[:50]
    # The normalized-message SELECT above starts a transaction. Release it
    # before a multi-minute model call so Postgres does not terminate the
    # connection under idle_in_transaction_session_timeout.
    await db.commit()
    logger.info("Embedding %d chunks for %s", len(chunks), doc.relative_path)
    try:
        embeddings = await _call_embedding_server(chunks, raise_on_busy=True)
    except EmbeddingServerBusy:
        # Healthy but occupied is admission control, not a failed attempt.
        # Keep the durable document retry-eligible for the next scanner pass.
        await _set_status("pending")
        return 0
    if embeddings is None:
        await _set_status("failed", bump_attempts=True)
        return 0
    if not embeddings:
        await _set_status("failed", bump_attempts=True)
        return 0

    if len(embeddings[0]) != EMBEDDING_DIM:
        logger.warning(
            "Embedding dim mismatch: got %d, expected %d",
            len(embeddings[0]),
            EMBEDDING_DIM,
        )
        await _set_status("failed", bump_attempts=True)
        return 0

    if len(embeddings) != len(chunks):
        logger.warning(
            "Embedding count mismatch: got %d, expected %d",
            len(embeddings),
            len(chunks),
        )
        await _set_status("failed", bump_attempts=True)
        return 0

    # Lock and re-check the exact revision immediately before writing vectors.
    # A concurrent ingest either wins this row lock and changes content_hash
    # first (making this return no row), or waits until our vectors/status are
    # committed and then resets the newer revision to pending.
    current_revision = await db.execute(
        select(Document.id)
        .where(
            Document.id == doc.id,
            Document.content_hash == revision_hash,
            Document.embedding_status == "processing",
            Document.embedding_claim_token == claim_token,
        )
        .with_for_update()
    )
    if current_revision.scalar_one_or_none() is None:
        await db.rollback()
        return 0

    # Upsert each chunk. Two concurrent post-ingest tasks on the same doc
    # used to race here: both ran DELETE(doc_embeddings WHERE doc_id=X)
    # → both saw empty → both INSERT → second one hit the
    # uq_doc_embedding_chunk unique violation and rolled back the whole
    # transaction. ON CONFLICT (document_id, chunk_index) DO UPDATE makes
    # each INSERT idempotent regardless of races, and naturally clobbers
    # stale chunks when content changes.
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = [
        {
            "document_id": doc.id,
            "chunk_index": i,
            "chunk_text": chunk,
            "embedding": embedding,
        }
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
    ]
    stmt = pg_insert(DocumentEmbedding).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["document_id", "chunk_index"],
        set_={
            "chunk_text": stmt.excluded.chunk_text,
            "embedding": stmt.excluded.embedding,
        },
    )
    await db.execute(stmt)

    # If we ended up with more chunks than this call produced (e.g. older
    # version of the same doc had more chunks), trim the tail. Same
    # transaction so concurrent readers never see a half-state.
    await db.execute(
        delete(DocumentEmbedding).where(
            DocumentEmbedding.document_id == doc.id,
            DocumentEmbedding.chunk_index >= len(chunks),
        )
    )

    await db.flush()
    await _set_status("ok", bump_attempts=True)
    logger.info(
        "Generated %d embeddings for %s/%s", len(chunks), doc.tool_id, doc.relative_path
    )
    return len(chunks)
