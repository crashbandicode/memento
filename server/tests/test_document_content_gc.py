from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import settings
from server.db.models import Base, Document, DocumentContentGcCandidate, Tool
from server.services.document_content_gc import (
    collect_unreferenced_document_content,
)


TEST_DATABASE_URL = os.environ.get("MEMENTO_CANVAS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL Canvas test database is not configured",
)


class _Paginator:
    def __init__(self, store: "_ObjectStore") -> None:
        self.store = store

    def paginate(self, *, Bucket: str, Prefix: str):
        del Bucket
        return [{"Contents": [{"Key": key} for key in self.store.keys if key.startswith(Prefix)]}]


class _ObjectStore:
    def __init__(self, keys: list[str]) -> None:
        self.keys = set(keys)
        self.deleted: list[str] = []

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        del Bucket
        self.deleted.append(Key)
        self.keys.discard(Key)


@pytest_asyncio.fixture
async def session_factory():
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        # Model metadata includes pgvector and trigram indexes.
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(delete(DocumentContentGcCandidate))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _reference_key(session, key: str) -> None:
    tool_id = f"gc-{uuid4().hex[:16]}"
    document_id = uuid4()
    session.add(Tool(id=tool_id, display_name="GC test"))
    session.add(
        Document(
            id=document_id,
            tool_id=tool_id,
            relative_path=f"gc/{document_id}.txt",
            category="memory",
            content_type="text",
            content_s3_key=key,
            content_object_sha256="a" * 64,
            content_object_size_bytes=1,
            content_object_verified_at=datetime.now(timezone.utc),
            content_hash="b" * 64,
            file_size_bytes=1,
            metadata_={},
        )
    )
    await session.commit()


def _key() -> str:
    return f"document-content/v1/{uuid4()}/{'c' * 64}"


@pytest.mark.asyncio
async def test_gc_marks_first_observed_unreferenced_key(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "document_content_gc_grace_hours", 48)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    key = _key()
    store = _ObjectStore([key])

    async with session_factory() as session:
        counts = await collect_unreferenced_document_content(
            session, s3_client=store, now=now
        )

    assert counts == {"marked": 1, "unmarked": 0, "deleted": 0}
    async with session_factory() as session:
        candidate = await session.get(DocumentContentGcCandidate, key)
        assert candidate is not None
        assert candidate.first_unreferenced_at == now


@pytest.mark.asyncio
async def test_gc_unmarks_key_referenced_again(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "document_content_gc_grace_hours", 48)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    key = _key()
    store = _ObjectStore([key])
    async with session_factory() as session:
        await collect_unreferenced_document_content(session, s3_client=store, now=now)
    async with session_factory() as session:
        await _reference_key(session, key)
    async with session_factory() as session:
        counts = await collect_unreferenced_document_content(
            session,
            s3_client=store,
            now=now + timedelta(hours=1),
        )

    assert counts == {"marked": 0, "unmarked": 1, "deleted": 0}
    assert store.deleted == []
    async with session_factory() as session:
        assert await session.get(DocumentContentGcCandidate, key) is None


@pytest.mark.asyncio
async def test_gc_waits_for_grace_then_deletes_orphan(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "document_content_gc_grace_hours", 48)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    key = _key()
    store = _ObjectStore([key])
    async with session_factory() as session:
        await collect_unreferenced_document_content(session, s3_client=store, now=now)
    async with session_factory() as session:
        before_grace = await collect_unreferenced_document_content(
            session,
            s3_client=store,
            now=now + timedelta(hours=47, minutes=59),
        )
    async with session_factory() as session:
        at_grace = await collect_unreferenced_document_content(
            session,
            s3_client=store,
            now=now + timedelta(hours=48),
        )

    assert before_grace == {"marked": 0, "unmarked": 0, "deleted": 0}
    assert at_grace == {"marked": 0, "unmarked": 0, "deleted": 1}
    assert store.deleted == [key]
    async with session_factory() as session:
        assert await session.get(DocumentContentGcCandidate, key) is None


@pytest.mark.asyncio
async def test_gc_never_deletes_a_live_pointer_even_after_grace(
    session_factory,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "document_content_gc_grace_hours", 48)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    key = _key()
    store = _ObjectStore([key])
    async with session_factory() as session:
        session.add(
            DocumentContentGcCandidate(
                content_s3_key=key,
                first_unreferenced_at=now - timedelta(hours=49),
                last_seen_at=now - timedelta(hours=49),
            )
        )
        await session.commit()
    async with session_factory() as session:
        await _reference_key(session, key)
    async with session_factory() as session:
        counts = await collect_unreferenced_document_content(
            session, s3_client=store, now=now
        )

    assert counts == {"marked": 0, "unmarked": 1, "deleted": 0}
    assert store.deleted == []
