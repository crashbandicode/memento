from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect

from server.db.models import Document, DocumentDeliveryState
from server.services.document_delivery import (
    advance_document_activity,
    attach_document_delivery,
    store_document_metadata,
    update_document_delivery,
)


def _document(now: datetime) -> Document:
    return Document(
        id=uuid.uuid4(),
        tool_id="codex",
        project_id=uuid.uuid4(),
        machine_id=uuid.uuid4(),
        relative_path="sessions/thread.jsonl",
        category="conversation",
        content_type="jsonl",
        title="Thread",
        content_hash="full-revision",
        file_size_bytes=100,
        metadata_={"session_id": "thread"},
        source_modified_at=now,
        activity_at=now,
        synced_at=now,
    )


def _state(document: Document, now: datetime) -> DocumentDeliveryState:
    return DocumentDeliveryState(
        document_id=document.id,
        project_id=document.project_id,
        revision_hash=document.content_hash,
        file_size_bytes=document.file_size_bytes,
        delivery_metadata=dict(document.metadata_),
        source_modified_at=document.source_modified_at,
        activity_at=document.activity_at,
        synced_at=document.synced_at,
    )


def test_delta_updates_only_projection_state() -> None:
    now = datetime.now(timezone.utc)
    document = _document(now)
    state = _state(document, now)
    attach_document_delivery(document, state, runtime_only=True)

    update_document_delivery(
        document,
        state,
        revision_hash="delta-revision",
        file_size_bytes=200,
        source_modified_at=now + timedelta(seconds=1),
        synced_at=now + timedelta(seconds=2),
    )
    store_document_metadata(
        document,
        {"session_id": "thread", "total_lines": 10},
    )

    assert state.revision_hash == "delta-revision"
    assert state.delivery_metadata["total_lines"] == 10
    mapper_state = inspect(document)
    assert not mapper_state.attrs.content_hash.history.has_changes()
    assert not mapper_state.attrs.file_size_bytes.history.has_changes()
    assert not mapper_state.attrs.metadata_.history.has_changes()
    assert not mapper_state.attrs.synced_at.history.has_changes()


def test_tool_only_delivery_does_not_advance_indexed_activity() -> None:
    now = datetime.now(timezone.utc)
    document = _document(now)
    state = _state(document, now)
    attach_document_delivery(document, state, runtime_only=True)

    assert advance_document_activity(document, None) is False
    assert advance_document_activity(document, now) is False
    assert state.activity_at == now

    later = now + timedelta(seconds=1)
    assert advance_document_activity(document, later) is True
    assert state.activity_at == later


def test_hot_delivery_columns_are_not_indexed() -> None:
    indexed_columns = {
        column.name
        for index in DocumentDeliveryState.__table__.indexes
        for column in index.columns
    }
    assert indexed_columns == {"activity_at", "project_id"}
    assert {
        "revision_hash",
        "file_size_bytes",
        "delivery_metadata",
        "source_modified_at",
        "synced_at",
    }.isdisjoint(indexed_columns)

    document_indexes = {index.name for index in Document.__table__.indexes}
    assert not {
        "idx_documents_synced_at",
        "idx_documents_tool_synced",
        "idx_documents_project_synced",
        "idx_documents_activity_at",
        "idx_documents_project_activity",
    } & document_indexes


def test_online_migration_does_not_rewrite_documents_table() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "server"
        / "scripts"
        / "20260807_document_delivery_projection.sql"
    ).read_text(encoding="utf-8")

    assert "UPDATE documents" not in migration
    assert "fillfactor = 70" in migration
    assert "CREATE INDEX CONCURRENTLY" in migration
    assert "DROP INDEX CONCURRENTLY" in migration
