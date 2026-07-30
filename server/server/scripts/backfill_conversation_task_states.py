"""Backfill current task projections from normalized message metadata only.

Dry-run is the default. No transcript content is read or reparsed::

    python -m server.scripts.backfill_conversation_task_states
    python -m server.scripts.backfill_conversation_task_states --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from server.db.session import async_session_factory
from server.services.conversation_tasks import backfill_task_projections


async def run(
    *,
    apply: bool,
    document_ids: list[UUID] | None = None,
) -> dict[str, object]:
    async with async_session_factory() as db:
        result = await backfill_task_projections(db, document_ids)
        if apply:
            await db.commit()
        else:
            await db.rollback()
        return {
            "mode": "apply" if apply else "dry-run",
            **result,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the idempotent current-state projections",
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
