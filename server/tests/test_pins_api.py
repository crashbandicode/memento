"""PostgreSQL-backed coverage for personal conversation message pins."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.pins import (
    PinRequest,
    get_conversation_pins,
    get_pins,
    pin_message,
    unpin_message,
)
from server.db.models import (
    Base,
    ConversationMessage,
    Document,
    Machine,
    PinnedMessage,
    Tool,
    User,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL pin test database is not configured",
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


async def _seed_documents(session):
    if await session.get(Tool, "codex") is None:
        session.add(Tool(id="codex", display_name="Codex"))

    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    other_user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="pin-owner",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    other_machine = Machine(
        id=uuid4(),
        name="pin-other-owner",
        collector_token_hash=str(uuid4()),
        user_id=other_user.id,
    )
    owned_document = Document(
        id=uuid4(),
        tool_id="codex",
        machine_id=machine.id,
        relative_path=f"sessions/{uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Pinned owner thread",
        content_hash=uuid4().hex,
        file_size_bytes=1,
        metadata_={"session_id": str(uuid4())},
    )
    other_document = Document(
        id=uuid4(),
        tool_id="codex",
        machine_id=other_machine.id,
        relative_path=f"sessions/{uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Other user's thread",
        content_hash=uuid4().hex,
        file_size_bytes=1,
        metadata_={"session_id": str(uuid4())},
    )
    session.add_all([
        user,
        other_user,
        machine,
        other_machine,
        owned_document,
        other_document,
    ])
    await session.flush()
    timestamp = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    owned_message = ConversationMessage(
        document_id=owned_document.id,
        line_number=7,
        role="assistant",
        content="A pinned response that stays inside the owner's machine scope.",
        timestamp=timestamp,
    )
    other_message = ConversationMessage(
        document_id=other_document.id,
        line_number=3,
        role="assistant",
        content="A response owned by another user.",
        timestamp=timestamp,
    )
    session.add_all([owned_message, other_message])
    await session.commit()
    return user, other_user, owned_document, other_document, owned_message, other_message


@requires_postgres
@pytest.mark.asyncio
async def test_pin_repin_unpin_and_lists_are_idempotent(session_factory) -> None:
    async with session_factory() as session:
        user, _other_user, document, _other_document, message, _other_message = (
            await _seed_documents(session)
        )

        first = await pin_message(
            document.id,
            message.id,
            PinRequest(note="First note"),
            db=session,
            user=user,
        )
        repeated = await pin_message(
            document.id,
            message.id,
            PinRequest(note="Updated note"),
            db=session,
            user=user,
        )

        assert repeated["id"] == first["id"]
        assert repeated["note"] == "Updated note"
        stored = (
            await session.execute(
                select(PinnedMessage).where(PinnedMessage.message_id == message.id)
            )
        ).scalars().all()
        assert len(stored) == 1

        thread = await get_conversation_pins(document.id, db=session, user=user)
        assert len(thread["pins"]) == 1
        assert thread["pins"][0]["message"] == {
            "id": message.id,
            "line_number": 7,
            "role": "assistant",
            "snippet": "A pinned response that stays inside the owner's machine scope.",
            "timestamp": "2026-08-25T12:00:00+00:00",
        }

        global_pins = await get_pins(limit=10, offset=0, db=session, user=user)
        assert global_pins["has_more"] is False
        assert len(global_pins["pins"]) == 1
        global_pin = global_pins["pins"][0]
        assert global_pin["document"] == {
            "id": str(document.id),
            "title": "Pinned owner thread",
            "tool_id": "codex",
        }
        assert global_pin["conversation_ref"] == str(document.id)
        assert global_pin["note"] == "Updated note"

        assert await unpin_message(document.id, message.id, db=session, user=user) == {"ok": True}
        assert await unpin_message(document.id, message.id, db=session, user=user) == {"ok": True}
        assert (await get_conversation_pins(document.id, db=session, user=user))["pins"] == []


@requires_postgres
@pytest.mark.asyncio
async def test_pin_endpoints_reject_other_users_machine_messages(session_factory) -> None:
    async with session_factory() as session:
        user, other_user, document, other_document, message, other_message = (
            await _seed_documents(session)
        )

        await pin_message(
            other_document.id,
            other_message.id,
            PinRequest(note="Private to the other user"),
            db=session,
            user=other_user,
        )

        with pytest.raises(HTTPException) as create_denied:
            await pin_message(
                other_document.id,
                other_message.id,
                PinRequest(),
                db=session,
                user=user,
            )
        assert create_denied.value.status_code == 404

        with pytest.raises(HTTPException) as list_denied:
            await get_conversation_pins(other_document.id, db=session, user=user)
        assert list_denied.value.status_code == 404

        with pytest.raises(HTTPException) as delete_denied:
            await unpin_message(other_document.id, other_message.id, db=session, user=user)
        assert delete_denied.value.status_code == 404

        assert (await get_pins(limit=10, offset=0, db=session, user=user))["pins"] == []
        await pin_message(document.id, message.id, PinRequest(), db=session, user=user)
        visible = await get_pins(limit=10, offset=0, db=session, user=user)
        assert [pin["document_id"] for pin in visible["pins"]] == [str(document.id)]
