"""Dashboard message rollup: population, scoped lookup, and live parity."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
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
from server.services.dashboard_conversation_message_rollup import (
    dashboard_conversation_message_rollup_is_populated,
    dashboard_message_activity_from_rollup,
    refresh_dashboard_conversation_message_rollup,
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
        await connection.execute(text(
            "TRUNCATE dashboard_conversation_message_rollups, "
            "conversation_messages, documents RESTART IDENTITY CASCADE"
        ))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_scope(session, *, tool_id: str):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@x.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name=f"m-{uuid.uuid4()}",
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
    )
    project = Project(
        id=uuid.uuid4(),
        slug=f"p-{uuid.uuid4()}",
        title="P",
        tool_id=tool_id,
    )
    if await session.get(Tool, tool_id) is None:
        session.add(Tool(id=tool_id, display_name=tool_id))
    session.add_all([user, machine, project])
    await session.flush()
    return machine, project


async def _seed_document(
    session,
    *,
    tool_id: str,
    project: Project,
    machine_id: uuid.UUID | None,
) -> Document:
    document = Document(
        id=uuid.uuid4(),
        tool_id=tool_id,
        project_id=project.id,
        machine_id=machine_id,
        relative_path=f"s/{uuid.uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        title="T",
        content_hash=uuid.uuid4().hex,
        file_size_bytes=10,
        metadata_={"session_id": str(uuid.uuid4())},
        synced_at=datetime.now(UTC),
    )
    session.add(document)
    await session.flush()
    return document


async def _seed_messages(session, document: Document, messages: list[tuple[str, str]]):
    session.add_all([
        ConversationMessage(
            document_id=document.id,
            line_number=line_number,
            role=role,
            content=content,
        )
        for line_number, (role, content) in enumerate(messages)
    ])
    await session.flush()


async def _live_activity(session, document_ids: list[uuid.UUID]):
    if not document_ids:
        return {}
    query = (
        select(
            ConversationMessage.document_id,
            func.count().label("message_count"),
            func.count().filter(
                ConversationMessage.role == "user"
            ).label("user_message_count"),
            func.count().filter(
                ConversationMessage.role == "assistant"
            ).label("assistant_message_count"),
            func.coalesce(
                func.sum(func.length(ConversationMessage.content)).filter(
                    ConversationMessage.role.in_(("user", "assistant"))
                ),
                0,
            ).label("human_character_count"),
        )
        .where(ConversationMessage.document_id.in_(document_ids))
        .group_by(ConversationMessage.document_id)
    )
    return {
        document_id: (total, users, assistants, characters)
        for document_id, total, users, assistants, characters
        in (await session.execute(query)).all()
    }


async def _scoped_document_ids(session, machine_ids: list[uuid.UUID] | None):
    query = select(Document.id)
    if machine_ids is not None:
        query = query.where(Document.machine_id.in_(machine_ids))
    return list((await session.scalars(query)).all())


@requires_postgres
@pytest.mark.asyncio
async def test_dashboard_conversation_message_rollup_matches_live_scopes(
    session_factory,
):
    async with session_factory() as session:
        machine1, cursor_project = await _seed_scope(session, tool_id="cursor")
        machine2, claude_project = await _seed_scope(
            session,
            tool_id="claude_code",
        )
        cursor_document = await _seed_document(
            session,
            tool_id="cursor",
            project=cursor_project,
            machine_id=machine1.id,
        )
        claude_document = await _seed_document(
            session,
            tool_id="claude_code",
            project=claude_project,
            machine_id=machine2.id,
        )
        legacy_document = await _seed_document(
            session,
            tool_id="cursor",
            project=cursor_project,
            machine_id=None,
        )
        await _seed_messages(
            session,
            cursor_document,
            [("user", "hello"), ("assistant", "world"), ("tool", "payload")],
        )
        await _seed_messages(
            session,
            claude_document,
            [("assistant", "reply"), ("assistant", "again")],
        )
        await _seed_messages(
            session,
            legacy_document,
            [("user", "legacy")],
        )
        await session.commit()

        assert await dashboard_conversation_message_rollup_is_populated(
            session
        ) is False
        assert await refresh_dashboard_conversation_message_rollup(session) == 3
        assert await dashboard_conversation_message_rollup_is_populated(
            session
        ) is True

        # The endpoint scopes legacy ids before the rollup lookup. These cases
        # mirror owner/admin, individual user/device, multi-device, and no
        # accessible device without exposing another machine's document.
        for machine_ids in (
            None,
            [machine1.id],
            [machine2.id],
            [machine1.id, machine2.id],
            [],
        ):
            document_ids = await _scoped_document_ids(session, machine_ids)
            assert await dashboard_message_activity_from_rollup(
                session,
                document_ids=document_ids,
            ) == await _live_activity(session, document_ids)
