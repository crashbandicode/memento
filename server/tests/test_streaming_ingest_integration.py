from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from server.db.models import (
    Base,
    CanvasArtifactReference,
    ConversationMessage,
    ConversationSearchTerm,
    ConversationTaskState,
    Machine,
    Tool,
    User,
)
from server.services.content_sanitizer import sanitize_content_file
from server.services.conversation_stream import ConversationFileSource
from server.services.ingest_service import (
    DeltaBaseMismatch,
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    ingest_file,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
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
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _claude_record(
    role: str,
    content: object,
    *,
    source_id: str,
    timestamp: str,
) -> dict:
    return {
        "type": role,
        "uuid": source_id,
        "timestamp": timestamp,
        "message": {
            "role": role,
            "content": content,
        },
    }


@pytest.mark.asyncio
async def test_streamed_full_preserves_normalized_projections(session_factory) -> None:
    canvas_path = (
        "/home/me/.cursor/projects/work/canvases/"
        "streaming-report.canvas.tsx"
    )
    secret = "sk-" + ("Z" * 20)
    records = [
        _claude_record(
            "user",
            f"Build streamingneedle report at [{canvas_path}]({canvas_path}).",
            source_id="user-1",
            timestamp="2026-08-07T12:00:00Z",
        ),
        _claude_record(
            "assistant",
            f"Never persist this token: {secret}",
            source_id="assistant-secret",
            timestamp="2026-08-07T12:00:01Z",
        ),
        _claude_record(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "todo-1",
                    "name": "TodoWrite",
                    "input": {
                        "is_current": True,
                        "todos": [
                            {
                                "id": "1",
                                "content": "Verify streamed ingest",
                                "status": "in_progress",
                            }
                        ],
                    },
                }
            ],
            source_id="assistant-todo",
            timestamp="2026-08-07T12:00:02Z",
        ),
        _claude_record(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "question-1",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": "Ship the streamed path?",
                                "header": "Decision",
                                "options": [
                                    {
                                        "label": "Yes",
                                        "description": "Use bounded reads",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
            source_id="assistant-question",
            timestamp="2026-08-07T12:00:03Z",
        ),
    ]
    raw_payload = (
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        + "\n"
    ).encode("utf-8")
    revision_hash = hashlib.sha256(raw_payload).hexdigest()
    delta_payload = (
        json.dumps(
            _claude_record(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "task-update-1",
                        "name": "TaskUpdate",
                        "input": {
                            "taskId": "1",
                            "status": "completed",
                        },
                    }
                ],
                source_id="assistant-task-update",
                timestamp="2026-08-07T12:00:04Z",
            ),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    delta_revision_hash = hashlib.sha256(raw_payload + delta_payload).hexdigest()

    with tempfile.TemporaryDirectory() as temporary:
        raw_path = Path(temporary) / "raw.jsonl"
        sanitized_path = Path(temporary) / "sanitized.jsonl"
        delta_path = Path(temporary) / "delta.jsonl"
        sanitized_delta_path = Path(temporary) / "sanitized-delta.jsonl"
        raw_path.write_bytes(raw_payload)
        delta_path.write_bytes(delta_payload)
        sanitized = sanitize_content_file(raw_path, sanitized_path, chunk_size=7)
        sanitized_delta = sanitize_content_file(
            delta_path,
            sanitized_delta_path,
            chunk_size=5,
        )
        source = ConversationFileSource.inspect(sanitized.path, chunk_size=11)
        delta_source = ConversationFileSource.inspect(
            sanitized_delta.path,
            chunk_size=13,
        )

        async with session_factory() as session:
            user = User(
                id=uuid4(),
                email=f"{uuid4()}@example.test",
                role="viewer",
                status="active",
            )
            machine = Machine(
                id=uuid4(),
                name="streaming-test",
                collector_token_hash=str(uuid4()),
                user_id=user.id,
            )
            session.add_all([user, machine])
            await session.flush()

            document = await ingest_file(
                session,
                tool_id="claude_code",
                category="conversation",
                content_type="jsonl",
                relative_path=f"sessions/{uuid4()}.jsonl",
                content="",
                content_hash=revision_hash,
                file_size=source.size,
                mode="full",
                offset=len(raw_payload),
                metadata={},
                machine_id=str(machine.id),
                user_id=str(user.id),
                schedule_post_ingest=False,
                persist_content=False,
                content_s3_key="raw/private/streamed.txt",
                content_already_sanitized=True,
                content_had_sensitive=sanitized.had_sensitive,
                conversation_source=source,
            )
            await session.commit()

            messages = (
                (
                    await session.execute(
                        select(ConversationMessage)
                        .where(ConversationMessage.document_id == document.id)
                        .order_by(ConversationMessage.line_number)
                    )
                )
                .scalars()
                .all()
            )
            task_state = await session.get(ConversationTaskState, document.id)
            canvas_references = (
                (
                    await session.execute(
                        select(CanvasArtifactReference).where(
                            CanvasArtifactReference.document_id == document.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            search_term = await session.get(
                ConversationSearchTerm,
                "streamingneedle",
            )

            assert document.content is None
            assert document.content_s3_key == "raw/private/streamed.txt"
            assert document.needs_review is True
            assert document.metadata_[STORED_SOURCE_REVISION_KEY] == revision_hash
            assert document.metadata_[STORED_SOURCE_HASH_KEY] == source.sha256
            assert document.metadata_[STORED_SOURCE_SIZE_KEY] == source.size
            assert document.title.startswith("Build streamingneedle report")
            assert [row.metadata_.get("source_id") for row in messages] == [
                "user-1",
                "assistant-secret",
                "assistant-todo:tool_use:0",
                "assistant-question:tool_use:0",
            ]
            assert "[API_KEY_REDACTED]" in messages[1].content
            assert secret not in "\n".join(row.content for row in messages)
            assert messages[3].metadata_["interaction"]["id"] == "question-1"
            assert task_state is not None
            assert task_state.state["tasks"][0]["content"] == (
                "Verify streamed ingest"
            )
            assert len(canvas_references) == 1
            assert canvas_references[0].recorded_path == canvas_path
            assert search_term is not None
            assert await session.get(Tool, "claude_code") is not None

            await ingest_file(
                session,
                tool_id="claude_code",
                category="conversation",
                content_type="jsonl",
                relative_path=document.relative_path,
                content="",
                content_hash=delta_revision_hash,
                file_size=delta_source.size,
                mode="delta",
                offset=len(raw_payload) + len(delta_payload),
                base_hash=revision_hash,
                base_offset=len(raw_payload),
                metadata={},
                machine_id=str(machine.id),
                user_id=str(user.id),
                schedule_post_ingest=False,
                persist_content=True,
                content_already_sanitized=True,
                content_had_sensitive=sanitized_delta.had_sensitive,
                conversation_source=delta_source,
            )
            await session.commit()
            await session.refresh(document)
            await session.refresh(task_state)

            assert document.content is None
            assert document.content_s3_key == "raw/private/streamed.txt"
            assert document.content_hash == delta_revision_hash
            assert document.metadata_[STORED_SOURCE_REVISION_KEY] == revision_hash
            assert document.metadata_[STORED_SOURCE_HASH_KEY] == source.sha256
            assert task_state.state["tasks"][0]["status"] == "completed"

            with pytest.raises(DeltaBaseMismatch):
                await ingest_file(
                    session,
                    tool_id="claude_code",
                    category="conversation",
                    content_type="jsonl",
                    relative_path=document.relative_path,
                    content="",
                    content_hash="f" * 64,
                    file_size=delta_source.size,
                    mode="delta",
                    offset=len(raw_payload) + (2 * len(delta_payload)),
                    base_hash="0" * 64,
                    base_offset=len(raw_payload) + len(delta_payload),
                    metadata={},
                    machine_id=str(machine.id),
                    user_id=str(user.id),
                    schedule_post_ingest=False,
                    persist_content=True,
                    content_already_sanitized=True,
                    conversation_source=delta_source,
                )
