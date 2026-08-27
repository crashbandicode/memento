"""Long-lived Phase 4 projector for deferred Canvas and search ingest work.

Candidate identity lives in PostgreSQL and is written inside the same ingest
commit as the messages.  This process polls that outbox (and accepts an
optional NOTIFY wake), so a crash between commit and projection cannot lose
work: restart recovers from the durable rows alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import settings

logger = logging.getLogger("realtime_ingest_projector")

KIND_CANVAS = "canvas"
KIND_SEARCH = "search"
NOTIFY_CHANNEL = "memento_ingest_projections"
MAX_GROUPS_PER_CYCLE = 64
PROJECTOR_LOCK_KEY = b"memento:ingest-projector:v1"


def deferred_projections_enabled() -> bool:
    """Return whether ingest should skip sync Canvas/search and enqueue instead."""
    return bool(settings.realtime_ingest_deferred_projections)


def projector_lock_id() -> int:
    """Return a stable signed 64-bit advisory-lock key for this process."""
    digest = hashlib.sha256(PROJECTOR_LOCK_KEY).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def message_is_canvas_projection_candidate(
    role: str | None,
    metadata: dict[str, Any] | None,
    content: str | None,
) -> bool:
    """Return whether a staged/updated row can change Canvas references."""
    from .canvas_artifacts import canvas_message_can_have_reference

    return canvas_message_can_have_reference(role, metadata) and (
        ".canvas.tsx" in str(content or "").casefold()
    )


def _dsn(database_url: str | None = None) -> str:
    url = database_url or settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def enqueue_projection_candidates(
    db: AsyncSession,
    *,
    document_id: uuid.UUID,
    revision_hash: str,
    canvas: bool,
    search: bool,
) -> tuple[str, ...]:
    """Persist real projector work inside the caller's ingest transaction."""
    from ..db.models import IngestProjectionCandidate

    kinds = tuple(
        kind
        for kind, requested in (
            (KIND_CANVAS, canvas),
            (KIND_SEARCH, search),
        )
        if requested
    )
    if not kinds or not revision_hash:
        return ()
    for kind in kinds:
        statement = pg_insert(IngestProjectionCandidate).values(
            document_id=document_id,
            revision_hash=revision_hash,
            kind=kind,
        )
        await db.execute(
            statement.on_conflict_do_nothing(
                constraint="uq_ingest_projection_candidate_fence"
            )
        )
    await db.execute(
        text("SELECT pg_notify('memento_ingest_projections', :payload)"),
        {"payload": str(document_id)},
    )
    return kinds


async def enqueue_projection_candidates_raw(
    connection: Any,
    *,
    document_id: uuid.UUID,
    revision_hash: str,
    canvas: bool,
    search: bool,
) -> tuple[str, ...]:
    """Same outbox insert for the raw asyncpg writer transaction."""
    kinds = tuple(
        kind
        for kind, requested in (
            (KIND_CANVAS, canvas),
            (KIND_SEARCH, search),
        )
        if requested
    )
    if not kinds or not revision_hash:
        return ()
    for kind in kinds:
        await connection.execute(
            """
            INSERT INTO ingest_projection_candidates
              (document_id, revision_hash, kind)
            VALUES ($1, $2, $3)
            ON CONFLICT (document_id, revision_hash, kind) DO NOTHING
            """,
            document_id,
            revision_hash,
            kind,
        )
    await connection.execute(
        "SELECT pg_notify('memento_ingest_projections', $1)",
        str(document_id),
    )
    return kinds


async def _current_revision(
    db: AsyncSession, document_id: uuid.UUID
) -> str | None:
    from ..db.models import Document, DocumentDeliveryState

    delivery = await db.get(DocumentDeliveryState, document_id)
    if delivery is not None and delivery.revision_hash:
        return str(delivery.revision_hash)
    document = await db.get(Document, document_id)
    if document is None or not document.content_hash:
        return None
    return str(document.content_hash)


