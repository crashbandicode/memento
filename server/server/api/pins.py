"""Personal pinned-message API for the conversation viewer."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, Machine, PinnedMessage, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.user_filter import user_machine_ids

router = APIRouter(tags=["pins"])

_PREVIEW_CHARS = 500


class PinRequest(BaseModel):
    note: str | None = Field(default=None, max_length=4_000)


def _pin_payload(pin: PinnedMessage) -> dict:
    return {
        "id": str(pin.id),
        "message_id": pin.message_id,
        "document_id": str(pin.document_id),
        "note": pin.note,
        "created_at": pin.created_at.isoformat() if pin.created_at else None,
    }


async def _authorized_document(
    db: AsyncSession,
    user: User,
    document_id: uuid.UUID,
) -> Document:
    """Return a document only when it belongs to a machine the user can read."""
    statement = select(Document).where(Document.id == document_id)
    if user.role not in ("admin", "owner"):
        statement = statement.where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user.id)
            )
        )
    document = (await db.execute(statement)).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404)
    return document


async def _authorized_message(
    db: AsyncSession,
    user: User,
    document_id: uuid.UUID,
    message_id: int,
) -> ConversationMessage:
    """Return a message only when both its document and machine are authorized."""
    await _authorized_document(db, user, document_id)
    message = (
        await db.execute(
            select(ConversationMessage).where(
                ConversationMessage.id == message_id,
                ConversationMessage.document_id == document_id,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404)
    return message


def _preview_payload(
    *,
    message_id: int,
    line_number: int,
    role: str | None,
    message_type: str | None,
    content: str | None,
    timestamp,
) -> dict:
    return {
        "id": message_id,
        "line_number": line_number,
        "role": role or message_type or "system",
        "snippet": (content or "").strip(),
        "timestamp": timestamp.isoformat() if timestamp else None,
    }


@router.post("/api/conversations/{document_id}/messages/{message_id}/pin")
async def pin_message(
    document_id: uuid.UUID,
    message_id: int,
    request: PinRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Create or update one personal pin without duplicating it."""
    message = await _authorized_message(db, user, document_id, message_id)
    note = request.note if request is not None else None
    statement = (
        insert(PinnedMessage)
        .values(
            id=uuid.uuid4(),
            user_id=user.id,
            message_id=message.id,
            document_id=document_id,
            note=note,
        )
        .on_conflict_do_update(
            constraint="uq_pinned_messages_user_message",
            set_={"note": note},
        )
        .returning(PinnedMessage)
    )
    pin = (await db.execute(statement)).scalar_one()
    await db.commit()
    return _pin_payload(pin)


@router.delete("/api/conversations/{document_id}/messages/{message_id}/pin")
async def unpin_message(
    document_id: uuid.UUID,
    message_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Remove a personal pin. Repeating the delete remains successful."""
    await _authorized_message(db, user, document_id, message_id)
    await db.execute(
        delete(PinnedMessage).where(
            PinnedMessage.user_id == user.id,
            PinnedMessage.message_id == message_id,
            PinnedMessage.document_id == document_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.get("/api/conversations/{document_id}/pins")
async def get_conversation_pins(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """List the current user's pins for one authorized conversation."""
    await _authorized_document(db, user, document_id)
    rows = (
        await db.execute(
            select(
                PinnedMessage,
                ConversationMessage.id,
                ConversationMessage.line_number,
                ConversationMessage.role,
                ConversationMessage.message_type,
                func.left(ConversationMessage.content, _PREVIEW_CHARS).label("content"),
                ConversationMessage.timestamp,
            )
            .join(
                ConversationMessage,
                ConversationMessage.id == PinnedMessage.message_id,
            )
            .where(
                PinnedMessage.user_id == user.id,
                PinnedMessage.document_id == document_id,
            )
            .order_by(PinnedMessage.created_at.desc(), PinnedMessage.id.desc())
        )
    ).all()
    return {
        "pins": [
            {
                **_pin_payload(pin),
                "message": _preview_payload(
                    message_id=message_id,
                    line_number=line_number,
                    role=role,
                    message_type=message_type,
                    content=content,
                    timestamp=timestamp,
                ),
            }
            for pin, message_id, line_number, role, message_type, content, timestamp in rows
        ]
    }


@router.get("/api/pins")
async def get_pins(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """List all currently accessible personal pins, newest first."""
    machine_ids = await user_machine_ids(db, user)
    statement = (
        select(
            PinnedMessage,
            ConversationMessage.id,
            ConversationMessage.line_number,
            ConversationMessage.role,
            ConversationMessage.message_type,
            func.left(ConversationMessage.content, _PREVIEW_CHARS).label("content"),
            ConversationMessage.timestamp,
            Document.id.label("document_id"),
            Document.title,
            Document.tool_id,
        )
        .join(ConversationMessage, ConversationMessage.id == PinnedMessage.message_id)
        .join(Document, Document.id == PinnedMessage.document_id)
        .where(PinnedMessage.user_id == user.id)
        .order_by(PinnedMessage.created_at.desc(), PinnedMessage.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    if machine_ids is not None:
        statement = statement.where(Document.machine_id.in_(machine_ids))
    rows = (await db.execute(statement)).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "pins": [
            {
                **_pin_payload(pin),
                "conversation_ref": str(document_id),
                "message": _preview_payload(
                    message_id=message_id,
                    line_number=line_number,
                    role=role,
                    message_type=message_type,
                    content=content,
                    timestamp=timestamp,
                ),
                "document": {
                    "id": str(document_id),
                    "title": title,
                    "tool_id": tool_id,
                },
            }
            for (
                pin,
                message_id,
                line_number,
                role,
                message_type,
                content,
                timestamp,
                document_id,
                title,
                tool_id,
            ) in page
        ],
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }
