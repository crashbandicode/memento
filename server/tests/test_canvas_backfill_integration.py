from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.conversations import _conversation_canvas_summaries
from server.db.models import (
    AgentControlCommand,
    Base,
    CanvasArtifact,
    CanvasArtifactBlob,
    CanvasArtifactReference,
    ConversationMessage,
    Document,
    Machine,
    Tool,
    User,
)
from server.services.canvas_artifact_store import (
    inventory_machine_canvases,
    normalized_path_hash,
    pending_machine_canvases,
    project_message_canvases,
    record_canvas_outcome,
    store_captured_canvas,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_CANVAS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL Canvas test database is not configured",
)

SOURCE = b"""
import { Card, Text } from "cursor/canvas";
export default function Report() { return <Card><Text>Safe</Text></Card>; }
"""
COMPILED = b"const Report=()=>React.createElement(Card,null);export default Report;"
RUNTIME = b"function mountCanvas(value){return value}export{mountCanvas};"
UPDATED_SOURCE = SOURCE.replace(b"Safe", b"Updated")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


async def _conversation(
    session,
    *,
    user: User,
    machine: Machine,
    tool_id: str,
    paths: list[str],
) -> tuple[Document, list[ConversationMessage]]:
    if await session.get(Tool, tool_id) is None:
        session.add(Tool(id=tool_id, display_name=tool_id))
        await session.flush()
    document = Document(
        id=uuid4(),
        tool_id=tool_id,
        machine_id=machine.id,
        relative_path=f"sessions/{uuid4()}.jsonl",
        category="conversation",
        content_type="jsonl",
        content_hash=uuid4().hex + uuid4().hex,
        file_size_bytes=1,
        metadata_={},
    )
    session.add(document)
    await session.flush()
    messages = [
        ConversationMessage(
            document_id=document.id,
            line_number=index,
            role="assistant",
            content=f"Canvas: [{Path.rsplit('/', 1)[-1]}]({Path})",
        )
        for index, Path in enumerate(paths, start=1)
    ]
    session.add_all(messages)
    await session.flush()
    return document, messages


