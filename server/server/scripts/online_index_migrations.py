"""Print or apply Memento's out-of-transaction index migration plan.

Usage:
    python -m server.scripts.online_index_migrations
    python -m server.scripts.online_index_migrations --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json

from ..db.online_migrations import (
    online_migration_plan,
    run_online_index_migrations,
)
from ..db.session import engine


async def _apply() -> dict:
    try:
        return await run_online_index_migrations(engine)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or apply concurrent PostgreSQL index migrations"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the plan; the default only prints DDL",
    )
    args = parser.parse_args()
    result = asyncio.run(_apply()) if args.apply else online_migration_plan()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
