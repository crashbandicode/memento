"""Backfill verified immutable document-content pointers.

Dry-run is the default::

    python -m server.scripts.backfill_document_content
    python -m server.scripts.backfill_document_content --apply

The compatibility ``documents.content`` value is never changed by this tool.
Each committed batch is independently resumable: already-verified rows fall
outside the keyset scan predicate on the next invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from uuid import UUID
from pathlib import Path

from sqlalchemy import and_, or_, select, update

from server.db.models import Document
from server.db.session import async_session_factory
from server.services.large_content_store import (
    copy_legacy_large_content_to_path,
    finalize_document_content,
)


DEFAULT_BATCH_SIZE = 100


def _needs_pointer_clause():
    return and_(
        or_(
            Document.content.is_not(None),
            # Earlier releases externalized large conversations at raw/... and
            # left PG content NULL. Move those historical values too, without
            # ever changing the compatibility column.
            Document.content_s3_key.like("raw/%"),
        ),
        or_(
            Document.content_s3_key.is_(None),
            Document.content_object_sha256.is_(None),
            Document.content_object_size_bytes.is_(None),
            Document.content_object_verified_at.is_(None),
        ),
    )


async def run(
    *,
    apply: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    start_after: UUID | None = None,
) -> dict[str, object]:
    """Backfill in UUID keyset batches without ever nulling inline content."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    cursor = start_after
    scanned = 0
    verified = 0
    skipped_changed = 0
    async with async_session_factory() as db:
        while True:
            query = (
                select(Document.id, Document.content, Document.content_s3_key)
                .where(_needs_pointer_clause())
                .order_by(Document.id)
                .limit(batch_size)
            )
            if cursor is not None:
                query = query.where(Document.id > cursor)
            rows = (await db.execute(query)).all()
            if not rows:
                break

            for document_id, content, legacy_key in rows:
                cursor = document_id
                scanned += 1
                if not apply:
                    continue

                # The finalizer hashes the exact UTF-8 compatibility text,
                # PUTs/reuses its immutable key, and streamed-GET verifies it
                # before this transaction may publish the pointer.
                if content is not None:
                    pointer = await finalize_document_content(
                        document_id=document_id,
                        content=content,
                    )
                else:
                    assert legacy_key is not None
                    with tempfile.TemporaryDirectory(
                        prefix="memento-document-content-backfill-"
                    ) as temporary:
                        payload_path = Path(temporary) / "legacy-content.bin"
                        await asyncio.to_thread(
                            copy_legacy_large_content_to_path,
                            legacy_key,
                            payload_path,
                        )
                        pointer = await finalize_document_content(
                            document_id=document_id,
                            payload_path=payload_path,
                        )
                content_fence = (
                    Document.content == content
                    if content is not None
                    else Document.content.is_(None)
                )
                result = await db.execute(
                    update(Document)
                    .where(
                        Document.id == document_id,
                        content_fence,
                        _needs_pointer_clause(),
                    )
                    .values(
                        content_s3_key=pointer.key,
                        content_object_sha256=pointer.sha256,
                        content_object_size_bytes=pointer.size_bytes,
                        content_object_verified_at=pointer.verified_at,
                    )
                )
                if result.rowcount:
                    verified += 1
                else:
                    # A concurrent update wins safely: its next keyset pass
                    # will upload/verify its new bytes. The orphan is GC-safe.
                    skipped_changed += 1

            if apply:
                await db.commit()
            else:
                # No writes occur in dry-run, but ending the read transaction
                # promptly keeps its snapshot from delaying normal cleanup.
                await db.rollback()

    return {
        "mode": "apply" if apply else "dry-run",
        "scanned": scanned,
        "verified": verified,
        "skipped_changed": skipped_changed,
        "last_document_id": str(cursor) if cursor else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="PUT/verify objects and commit pointer fields; dry-run is default",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )
    parser.add_argument(
        "--start-after",
        type=UUID,
        help="resume after this document UUID (normally not needed)",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    apply=args.apply,
                    batch_size=args.batch_size,
                    start_after=args.start_after,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
