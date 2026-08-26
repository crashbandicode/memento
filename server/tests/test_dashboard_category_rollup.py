"""Dashboard category rollup: population, scoping, and live-query parity."""

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
    DashboardDocumentProjection,
    Document,
    Machine,
    Project,
    Tool,
    User,
)
from server.services.dashboard_category_rollup import (
    dashboard_categories_from_rollup,
    dashboard_category_rollup_is_populated,
    refresh_dashboard_document_category_rollup,
)
from server.services.dashboard_projection import (
    refresh_dashboard_document_projection,
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
        # The rollup aggregates every dashboard projection, so keep this
        # test's source rows isolated even if its database is reused locally.
        await connection.execute(text(
            "TRUNCATE dashboard_document_category_rollups, "
            "dashboard_document_projections, documents RESTART IDENTITY CASCADE"
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
    return user, machine, project


async def _seed_document(
    session,
    *,
    tool_id: str,
    project: Project,
    machine_id: uuid.UUID | None,
    category: str,
) -> Document:
    document = Document(
        id=uuid.uuid4(),
        tool_id=tool_id,
        project_id=project.id,
        machine_id=machine_id,
        relative_path=f"s/{uuid.uuid4()}.jsonl",
        category=category,
        content_type="jsonl",
        title="T",
        content_hash=uuid.uuid4().hex,
        file_size_bytes=10,
        metadata_={"session_id": str(uuid.uuid4())},
        synced_at=datetime.now(UTC),
    )
    session.add(document)
    await session.flush()
    await refresh_dashboard_document_projection(session, document)
    return document


async def _live_categories(session, machine_ids: list[uuid.UUID] | None):
    query = select(
        DashboardDocumentProjection.tool_id,
        DashboardDocumentProjection.category,
        func.count().label("document_count"),
    )
    if machine_ids is not None:
        query = query.where(
            DashboardDocumentProjection.machine_id.in_(machine_ids)
        )
    query = query.group_by(
        DashboardDocumentProjection.tool_id,
        DashboardDocumentProjection.category,
    )
    categories: dict[str, dict[str, int]] = {}
    for tool_id, category, count in (await session.execute(query)).all():
        categories.setdefault(tool_id, {})[category] = int(count)
    return categories


@requires_postgres
@pytest.mark.asyncio
async def test_dashboard_category_rollup_matches_live_scopes(session_factory):
    async with session_factory() as session:
        _user1, machine1, cursor_project = await _seed_scope(
            session,
            tool_id="cursor",
        )
        _user2, machine2, claude_project = await _seed_scope(
            session,
            tool_id="claude_code",
        )
        await _seed_document(
            session,
            tool_id="cursor",
            project=cursor_project,
            machine_id=machine1.id,
            category="conversation",
        )
        await _seed_document(
            session,
            tool_id="cursor",
            project=cursor_project,
            machine_id=machine1.id,
            category="conversation",
        )
        await _seed_document(
            session,
            tool_id="cursor",
            project=cursor_project,
            machine_id=machine1.id,
            category="document",
        )
        await _seed_document(
            session,
            tool_id="claude_code",
            project=claude_project,
            machine_id=machine2.id,
            category="conversation",
        )
        await _seed_document(
            session,
            tool_id="claude_code",
            project=claude_project,
            machine_id=machine2.id,
            category="document",
        )
        # A legacy unassigned document is visible to owner/admin but must not
        # leak through a machine-scoped ordinary-user read.
        await _seed_document(
            session,
            tool_id="cursor",
            project=cursor_project,
            machine_id=None,
            category="memo",
        )
        await session.commit()

        assert await dashboard_category_rollup_is_populated(session) is False
        assert await refresh_dashboard_document_category_rollup(session) == 5
        assert await dashboard_category_rollup_is_populated(session) is True

        # ``None`` is owner/admin's unscoped read; lists model ordinary-user
        # machine scoping and a device group; [] represents no owned devices.
        for machine_ids in (
            None,
            [machine1.id],
            [machine2.id],
            [machine1.id, machine2.id],
            [],
        ):
            assert await dashboard_categories_from_rollup(
                session,
                machine_ids=machine_ids,
            ) == await _live_categories(session, machine_ids)