async def _content_tsv_text(
    db: AsyncSession, document_id: uuid.UUID
) -> str | None:
    return (
        await db.execute(
            text("SELECT content_tsv::text FROM documents WHERE id = :id"),
            {"id": document_id},
        )
    ).scalar()


async def _canvas_reference_snapshot(
    db: AsyncSession, document_id: uuid.UUID
) -> list[tuple[int, str, str, str]]:
    from ..db.models import CanvasArtifactReference, ConversationMessage

    rows = (
        await db.execute(
            select(
                ConversationMessage.line_number,
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.recorded_path,
                CanvasArtifactReference.status,
            )
            .join(
                ConversationMessage,
                ConversationMessage.id == CanvasArtifactReference.message_id,
            )
            .where(CanvasArtifactReference.document_id == document_id)
            .order_by(
                ConversationMessage.line_number,
                CanvasArtifactReference.path_hash,
            )
        )
    ).all()
    return [
        (int(row.line_number), str(row.path_hash), str(row.recorded_path), str(row.status))
        for row in rows
    ]


async def _apply_canvas(db: AsyncSession, document_id: uuid.UUID) -> bool:
    from ..db.models import CanvasArtifactReference, ConversationMessage, Document
    from .canvas_artifact_store import reconcile_message_canvases

    document = await db.get(Document, document_id)
    if document is None:
        return False
    before = await _canvas_reference_snapshot(db, document_id)
    referenced_ids = select(CanvasArtifactReference.message_id).where(
        CanvasArtifactReference.document_id == document_id
    )
    messages = (
        (
            await db.execute(
                select(ConversationMessage).where(
                    ConversationMessage.document_id == document_id,
                    or_(
                        ConversationMessage.id.in_(referenced_ids),
                        ConversationMessage.content.ilike("%.canvas.tsx%"),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if messages:
        await reconcile_message_canvases(db, document, list(messages))
    after = await _canvas_reference_snapshot(db, document_id)
    return after != before


async def _apply_search(db: AsyncSession, document_id: uuid.UUID) -> bool:
    from sqlalchemy import update as sql_update

    from ..db.models import (
        ConversationMessage,
        Document,
        IngestProjectionCandidate,
    )
    from .ingest_service import MAX_SEARCH_TEXT_CHARS, _bounded_message_text
    from .message_search import (
        MAX_LEXICON_TERMS_PER_INGEST,
        extract_search_terms,
        upsert_search_terms,
    )
    from .tokenize import tokenize_for_index

    document = await db.get(Document, document_id)
    if document is None:
        return False
    before = await _content_tsv_text(db, document_id)
    # A new document's synchronous FULL ingest admits lexicon terms across
    # its complete parsed transcript.  Reproduce that once, before this
    # document has a completed/superseded search candidate.  Later DELTAs
    # keep the regular bounded last-200 scan so a hot large transcript never
    # pays this full-document read on every projection apply.
    first_search_apply = not await db.scalar(
        select(IngestProjectionCandidate.id)
        .where(
            IngestProjectionCandidate.document_id == document_id,
            IngestProjectionCandidate.kind == KIND_SEARCH,
            or_(
                IngestProjectionCandidate.completed_at.is_not(None),
                IngestProjectionCandidate.superseded_at.is_not(None),
            ),
        )
        .limit(1)
    )
    latest_search_rows = (
        (
            await db.execute(
                select(func.left(ConversationMessage.content, 2_048)).where(
                    ConversationMessage.document_id == document_id,
                    ConversationMessage.role.in_(("user", "assistant")),
                )
                .order_by(ConversationMessage.line_number.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    conversation_search_text = _bounded_message_text(
        "\n".join(row for row in reversed(latest_search_rows) if row),
        MAX_SEARCH_TEXT_CHARS,
    )
    tsv_input = tokenize_for_index(f"{document.title or ''} {conversation_search_text}")
    await db.execute(
        sql_update(Document)
        .where(Document.id == document_id)
        .values(content_tsv=func.to_tsvector("simple", tsv_input))
    )
    terms: set[str] = set()
    lexicon_rows = latest_search_rows
    if first_search_apply:
        lexicon_rows = (
            (
                await db.execute(
                    select(func.left(ConversationMessage.content, 2_048))
                    .where(
                        ConversationMessage.document_id == document_id,
                        ConversationMessage.role.in_(("user", "assistant")),
                    )
                    .order_by(ConversationMessage.line_number.asc())
                )
            )
            .scalars()
            .all()
        )
    for row in lexicon_rows:
        if len(terms) >= MAX_LEXICON_TERMS_PER_INGEST:
            break
        remaining = MAX_LEXICON_TERMS_PER_INGEST - len(terms)
        terms.update(list(extract_search_terms(row or ""))[:remaining])
    await upsert_search_terms(db, terms)
    after = await _content_tsv_text(db, document_id)
    return after != before


async def _publish_projection_event(
    db: AsyncSession,
    *,
    document_id: uuid.UUID,
    changes: list[str],
) -> None:
    from ..db.models import Document, Machine
    from .ingest_service import _publish_file_synced_event

    document = await db.get(Document, document_id)
    if document is None or not changes:
        return
    user_id = None
    if document.machine_id is not None:
        machine = await db.get(Machine, document.machine_id)
        if machine is not None and machine.user_id is not None:
            user_id = str(machine.user_id)
    _publish_file_synced_event(db, document, user_id, changes=changes)


async def process_pending_candidates(
    db: AsyncSession,
    *,
    limit: int | None = None,
    document_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None = None,
    skip_pairs: set[tuple[uuid.UUID, str]] | None = None,
) -> list[dict[str, Any]]:
    """Apply pending Canvas/search work, collapsing each document to current revision.

    Completes inside the caller's transaction.  A crash before commit leaves
    the outbox rows pending and the projection writes uncommitted, so replay
    is the same work against the same current revision.  Each group runs in
    its own SAVEPOINT so one failing document cannot roll back or starve the
    rest of the outbox.
    """
    from ..db.models import IngestProjectionCandidate

    cap = MAX_GROUPS_PER_CYCLE if limit is None else max(1, int(limit))
    pending_filter = [
        IngestProjectionCandidate.completed_at.is_(None),
        IngestProjectionCandidate.superseded_at.is_(None),
    ]
    if document_ids:
        pending_filter.append(
            IngestProjectionCandidate.document_id.in_(tuple(document_ids))
        )
    pairs = (
        await db.execute(
            select(
                IngestProjectionCandidate.document_id,
                IngestProjectionCandidate.kind,
            )
            .where(*pending_filter)
            .distinct()
            .order_by(
                IngestProjectionCandidate.document_id,
                IngestProjectionCandidate.kind,
            )
            .limit(cap)
        )
    ).all()
    results: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for document_id, kind in pairs:
        if skip_pairs and (document_id, kind) in skip_pairs:
            continue
        try:
            async with db.begin_nested():
                rows = (
                    (
                        await db.execute(
                            select(IngestProjectionCandidate)
                            .where(
                                IngestProjectionCandidate.document_id == document_id,
                                IngestProjectionCandidate.kind == kind,
                                *pending_filter,
                            )
                            .with_for_update(skip_locked=True)
                            .order_by(IngestProjectionCandidate.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    continue
                current_revision = await _current_revision(db, document_id)
                changed = False
                status = "applied"
                if current_revision is None:
                    status = "superseded"
                elif kind == KIND_CANVAS:
                    changed = await _apply_canvas(db, document_id)
                elif kind == KIND_SEARCH:
                    changed = await _apply_search(db, document_id)
                else:
                    status = "superseded"
                if changed:
                    namespace = (
                        "conversation.canvas" if kind == KIND_CANVAS else "conversation.search"
                    )
                    await _publish_projection_event(
                        db, document_id=document_id, changes=[namespace]
                    )
                for row in rows:
                    row.claimed_at = now
                    row.completed_at = now
                    if current_revision is None or row.revision_hash != current_revision:
                        row.superseded_at = now
        except Exception:
            # Poison-group protection: roll back only this SAVEPOINT and keep
            # draining other documents.  The caller applies retry backoff so
            # a persistently failing group cannot spin at poll frequency.
            logger.exception(
                "Projection group failed for %s/%s", document_id, kind
            )
            results.append(
                {
                    "document_id": str(document_id),
                    "kind": kind,
                    "revision_hash": None,
                    "status": "error",
                    "changed": False,
                    "candidates": 0,
                    "superseded": 0,
                }
            )
            continue
        results.append(
            {
                "document_id": str(document_id),
                "kind": kind,
                "revision_hash": current_revision,
                "status": status,
                "changed": changed,
                "candidates": len(rows),
                "superseded": sum(
                    1
                    for row in rows
                    if current_revision is None or row.revision_hash != current_revision
                ),
            }
        )
    return results


class RealtimeIngestProjector:
    """One serialized projector lifecycle with poll plus optional NOTIFY wake."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        database_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], asyncio.Future] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._database_url = database_url
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._retry_after: dict[tuple[uuid.UUID, str], float] = {}
        self._attempts: dict[tuple[uuid.UUID, str], int] = {}

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def notify_wake(self) -> None:
        self._wake.set()

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        from ..db.session import async_session_factory

        return async_session_factory

    async def run_once(
        self,
        *,
        limit: int | None = None,
        document_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None = None,
    ) -> list[dict[str, Any]]:
        now = self._clock()
        skip = {pair for pair, when in self._retry_after.items() if when > now}
        factory = self._factory()
        async with factory() as session:
            results = await process_pending_candidates(
                session, limit=limit, document_ids=document_ids, skip_pairs=skip
            )
            await session.commit()
        for result in results:
            pair = (uuid.UUID(result["document_id"]), str(result["kind"]))
            if result["status"] == "error":
                attempts = self._attempts.get(pair, 0) + 1
                self._attempts[pair] = attempts
                self._retry_after[pair] = now + min(60.0, 2.0 ** attempts)
            else:
                self._attempts.pop(pair, None)
                self._retry_after.pop(pair, None)
        return results

    async def run_until_quiescent(
        self,
        *,
        limit_per_cycle: int | None = None,
        max_cycles: int = 10_000,
        document_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None = None,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for _ in range(max_cycles):
            results = await self.run_once(
                limit=limit_per_cycle, document_ids=document_ids
            )
            if not results:
                break
            collected.extend(results)
        return collected

    async def run(self) -> None:
        poll_seconds = max(0.02, float(settings.realtime_ingest_projector_poll_seconds))
        import asyncpg

        try:
            connection = await asyncpg.connect(self._dsn_for_lock())
        except Exception:
            logger.exception("Realtime ingest projector could not open a lock connection")
            return
        try:
            acquired = await connection.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)",
                projector_lock_id(),
            )
            if not acquired:
                logger.error("Another realtime ingest projector owns the advisory lock")
                return
            def _on_notify(*_args: object) -> None:
                self._wake.set()

            await connection.add_listener(NOTIFY_CHANNEL, _on_notify)
            logger.info(
                "Realtime ingest projector started (outbox recovery authoritative)"
            )
            while not self._stopping.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("Realtime ingest projector cycle failed")
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
            logger.info("Realtime ingest projector stopped")
        finally:
            try:
                await connection.execute(
                    "SELECT pg_advisory_unlock($1::bigint)",
                    projector_lock_id(),
                )
            except Exception:
                pass
            await connection.close()

    def _dsn_for_lock(self) -> str:
        return _dsn(self._database_url)


def main() -> None:
    projector = RealtimeIngestProjector()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, projector.stop)
        except NotImplementedError:
            signal.signal(signum, lambda *_args: projector.stop())
    try:
        loop.run_until_complete(projector.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
