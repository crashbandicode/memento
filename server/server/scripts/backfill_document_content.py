"""Retired document-content backfill entrypoint.

``documents.content`` was verified, nulled, and then dropped during the
MinIO contract migration. Historical raw-source recovery is now reprocessing
the originating client source, not rebuilding a PostgreSQL compatibility copy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID


MESSAGE = (
    "No-op: documents.content has been dropped; document-content backfill is "
    "retired. Reprocess the originating client sources for recovery."
)


async def run(
    *,
    apply: bool,
    batch_size: int = 100,
    start_after: UUID | None = None,
) -> dict[str, object]:
    """Return a successful, explicit no-op for obsolete automation."""
    del apply, batch_size, start_after
    return {"status": "no-op", "message": MESSAGE}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="accepted for compatibility")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--start-after", type=UUID)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    apply=args.apply,
                    batch_size=args.batch_size,
                    start_after=args.start_after,
                )
            )
        )
    )


if __name__ == "__main__":
    main()
