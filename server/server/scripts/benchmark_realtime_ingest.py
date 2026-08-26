"""Repeatable fixture-DB benchmark for Phase 0/1 conversation DELTA ingest.

Run from the repository's ``server`` directory so the checkout is imported,
for example:

    Set-Location server
    $env:MEMENTO_TASK_TEST_DATABASE_URL =
      'postgresql+asyncpg://postgres:test@localhost:55437/postgres'
    python -m server.scripts.benchmark_realtime_ingest --writer legacy
    python -m server.scripts.benchmark_realtime_ingest --writer core

The setup FULL is deliberately outside the measurement.  Each timed sample
therefore reports one real, exact-base DELTA sync over a fresh fixture document.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
import tracemalloc
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..db.models import Base, Machine, Tool, User
from ..services.ingest_service import ingest_file


@dataclass(frozen=True)
class FixtureDelta:
    full: str
    delta: str
    final: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture_delta(message_count: int) -> FixtureDelta:
    """Build a fixed-shape Codex append containing only genuine new records."""
    full = json.dumps(
        {
            "type": "event_msg",
            "timestamp": "2026-08-04T12:00:00Z",
            "payload": {
                "type": "user_message",
                "client_id": "benchmark-user",
                "message": "Benchmark Core DELTA message staging.",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    filler = "x" * 256
    delta = "\n".join(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": f"2026-08-04T12:{index // 60:02d}:{index % 60:02d}Z",
                "payload": {
                    "type": "agent_message",
                    "message": f"Benchmark assistant row {index}: {filler}",
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        for index in range(1, message_count + 1)
    )
    return FixtureDelta(full=full, delta=delta, final=f"{full}\n{delta}")


async def _seed_document(
    session: AsyncSession,
    fixture: FixtureDelta,
    *,
    suffix: str,
    use_core_delta_message_staging: bool,
) -> tuple[User, Machine, str, str]:
    user = User(
        id=uuid.uuid4(),
        email=f"realtime-ingest-benchmark-{suffix}-{uuid.uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name=f"realtime-ingest-benchmark-{suffix}",
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
    )
    if await session.get(Tool, "codex") is None:
        session.add(Tool(id="codex", display_name="codex"))
    session.add_all((user, machine))
    await session.commit()
    relative_path = f"phase0-benchmark/{suffix}.jsonl"
    session_id = f"phase0-benchmark-{suffix}"
    await ingest_file(
        session,
        tool_id="codex",
        category="conversation",
        content_type="jsonl",
        relative_path=relative_path,
        content=fixture.full,
        content_hash=_hash(fixture.full),
        file_size=len(fixture.full.encode("utf-8")),
        mode="full",
        offset=len(fixture.full.encode("utf-8")),
        metadata={"session_id": session_id},
        timestamp=1_785_932_800.0,
        machine_id=machine.id,
        user_id=str(user.id),
        schedule_post_ingest=False,
        use_core_delta_message_staging=use_core_delta_message_staging,
    )
    await session.commit()
    return user, machine, relative_path, session_id


async def _run_delta(
    session: AsyncSession,
    fixture: FixtureDelta,
    *,
    user: User,
    machine: Machine,
    relative_path: str,
    session_id: str,
    use_core_delta_message_staging: bool,
) -> None:
    await ingest_file(
        session,
        tool_id="codex",
        category="conversation",
        content_type="jsonl",
        relative_path=relative_path,
        content=fixture.delta,
        content_hash=_hash(fixture.final),
        file_size=len(fixture.delta.encode("utf-8")),
        mode="delta",
        offset=len(fixture.final.encode("utf-8")),
        base_hash=_hash(fixture.full),
        base_offset=len(fixture.full.encode("utf-8")),
        metadata={"session_id": session_id},
        timestamp=1_785_932_801.0,
        machine_id=machine.id,
        user_id=str(user.id),
        schedule_post_ingest=False,
        use_core_delta_message_staging=use_core_delta_message_staging,
    )
    await session.commit()


async def _timed_sample(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: FixtureDelta,
    *,
    sample: int,
    use_core_delta_message_staging: bool,
) -> dict[str, int]:
    async with session_factory() as session:
        user, machine, relative_path, session_id = await _seed_document(
            session,
            fixture,
            suffix=f"timed-{sample}-{uuid.uuid4()}",
            use_core_delta_message_staging=use_core_delta_message_staging,
        )
        wall_started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        await _run_delta(
            session,
            fixture,
            user=user,
            machine=machine,
            relative_path=relative_path,
            session_id=session_id,
            use_core_delta_message_staging=use_core_delta_message_staging,
        )
        return {
            "wall_ns": time.perf_counter_ns() - wall_started,
            "cpu_ns": time.process_time_ns() - cpu_started,
        }


async def _allocation_sample(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: FixtureDelta,
    *,
    sample: int,
    use_core_delta_message_staging: bool,
) -> dict[str, int]:
    async with session_factory() as session:
        user, machine, relative_path, session_id = await _seed_document(
            session,
            fixture,
            suffix=f"allocation-{sample}-{uuid.uuid4()}",
            use_core_delta_message_staging=use_core_delta_message_staging,
        )
        tracemalloc.start()
        before = tracemalloc.take_snapshot()
        await _run_delta(
            session,
            fixture,
            user=user,
            machine=machine,
            relative_path=relative_path,
            session_id=session_id,
            use_core_delta_message_staging=use_core_delta_message_staging,
        )
        after = tracemalloc.take_snapshot()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        diffs = after.compare_to(before, "lineno")
        return {
            "net_allocated_bytes": sum(
                max(0, stat.size_diff) for stat in diffs
            ),
            "net_allocation_blocks": sum(
                max(0, stat.count_diff) for stat in diffs
            ),
            "peak_traced_bytes": peak,
        }


def _median_ms(values: list[int]) -> float:
    return round(statistics.median(values) / 1_000_000, 3)


async def _benchmark(args: argparse.Namespace) -> dict[str, Any]:
    database_url = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("MEMENTO_TASK_TEST_DATABASE_URL is required")
    fixture = _fixture_delta(args.messages)
    use_core = args.writer == "core"
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for warmup in range(args.warmup):
            await _timed_sample(
                session_factory,
                fixture,
                sample=warmup,
                use_core_delta_message_staging=use_core,
            )
        timed = [
            await _timed_sample(
                session_factory,
                fixture,
                sample=sample,
                use_core_delta_message_staging=use_core,
            )
            for sample in range(args.iterations)
        ]
        allocations = [
            await _allocation_sample(
                session_factory,
                fixture,
                sample=sample,
                use_core_delta_message_staging=use_core,
            )
            for sample in range(args.allocation_samples)
        ]
    finally:
        await engine.dispose()
    return {
        "writer": args.writer,
        "fixture": {
            "tool": "codex",
            "normalized_delta_messages": args.messages,
            "delta_source_bytes": len(fixture.delta.encode("utf-8")),
            "iterations": args.iterations,
            "warmup": args.warmup,
            "allocation_samples": args.allocation_samples,
        },
        "median": {
            "cpu_ms_per_sync": _median_ms([item["cpu_ns"] for item in timed]),
            "wall_ms_per_sync": _median_ms([item["wall_ns"] for item in timed]),
            "net_allocated_bytes": int(
                statistics.median(
                    item["net_allocated_bytes"] for item in allocations
                )
            ),
            "net_allocation_blocks": int(
                statistics.median(
                    item["net_allocation_blocks"] for item in allocations
                )
            ),
            "peak_traced_bytes": int(
                statistics.median(
                    item["peak_traced_bytes"] for item in allocations
                )
            ),
        },
        "samples": {"timed": timed, "allocations": allocations},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writer", choices=("legacy", "core"), required=True)
    parser.add_argument("--messages", type=int, default=200)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--allocation-samples", type=int, default=3)
    args = parser.parse_args()
    if args.messages < 1 or args.iterations < 1 or args.allocation_samples < 1:
        parser.error("messages, iterations, and allocation samples must be positive")
    print(json.dumps(asyncio.run(_benchmark(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