@pytest.mark.asyncio
async def test_multi_artifact_backfill_is_owned_deduped_and_resumable(
    session_factory,
) -> None:
    async with session_factory() as session:
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
            name="source-a",
            collector_token_hash=uuid4().hex,
            user_id=user.id,
        )
        other_machine = Machine(
            id=uuid4(),
            name="source-b",
            collector_token_hash=uuid4().hex,
            user_id=other_user.id,
        )
        session.add_all([user, other_user, machine, other_machine])
        await session.flush()

        shared = "/home/me/.cursor/projects/ws/canvases/shared.canvas.tsx"
        missing = "/home/me/.cursor/projects/ws/canvases/missing.canvas.tsx"
        _doc, messages = await _conversation(
            session,
            user=user,
            machine=machine,
            tool_id="cursor",
            paths=[shared, shared, missing],
        )
        _other_doc, _other_messages = await _conversation(
            session,
            user=other_user,
            machine=other_machine,
            tool_id="claude_code",
            paths=[shared],
        )

        first_inventory = await inventory_machine_canvases(session, machine.id)
        second_inventory = await inventory_machine_canvases(session, machine.id)
        assert first_inventory == {"discovered": 3, "unsupported": 0}
        assert second_inventory == {"discovered": 0, "unsupported": 0}

        pending = await pending_machine_canvases(session, machine.id)
        assert len(pending) == 2
        shared_request = next(item for item in pending if item["path"] == shared)
        missing_request = next(item for item in pending if item["path"] == missing)
        assert len(shared_request["reference_ids"]) == 2

        metadata = {
            "reference_ids": shared_request["reference_ids"],
            "path_hash": shared_request["path_hash"],
            "name": "shared",
            "source_hash": _sha(SOURCE),
            "compiled_hash": _sha(COMPILED),
            "runtime_hash": _sha(RUNTIME),
            "render_mode": "interactive",
            "compiler_version": "test-v1",
            "runtime_sdk_version": "test-sdk",
        }
        artifact, status, linked = await store_captured_canvas(
            session,
            user=user,
            machine=machine,
            metadata=metadata,
            source=SOURCE,
            compiled=COMPILED,
            runtime=RUNTIME,
        )
        assert status == "renderable"
        assert linked == 2

        same_artifact, repeat_status, repeat_linked = await store_captured_canvas(
            session,
            user=user,
            machine=machine,
            metadata=metadata,
            source=SOURCE,
            compiled=COMPILED,
            runtime=RUNTIME,
        )
        assert same_artifact.id == artifact.id
        assert repeat_status == "already_current"
        assert repeat_linked == 2
        assert (
            await session.scalar(select(func.count()).select_from(CanvasArtifact))
        ) == 1
        assert (
            await session.scalar(select(func.count()).select_from(CanvasArtifactBlob))
        ) == 3

        await inventory_machine_canvases(session, other_machine.id)
        other_reference = (
            await session.execute(
                select(CanvasArtifactReference).where(
                    CanvasArtifactReference.machine_id == other_machine.id
                )
            )
        ).scalar_one()
        with pytest.raises(HTTPException, match="not found"):
            await store_captured_canvas(
                session,
                user=user,
                machine=machine,
                metadata={
                    **metadata,
                    "reference_ids": [str(other_reference.id)],
                    "path_hash": normalized_path_hash(shared),
                },
                source=SOURCE,
                compiled=COMPILED,
                runtime=RUNTIME,
            )

        # A missing source is terminal for only that path; the successful path
        # remains linked and renderable (partial failure does not roll it back).
        missing_ids = [UUID(value) for value in missing_request["reference_ids"]]
        missing_count = await record_canvas_outcome(
            session,
            machine_id=machine.id,
            reference_ids=missing_ids,
            path_hash=missing_request["path_hash"],
            status="missing",
            reason="missing",
        )
        assert missing_count == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CanvasArtifactReference)
                .where(CanvasArtifactReference.status == "already_current")
            )
        ) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CanvasArtifactReference)
                .where(CanvasArtifactReference.status == "missing")
            )
        ) == 1
        assert messages[0].id != messages[1].id
        await session.rollback()


@pytest.mark.asyncio
async def test_new_messages_project_canvas_references_without_inventory_scan(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="projected-source",
            collector_token_hash=uuid4().hex,
            user_id=user.id,
            capabilities={"control": {"commands": ["canvas.sync"]}},
        )
        session.add_all([user, machine])
        await session.flush()
        document, messages = await _conversation(
            session,
            user=user,
            machine=machine,
            tool_id="cursor",
            paths=["/home/me/.cursor/projects/ws/canvases/projected.canvas.tsx"],
        )

        assert await project_message_canvases(session, document, messages) == 1
        reference = (
            await session.execute(
                select(CanvasArtifactReference).where(
                    CanvasArtifactReference.document_id == document.id
                )
            )
        ).scalar_one()
        assert reference.status == "discovered"
        command = (
            await session.execute(
                select(AgentControlCommand).where(
                    AgentControlCommand.machine_id == machine.id,
                    AgentControlCommand.kind == "canvas.sync",
                )
            )
        ).scalar_one()
        assert command.document_id == document.id


@pytest.mark.asyncio
async def test_inventory_locks_messages_against_concurrent_ingest_replacement(
    session_factory,
) -> None:
    async with session_factory() as setup:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="racing-source",
            collector_token_hash=uuid4().hex,
            user_id=user.id,
        )
        setup.add_all([user, machine])
        await setup.flush()
        _document, messages = await _conversation(
            setup,
            user=user,
            machine=machine,
            tool_id="cursor",
            paths=["/home/me/.cursor/projects/ws/canvases/race.canvas.tsx"],
        )
        message_id = messages[0].id
        await setup.commit()

    async with session_factory() as inventory_session:
        async with inventory_session.begin():
            assert await inventory_machine_canvases(inventory_session, machine.id) == {
                "discovered": 1,
                "unsupported": 0,
            }
            async with session_factory() as ingest_session:
                await ingest_session.execute(text("SET LOCAL lock_timeout = '200ms'"))
                with pytest.raises(DBAPIError):
                    await ingest_session.execute(
                        delete(ConversationMessage).where(
                            ConversationMessage.id == message_id
                        )
                    )
                await ingest_session.rollback()


