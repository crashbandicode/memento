"""Delayed mark-and-sweep for immutable document-content objects."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.models import Document, DocumentContentGcCandidate
from .large_content_store import DOCUMENT_CONTENT_PREFIX, _client


def _content_keys(s3_client=None) -> list[str]:
    """List application-owned immutable keys, excluding every other bucket use."""
    client = s3_client or _client()
    prefix = f"{DOCUMENT_CONTENT_PREFIX}/"
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        keys.extend(
            entry["Key"]
            for entry in page.get("Contents", [])
            if entry.get("Key", "").startswith(prefix)
        )
    return keys


async def _mark_and_unmark(
    db: AsyncSession,
    *,
    object_keys: Iterable[str],
    observed_at: datetime,
) -> tuple[int, int]:
    """Persist first-seen orphans and remove all candidates now referenced."""
    keys = sorted(set(object_keys))
    async with db.begin():
        live_keys = set(
            (
                await db.execute(
                    select(Document.content_s3_key).where(
                        Document.content_s3_key.is_not(None)
                    )
                )
            ).scalars()
        )
        unreferenced_keys = [key for key in keys if key not in live_keys]
        existing_candidates: set[str] = set()
        if unreferenced_keys:
            existing_candidates = set(
                (
                    await db.execute(
                        select(DocumentContentGcCandidate.content_s3_key).where(
                            DocumentContentGcCandidate.content_s3_key.in_(
                                unreferenced_keys
                            )
                        )
                    )
                ).scalars()
            )
            new_keys = [
                key for key in unreferenced_keys if key not in existing_candidates
            ]
            if new_keys:
                await db.execute(
                    pg_insert(DocumentContentGcCandidate)
                    .values(
                        [
                            {
                                "content_s3_key": key,
                                "first_unreferenced_at": observed_at,
                                "last_seen_at": observed_at,
                            }
                            for key in new_keys
                        ]
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            DocumentContentGcCandidate.content_s3_key
                        ]
                    )
                )
            if existing_candidates:
                await db.execute(
                    text(
                        "UPDATE document_content_gc_candidates "
                        "SET last_seen_at = :observed_at "
                        "WHERE content_s3_key = ANY(:keys)"
                    ),
                    {"observed_at": observed_at, "keys": list(existing_candidates)},
                )
        else:
            new_keys = []

        # Do this from the live pointer table, not only the listing: a key can
        # be re-referenced between S3 list pages or after being marked.
        unmarked = await db.execute(
            delete(DocumentContentGcCandidate).where(
                DocumentContentGcCandidate.content_s3_key.in_(
                    select(Document.content_s3_key).where(
                        Document.content_s3_key.is_not(None)
                    )
                )
            )
        )
    return len(new_keys), int(unmarked.rowcount or 0)


async def collect_unreferenced_document_content(
    db: AsyncSession,
    *,
    s3_client=None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Mark, unmark, and safely delete old unreferenced immutable objects.

    The deletion lock matches the transaction-scoped ingest finalizer lock.
    A GC pass that wins the race deletes before a writer verifies/recreates and
    commits its pointer; a writer that wins makes the live-pointer recheck
    preserve the object.
    """
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    object_keys = await asyncio.to_thread(_content_keys, s3_client)
    marked, unmarked = await _mark_and_unmark(
        db,
        object_keys=object_keys,
        observed_at=observed_at,
    )
    grace = timedelta(hours=max(0, settings.document_content_gc_grace_hours))
    cutoff = observed_at - grace
    async with db.begin():
        due_keys = (
            (
                await db.execute(
                    select(DocumentContentGcCandidate.content_s3_key)
                    .where(DocumentContentGcCandidate.first_unreferenced_at <= cutoff)
                    .order_by(DocumentContentGcCandidate.first_unreferenced_at)
                )
            )
            .scalars()
            .all()
        )

    deleted = 0
    for key in due_keys:
        # A separate transaction per object keeps the network delete bounded
        # and lets a transient failure leave just that candidate for retry.
        async with db.begin():
            candidate = (
                await db.execute(
                    select(DocumentContentGcCandidate)
                    .where(DocumentContentGcCandidate.content_s3_key == key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if candidate is None or candidate.first_unreferenced_at > cutoff:
                continue
            acquired = bool(
                await db.scalar(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
            )
            if not acquired:
                continue
            try:
                # This recheck deliberately occurs inside the deletion
                # transaction while holding the same object-key lock as ingest.
                referenced = await db.scalar(
                    select(Document.id)
                    .where(Document.content_s3_key == key)
                    .limit(1)
                )
                if referenced is not None:
                    await db.delete(candidate)
                    unmarked += 1
                    continue
                await asyncio.to_thread(
                    (s3_client or _client()).delete_object,
                    Bucket=settings.s3_bucket,
                    Key=key,
                )
                await db.delete(candidate)
                deleted += 1
            finally:
                await db.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": key},
                )

    return {"marked": marked, "unmarked": unmarked, "deleted": deleted}
