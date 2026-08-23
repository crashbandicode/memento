from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.dashboard import (
    _unarchived_conversation_filter,
    dashboard_source_statement,
    get_dashboard,
)
from server.api.projects import (
    _decode_timeline_cursor,
    _encode_timeline_cursor,
    project_timeline_page_statement,
    timeline_preview_statement,
)
from server.db.models import (
    Base,
    ConversationMessage,
    DashboardDocumentProjection,
    DashboardProjectionState,
    Document,
    DocumentDeliveryState,
    Machine,
    Project,
    Tool,
    User,
)
from server.services.dashboard_projection import (
    DASHBOARD_PROJECTION_VERSION,
    backfill_dashboard_document_projections,
    dashboard_backfill_documents_statement,
    dashboard_projection_values,
    dashboard_projection_version_upgrade_statement,
    document_is_archived,
    refresh_dashboard_document_projection,
    upgrade_dashboard_document_projections,
)
from server.services.document_delivery import attach_document_delivery
from server.services.ingest_service import _conversation_event_changes
from server.services.conversation_read_model import (
    backfill_conversation_read_models,
    conversation_backfill_documents_statement,
    refresh_conversation_read_model,
    refresh_conversation_read_model_in_batches,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)


def test_timeline_session_page_is_sql_bounded_and_keyset_capable() -> None:
    project_id = uuid.uuid4()
    offset_statement = project_timeline_page_statement(
        project_id,
        category=None,
        machine_ids=None,
        order="desc",
        limit=25,
        offset=50,
    )
    offset_sql = str(
        offset_statement.compile(dialect=postgresql.dialect())
    ).upper()
    assert "GROUP BY" in offset_sql
    assert " LIMIT " in offset_sql
    assert " OFFSET " in offset_sql
    assert "DOCUMENTS.CONTENT" not in offset_sql
    assert "DOCUMENTS.RENDERED_HTML" not in offset_sql
    assert "DOCUMENT_DELIVERY_STATE" in offset_sql

    cursor_statement = project_timeline_page_statement(
        project_id,
        category=None,
        machine_ids=None,
        order="desc",
        limit=25,
        cursor_timestamp=datetime(2026, 8, 7, tzinfo=UTC),
        cursor_sort_key="session-key",
    )
    cursor_sql = str(
        cursor_statement.compile(dialect=postgresql.dialect())
    ).upper()
    assert "TIMELINE_GROUPS.TIMESTAMP <" in cursor_sql
    assert "TIMELINE_GROUPS.SORT_KEY <" in cursor_sql
    assert " OFFSET " not in cursor_sql


def test_timeline_preview_query_caps_rows_per_document_in_database() -> None:
    statement = timeline_preview_statement(
        {uuid.uuid4(), uuid.uuid4()},
        preview_limit=4,
    )
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "ROW_NUMBER() OVER (PARTITION BY" in sql
    assert "PREVIEW_RANK <=" in sql
    assert "ORDER BY RANKED_TIMELINE_PREVIEWS.DOCUMENT_ID" in sql
    assert " OFFSET " not in sql


def test_projection_backfills_keyset_only_lightweight_document_columns() -> None:
    after_id = uuid.uuid4()
    conversation_sql = str(
        conversation_backfill_documents_statement(
            after_id=after_id,
            batch_size=25,
        ).compile(dialect=postgresql.dialect())
    ).upper()
    dashboard_sql = str(
        dashboard_backfill_documents_statement(
            after_id=after_id,
        ).compile(dialect=postgresql.dialect())
    ).upper()

    for sql in (conversation_sql, dashboard_sql):
        assert "DOCUMENTS.ID >" in sql
        assert " LIMIT " in sql
        assert "DOCUMENTS.CONTENT," not in sql
        assert "DOCUMENTS.CONTENT_TSV" not in sql
        assert "DOCUMENTS.RENDERED_HTML" not in sql
        assert "DOCUMENTS.AI_SUMMARY" not in sql


def test_timeline_cursor_round_trip_preserves_ordering_tuple() -> None:
    row = SimpleNamespace(
        timestamp=datetime(2026, 8, 7, 17, 30, tzinfo=UTC),
        sort_key="thread-123",
    )
    encoded = _encode_timeline_cursor(row, "desc")
    timestamp, sort_key = _decode_timeline_cursor(encoded, order="desc")
    assert timestamp == row.timestamp
    assert sort_key == row.sort_key