@pytest.mark.asyncio
async def test_captured_canvas_is_rechecked_and_updates_every_path_reference(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="source-refresh",
            collector_token_hash=uuid4().hex,
            user_id=user.id,
        )
        session.add_all([user, machine])
        await session.flush()
        path = "/home/me/.cursor/projects/ws/canvases/live.canvas.tsx"
        document, _messages = await _conversation(
            session,
            user=user,
            machine=machine,
            tool_id="cursor",
            paths=[path, path],
        )
        await inventory_machine_canvases(session, machine.id)
        request = (await pending_machine_canvases(session, machine.id))[0]
        metadata = {
            "reference_ids": request["reference_ids"],
            "path_hash": request["path_hash"],
            "name": "live",
            "source_hash": _sha(SOURCE),
            "compiled_hash": _sha(COMPILED),
            "runtime_hash": _sha(RUNTIME),
            "render_mode": "interactive",
        }
        original, status, linked = await store_captured_canvas(
            session,
            user=user,
            machine=machine,
            metadata=metadata,
            source=SOURCE,
            compiled=COMPILED,
            runtime=RUNTIME,
        )
        assert status == "renderable"
        assert linked == 2
        summary = await _conversation_canvas_summaries(session, document.id)
        assert len(summary) == 1
        assert summary[0]["name"] == "live"
        assert summary[0]["artifact_id"] == str(original.id)
        assert summary[0]["source_kind"] == "interactive"
        assert await pending_machine_canvases(session, machine.id) == []

        refresh = await pending_machine_canvases(
            session,
            machine.id,
            refresh_before=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        assert len(refresh) == 1
        assert refresh[0]["current_source_hash"] == _sha(SOURCE)
        assert refresh[0]["current_render_mode"] == "interactive"
        unchanged = await record_canvas_outcome(
            session,
            machine_id=machine.id,
            reference_ids=[UUID(value) for value in refresh[0]["reference_ids"]],
            path_hash=refresh[0]["path_hash"],
            status="unchanged",
            reason="source_hash_match",
        )
        assert unchanged == 2

        updated, updated_status, updated_links = await store_captured_canvas(
            session,
            user=user,
            machine=machine,
            metadata={**metadata, "source_hash": _sha(UPDATED_SOURCE)},
            source=UPDATED_SOURCE,
            compiled=COMPILED,
            runtime=RUNTIME,
        )
        assert updated_status == "renderable"
        assert updated_links == 2
        assert updated.id != original.id
        refreshed_summary = await _conversation_canvas_summaries(session, document.id)
        assert len(refreshed_summary) == 1
        assert refreshed_summary[0]["artifact_id"] == str(updated.id)
        linked_artifacts = (
            await session.execute(
                select(CanvasArtifactReference.artifact_id).where(
                    CanvasArtifactReference.machine_id == machine.id,
                    CanvasArtifactReference.path_hash == request["path_hash"],
                )
            )
        ).scalars().all()
        assert linked_artifacts == [updated.id, updated.id]
        assert (
            await session.scalar(select(func.count()).select_from(CanvasArtifact))
        ) == 2
        await session.rollback()


@pytest.mark.asyncio
async def test_read_tool_documentation_is_not_projected_or_shown(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="documentation-source",
            collector_token_hash=uuid4().hex,
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="cursor"))
        session.add_all([user, machine])
        await session.flush()
        document = Document(
            id=uuid4(),
            tool_id="cursor",
            machine_id=machine.id,
            relative_path=f"sessions/{uuid4()}.jsonl",
            category="conversation",
            content_type="jsonl",
            content_hash=uuid4().hex + uuid4().hex,
            file_size_bytes=1,
            metadata_={},
        )
        session.add(document)
        await session.flush()
        placeholder = "/Users/<user>/.cursor/projects/<workspace>/canvases/billing-review.canvas.tsx"
        message = ConversationMessage(
            document_id=document.id,
            line_number=1,
            role="tool",
            content=f"Canvas skill location example: {placeholder}",
            metadata_={"tool_name": "Read"},
        )
        session.add(message)
        await session.flush()

        assert await project_message_canvases(session, document, [message]) == 0
        assert await _conversation_canvas_summaries(session, document.id) == []

        # A pre-fix row is removed by the bounded compatibility cleanup on the
        # next collector inventory poll.
        session.add(
            CanvasArtifactReference(
                document_id=document.id,
                message_id=message.id,
                machine_id=machine.id,
                recorded_path=placeholder,
                path_hash=normalized_path_hash(placeholder),
                name="billing-review",
                status="rejected",
                reason="outside_allowlisted_root",
            )
        )
        await session.flush()
        assert await inventory_machine_canvases(session, machine.id) == {
            "discovered": 0,
            "unsupported": 0,
        }
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CanvasArtifactReference)
                .where(CanvasArtifactReference.document_id == document.id)
            )
        ) == 0
        await session.rollback()


