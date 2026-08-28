from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.conversations import get_conversation
from server.db.models import (
    Base,
    ConversationMessage,
    Document,
    DocumentDeliveryState,
    Machine,
    Tool,
    User,
)


TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL tangent-link test database is not configured",
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
    activity_at: datetime | None = None,
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
        activity_at=activity_at,
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


async def _seed_delivery_state(
    session,
    document: Document,
    *,
    activity_at: datetime,
) -> None:
    session.add(
        DocumentDeliveryState(
            document_id=document.id,
            revision_hash=document.content_hash,
            file_size_bytes=document.file_size_bytes,
            delivery_metadata=document.metadata_,
            activity_at=activity_at,
            synced_at=activity_at,
        )
    )
    await session.flush()


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
        name="tangent-links-owner",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    session.add_all([user, machine])
    await session.flush()
    return user, machine


def _reference(document: Document, session_id: UUID) -> dict[str, str]:
    return {
        "document_id": str(document.id),
        "tool_id": "claude_code",
        "title": document.title,
        "canonical_url": f"/conversations/claude/{session_id}",
    }


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_links_tangent_to_parent(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        parent_session_id = uuid4()
        parent = await _seed_thread(
            session,
            session_id=parent_session_id,
            title="Original primary thread",
            first_user_content="Start here.",
            machine_id=machine.id,
        )
        tangent_session_id = uuid4()
        tangent = await _seed_thread(
            session,
            session_id=tangent_session_id,
            title="Independent tangent",
            first_user_content=(
                f"MEMENTO-TANGENT-FROM: {parent_session_id}\n"
                "Explore the alternate approach."
            ),
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(tangent.id, db=session, _user=user)

    assert payload["tangent_parent"] == _reference(parent, parent_session_id)
    assert "tangent_branches" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_lists_all_tangent_branches_by_delivery_activity(
    session_factory,
) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        parent_session_id = uuid4()
        parent = await _seed_thread(
            session,
            session_id=parent_session_id,
            title="Primary thread",
            first_user_content="Start here.",
            machine_id=machine.id,
        )
        older = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Older tangent",
            first_user_content=f"MEMENTO-TANGENT-FROM: {parent_session_id}",
            machine_id=machine.id,
            activity_at=now + timedelta(days=2),
        )
        newer = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Newest delivery tangent",
            first_user_content=f"MEMENTO-TANGENT-FROM: {parent_session_id}",
            machine_id=machine.id,
            activity_at=now - timedelta(days=2),
        )
        middle = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Middle tangent",
            first_user_content=f"MEMENTO-TANGENT-FROM: {parent_session_id}",
            machine_id=machine.id,
            activity_at=now,
        )
        await _seed_delivery_state(session, older, activity_at=now)
        await _seed_delivery_state(session, newer, activity_at=now + timedelta(hours=2))
        await _seed_delivery_state(session, middle, activity_at=now + timedelta(hours=1))
        await session.commit()

        payload = await get_conversation(parent.id, db=session, _user=user)

    assert payload["tangent_branches"] == [
        _reference(newer, UUID(newer.metadata_["session_id"])),
        _reference(middle, UUID(middle.metadata_["session_id"])),
        _reference(older, UUID(older.metadata_["session_id"])),
    ]
    # `older` has the newest frozen documents.activity_at. It must still sort
    # last because the delivery projection is the source of activity ordering.
    assert older.activity_at > newer.activity_at


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_omits_tangent_fields_when_marker_is_absent(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        document = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Independent thread",
            first_user_content="No tangent marker is present.",
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(document.id, db=session, _user=user)

    assert "tangent_parent" not in payload
    assert "tangent_branches" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_ignores_malformed_tangent_uuid(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        document = await _seed_thread(
            session,
            session_id=uuid4(),
            title="Malformed tangent marker",
            first_user_content="MEMENTO-TANGENT-FROM: not-a-uuid\nNo link.",
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(document.id, db=session, _user=user)

    assert "tangent_parent" not in payload
    assert "tangent_branches" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_keeps_handoff_and_tangent_link_families(session_factory) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        handoff_parent_session_id = uuid4()
        handoff_parent = await _seed_thread(
            session,
            session_id=handoff_parent_session_id,
            title="Handoff predecessor",
            first_user_content="Start here.",
            machine_id=machine.id,
        )
        shared_session_id = uuid4()
        shared = await _seed_thread(
            session,
            session_id=shared_session_id,
            title="Handoff successor and tangent parent",
            first_user_content=(
                f"MEMENTO-HANDOFF-FROM: {handoff_parent_session_id}\n"
                "Continue the work."
            ),
            machine_id=machine.id,
        )
        tangent_session_id = uuid4()
        tangent = await _seed_thread(
            session,
            session_id=tangent_session_id,
            title="Branch from the successor",
            first_user_content=(
                f"MEMENTO-TANGENT-FROM: {shared_session_id}\n"
                "Investigate separately."
            ),
            machine_id=machine.id,
        )
        await session.commit()

        payload = await get_conversation(shared.id, db=session, _user=user)

    assert payload["handoff_predecessor"] == _reference(
        handoff_parent,
        handoff_parent_session_id,
    )
    assert payload["tangent_branches"] == [_reference(tangent, tangent_session_id)]
