"""Null verified PostgreSQL document-content compatibility copies.

Dry-run is the default::

    python -m server.scripts.null_document_content
    python -m server.scripts.null_document_content --apply

Only rows whose immutable object is complete and stream-verified against both
the stored pointer proof and the current PostgreSQL bytes are eligible.  The
UUID keyset makes independently committed batches resumable; this script never
touches a row without a complete verified pointer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, update

from server.db.models import Document
from server.db.session import async_session_factory
from server.services.large_content_store import (
    DocumentContentIntegrityError,
    verify_document_content_object,
)


DEFAULT_BATCH_SIZE = 100
logger = logging.getLogger(__name__)


def _pointer_is_verified(row) -> bool:
    return bool(
        row.content_s3_key
        and row.content_object_sha256
        and row.content_object_size_bytes is not None
        and row.content_object_verified_at is not None
    )


async def run(
    *,
    apply: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    start_after: UUID | None = None,
) -> dict[str, object]:
    """Verify each compatibility value before optionally nulling it."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    cursor = start_after
    scanned = 0
    nulled = 0
    skipped_mismatch = 0
    skipped_unverified = 0
    skipped_changed = 0
    async with async_session_factory() as db:
        while True:
            query = (
                select(
                    Document.id,
                    Document.content,
                    Document.content_s3_key,
                    Document.content_object_sha256,
                    Document.content_object_size_bytes,
                    Document.content_object_verified_at,
                )
                .where(Document.content.is_not(None))
                .order_by(Document.id)
                .limit(batch_size)
            )
            if cursor is not None:
                query = query.where(Document.id > cursor)
            rows = (await db.execute(query)).all()
            if not rows:
                break

            for row in rows:
                cursor = row.id
                scanned += 1
                if not _pointer_is_verified(row):
                    skipped_unverified += 1
                    continue

                inline_sha256 = hashlib.sha256(row.content.encode("utf-8")).hexdigest()
                try:
                    await asyncio.to_thread(
                        verify_document_content_object,
                        row.content_s3_key,
                        sha256=row.content_object_sha256,
                        size_bytes=int(row.content_object_size_bytes),
                    )
                except (
                    BotoCoreError,
                    ClientError,
                    DocumentContentIntegrityError,
                    OSError,
                ) as exc:
                    skipped_mismatch += 1
                    logger.warning(
                        "Skipping document %s: verified object read failed (%s)",
                        row.id,
                        type(exc).__name__,
                    )
                    continue

                if inline_sha256 != row.content_object_sha256:
                    skipped_mismatch += 1
                    logger.warning(
                        "Skipping document %s: PostgreSQL content hash differs from pointer",
                        row.id,
                    )
                    continue
                if not apply:
                    continue

                result = await db.execute(
                    update(Document)
                    .where(
                        Document.id == row.id,
                        Document.content == row.content,
                        Document.content_s3_key == row.content_s3_key,
                        Document.content_object_sha256 == row.content_object_sha256,
                        Document.content_object_size_bytes
                        == row.content_object_size_bytes,
                        Document.content_object_verified_at
                        == row.content_object_verified_at,
                    )
                    .values(content=None)
                )
                if result.rowcount:
                    nulled += 1
                else:
                    # A concurrent writer changed a proof or compatibility
                    # value after our streamed GET; its next keyset run can
                    # evaluate the new version safely.
                    skipped_changed += 1

            if apply:
                await db.commit()
            else:
                await db.rollback()

    return {
        "mode": "apply" if apply else "dry-run",
        "scanned": scanned,
        "nulled": nulled,
        "skipped_mismatch": skipped_mismatch,
        "skipped_unverified": skipped_unverified,
        "skipped_changed": skipped_changed,
        "last_document_id": str(cursor) if cursor else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="null only proof-matching PostgreSQL values; dry-run is default",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
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
