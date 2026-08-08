"""Latest-delivery projection for append-heavy documents.

The documents table remains the canonical full snapshot. Conversation DELTAs
write this narrow row so sync/revision churn does not rewrite every documents
index (or its wider heap tuple).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import and_, exists, func, or_, select

from ..db.models import Document, DocumentDeliveryState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import ColumnElement


def _delivery_expression(projected_column, legacy_column, *, joined: bool):
    projected = (
        projected_column
        if joined
        else (
            select(projected_column)
            .where(DocumentDeliveryState.document_id == Document.id)
            .correlate(Document)
            .scalar_subquery()
        )
    )
    return func.coalesce(projected, legacy_column)


def delivery_revision_expression(*, joined: bool = False):
    return _delivery_expression(
        DocumentDeliveryState.revision_hash,
        Document.content_hash,
        joined=joined,
    )


def delivery_file_size_expression(*, joined: bool = False):
    return _delivery_expression(
        DocumentDeliveryState.file_size_bytes,
        Document.file_size_bytes,
        joined=joined,
    )


def delivery_metadata_expression(*, joined: bool = False):
    return _delivery_expression(
        DocumentDeliveryState.delivery_metadata,
        Document.metadata_,
        joined=joined,
    )


def delivery_source_modified_expression(*, joined: bool = False):
    return _delivery_expression(
        DocumentDeliveryState.source_modified_at,
        Document.source_modified_at,
        joined=joined,
    )


def delivery_activity_expression(*, joined: bool = False):
    return _delivery_expression(
        DocumentDeliveryState.activity_at,
        Document.activity_at,
        joined=joined,
    )


def delivery_synced_expression(*, joined: bool = False):
    return _delivery_expression(
        DocumentDeliveryState.synced_at,
        Document.synced_at,
        joined=joined,
    )


def outerjoin_document_delivery(statement):
    """Join the optional projection while preserving unmigrated rows."""
    return statement.outerjoin(
        DocumentDeliveryState,
        DocumentDeliveryState.document_id == Document.id,
    )


def current_revision_predicate(
    document_id: "ColumnElement",
    revision_hash: str,
):
    """Fence a write against projection state with a legacy-row fallback."""
    projected = exists(
        select(1).where(
            DocumentDeliveryState.document_id == document_id,
            DocumentDeliveryState.revision_hash == revision_hash,
        )
    )
    any_projection = exists(
        select(1).where(DocumentDeliveryState.document_id == document_id)
    )
    return or_(
        projected,
        and_(~any_projection, Document.content_hash == revision_hash),
    )


async def ensure_document_delivery_state(
    db: "AsyncSession",
    document: Document,
) -> DocumentDeliveryState:
    """Create a lazy projection from legacy columns on first changed ingest."""
    state = getattr(document, "delivery_state", None)
    if state is not None:
        return state
    state = DocumentDeliveryState(
        document_id=document.id,
        project_id=document.project_id,
        revision_hash=document.content_hash,
        file_size_bytes=getattr(document, "file_size_bytes", 0),
        delivery_metadata=dict(document.metadata_ or {}),
        source_modified_at=getattr(document, "source_modified_at", None),
        activity_at=getattr(document, "activity_at", None),
        synced_at=document.synced_at,
    )
    document.delivery_state = state
    add = getattr(db, "add", None)
    if add is not None:
        add(state)
    return state


def attach_document_delivery(
    document: Document,
    state: DocumentDeliveryState,
    *,
    runtime_only: bool,
) -> None:
    """Route metadata helpers to the projection for this ingest."""
    document.delivery_state = state
    setattr(document, "_memento_delivery_state", state)
    setattr(document, "_memento_runtime_only", runtime_only)


def document_delivery_state(document: Document) -> DocumentDeliveryState | None:
    state = getattr(document, "_memento_delivery_state", None)
    if isinstance(state, DocumentDeliveryState):
        return state
    return getattr(document, "delivery_state", None)


def document_metadata(document: Document) -> dict:
    state = document_delivery_state(document)
    if state is not None:
        return dict(state.delivery_metadata or {})
    return dict(document.metadata_ or {})


def store_document_metadata(document: Document, metadata: dict) -> bool:
    """Persist effective metadata without touching canonical DELTA snapshots."""
    value = dict(metadata)
    state = document_delivery_state(document)
    changed = document_metadata(document) != value
    if state is not None:
        state.delivery_metadata = value
    if state is None or not getattr(document, "_memento_runtime_only", False):
        if dict(document.metadata_ or {}) != value:
            document.metadata_ = value
    else:
        if isinstance(document, Document):
            from sqlalchemy.orm.attributes import set_committed_value

            set_committed_value(document, "metadata_", value)
        else:
            document.metadata_ = value
    return changed


def update_document_delivery(
    document: Document,
    state: DocumentDeliveryState,
    *,
    revision_hash: str,
    file_size_bytes: int,
    source_modified_at: datetime | None,
    synced_at: datetime,
) -> None:
    """Advance delivery state; mirror only non-DELTA canonical snapshots."""
    state.project_id = document.project_id
    state.revision_hash = revision_hash
    state.file_size_bytes = file_size_bytes
    state.source_modified_at = source_modified_at
    state.synced_at = synced_at
    if not getattr(document, "_memento_runtime_only", False):
        document.content_hash = revision_hash
        document.file_size_bytes = file_size_bytes
        document.source_modified_at = source_modified_at
        document.synced_at = synced_at
    else:
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(document, "content_hash", revision_hash)
        set_committed_value(document, "file_size_bytes", file_size_bytes)
        set_committed_value(document, "source_modified_at", source_modified_at)
        set_committed_value(document, "synced_at", synced_at)


def update_document_source_modified(
    document: Document,
    source_modified_at: datetime | None,
) -> None:
    state = document_delivery_state(document)
    if state is not None:
        state.source_modified_at = source_modified_at
    if state is None or not getattr(document, "_memento_runtime_only", False):
        document.source_modified_at = source_modified_at
    elif isinstance(document, Document):
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(
            document,
            "source_modified_at",
            source_modified_at,
        )
    else:
        document.source_modified_at = source_modified_at


def advance_document_activity(
    document: Document,
    activity_at: datetime | None,
) -> bool:
    """Advance activity only for a newer normalized human/assistant turn."""
    if activity_at is None:
        return False
    state = document_delivery_state(document)
    current = state.activity_at if state is not None else document.activity_at
    if current is not None and current >= activity_at:
        return False
    if state is not None:
        state.activity_at = activity_at
    if state is None or not getattr(document, "_memento_runtime_only", False):
        document.activity_at = activity_at
    else:
        if isinstance(document, Document):
            from sqlalchemy.orm.attributes import set_committed_value

            set_committed_value(document, "activity_at", activity_at)
        else:
            document.activity_at = activity_at
    return True
