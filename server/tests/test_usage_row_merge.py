"""Same-turn usage observations must merge before the multi-row upsert.

Production failure reproduced 2026-08-22: one collector delta bundled seven
minutes of Codex ``token_count`` snapshots for a single long turn (any upload
stall or server restart creates this shape). The batch upsert then contained
many VALUES rows with one ``(document_id, source_id)`` key and PostgreSQL
rejected it with ``CardinalityViolationError: ON CONFLICT DO UPDATE command
cannot affect row a second time``, wedging that document's ingest in an
endless Celery retry loop.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import Base, ConversationUsageEvent, Document, Machine, Tool, User
from server.services.ingest_service import _merge_usage_rows, _upsert_assistant_usage_rows

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)


def _row(
    document_id,
    source_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str | None = "gpt-5.6-terra",
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "machine_id": uuid.uuid4(),
        "tool_id": "codex",
        "source_id": source_id,
        "source": "codex",
        "occurred_at": occurred_at,
        "model": model,
        "reasoning_effort": "xhigh",
        "service_tier": None,
        "attribution_status": "attributed",
        "input_tokens": input_tokens,
        "uncached_input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }


def test_accumulating_merge_sums_counts_and_keeps_newest_identity() -> None:
    document_id = uuid.uuid4()
    base = datetime(2026, 8, 22, 12, 16, tzinfo=timezone.utc)
    rows = [
        _row(document_id, "codex:turn:t1", occurred_at=base, input_tokens=10, output_tokens=1),
        _row(
            document_id,
            "codex:turn:t1",
            occurred_at=base + timedelta(minutes=7),
            input_tokens=5,
            output_tokens=2,
            model="gpt-5.6-terra-final",
        ),
        _row(document_id, "codex:turn:t2", occurred_at=base, input_tokens=3),
    ]

    merged = _merge_usage_rows(rows, accumulate=True)

    assert [row["source_id"] for row in merged] == ["codex:turn:t1", "codex:turn:t2"]
    turn_one = merged[0]
    assert turn_one["input_tokens"] == 15
    assert turn_one["output_tokens"] == 3
    assert turn_one["total_tokens"] == 18
    assert turn_one["occurred_at"] == base + timedelta(minutes=7)
    assert turn_one["model"] == "gpt-5.6-terra-final"


def test_replacement_merge_keeps_last_row_per_key() -> None:
    document_id = uuid.uuid4()
    base = datetime(2026, 8, 22, 12, 16, tzinfo=timezone.utc)
    rows = [
        _row(document_id, "msg_1", occurred_at=base, input_tokens=100),
        _row(document_id, "msg_1", occurred_at=base + timedelta(seconds=5), input_tokens=42),
    ]

    merged = _merge_usage_rows(rows, accumulate=False)

    assert len(merged) == 1
    assert merged[0]["input_tokens"] == 42


def test_merge_preserves_distinct_keys_untouched() -> None:
    document_id = uuid.uuid4()
    base = datetime(2026, 8, 22, 12, 16, tzinfo=timezone.utc)
    rows = [
        _row(document_id, f"codex:turn:{index}", occurred_at=base, input_tokens=index)
        for index in range(4)
    ]

    merged = _merge_usage_rows(rows, accumulate=True)

    assert merged == rows


@pytest_asyncio.fixture
async def session_factory():
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@requires_postgres
@pytest.mark.asyncio
async def test_single_batch_with_duplicate_turn_snapshots_upserts_once(
    session_factory,
) -> None:
    """The exact production shape: one batch, many snapshots, one turn."""
    async with session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            role="owner",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="usage-merge-test",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await db.get(Tool, "codex") is None:
            db.add(Tool(id="codex", display_name="Codex"))
        document = Document(
            id=uuid.uuid4(),
            tool_id="codex",
            machine_id=machine.id,
            relative_path=f"sessions/{uuid.uuid4()}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Long single turn",
            content_hash=uuid.uuid4().hex,
            file_size_bytes=1,
        )
        db.add_all([user, machine, document])
        await db.flush()

        base = datetime.now(timezone.utc)
        turn = "codex:turn:01a02966-5d93-78b2-ae4c-3ec2433b8c3f"
        batch = [
            _row(
                document.id,
                turn,
                occurred_at=base + timedelta(seconds=index),
                input_tokens=100 + index,
                output_tokens=index,
            )
            for index in range(26)
        ]
        for row in batch:
            row["machine_id"] = machine.id

        # Before the merge fix this raised CardinalityViolationError.
        await _upsert_assistant_usage_rows(db, batch, accumulate_existing=True)
        await db.flush()

        event = (
            await db.execute(
                select(ConversationUsageEvent).where(
                    ConversationUsageEvent.document_id == document.id
                )
            )
        ).scalar_one()
        assert event.source_id == turn
        assert event.input_tokens == sum(100 + index for index in range(26))
        assert event.output_tokens == sum(range(26))

        # A later drain for the same turn still accumulates onto the one row.
        await _upsert_assistant_usage_rows(
            db,
            [
                _row(document.id, turn, occurred_at=base + timedelta(minutes=1), input_tokens=1)
                | {"machine_id": machine.id}
            ],
            accumulate_existing=True,
        )
        await db.flush()
        await db.refresh(event)
        assert event.input_tokens == sum(100 + index for index in range(26)) + 1