@pytest.mark.asyncio
async def test_pending_repairs_legacy_json_escaped_windows_reference(
    session_factory,
) -> None:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4()}@example.test",
            role="viewer",
            status="active",
        )
        machine = Machine(
            id=uuid4(),
            name="escaped-source",
            collector_token_hash=uuid4().hex,
            user_id=user.id,
        )
        if await session.get(Tool, "cursor") is None:
            session.add(Tool(id="cursor", display_name="cursor"))
        session.add_all([user, machine])
        await session.flush()
        document = Document(
            id=uuid4(),
            tool_id="cursor",
            machine_id=machine.id,
            relative_path=f"sessions/{uuid4()}.jsonl",
            category="conversation",
            content_type="jsonl",
            content_hash=uuid4().hex + uuid4().hex,
            file_size_bytes=1,
            metadata_={},
        )
        session.add(document)
        await session.flush()
        escaped = (
            r"C:\\Users\\intpa\\.cursor\\projects\\workspace\\canvases"
            r"\\incident.canvas.tsx"
        )
        message = ConversationMessage(
            document_id=document.id,
            line_number=1,
            role="tool",
            content='{"result":"Opened canvas: ' + escaped + '"}',
        )
        ordinary_path = r"C:\temp\outside.canvas.tsx"
        ordinary_message = ConversationMessage(
            document_id=document.id,
            line_number=2,
            role="tool",
            content=f"Opened canvas: {ordinary_path}",
        )
        session.add_all([message, ordinary_message])
        await session.flush()
        ordinary_reference = CanvasArtifactReference(
            document_id=document.id,
            message_id=ordinary_message.id,
            machine_id=machine.id,
            recorded_path=ordinary_path,
            path_hash=normalized_path_hash(ordinary_path),
            name="outside",
            status="unsupported",
            reason="non_local_or_unsupported_path",
        )
        session.add_all([
            CanvasArtifactReference(
                document_id=document.id,
                message_id=message.id,
                machine_id=machine.id,
                recorded_path=escaped,
                path_hash=normalized_path_hash(escaped),
                name="incident",
                status="unsupported",
                reason="non_local_or_unsupported_path",
            ),
            ordinary_reference,
        ])
        await session.flush()
        ordinary_reference_id = ordinary_reference.id

        pending = await pending_machine_canvases(session, machine.id)

        assert len(pending) == 1
        assert pending[0]["path"] == (
            r"C:\Users\intpa\.cursor\projects\workspace\canvases"
            r"\incident.canvas.tsx"
        )
        references = (
            await session.execute(
                select(CanvasArtifactReference).where(
                    CanvasArtifactReference.document_id == document.id
                )
            )
        ).scalars().all()
        assert len(references) == 2
        repaired = next(reference for reference in references if reference.name == "incident")
        untouched = next(reference for reference in references if reference.name == "outside")
        assert repaired.status == "discovered"
        assert untouched.id == ordinary_reference_id
        assert untouched.status == "unsupported"
        await session.rollback()
