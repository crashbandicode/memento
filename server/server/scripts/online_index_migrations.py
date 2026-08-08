"""Inspect or apply Memento's deployment-run index migration plan.

Usage:
    python -m server.scripts.online_index_migrations
    python -m server.scripts.online_index_migrations --status
    python -m server.scripts.online_index_migrations --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from ..db.online_migrations import (
    online_migration_status,
    online_migration_plan,
    run_online_index_migrations,
)
from ..db.session import engine


async def _run(mode: str) -> dict:
    try:
        if mode == "apply":
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
            registered_signals: list[signal.Signals] = []
            if task is not None:
                for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
                    try:
                        loop.add_signal_handler(shutdown_signal, task.cancel)
                    except (NotImplementedError, RuntimeError):
                        continue
                    registered_signals.append(shutdown_signal)
            try:
                return await run_online_index_migrations(engine)
            finally:
                for shutdown_signal in registered_signals:
                    loop.remove_signal_handler(shutdown_signal)
        return await online_migration_status(engine)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Plan, inspect, or apply concurrent PostgreSQL index migrations"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--apply",
        action="store_true",
        help="apply the plan; the default only prints DDL",
    )
    action.add_argument(
        "--status",
        action="store_true",
        help="show durable state, catalog validity, and live build progress",
    )
    args = parser.parse_args()
    if not args.apply and not args.status:
        print(json.dumps(online_migration_plan(), indent=2, sort_keys=True))
        return

    mode = "apply" if args.apply else "status"
    try:
        result = asyncio.run(_run(mode))
    except asyncio.CancelledError:
        print(
            json.dumps(
                {
                    "status": "interrupted",
                    "detail": "shutdown requested; retry is safe",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(130)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