def test_steady_state_dashboard_source_is_projection_only() -> None:
    sql = str(
        dashboard_source_statement(include_legacy=False).compile(
            dialect=postgresql.dialect()
        )
    ).upper()
    assert "FROM DASHBOARD_DOCUMENT_PROJECTIONS" in sql
    assert "FROM DOCUMENTS" not in sql
    assert "CONVERSATION_MESSAGES" not in sql
    assert "LENGTH(" not in sql


def test_legacy_dashboard_source_uses_current_delivery_projection() -> None:
    sql = str(
        dashboard_source_statement(include_legacy=True).compile(
            dialect=postgresql.dialect()
        )
    ).upper()
    assert "DOCUMENT_DELIVERY_STATE" in sql
    assert "DASHBOARD_DOCUMENT_PROJECTIONS" in sql


def test_tool_only_delta_invalidates_only_related_resources() -> None:
    changes = _conversation_event_changes(
        mode="delta",
        search_text="",
        title_changed=False,
        interactions_changed=False,
    )
    assert "dashboard" in changes
    assert "project" in changes
    assert "conversation.messages" in changes
    assert "conversation.prompts" not in changes
    assert "conversation.search" not in changes
    assert "conversation.pending_interactions" not in changes


def test_dashboard_projection_reads_volatile_delivery_state() -> None:
    old_time = datetime(2026, 8, 7, 10, tzinfo=UTC)
    new_time = old_time + timedelta(minutes=5)
    document = Document(
        id=uuid.uuid4(),
        tool_id="cursor",
        project_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
        relative_path="sessions/thread.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Thread",
        content_hash="a" * 64,
        file_size_bytes=10,
        metadata_={"session_id": "old-thread"},
        synced_at=old_time,
        source_modified_at=old_time,
        activity_at=old_time,
    )
    delivery = DocumentDeliveryState(
        document_id=document.id,
        project_id=document.project_id,
        revision_hash="b" * 64,
        file_size_bytes=25,
        delivery_metadata={"session_id": "new-thread"},
        synced_at=new_time,
        source_modified_at=new_time,
        activity_at=new_time,
    )
    attach_document_delivery(document, delivery, runtime_only=True)

    values = dashboard_projection_values(document, None)

    assert values["file_size_bytes"] == 25
    assert values["session_id"] == "new-thread"
    assert values["synced_at"] == new_time
    assert values["source_modified_at"] == new_time
    assert values["activity_at"] == new_time
    assert document.file_size_bytes == 10
    assert document.metadata_ == {"session_id": "old-thread"}

    document.category = "state"
    document.visibility = "public"
    replaced = dashboard_projection_values(document, None)
    assert replaced["category"] == "state"
    assert replaced["visibility"] == "public"
    assert replaced["is_subagent"] is False
    assert replaced["is_archived"] is False


def test_document_is_archived_reads_collector_metadata_key() -> None:
    assert document_is_archived({"archived": True}) is True
    assert document_is_archived({"archived": False}) is False
    assert document_is_archived({"archived": "true"}) is True
    assert document_is_archived({"archived": 1}) is True
    assert document_is_archived({"session_id": "thread"}) is False
    assert document_is_archived(None) is False


def test_dashboard_projection_reads_archived_metadata() -> None:
    document = Document(
        id=uuid.uuid4(),
        tool_id="codex",
        project_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
        relative_path="archived_sessions/thread.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Archived thread",
        content_hash="a" * 64,
        file_size_bytes=10,
        metadata_={"session_id": "thread", "archived": True},
        synced_at=datetime(2026, 8, 7, 10, tzinfo=UTC),
    )

    values = dashboard_projection_values(document, None)

    assert values["is_archived"] is True
    assert values["projection_version"] == DASHBOARD_PROJECTION_VERSION

    document.metadata_ = {"session_id": "thread", "archived": False}
    assert dashboard_projection_values(document, None)["is_archived"] is False


def test_dashboard_conversation_lists_exclude_archived() -> None:
    source = dashboard_source_statement(include_legacy=False).subquery(
        "dashboard_source"
    )
    sql = str(
        select(source.c.id).where(
            _unarchived_conversation_filter(source)
        ).compile(dialect=postgresql.dialect())
    ).upper()
    assert "IS_ARCHIVED" in sql
    assert "FALSE" in sql


