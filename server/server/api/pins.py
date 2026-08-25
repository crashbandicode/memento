"""Personal pinned-message API for the conversation viewer.

Pins anchor to a message's stable native id (``metadata->>'source_id'``), with
the document-local line number as a fallback, so a full conversation re-ingest
— which deletes and recreates its message rows with new autoincrement ids —
never drops them. ``message_id`` is kept only as a nullable last-known pointer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select, text
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


def _message_anchor(message: ConversationMessage) -> tuple[str | None, int]:
    """The stable (source_id, line_number) a pin is keyed on."""
    raw = (message.metadata_ or {}).get("source_id")
    source_id = raw if isinstance(raw, str) and raw else None
    return source_id, message.line_number


def _resolved_message_join():
    """Join a pin to its CURRENT message row through the stable anchor.

    Prefers ``source_id`` (the common case, backed by idx_conv_msg_doc_source_id)
    and falls back to the document-local line number when a message carries no
    native id.
    """
    return and_(
        ConversationMessage.document_id == PinnedMessage.document_id,
        or_(
            and_(
                PinnedMessage.source_id.isnot(None),
                ConversationMessage.metadata_["source_id"].astext
                == PinnedMessage.source_id,
            ),
            and_(
                PinnedMessage.source_id.is_(None),
                ConversationMessage.line_number == PinnedMessage.line_number,
            ),
        ),
    )


def _pin_payload(pin: PinnedMessage) -> dict:
    return {
        "id": str(pin.id),
        "document_id": str(pin.document_id),
        "source_id": pin.source_id,
        "note": pin.note,
        "created_at": pin.created_at.isoformat() if pin.created_at else None,
    }


def _preview_payload(
    *,
    message_id: int | None,
    line_number: int | None,
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


def _preview_for_message(message: ConversationMessage) -> dict:
    return _preview_payload(
        message_id=message.id,
        line_number=message.line_number,
        role=message.role,
        message_type=message.message_type,
        content=(message.content or "")[:_PREVIEW_CHARS],
        timestamp=message.timestamp,
    )


@router.post("/api/conversations/{document_id}/messages/{message_id}/pin")
async def pin_message(
    document_id: uuid.UUID,
    message_id: int,
    request: PinRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Create or update one personal pin, keyed on the message's stable anchor."""
    message = await _authorized_message(db, user, document_id, message_id)
    source_id, line_number = _message_anchor(message)
    note = request.note if request is not None else None
    statement = insert(PinnedMessage).values(
        id=uuid.uuid4(),
        user_id=user.id,
        document_id=document_id,
        source_id=source_id,
        line_number=line_number,
        message_id=message.id,
        note=note,
    )
    if source_id is not None:
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "document_id", "source_id"],
            index_where=text("source_id IS NOT NULL"),
            set_={"note": note, "message_id": message.id, "line_number": line_number},
        )
    else:
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "document_id", "line_number"],
            index_where=text("source_id IS NULL"),
            set_={"note": note, "message_id": message.id},
        )
    pin = (await db.execute(statement.returning(PinnedMessage))).scalar_one()
    await db.commit()
    return {
        **_pin_payload(pin),
        "message_id": message.id,
        "message": _preview_for_message(message),
    }


@router.delete("/api/conversations/{document_id}/messages/{message_id}/pin")
async def unpin_message(
    document_id: uuid.UUID,
    message_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Remove a personal pin by its stable anchor. Repeat deletes stay successful."""
    message = await _authorized_message(db, user, document_id, message_id)
    source_id, line_number = _message_anchor(message)
    if source_id is not None:
        anchor = PinnedMessage.source_id == source_id
    else:
        anchor = and_(
            PinnedMessage.source_id.is_(None),
            PinnedMessage.line_number == line_number,
        )
    await db.execute(
        delete(PinnedMessage).where(
            PinnedMessage.user_id == user.id,
            PinnedMessage.document_id == document_id,
            anchor,
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
            .outerjoin(ConversationMessage, _resolved_message_join())
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
                "message_id": message_id,
                "message": _preview_payload(
                    message_id=message_id,
                    line_number=line_number if line_number is not None else pin.line_number,
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
            Document.title,
            Document.tool_id,
        )
        .join(Document, Document.id == PinnedMessage.document_id)
        .outerjoin(ConversationMessage, _resolved_message_join())
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
                "message_id": message_id,
                "conversation_ref": str(pin.document_id),
                "message": _preview_payload(
                    message_id=message_id,
                    line_number=line_number if line_number is not None else pin.line_number,
                    role=role,
                    message_type=message_type,
                    content=content,
                    timestamp=timestamp,
                ),
                "document": {
                    "id": str(pin.document_id),
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
                title,
                tool_id,
            ) in page
        ],
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }
