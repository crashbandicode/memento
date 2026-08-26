"""Retired PostgreSQL document-content nulling entrypoint.

The column was already independently verified and nulled before it was dropped.
This script remains as a harmless compatibility target for old runbooks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID


MESSAGE = (
    "No-op: documents.content has already been dropped; there is no inline "
    "content left to null."
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
