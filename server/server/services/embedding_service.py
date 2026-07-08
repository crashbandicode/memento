"""Embedding generation pipeline — calls external embedding HTTP server.

The embedding model (BGE-M3) runs on the host machine as a separate process,
not inside the Docker container. This avoids OOM issues.

Host server: python -m server.services.embedding_server --port 8002
API container calls: http://host.docker.internal:8002/embed
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
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
try:
    _configured_request_timeout = float(
        os.environ.get("MEMENTO_EMBEDDING_REQUEST_TIMEOUT_SECONDS", "1200")
    )
except (TypeError, ValueError):
    _configured_request_timeout = 1200.0
if not math.isfinite(_configured_request_timeout):
    _configured_request_timeout = 1200.0
EMBEDDING_REQUEST_TIMEOUT_SECONDS = min(
    1200.0,
    max(60.0, _configured_request_timeout),
)
# Keep abandoned-claim recovery comfortably beyond the capped 3-CPU request
# deadline and Celery's task margin. A legitimate 50-chunk BGE-M3 request must
# never be reclaimed while it is still making progress.
EMBEDDING_PROCESSING_STALE_AFTER = timedelta(minutes=35)
CONVERSATION_EMBEDDING_MESSAGE_LIMIT = 100
CONVERSATION_EMBEDDING_MESSAGE_CHARS = 4_000
CONVERSATION_EMBEDDING_TOTAL_CHARS = 100_000


class EmbeddingServerBusy(RuntimeError):
    """The healthy embedding server is already processing another request."""


def conversation_embedding_content(
    message_contents: list[str | None],
) -> str:
    """Build the exact normalized-message fallback used for conversations.

    Callers must supply user/assistant messages in transcript order. Keeping
    this transformation shared lets repair jobs determine whether changing
    stored presentation rows actually changes the model input.
    """
    parts: list[str] = []
    used = 0
    for message in message_contents[:CONVERSATION_EMBEDDING_MESSAGE_LIMIT]:
        fragment = (message or "")[:CONVERSATION_EMBEDDING_MESSAGE_CHARS].strip()
        if not fragment:
            continue
        remaining = CONVERSATION_EMBEDDING_TOTAL_CHARS - used
        if remaining <= 0:
            break
        fragment = fragment[:remaining]
        parts.append(fragment)
        used += len(fragment)
    return "\n\n".join(parts)


def _chunk_text(
    text: str,
    chunk_chars: int = CHUNK_SIZE,
    overlap_chars: int = CHUNK_OVERLAP,
    *,
    max_chunks: int | None = None,
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
        chunk = text[start:end].strip()
        if len(chunk) > 50:
            chunks.append(chunk)
        if max_chunks is not None and len(chunks) >= max_chunks:
            break
        start = end - overlap_chars
    return chunks


def embedding_input_hash(chunks: list[str]) -> str:
    """Hash the exact ordered JSON array submitted to the model.

    Hashing the final bounded chunks (rather than the raw file) makes the
    identity match the real model input, including chunk boundaries. JSON is
    used so different chunk partitions cannot collide through concatenation.
    """
    payload = json.dumps(
        chunks,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def document_embedding_input(
    db: AsyncSession,
    doc: Document,
) -> tuple[list[str], str]:
    """Return the exact bounded model input and its stable identity hash."""
    if doc.content_type in ("sqlite", "sqlite_export", "binary"):
        chunks: list[str] = []
        return chunks, embedding_input_hash(chunks)

    embedding_content = doc.content or ""
    if not embedding_content and doc.category == "conversation":
        rows = (
            (
                await db.execute(
                    select(
                        func.left(
                            ConversationMessage.content,
                            CONVERSATION_EMBEDDING_MESSAGE_CHARS,
                        )
                    )
                    .where(
                        ConversationMessage.document_id == doc.id,
                        ConversationMessage.role.in_(("user", "assistant")),
                    )
                    .order_by(
                        ConversationMessage.line_number,
                        ConversationMessage.id,
                    )
                    .limit(CONVERSATION_EMBEDDING_MESSAGE_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        embedding_content = conversation_embedding_content(list(rows))

    chunks = []
    if len(embedding_content) >= 100:
        chunks = _chunk_text(embedding_content, max_chunks=50)
    return chunks, embedding_input_hash(chunks)


async def _call_embedding_server(
    texts: list[str],
    timeout: float = EMBEDDING_REQUEST_TIMEOUT_SECONDS,
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
    chunks, input_hash = await document_embedding_input(db, doc)

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
                embedding_content_hash=input_hash,
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
                Document.embedding_content_hash == input_hash,
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

    if not chunks:
        await _set_status("skipped", bump_attempts=True)
        return 0
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

    # Lock and re-check the exact model input immediately before writing
    # vectors. A concurrent append outside the bounded input may change the raw
    # file hash but can safely let this worker finish. A change within the
    # input updates embedding_content_hash and makes this return no row.
    current_revision = await db.execute(
        select(Document.id)
        .where(
            Document.id == doc.id,
            Document.embedding_content_hash == input_hash,
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