def test_legacy_dashboard_source_projects_archived_from_metadata() -> None:
    sql = str(
        dashboard_source_statement(include_legacy=True).compile(
            dialect=postgresql.dialect()
        )
    ).upper()
    assert "ARCHIVED" in sql
    assert "IS_ARCHIVED" in sql
    assert "DOCUMENT_DELIVERY_STATE" in sql
    assert "PROJECTION_VERSION" in sql


def test_dashboard_archived_upgrade_is_keyset_and_metadata_only() -> None:
    sql = str(
        dashboard_projection_version_upgrade_statement(
            after_id=uuid.uuid4(),
        ).compile(dialect=postgresql.dialect())
    ).upper()
    assert "DASHBOARD_DOCUMENT_PROJECTIONS" in sql
    assert "PROJECTION_VERSION <" in sql
    assert " LIMIT " in sql
    assert "DOCUMENT_ID >" in sql
    assert "CONVERSATION_MESSAGES" not in sql
    assert "DOCUMENTS.CONTENT," not in sql
    assert "DOCUMENTS.CONTENT_TSV" not in sql
    assert "DOCUMENTS.RENDERED_HTML" not in sql
    assert "DOCUMENTS.AI_SUMMARY" not in sql


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


async def _document(
    session,
    *,
    project: Project,
    machine: Machine,
    category: str = "conversation",
    metadata: dict | None = None,
    title: str = "Projection test",
) -> Document:
    document = Document(
        id=uuid.uuid4(),
        tool_id="cursor",
        project_id=project.id,
        machine_id=machine.id,
        relative_path=f"sessions/{uuid.uuid4()}.jsonl",
        category=category,
        content_type="jsonl" if category == "conversation" else "markdown",
        title=title,
        content_hash=uuid.uuid4().hex,
        file_size_bytes=10,
        metadata_=metadata or {"session_id": str(uuid.uuid4())},
        synced_at=datetime.now(UTC),
    )
    session.add(document)
    await session.flush()
    return document


