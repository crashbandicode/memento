from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from server.db.models import (
    Base,
    CanvasArtifactReference,
    ConversationMessage,
    ConversationPromptProjection,
    ConversationReadModel,
    ConversationSearchTerm,
    ConversationTaskState,
    Document,
    DocumentDeliveryState,
    Machine,
    Tool,
    User,
)
from server.db.session import TransactionalAsyncSession
from server.config import settings
from server.services import cache, sse_service
from server.services.content_sanitizer import sanitize_content_file
from server.services.conversation_stream import ConversationFileSource
from server.services.ingest_service import (
    DeltaBaseMismatch,
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    ingest_file,
)
from server.services.large_content_store import (
    DocumentContentPointer,
    document_content_key,
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
    yield async_sessionmaker(
        engine,
        class_=TransactionalAsyncSession,
        expire_on_commit=False,
    )
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


class _Redis:
    def __init__(self) -> None:
        self.increments: list[str] = []

    async def incr(self, key: str) -> int:
        self.increments.append(key)
        return len(self.increments)


@pytest.mark.asyncio
async def test_streamed_ingest_projects_transactionally_and_rebases(
    session_factory,
    monkeypatch,
) -> None:
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
    rollback_payload = (
        json.dumps(
            _claude_record(
                "user",
                "This rolled-back prompt must never become visible.",
                source_id="user-rollback",
                timestamp="2026-08-07T12:00:05Z",
            ),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    rollback_revision_hash = hashlib.sha256(
        raw_payload + delta_payload + rollback_payload
    ).hexdigest()
    rebase_tail = (
        json.dumps(
            _claude_record(
                "user",
                "Ship the committed full rebase.",
                source_id="user-rebase",
                timestamp="2026-08-07T12:00:06Z",
            ),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    rebase_payload = raw_payload + delta_payload + rebase_tail
    rebase_revision_hash = hashlib.sha256(rebase_payload).hexdigest()

    with tempfile.TemporaryDirectory() as temporary:
        raw_path = Path(temporary) / "raw.jsonl"
        sanitized_path = Path(temporary) / "sanitized.jsonl"
        delta_path = Path(temporary) / "delta.jsonl"
        sanitized_delta_path = Path(temporary) / "sanitized-delta.jsonl"
        rollback_path = Path(temporary) / "rollback.jsonl"
        sanitized_rollback_path = Path(temporary) / "sanitized-rollback.jsonl"
        rebase_path = Path(temporary) / "rebase.jsonl"
        sanitized_rebase_path = Path(temporary) / "sanitized-rebase.jsonl"
        raw_path.write_bytes(raw_payload)
        delta_path.write_bytes(delta_payload)
        rollback_path.write_bytes(rollback_payload)
        rebase_path.write_bytes(rebase_payload)
        sanitized = sanitize_content_file(raw_path, sanitized_path, chunk_size=7)
        sanitized_delta = sanitize_content_file(
            delta_path,
            sanitized_delta_path,
            chunk_size=5,
        )
        sanitized_rollback = sanitize_content_file(
            rollback_path,
            sanitized_rollback_path,
            chunk_size=5,
        )
        sanitized_rebase = sanitize_content_file(
            rebase_path,
            sanitized_rebase_path,
            chunk_size=7,
        )
        source = ConversationFileSource.inspect(sanitized.path, chunk_size=11)
        delta_source = ConversationFileSource.inspect(
            sanitized_delta.path,
            chunk_size=13,
        )
        rollback_source = ConversationFileSource.inspect(
            sanitized_rollback.path,
            chunk_size=11,
        )
        rebase_source = ConversationFileSource.inspect(
            sanitized_rebase.path,
            chunk_size=13,
        )
        redis = _Redis()
        published: list[tuple[str, dict, str | None]] = []

        async def publish_event(
            event_type: str,
            data: dict,
            user_id: str | None = None,
        ) -> None:
            published.append((event_type, data, user_id))

        monkeypatch.setattr(cache, "_client", redis)
        monkeypatch.setattr(sse_service, "publish_event", publish_event)
        monkeypatch.setattr(settings, "document_content_minio_enabled", True)

        async def finalize_content(
            *,
            document_id,
            content=None,
            payload_path=None,
            **_kwargs,
        ) -> DocumentContentPointer:
            payload = (
                content.encode("utf-8")
                if content is not None
                else payload_path.read_bytes()
            )
            sha256 = hashlib.sha256(payload).hexdigest()
            return DocumentContentPointer(
                key=document_content_key(document_id=document_id, sha256=sha256),
                sha256=sha256,
                size_bytes=len(payload),
                verified_at=datetime.now(timezone.utc),
            )

        monkeypatch.setattr(
            "server.services.ingest_service.finalize_document_content",
            finalize_content,
        )

        async def no_raw_object_read(*_args, **_kwargs) -> str:
            # This fixture verifies normalized projections; raw-object bytes
            # are represented by the finalizer pointer above, not a live MinIO.
            return ""

        monkeypatch.setattr(
            "server.services.embedding_service.document_content",
            no_raw_object_read,
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
            document_id = document.id
            relative_path = document.relative_path
            user_id = str(user.id)
            machine_id = str(machine.id)
            assert redis.increments == []
            assert published == []
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
            delivery_state = await session.get(
                DocumentDeliveryState,
                document.id,
            )
            read_model = await session.get(ConversationReadModel, document.id)
            prompts = (
                await session.execute(
                    select(ConversationPromptProjection)
                    .where(
                        ConversationPromptProjection.document_id == document.id
                    )
                    .order_by(ConversationPromptProjection.line_number)
                )
            ).scalars().all()

            initial_content_key = document.content_s3_key
            assert initial_content_key == document_content_key(
                document_id=document.id,
                sha256=source.sha256,
            )
            assert document.content_object_sha256 == source.sha256
            assert document.content_object_size_bytes == source.size
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
            assert delivery_state is not None
            assert delivery_state.revision_hash == revision_hash
            assert read_model is not None
            assert read_model.message_count == 4
            assert read_model.projected_through_line == 4
            assert [(prompt.line_number, prompt.content) for prompt in prompts] == [
                (1, f"Build streamingneedle report at [{canvas_path}]({canvas_path}).")
            ]
            assert redis.increments == [
                f"cache:generation:daily:{user_id}",
            ]
            assert len(published) == 1
            assert published[0][0] == "file_synced"
            assert "conversation.prompts" in published[0][1]["changes"]
            first_message_id = messages[0].id

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
            assert len(published) == 1
            await session.commit()
            await session.refresh(document)
            await session.refresh(task_state)
            await session.refresh(delivery_state)
            await session.refresh(read_model)

            assert document.content_s3_key == initial_content_key
            assert document.content_hash == revision_hash
            assert delivery_state.revision_hash == delta_revision_hash
            assert document.metadata_[STORED_SOURCE_REVISION_KEY] == revision_hash
            assert document.metadata_[STORED_SOURCE_HASH_KEY] == source.sha256
            assert task_state.state["tasks"][0]["status"] == "completed"
            assert read_model.message_count == 5
            assert len(published) == 2
            assert redis.increments == [
                f"cache:generation:daily:{user_id}",
            ]

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
            await session.rollback()
            assert len(published) == 2
            assert len(redis.increments) == 1

            document = await session.get(Document, document_id)
            assert document is not None
            await ingest_file(
                session,
                tool_id="claude_code",
                category="conversation",
                content_type="jsonl",
                relative_path=relative_path,
                content="",
                content_hash=rollback_revision_hash,
                file_size=rollback_source.size,
                mode="delta",
                offset=len(raw_payload) + len(delta_payload) + len(rollback_payload),
                base_hash=delta_revision_hash,
                base_offset=len(raw_payload) + len(delta_payload),
                metadata={},
                machine_id=machine_id,
                user_id=user_id,
                schedule_post_ingest=False,
                persist_content=True,
                content_already_sanitized=True,
                content_had_sensitive=sanitized_rollback.had_sensitive,
                conversation_source=rollback_source,
            )
            assert len(published) == 2
            assert len(redis.increments) == 1
            await session.rollback()
            assert len(published) == 2
            assert len(redis.increments) == 1

            delivery_state = await session.get(
                DocumentDeliveryState,
                document_id,
            )
            read_model = await session.get(ConversationReadModel, document_id)
            prompts = (
                await session.execute(
                    select(ConversationPromptProjection).where(
                        ConversationPromptProjection.document_id == document_id
                    )
                )
            ).scalars().all()
            assert delivery_state is not None
            assert delivery_state.revision_hash == delta_revision_hash
            assert read_model is not None
            assert read_model.message_count == 5
            assert len(prompts) == 1

            document = await session.get(Document, document_id)
            assert document is not None
            await ingest_file(
                session,
                tool_id="claude_code",
                category="conversation",
                content_type="jsonl",
                relative_path=relative_path,
                content="",
                content_hash=rebase_revision_hash,
                file_size=rebase_source.size,
                mode="full",
                offset=len(rebase_payload),
                metadata={},
                machine_id=machine_id,
                user_id=user_id,
                schedule_post_ingest=False,
                persist_content=False,
                content_s3_key="raw/private/rebased.txt",
                content_already_sanitized=True,
                content_had_sensitive=sanitized_rebase.had_sensitive,
                conversation_source=rebase_source,
                authoritative_rebase=True,
            )
            assert len(published) == 2
            await session.commit()

            messages = (
                await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.document_id == document_id)
                    .order_by(ConversationMessage.line_number)
                )
            ).scalars().all()
            delivery_state = await session.get(
                DocumentDeliveryState,
                document_id,
            )
            read_model = await session.get(ConversationReadModel, document_id)
            prompts = (
                await session.execute(
                    select(ConversationPromptProjection)
                    .where(
                        ConversationPromptProjection.document_id == document_id
                    )
                    .order_by(ConversationPromptProjection.line_number)
                )
            ).scalars().all()
            assert messages[0].id == first_message_id
            assert len(messages) == 6
            assert delivery_state is not None
            assert delivery_state.revision_hash == rebase_revision_hash
            assert read_model is not None
            assert read_model.message_count == 6
            assert [prompt.content for prompt in prompts] == [
                f"Build streamingneedle report at [{canvas_path}]({canvas_path}).",
                "Ship the committed full rebase.",
            ]
            assert len(published) == 3
            assert redis.increments == [
                f"cache:generation:daily:{user_id}",
                f"cache:generation:daily:{user_id}",
            ]
