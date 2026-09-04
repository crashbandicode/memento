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
    tool_id: str = "claude_code",
    leading_system: bool = False,
) -> Document:
    relative_path = {
        "claude_code": f"projects/memento/{session_id}.jsonl",
        "codex": f"sessions/2026/09/04/rollout-{session_id}.jsonl",
        "cursor": f"projects/memento/agent-transcripts/{session_id}.jsonl",
    }[tool_id]
    metadata = {"session_id": str(session_id)}
    if tool_id == "codex":
        metadata["thread_id"] = str(session_id)
    document = Document(
        id=uuid4(),
        tool_id=tool_id,
        machine_id=machine_id,
        relative_path=relative_path,
        category="conversation",
        content_type="jsonl",
        title=title,
        content_hash=uuid4().hex,
        file_size_bytes=1,
        metadata_=metadata,
    )
    session.add(document)
    await session.flush()
    messages = []
    if leading_system:
        messages.append(
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                role="system",
                content="Session context.",
                metadata_={},
            )
        )
    messages.extend(
        [
            ConversationMessage(
                document_id=document.id,
                line_number=2 if leading_system else 1,
                role="user",
                content=first_user_content,
                metadata_={},
            ),
            ConversationMessage(
                document_id=document.id,
                line_number=3 if leading_system else 2,
                role="assistant",
                content="Acknowledged.",
                metadata_={},
            ),
        ]
    )
    session.add_all(messages)
    await session.flush()
    return document


async def _seed_owner(session):
    for tool_id, display_name in (
        ("claude_code", "Claude Code"),
        ("codex", "Codex"),
        ("cursor", "Cursor"),
    ):
        if await session.get(Tool, tool_id) is None:
            session.add(Tool(id=tool_id, display_name=display_name))
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
            title=str(predecessor_session_id),
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
        successor.metadata_ = {
            **successor.metadata_,
            "briefing_kind": "handoff",
            "briefing_session_id": str(predecessor_session_id),
            "handoff_chain_name": "memento-run-6",
        }
        await session.commit()

        payload = await get_conversation(predecessor.id, db=session, _user=user)

    assert payload["title"] == "memento-run-5"
    assert payload["handoff_successor"] == {
        "document_id": str(successor.id),
        "tool_id": "claude_code",
        "title": "memento-run-6",
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
        successor.metadata_ = {
            **successor.metadata_,
            "briefing_kind": "handoff",
            "briefing_session_id": str(predecessor_session_id),
            "handoff_chain_name": "memento-run-6",
        }
        await session.commit()

        payload = await get_conversation(successor.id, db=session, _user=user)

    assert payload["title"] == "memento-run-6"
    assert payload["handoff_predecessor"] == {
        "document_id": str(predecessor.id),
        "tool_id": "claude_code",
        "title": "memento-run-5",
        "canonical_url": f"/conversations/claude/{predecessor_session_id}",
    }
    assert "handoff_successor" not in payload


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_links_codex_handoff_in_both_directions(
    session_factory,
) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        predecessor_session_id = uuid4()
        predecessor = await _seed_thread(
            session,
            session_id=predecessor_session_id,
            title="Codex predecessor",
            first_user_content="Start the Codex work.",
            machine_id=machine.id,
            tool_id="codex",
            leading_system=True,
        )
        successor_session_id = uuid4()
        successor = await _seed_thread(
            session,
            session_id=successor_session_id,
            title="Codex successor",
            first_user_content=(
                f"MEMENTO-HANDOFF-FROM: {predecessor_session_id}\n"
                "Continue the Codex work."
            ),
            machine_id=machine.id,
            tool_id="codex",
            leading_system=True,
        )
        successor.metadata_ = {
            **successor.metadata_,
            "briefing_kind": "handoff",
            "briefing_session_id": str(predecessor_session_id),
        }
        await session.commit()

        predecessor_payload = await get_conversation(
            predecessor.id,
            db=session,
            _user=user,
        )
        successor_payload = await get_conversation(
            successor.id,
            db=session,
            _user=user,
        )

    assert predecessor_payload["handoff_successor"] == {
        "document_id": str(successor.id),
        "tool_id": "codex",
        "title": "Codex successor",
        "canonical_url": f"/conversations/codex/{successor_session_id}",
    }
    assert successor_payload["handoff_predecessor"] == {
        "document_id": str(predecessor.id),
        "tool_id": "codex",
        "title": "Codex predecessor",
        "canonical_url": f"/conversations/codex/{predecessor_session_id}",
    }


@requires_postgres
@pytest.mark.asyncio
async def test_detail_api_links_cross_engine_handoff_in_both_directions(
    session_factory,
) -> None:
    async with session_factory() as session:
        user, machine = await _seed_owner(session)
        predecessor_session_id = uuid4()
        predecessor = await _seed_thread(
            session,
            session_id=predecessor_session_id,
            title="Cursor architecture thread",
            first_user_content="Design the change.",
            machine_id=machine.id,
            tool_id="cursor",
        )
        successor_session_id = uuid4()
        successor = await _seed_thread(
            session,
            session_id=successor_session_id,
            title="Codex implementation thread",
            first_user_content=(
                f"MEMENTO-HANDOFF-FROM: {predecessor_session_id}\n"
                "Implement the handed-off design."
            ),
            machine_id=machine.id,
            tool_id="codex",
            leading_system=True,
        )
        successor.metadata_ = {
            **successor.metadata_,
            "briefing_kind": "handoff",
            "briefing_session_id": str(predecessor_session_id),
        }
        await session.commit()

        predecessor_payload = await get_conversation(
            predecessor.id,
            db=session,
            _user=user,
        )
        successor_payload = await get_conversation(
            successor.id,
            db=session,
            _user=user,
        )

    assert predecessor_payload["handoff_successor"] == {
        "document_id": str(successor.id),
        "tool_id": "codex",
        "title": "Codex implementation thread",
        "canonical_url": f"/conversations/codex/{successor_session_id}",
    }
    assert successor_payload["handoff_predecessor"] == {
        "document_id": str(predecessor.id),
        "tool_id": "cursor",
        "title": "Cursor architecture thread",
        "canonical_url": f"/conversations/cursor/{predecessor_session_id}",
    }


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