@requires_postgres
@pytest.mark.asyncio
async def test_conversation_read_backfill_batches_message_rows(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="read-backfill-test",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="Cursor"))
        project = Project(
            id=uuid.uuid4(),
            slug=f"read-backfill-{uuid.uuid4()}",
            title="Read backfill",
            tool_id="cursor",
        )
        session.add_all([user, machine, project])
        await session.flush()
        document = await _document(
            session,
            project=project,
            machine=machine,
        )
        session.add_all([
            ConversationMessage(
                document_id=document.id,
                line_number=line,
                role="user" if line % 2 else "assistant",
                message_type="message",
                content=f"message {line}",
                timestamp=datetime.now(UTC) + timedelta(seconds=line),
            )
            for line in range(1, 6)
        ])
        await session.flush()

        statements: list[str] = []
        bind = session_factory.kw["bind"]

        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            projection = await refresh_conversation_read_model_in_batches(
                session,
                document,
                batch_size=2,
            )
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)

        assert projection.message_count == 5
        assert projection.projected_through_line == 5
        message_reads = [
            statement
            for statement in statements
            if statement.lstrip().startswith("select")
            and "from conversation_messages" in statement
            and "order by conversation_messages.line_number" in statement
        ]
        assert len(message_reads) == 3
        assert all(" limit " in statement for statement in message_reads)
        result = await backfill_conversation_read_models(
            session,
            [document.id],
            message_batch_size=2,
        )
        assert result == {"documents": 1, "created_or_updated": 1}
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_preview_result_rows_are_independent_of_transcript_length(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="preview-test",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        tool = await session.get(Tool, "cursor")
        if tool is None:
            session.add(Tool(id="cursor", display_name="Cursor"))
        project = Project(
            id=uuid.uuid4(),
            slug=f"preview-{uuid.uuid4()}",
            title="Preview",
            tool_id="cursor",
        )
        session.add_all([user, machine, project])
        await session.flush()
        documents = [
            await _document(session, project=project, machine=machine)
            for _ in range(3)
        ]
        first_page = (
            await session.execute(
                project_timeline_page_statement(
                    project.id,
                    category=None,
                    machine_ids=None,
                    order="desc",
                    limit=2,
                )
            )
        ).all()
        assert len(first_page) == 2
        second_page = (
            await session.execute(
                project_timeline_page_statement(
                    project.id,
                    category=None,
                    machine_ids=None,
                    order="desc",
                    limit=2,
                    cursor_timestamp=first_page[-1].timestamp,
                    cursor_sort_key=first_page[-1].sort_key,
                )
            )
        ).all()
        assert len(second_page) == 1
        assert {row.group_key for row in first_page}.isdisjoint(
            {row.group_key for row in second_page}
        )
        for document in documents:
            session.add_all([
                ConversationMessage(
                    document_id=document.id,
                    line_number=line,
                    role="user" if line % 2 else "assistant",
                    message_type="message",
                    content=f"message {line}",
                )
                for line in range(1, 501)
            ])
        await session.flush()

        rows = (
            await session.execute(
                timeline_preview_statement(
                    {document.id for document in documents},
                    preview_limit=4,
                )
            )
        ).all()
        assert len(rows) == len(documents) * 4
        assert {
            document.id: sum(row.document_id == document.id for row in rows)
            for document in documents
        } == {document.id: 4 for document in documents}
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_dashboard_projection_delta_is_idempotent_and_bounded(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="dashboard-test",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        tool = await session.get(Tool, "cursor")
        if tool is None:
            session.add(Tool(id="cursor", display_name="Cursor"))
        project = Project(
            id=uuid.uuid4(),
            slug=f"dashboard-{uuid.uuid4()}",
            title="Dashboard",
            tool_id="cursor",
        )
        session.add_all([user, machine, project])
        await session.flush()
        document = await _document(
            session,
            project=project,
            machine=machine,
        )
        session.add_all([
            ConversationMessage(
                document_id=document.id,
                line_number=line,
                role="user" if line % 2 else "assistant",
                message_type="message",
                content="x" * 20,
                timestamp=datetime.now(UTC) + timedelta(seconds=line),
            )
            for line in range(1, 101)
        ])
        await session.flush()
        await refresh_conversation_read_model(
            session,
            document,
            mode="full",
            force_full=True,
        )
        await refresh_dashboard_document_projection(session, document)

        tool_row = ConversationMessage(
            document_id=document.id,
            line_number=101,
            role="tool",
            message_type="tool",
            content="large tool payload",
        )
        session.add(tool_row)
        await session.flush()
        statements: list[str] = []
        bind = session_factory.kw["bind"]

        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            await refresh_conversation_read_model(
                session,
                document,
                mode="delta",
            )
            await refresh_dashboard_document_projection(session, document)
            await refresh_conversation_read_model(
                session,
                document,
                mode="delta",
            )
            await refresh_dashboard_document_projection(session, document)
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)

        projection = await session.get(
            DashboardDocumentProjection,
            document.id,
        )
        assert projection is not None
        assert projection.message_count == 101
        assert projection.user_message_count == 50
        assert projection.assistant_message_count == 50
        assert projection.human_character_count == 2_000
        assert not any("sum(length(" in statement for statement in statements)
        message_reads = [
            statement
            for statement in statements
            if "from conversation_messages" in statement
            and statement.lstrip().startswith("select")
        ]
        assert message_reads
        assert all("line_number >" in statement for statement in message_reads)
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_dashboard_refresh_does_not_scan_documents_or_messages(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="refresh-test",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="Cursor"))
        project = Project(
            id=uuid.uuid4(),
            slug=f"refresh-{uuid.uuid4()}",
            title="Refresh",
            tool_id="cursor",
        )
        session.add_all([user, machine, project])
        await session.flush()
        document = await _document(
            session,
            project=project,
            machine=machine,
        )
        session.add_all([
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                role="user",
                message_type="message",
                content="Question",
                timestamp=datetime.now(UTC),
            ),
            ConversationMessage(
                document_id=document.id,
                line_number=2,
                role="assistant",
                message_type="message",
                content="Answer",
                timestamp=datetime.now(UTC) + timedelta(seconds=1),
            ),
        ])
        await session.flush()
        await refresh_conversation_read_model(
            session,
            document,
            mode="full",
            force_full=True,
        )
        await refresh_dashboard_document_projection(session, document)
        state = await session.get(DashboardProjectionState, 1)
        if state is None:
            session.add(DashboardProjectionState(
                id=1,
                projection_version=DASHBOARD_PROJECTION_VERSION,
                backfill_complete=True,
            ))
        else:
            state.backfill_complete = True
            state.projection_version = DASHBOARD_PROJECTION_VERSION
        await session.flush()

        statements: list[str] = []
        bind = session_factory.kw["bind"]

        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            payload = await get_dashboard(
                device_id=None,
                tz_offset=0,
                db=session,
                _user=user,
            )
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)

        assert payload["stats"]["total_documents"] == 1
        assert payload["recent_conversations"][0]["message_count"] == 2
        reads = [
            statement
            for statement in statements
            if statement.lstrip().startswith("select")
        ]
        assert not any("from conversation_messages" in item for item in reads)
        assert not any("from documents" in item for item in reads)
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_dashboard_backfill_replaces_rows_and_marks_completion(
    session_factory,
) -> None:
    async with session_factory() as session:
        await session.execute(delete(DashboardProjectionState))
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid.uuid4(),
            name="backfill-test",
            collector_token_hash=str(uuid.uuid4()),
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="Cursor"))
        project = Project(
            id=uuid.uuid4(),
            slug=f"backfill-{uuid.uuid4()}",
            title="Backfill",
            tool_id="cursor",
        )
        session.add_all([user, machine, project])
        await session.flush()
        document = await _document(
            session,
            project=project,
            machine=machine,
            category="plan",
        )

        first = await backfill_dashboard_document_projections(
            session,
            [document.id],
        )
        second = await backfill_dashboard_document_projections(
            session,
            [document.id],
        )
        assert first == {"documents": 1, "created_or_updated": 1}
        assert second == {"documents": 1, "created_or_updated": 0}

        await backfill_dashboard_document_projections(session)
        state = await session.get(DashboardProjectionState, 1)
        assert state is not None
        assert state.backfill_complete is True
        assert state.projection_version == DASHBOARD_PROJECTION_VERSION
        await session.rollback()


