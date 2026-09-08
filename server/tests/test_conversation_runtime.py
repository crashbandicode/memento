from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import Base, ConversationRuntime, Document, Machine, Tool, User
from server.services.conversation_runtime import (
    apply_conversation_runtime_update,
    conversation_runtime_for_document,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL conversation-runtime test database is not configured",
)


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
async def test_runtime_signal_is_ordered_and_resume_reopens(session_factory) -> None:
    user_id = uuid.uuid4()
    machine_id = uuid.uuid4()
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    base = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    async with session_factory() as db:
        db.add(
            User(
                id=user_id,
                email=f"{user_id}@example.test",
                role="owner",
                status="active",
            )
        )
        db.add(Tool(id="codex", display_name="Codex"))
        db.add(
            Machine(
                id=machine_id,
                name="butterbridge (Windows)",
                collector_token_hash=str(uuid.uuid4()),
                user_id=user_id,
            )
        )
        await db.commit()

        assert (
            await apply_conversation_runtime_update(
                db,
                machine_id=machine_id,
                tool_id="codex",
                session_id=session_id,
                runtime_state="running",
                runtime_privilege="administrator",
                timestamp=base.isoformat(),
            )
        ).updated == 1
        assert (
            await apply_conversation_runtime_update(
                db,
                machine_id=machine_id,
                tool_id="codex",
                session_id=session_id,
                runtime_state="ended",
                runtime_privilege="administrator",
                timestamp=(base + timedelta(minutes=5)).isoformat(),
            )
        ).updated == 1
        assert (
            await apply_conversation_runtime_update(
                db,
                machine_id=machine_id,
                tool_id="codex",
                session_id=session_id,
                runtime_state="running",
                runtime_privilege="administrator",
                timestamp=(base + timedelta(minutes=2)).isoformat(),
            )
        ).ignored == 1
        assert (
            await apply_conversation_runtime_update(
                db,
                machine_id=machine_id,
                tool_id="codex",
                session_id=session_id,
                runtime_state="running",
                runtime_privilege="administrator",
                timestamp=(base + timedelta(minutes=8)).isoformat(),
            )
        ).updated == 1

        row = (
            await db.execute(
                select(ConversationRuntime).where(
                    ConversationRuntime.machine_id == machine_id,
                    ConversationRuntime.tool_id == "codex",
                    ConversationRuntime.session_id == session_id,
                )
            )
        ).scalar_one()
        assert row.state == "running"
        assert row.privilege == "administrator"
        assert row.observed_at == base + timedelta(minutes=8)

        document = Document(
            tool_id="codex",
            machine_id=machine_id,
            relative_path=f"sessions/2026/09/08/rollout-{session_id}.jsonl",
            category="conversation",
            content_type="jsonl",
            title="Runtime lookup",
            content_hash=uuid.uuid4().hex,
            file_size_bytes=1,
            metadata_={"session_id": session_id, "thread_id": session_id},
        )
        db.add(document)
        await db.flush()

        assert await conversation_runtime_for_document(db, document) is row
