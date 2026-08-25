"""Daily-calendar activity rollup: correctness, tz, scoping, live-query parity."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    Base,
    ConversationMessage,
    Document,
    Machine,
    Project,
    Tool,
    User,
)
from server.services.activity_rollup import (
    daily_dates_from_rollup,
    refresh_activity_hourly,
    rollup_is_populated,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)


@pytest_asyncio.fixture
async def session_factory():
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
        # The rollup aggregates the WHOLE messages table, so isolate this
        # test file's dataset from anything left in a shared DB.
        await connection.execute(
            text(
                "TRUNCATE conversation_messages, documents, "
                "conversation_activity_hourly RESTART IDENTITY CASCADE"
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(session, *, tool_id="cursor"):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@x.test", role="viewer", status="active")
    machine = Machine(
        id=uuid.uuid4(), name=f"m-{uuid.uuid4()}",
        collector_token_hash=str(uuid.uuid4()), user_id=user.id,
    )
    project = Project(id=uuid.uuid4(), slug=f"p-{uuid.uuid4()}", title="P", tool_id=tool_id)
    if await session.get(Tool, tool_id) is None:
        session.add(Tool(id=tool_id, display_name=tool_id))
    session.add_all([user, machine, project])
    await session.flush()
    doc = Document(
        id=uuid.uuid4(), tool_id=tool_id, project_id=project.id, machine_id=machine.id,
        relative_path=f"s/{uuid.uuid4()}.jsonl", category="conversation",
        content_type="jsonl", title="T", content_hash=uuid.uuid4().hex,
        file_size_bytes=10, metadata_={"session_id": str(uuid.uuid4())},
        synced_at=datetime.now(UTC),
    )
    session.add(doc)
    await session.flush()
    return user, machine, doc


def _msg(doc_id, line, role, content, ts):
    return ConversationMessage(
        document_id=doc_id, line_number=line, role=role,
        message_type="message", content=content, timestamp=ts,
    )


@requires_postgres
@pytest.mark.asyncio
async def test_rollup_counts_only_countable_messages(session_factory):
    day = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as s:
        _user, machine, doc = await _seed(s)
        s.add_all([
            _msg(doc.id, 1, "user", "hi", day),                 # counts
            _msg(doc.id, 2, "assistant", "hello", day),         # counts
            _msg(doc.id, 3, "assistant", "[Result] x", day),    # excluded
            _msg(doc.id, 4, "assistant", "[Tool: bash]", day),  # excluded
            _msg(doc.id, 5, "tool", "output", day),             # excluded (role)
            _msg(doc.id, 6, "user", "nots", None),              # excluded (null ts)
        ])
        await s.flush()
        await s.commit()

        n = await refresh_activity_hourly(s)
        assert n == 1  # one (hour, machine, tool) bucket

        rows = await daily_dates_from_rollup(
            s, machine_ids=[machine.id], cutoff=day - timedelta(days=1), tz_offset=0,
        )
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-08-20"
        assert rows[0]["message_count"] == 2  # only the 2 countable turns
        assert rows[0]["tools"] == ["cursor"]


@requires_postgres
@pytest.mark.asyncio
async def test_tz_offset_shifts_day_boundary(session_factory):
    # 01:30 UTC → with tz_offset 180 (i.e. UTC+3? offset subtracts) check both days.
    near_midnight = datetime(2026, 8, 21, 1, 30, tzinfo=UTC)
    async with session_factory() as s:
        _user, machine, doc = await _seed(s)
        s.add_all([_msg(doc.id, 1, "user", "a", near_midnight)])
        await s.flush()
        await s.commit()
        await refresh_activity_hourly(s)

        utc_rows = await daily_dates_from_rollup(
            s, machine_ids=[machine.id], cutoff=near_midnight - timedelta(days=2), tz_offset=0,
        )
        assert utc_rows[0]["date"] == "2026-08-21"
        # tz_offset=180 subtracts 180 min → 22:30 the previous day.
        shifted = await daily_dates_from_rollup(
            s, machine_ids=[machine.id], cutoff=near_midnight - timedelta(days=2), tz_offset=180,
        )
        assert shifted[0]["date"] == "2026-08-20"


@requires_postgres
@pytest.mark.asyncio
async def test_machine_scoping_and_admin_unscoped(session_factory):
    day = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    async with session_factory() as s:
        _u1, m1, d1 = await _seed(s)
        _u2, m2, d2 = await _seed(s)
        s.add_all([
            _msg(d1.id, 1, "user", "one", day),
            _msg(d2.id, 1, "user", "two", day),
            _msg(d2.id, 2, "assistant", "two-b", day),
        ])
        await s.flush()
        await s.commit()
        await refresh_activity_hourly(s)

        only_m1 = await daily_dates_from_rollup(
            s, machine_ids=[m1.id], cutoff=day - timedelta(days=1), tz_offset=0,
        )
        assert only_m1[0]["message_count"] == 1
        # None = admin/owner, sees both machines.
        allm = await daily_dates_from_rollup(
            s, machine_ids=None, cutoff=day - timedelta(days=1), tz_offset=0,
        )
        total = sum(r["message_count"] for r in allm if r["date"] == "2026-08-19")
        assert total == 3
        # Empty machine list = user with no devices sees nothing.
        none = await daily_dates_from_rollup(
            s, machine_ids=[], cutoff=day - timedelta(days=1), tz_offset=0,
        )
        assert none == []


@requires_postgres
@pytest.mark.asyncio
async def test_rollup_is_populated_flag(session_factory):
    async with session_factory() as s:
        assert await rollup_is_populated(s) is False
        _user, machine, doc = await _seed(s)
        s.add_all([_msg(doc.id, 1, "user", "hi", datetime(2026, 8, 18, 8, tzinfo=UTC))])
        await s.flush()
        await s.commit()
        await refresh_activity_hourly(s)
        assert await rollup_is_populated(s) is True