async def _seed_dashboard_scope(session, *, name: str):
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name=name,
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
    )
    if await session.get(Tool, "cursor") is None:
        session.add(Tool(id="cursor", display_name="Cursor"))
    project = Project(
        id=uuid.uuid4(),
        slug=f"{name}-{uuid.uuid4()}",
        title=name,
        tool_id="cursor",
    )
    session.add_all([user, machine, project])
    await session.flush()
    return user, machine, project


async def _mark_dashboard_complete(
    session,
    *,
    version: int = DASHBOARD_PROJECTION_VERSION,
    complete: bool = True,
) -> None:
    state = await session.get(DashboardProjectionState, 1)
    if state is None:
        session.add(DashboardProjectionState(
            id=1,
            projection_version=version,
            backfill_complete=complete,
        ))
    else:
        state.projection_version = version
        state.backfill_complete = complete
    await session.flush()


@requires_postgres
@pytest.mark.asyncio
async def test_projected_dashboard_excludes_archived_conversations(
    session_factory,
) -> None:
    async with session_factory() as session:
        user, machine, project = await _seed_dashboard_scope(
            session,
            name="projected-archived",
        )
        active = await _document(
            session,
            project=project,
            machine=machine,
            metadata={
                "session_id": "active",
                "archived": False,
                "pending_question_count": 1,
            },
            title="Active thread",
        )
        archived = await _document(
            session,
            project=project,
            machine=machine,
            metadata={
                "session_id": "archived",
                "archived": True,
                "pending_question_count": 2,
            },
            title="Archived thread",
        )
        await refresh_dashboard_document_projection(session, active)
        await refresh_dashboard_document_projection(session, archived)
        await _mark_dashboard_complete(session)

        payload = await get_dashboard(
            device_id=None,
            tz_offset=0,
            db=session,
            _user=user,
        )
        titles = [row["title"] for row in payload["recent_conversations"]]
        assert "Active thread" in titles
        assert "Archived thread" not in titles
        assert next(
            row["pending_question_count"]
            for row in payload["recent_conversations"]
            if row["title"] == "Active thread"
        ) == 1
        archived_projection = await session.get(
            DashboardDocumentProjection,
            archived.id,
        )
        assert archived_projection is not None
        assert archived_projection.is_archived is True
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_legacy_dashboard_excludes_archived_conversations(
    session_factory,
) -> None:
    async with session_factory() as session:
        user, machine, project = await _seed_dashboard_scope(
            session,
            name="legacy-archived",
        )
        active = await _document(
            session,
            project=project,
            machine=machine,
            metadata={"session_id": "active", "archived": False},
            title="Legacy active",
        )
        archived = await _document(
            session,
            project=project,
            machine=machine,
            metadata={
                "session_id": "archived",
                "archived": True,
                "pending_question_count": 3,
            },
            title="Legacy archived",
        )
        await refresh_dashboard_document_projection(session, archived)
        archived_projection = await session.get(
            DashboardDocumentProjection,
            archived.id,
        )
        assert archived_projection is not None
        archived_projection.projection_version = 1
        archived_projection.is_archived = False
        await _mark_dashboard_complete(session, version=1, complete=False)

        payload = await get_dashboard(
            device_id=None,
            tz_offset=0,
            db=session,
            _user=user,
        )
        titles = [row["title"] for row in payload["recent_conversations"]]
        assert "Legacy active" in titles
        assert "Legacy archived" not in titles
        assert await session.get(
            DashboardDocumentProjection,
            active.id,
        ) is None
        assert await session.get(
            DashboardDocumentProjection,
            archived.id,
        ) is archived_projection
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_dashboard_archived_upgrade_reads_metadata_not_transcripts(
    session_factory,
) -> None:
    async with session_factory() as session:
        user, machine, project = await _seed_dashboard_scope(
            session,
            name="upgrade-archived",
        )
        document = await _document(
            session,
            project=project,
            machine=machine,
            metadata={"session_id": "stale", "archived": False},
            title="Became archived",
        )
        session.add_all([
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                role="user",
                message_type="message",
                content="should not be read",
                timestamp=datetime.now(UTC),
            ),
        ])
        await session.flush()
        await refresh_dashboard_document_projection(session, document)
        projection = await session.get(DashboardDocumentProjection, document.id)
        assert projection is not None
        projection.projection_version = 1
        projection.is_archived = False
        document.metadata_ = {**document.metadata_, "archived": True}
        await _mark_dashboard_complete(session, version=1, complete=True)
        await session.flush()

        statements: list[str] = []
        bind = session_factory.kw["bind"]

        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            result = await upgrade_dashboard_document_projections(session)
            payload = await get_dashboard(
                device_id=None,
                tz_offset=0,
                db=session,
                _user=user,
            )
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)

        assert result["documents"] == 1
        assert result["created_or_updated"] == 1
        assert projection.is_archived is True
        assert projection.projection_version == DASHBOARD_PROJECTION_VERSION
        state = await session.get(DashboardProjectionState, 1)
        assert state is not None
        assert state.projection_version == DASHBOARD_PROJECTION_VERSION
        assert all(
            row["title"] != "Became archived"
            for row in payload["recent_conversations"]
        )
        assert not any("from conversation_messages" in item for item in statements)
        await session.rollback()


