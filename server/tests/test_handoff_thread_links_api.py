from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.conversations import get_conversation
from server.db.models import Base, ConversationMessage, Document, Machine, Tool, User


TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL handoff-link test database is not configured",
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


async def _seed_thread(
    session,
    *,
    session_id: UUID,
    title: str,
    first_user_content: str,
    machine_id: UUID,
) -> Document:
    document = Document(
        id=uuid4(),
        tool_id="claude_code",
        machine_id=machine_id,
        relative_path=f"projects/memento/{session_id}.jsonl",
        category="conversation",
        content_type="jsonl",
        title=title,
        content_hash=uuid4().hex,
        file_size_bytes=1,
        metadata_={"session_id": str(session_id)},
    )
    session.add(document)
    await session.flush()
    session.add_all(
        [
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                role="user",
                content=first_user_content,
                metadata_={},
            ),
            ConversationMessage(
                document_id=document.id,
                line_number=2,
                role="assistant",
                content="Acknowledged.",
                metadata_={},
            ),
        ]
    )
    await session.flush()
    return document


async def _seed_owner(session):
    if await session.get(Tool, "claude_code") is None:
        session.add(Tool(id="claude_code", display_name="Claude Code"))
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="handoff-links-owner",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    session.add_all([user, machine])
    await session.flush()
    return user, machine


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_links_predecessor_to_successor(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        predecessor_session_id = uuid4()
        predecessor = await _seed_thread(
            session,
            session_id=predecessor_session_id,
            title="Original implementation thread",
            first_user_content="Implement the original task.",
            machine_id=machine.id,
        )
        successor = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Continuation implementation thread",
            first_user_content=(
                f"MEMENTO-HANDOFF-FROM: {predecessor_session_id}\n"
                "Continue the implementation."
            ),
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(predecessor.id, db=session, _user=user)

    assert payload["handoff_successor"] == {
        "document_id": str(successor.id),
        "tool_id": "claude_code",
        "title": "Continuation implementation thread",
        "canonical_url": f"/conversations/claude/{successor.metadata_['session_id']}",
    }
    assert "handoff_predecessor" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_links_successor_to_predecessor(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        predecessor_session_id = uuid4()
        predecessor = await _seed_thread(
            session,
            session_id=predecessor_session_id,
            title="Previous thread",
            first_user_content="Start here.",
            machine_id=machine.id,
        )
        successor = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Current thread",
            first_user_content=(
                f"MEMENTO-HANDOFF-FROM: {predecessor_session_id}\n"
                "Continue here."
            ),
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(successor.id, db=session, _user=user)

    assert payload["handoff_predecessor"] == {
        "document_id": str(predecessor.id),
        "tool_id": "claude_code",
        "title": "Previous thread",
        "canonical_url": f"/conversations/claude/{predecessor_session_id}",
    }
    assert "handoff_successor" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_omits_handoff_fields_when_marker_is_absent(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        document = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Independent thread",
            first_user_content="No handoff marker is present.",
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(document.id, db=session, _user=user)

    assert "handoff_predecessor" not in payload
    assert "handoff_successor" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_ignores_malformed_handoff_uuid(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        document = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Malformed marker thread",
            first_user_content=(
                "MEMENTO-HANDOFF-FROM: not-a-uuid\n"
                "This must not create a link."
            ),
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(document.id, db=session, _user=user)

    assert "handoff_predecessor" not in payload
    assert "handoff_successor" not in payload
