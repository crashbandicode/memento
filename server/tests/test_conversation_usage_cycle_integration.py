from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import (
    Base,
    ConversationMessage,
    ConversationUsageEvent,
    Document,
    DocumentDeliveryState,
    Machine,
    Tool,
    User,
)
from server.services.conversation_usage import (
    LAST_ACTIVITY_AT_METADATA_KEY,
    STARTED_AT_METADATA_KEY,
)
from server.services.conversation_usage_cycle import aggregate_usage_cycle
from server.scripts.backfill_conversation_token_usage import (
    TokenUsageUpdate,
    _apply_updates,
)
from server.services.conversation_parser import (
    AssistantIdentityState,
    AssistantUsageObservation,
)
from server.services.ingest_service import (
    _remove_replaced_usage,
    _upsert_assistant_usage_rows,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_USAGE_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL usage test database is not configured",
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


@requires_postgres
@pytest.mark.asyncio
async def test_real_postgres_cycle_groups_models_and_returns_threads(
    session_factory,
) -> None:
    started = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    last_activity = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="usage-test",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    documents = []
    async with session_factory() as session:
        session.add_all(
            [
                user,
                machine,
                Tool(id="codex", display_name="Codex"),
                Tool(id="claude_code", display_name="Claude Code"),
                Tool(id="cursor", display_name="Cursor"),
            ]
        )
        for index, tool_id in enumerate(("codex", "claude_code", "cursor")):
            document = Document(
                id=uuid4(),
                tool_id=tool_id,
                machine_id=machine.id,
                relative_path=f"sessions/{tool_id}-{uuid4()}.jsonl",
                category="conversation",
                content_type="jsonl",
                title=f"{tool_id} usage",
                content_hash=str(uuid4()).replace("-", ""),
                file_size_bytes=1,
                metadata_={"session_id": str(uuid4())},
                activity_at=started,
            )
            session.add(document)
            await session.flush()
            session.add(
                DocumentDeliveryState(
                    document_id=document.id,
                    revision_hash=document.content_hash,
                    file_size_bytes=1,
                    delivery_metadata={
                        **document.metadata_,
                        STARTED_AT_METADATA_KEY: started.isoformat(),
                        LAST_ACTIVITY_AT_METADATA_KEY: last_activity.isoformat(),
                    },
                    activity_at=started,
                    synced_at=started,
                )
            )
            session.add(
                ConversationMessage(
                    document_id=document.id,
                    line_number=1,
                    role="assistant",
                    message_type="assistant",
                    content="response",
                    metadata_={},
                    timestamp=started,
                )
            )
            documents.append(document)
        for index, (document, model, effort, values) in enumerate(
            (
                (
                    documents[0],
                    "gpt-5.6-sol",
                    "xhigh",
                    (100, 60, 40, 0, 20, 5, 120),
                ),
                (
                    documents[1],
                    "claude-opus-4-1",
                    "extended",
                    (600, 100, 200, 300, 40, 0, 640),
                ),
            )
        ):
            session.add(
                ConversationUsageEvent(
                    document_id=document.id,
                    machine_id=machine.id,
                    tool_id=document.tool_id,
                    source_id=f"event-{index}",
                    source=document.tool_id,
                    occurred_at=started,
                    model=model,
                    reasoning_effort=effort,
                    attribution_status="attributed",
                    input_tokens=values[0],
                    uncached_input_tokens=values[1],
                    cached_input_tokens=values[2],
                    cache_write_input_tokens=values[3],
                    output_tokens=values[4],
                    reasoning_output_tokens=values[5],
                    total_tokens=values[6],
                )
            )
        await session.commit()

        payload = await aggregate_usage_cycle(
            session,
            since=datetime(2026, 8, 1, tzinfo=timezone.utc),
            until=datetime(2026, 9, 1, tzinfo=timezone.utc),
            machine_ids={machine.id},
            include_threads=True,
        )

    assert payload["conversation_count"] == 3
    assert payload["attributed_conversation_count"] == 2
    assert payload["unattributed"]["cursor_null"] == 1
    assert len(payload["models"]) == 2
    assert payload["token_usage"]["cache_read_tokens"] == 240
    assert payload["token_usage"]["cache_write_tokens"] == 300
    assert len(payload["threads"]) == 3
    assert all(row["started_at"] == started.isoformat() for row in payload["threads"])
    assert all(
        row["last_activity_at"] == last_activity.isoformat()
        for row in payload["threads"]
    )


@requires_postgres
@pytest.mark.asyncio
async def test_backfill_upsert_is_idempotent_and_preserves_newer_live_total(
    session_factory,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=timezone.utc)
    current = datetime(2026, 8, 20, tzinfo=timezone.utc)
    tool_id = f"codex-{uuid4()}"
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="usage-backfill-test",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    document = Document(
        id=uuid4(),
        tool_id=tool_id,
        machine_id=machine.id,
        relative_path=f"sessions/{uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Usage backfill",
        content_hash=str(uuid4()).replace("-", ""),
        file_size_bytes=1,
        metadata_={},
    )
    async with session_factory() as session:
        session.add_all(
            [
                user,
                machine,
                Tool(id=tool_id, display_name=tool_id),
                document,
            ]
        )
        await session.flush()
        session.add(
            DocumentDeliveryState(
                document_id=document.id,
                revision_hash=document.content_hash,
                file_size_bytes=1,
                delivery_metadata={
                    "_assistant_token_usage": {
                        "input_tokens": 900,
                        "output_tokens": 100,
                        "total_tokens": 1_000,
                    },
                    STARTED_AT_METADATA_KEY: current.isoformat(),
                },
                activity_at=current,
                synced_at=current,
            )
        )
        await session.commit()

    update = TokenUsageUpdate(
        document_id=document.id,
        machine_id=machine.id,
        tool_id=tool_id,
        content_hash=document.content_hash,
        usage={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
        started_at=started.isoformat(),
        last_activity_at=started.isoformat(),
        observations=(
            AssistantUsageObservation(
                source_id="codex:event-1",
                timestamp=started.isoformat(),
                source="codex",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                service_tier="priority",
                attribution_status="attributed",
                token_usage={
                    "input_tokens": 90,
                    "output_tokens": 10,
                    "total_tokens": 100,
                },
            ),
        ),
    )
    connection = await asyncpg.connect(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        first = await _apply_updates(connection, [update])
        second = await _apply_updates(connection, [update])
        delivery = await connection.fetchval(
            """
            SELECT delivery_metadata
            FROM document_delivery_state
            WHERE document_id=$1
            """,
            document.id,
        )
        event_count = await connection.fetchval(
            """
            SELECT count(*)
            FROM conversation_usage_events
            WHERE document_id=$1
            """,
            document.id,
        )
    finally:
        await connection.close()

    if isinstance(delivery, str):
        delivery = json.loads(delivery)
    assert first == (1, 1, 1)
    assert second == (1, 0, 1)
    assert delivery["_assistant_token_usage"]["total_tokens"] == 1_000
    assert delivery[STARTED_AT_METADATA_KEY] == started.isoformat()
    assert event_count == 1


@requires_postgres
@pytest.mark.asyncio
async def test_claude_delta_replaces_prior_event_without_double_counting(
    session_factory,
) -> None:
    occurred_at = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    tool_id = f"claude-{uuid4()}"
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid4(),
        name="usage-replacement-test",
        collector_token_hash=str(uuid4()),
        user_id=user.id,
    )
    document = Document(
        id=uuid4(),
        tool_id=tool_id,
        machine_id=machine.id,
        relative_path=f"sessions/{uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Claude replacement",
        content_hash=str(uuid4()).replace("-", ""),
        file_size_bytes=1,
        metadata_={},
    )
    source_id = "claude_code:api-message-1"
    async with session_factory() as session:
        session.add_all(
            [
                user,
                machine,
                Tool(id=tool_id, display_name=tool_id),
                document,
                ConversationUsageEvent(
                    document_id=document.id,
                    machine_id=machine.id,
                    tool_id=tool_id,
                    source_id=source_id,
                    source="claude",
                    occurred_at=occurred_at,
                    model="claude-opus-4-1",
                    reasoning_effort="extended",
                    attribution_status="attributed",
                    input_tokens=10,
                    uncached_input_tokens=10,
                    cached_input_tokens=0,
                    cache_write_input_tokens=0,
                    output_tokens=2,
                    reasoning_output_tokens=0,
                    total_tokens=12,
                ),
            ]
        )
        await session.commit()

        identity = AssistantIdentityState(
            token_usage={
                "input_tokens": 30,
                "uncached_input_tokens": 30,
                "output_tokens": 7,
                "total_tokens": 37,
                "source": "claude",
            }
        )
        replacement = {
            "document_id": document.id,
            "machine_id": machine.id,
            "tool_id": tool_id,
            "source_id": source_id,
            "source": "claude",
            "occurred_at": occurred_at,
            "model": "claude-opus-4-1",
            "reasoning_effort": "extended",
            "service_tier": None,
            "attribution_status": "attributed",
            "input_tokens": 20,
            "uncached_input_tokens": 20,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
            "total_tokens": 25,
        }
        prior_usage = await _upsert_assistant_usage_rows(
            session,
            [replacement],
            detect_replacements=True,
        )
        _remove_replaced_usage(identity, prior_usage)
        await session.commit()
        stored = (
            await session.execute(
                select(ConversationUsageEvent).where(
                    ConversationUsageEvent.document_id == document.id,
                    ConversationUsageEvent.source_id == source_id,
                )
            )
        ).scalar_one()

    assert identity.token_usage["total_tokens"] == 25
    assert stored.input_tokens == 20
    assert stored.output_tokens == 5
    assert stored.total_tokens == 25
