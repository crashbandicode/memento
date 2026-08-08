from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import load_only

from server.db.models import Document
from server.db.session import TransactionalAsyncSession
from server.services import cache


DATABASE_URL = os.environ.get("MEMENTO_DELIVERY_TEST_DATABASE_URL")
if not DATABASE_URL and os.environ.get("MEMENTO_RUN_DELIVERY_INTEGRATION") == "1":
    DATABASE_URL = os.environ.get("MEMENTO_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set MEMENTO_DELIVERY_TEST_DATABASE_URL for PostgreSQL integration",
)


class _Redis:
    def __init__(self) -> None:
        self.increments: list[str] = []

    async def incr(self, key: str) -> int:
        self.increments.append(key)
        return len(self.increments)


@pytest.mark.asyncio
async def test_delivery_updates_are_hot_and_cache_publish_is_transactional(
    monkeypatch,
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    table = f"delivery_hot_{uuid.uuid4().hex}"
    redis = _Redis()
    monkeypatch.setattr(cache, "_client", redis)

    try:
        async with TransactionalAsyncSession(engine, expire_on_commit=False) as db:
            await db.execute(text(
                f"CREATE TEMP TABLE {table} ("
                "document_id UUID PRIMARY KEY, "
                "activity_at TIMESTAMPTZ, "
                "revision_hash TEXT NOT NULL, "
                "synced_at TIMESTAMPTZ NOT NULL, "
                "delivery_metadata JSONB NOT NULL"
                ") WITH (fillfactor = 70) ON COMMIT PRESERVE ROWS"
            ))
            await db.execute(text(
                f"CREATE INDEX {table}_activity ON {table} (activity_at DESC)"
            ))
            await db.execute(
                text(
                    f"INSERT INTO {table} VALUES "
                    "(:id, now(), 'r0', now(), '{}'::jsonb)"
                ),
                {"id": uuid.uuid4()},
            )
            await db.commit()

            relation_oid = await db.scalar(
                text("SELECT to_regclass(:name)::oid"),
                {"name": f"pg_temp.{table}"},
            )
            for revision in range(1, 21):
                await db.execute(text(
                    f"UPDATE {table} SET "
                    f"revision_hash = 'r{revision}', "
                    "synced_at = clock_timestamp(), "
                    f"delivery_metadata = jsonb_build_object('r', {revision})"
                ))
            cache.stage_cache_invalidation(db, "daily:integration")
            await db.rollback()
            assert redis.increments == []

            for revision in range(1, 21):
                await db.execute(text(
                    f"UPDATE {table} SET "
                    f"revision_hash = 'c{revision}', "
                    "synced_at = clock_timestamp(), "
                    f"delivery_metadata = jsonb_build_object('c', {revision})"
                ))
            cache.stage_cache_invalidation(db, "daily:integration")
            await db.commit()
            assert redis.increments == [
                "cache:generation:daily:integration"
            ]

            await db.execute(text("SELECT pg_stat_force_next_flush()"))
            await db.commit()
            hot_updates = await db.scalar(
                text("SELECT pg_stat_get_tuples_hot_updated(:oid)"),
                {"oid": relation_oid},
            )
            assert int(hot_updates or 0) >= 20
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_document_reads_hydrate_from_projection() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    document_id = uuid.uuid4()
    try:
        async with TransactionalAsyncSession(engine, expire_on_commit=False) as db:
            await db.execute(text(
                "CREATE TEMP TABLE documents ("
                "id UUID PRIMARY KEY, content_hash VARCHAR(64) NOT NULL, "
                "file_size_bytes BIGINT NOT NULL, metadata JSONB NOT NULL, "
                "source_modified_at TIMESTAMPTZ, activity_at TIMESTAMPTZ, "
                "synced_at TIMESTAMPTZ NOT NULL"
                ") ON COMMIT DROP"
            ))
            await db.execute(text(
                "CREATE TEMP TABLE document_delivery_state ("
                "document_id UUID PRIMARY KEY, project_id UUID, "
                "revision_hash VARCHAR(64) NOT NULL, "
                "file_size_bytes BIGINT NOT NULL, "
                "delivery_metadata JSONB NOT NULL, "
                "source_modified_at TIMESTAMPTZ, activity_at TIMESTAMPTZ, "
                "synced_at TIMESTAMPTZ NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL, "
                "updated_at TIMESTAMPTZ NOT NULL"
                ") ON COMMIT DROP"
            ))
            await db.execute(
                text(
                    "INSERT INTO documents VALUES "
                    "(:id, 'full', 100, '{}'::jsonb, now(), now(), now())"
                ),
                {"id": document_id},
            )
            await db.execute(
                text(
                    "INSERT INTO document_delivery_state VALUES ("
                    ":id, NULL, 'delta', 200, '{\"total_lines\": 9}'::jsonb, "
                    "now(), now(), now(), now(), now())"
                ),
                {"id": document_id},
            )

            statement = (
                select(Document)
                .options(load_only(
                    Document.id,
                    Document.content_hash,
                    Document.file_size_bytes,
                    Document.metadata_,
                    Document.source_modified_at,
                    Document.activity_at,
                    Document.synced_at,
                ))
                .where(Document.id == document_id)
                .with_for_update(of=Document)
            )
            document = (await db.execute(statement)).scalar_one()

            assert document.content_hash == "delta"
            assert document.file_size_bytes == 200
            assert document.metadata_["total_lines"] == 9
            assert inspect(document).modified is False
    finally:
        await engine.dispose()
