"""Backfill ingest-owned conversation read projections.

Dry-run is the default::

    python -m server.scripts.backfill_conversation_read_models
    python -m server.scripts.backfill_conversation_read_models --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from server.db.session import async_session_factory
from server.services.conversation_read_model import (
    backfill_conversation_read_models,
)


async def run(
    *,
    apply: bool,
    document_ids: list[UUID] | None = None,
) -> dict[str, object]:
    async with async_session_factory() as db:
        result = await backfill_conversation_read_models(db, document_ids)
        if apply:
            await db.commit()
        else:
            await db.rollback()
        return {"mode": "apply" if apply else "dry-run", **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the idempotent read projections",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        type=UUID,
        dest="document_ids",
        help="limit the backfill to one document (repeatable)",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    apply=args.apply,
                    document_ids=args.document_ids,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
