from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import Base, Document, Machine, Tool, User
from server.scripts import null_document_content


TEST_DATABASE_URL = os.environ.get("MEMENTO_CANVAS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL Canvas test database is not configured",
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


@pytest.mark.asyncio
async def test_apply_refuses_to_null_when_pg_bytes_do_not_match_pointer(
    session_factory,
    monkeypatch,
) -> None:
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="null-content-test",
        collector_token_hash=uuid4().hex,
        user_id=user.id,
    )
    document_id = UUID("ffffffff-ffff-ffff-ffff-fffffffffffe")
    start_after = UUID("ffffffff-ffff-ffff-ffff-fffffffffffd")
    pointer_payload = b"object payload"
    async with session_factory() as session:
        await session.execute(
            delete(Document).where(
                Document.relative_path.like("null-content-test/%")
            )
        )
        await session.commit()
        if await session.get(Tool, "codex") is None:
            session.add(Tool(id="codex", display_name="Codex"))
        session.add_all(
            [
                user,
                machine,
                Document(
                    id=document_id,
                    tool_id="codex",
                    machine_id=machine.id,
                    relative_path=f"null-content-test/{document_id}.jsonl",
                    category="conversation",
                    content_type="jsonl",
                    content="different PostgreSQL bytes",
                    content_s3_key=f"document-content/v1/{document_id}/pointer",
                    content_object_sha256=hashlib.sha256(pointer_payload).hexdigest(),
                    content_object_size_bytes=len(pointer_payload),
                    content_object_verified_at=datetime.now(timezone.utc),
                    content_hash=uuid4().hex + uuid4().hex,
                    file_size_bytes=1,
                    metadata_={},
                ),
            ]
        )
        await session.commit()

    verified: list[tuple[str, str, int]] = []

    def verify_object(key: str, *, sha256: str, size_bytes: int) -> None:
        verified.append((key, sha256, size_bytes))

    monkeypatch.setattr(null_document_content, "async_session_factory", session_factory)
    monkeypatch.setattr(
        null_document_content,
        "verify_document_content_object",
        verify_object,
    )

    result = await null_document_content.run(
        apply=True,
        batch_size=10,
        start_after=start_after,
    )

    assert result["nulled"] == 0
    assert result["skipped_mismatch"] == 1
    assert result["skipped_unverified"] == 0
    assert verified == [
        (
            f"document-content/v1/{document_id}/pointer",
            hashlib.sha256(pointer_payload).hexdigest(),
            len(pointer_payload),
        )
    ]
    async with session_factory() as session:
        inline = await session.scalar(
            select(Document.content).where(Document.id == document_id)
        )
        assert inline == "different PostgreSQL bytes"
        await session.execute(delete(Document).where(Document.id == document_id))
        await session.execute(delete(Machine).where(Machine.id == machine.id))
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