@requires_postgres
@pytest.mark.asyncio
async def test_version_upgrade_backfill_skips_conversation_reparse(
    session_factory,
) -> None:
    async with session_factory() as session:
        _user, machine, project = await _seed_dashboard_scope(
            session,
            name="upgrade-skip-reparse",
        )
        document = await _document(
            session,
            project=project,
            machine=machine,
            metadata={"session_id": "stale", "archived": True},
            title="Archived after version bump",
        )
        session.add(
            ConversationMessage(
                document_id=document.id,
                line_number=1,
                role="user",
                message_type="message",
                content="do not reparse",
                timestamp=datetime.now(UTC),
            )
        )
        await session.flush()
        await refresh_dashboard_document_projection(session, document)
        projection = await session.get(DashboardDocumentProjection, document.id)
        assert projection is not None
        projection.projection_version = 1
        projection.is_archived = False
        await _mark_dashboard_complete(session, version=1, complete=True)
        await session.flush()

        statements: list[str] = []
        bind = session_factory.kw["bind"]

        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            result = await backfill_dashboard_document_projections(session)
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)

        assert result["documents"] == 1
        assert projection.is_archived is True
        assert projection.projection_version == DASHBOARD_PROJECTION_VERSION
        state = await session.get(DashboardProjectionState, 1)
        assert state is not None
        assert state.backfill_complete is True
        assert state.projection_version == DASHBOARD_PROJECTION_VERSION
        assert not any("from conversation_messages" in item for item in statements)
        await session.rollback()
