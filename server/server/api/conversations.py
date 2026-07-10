"""Conversations API — paginated message viewer with normalized parsing."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..db.models import ConversationMessage, Document, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_parser import (
    count_conversation_messages,
    normalize_tool_calls,
    parse_conversation,
)
from ..services.conversation_hierarchy import (
    ConversationRef,
    FOLDABLE_CONVERSATION_TOOLS,
    build_conversation_companion_filter,
    build_logical_activity_map,
    build_subagent_summaries,
    effective_conversation_timestamp,
    fold_conversation_subagents,
    group_conversation_root_thread_ids,
)
from ..services.user_filter import user_machine_ids

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _parsed_tool_calls(message: object) -> list[dict[str, str]]:
    return normalize_tool_calls(getattr(message, "tool_calls", None))


def _stored_tool_calls(metadata: object) -> list[dict[str, str]]:
    """Read the same bounded tool-call shape used by raw-content parsing."""
    if not isinstance(metadata, dict):
        return []
    return normalize_tool_calls(metadata.get("tool_calls"))


@router.get("/{doc_id}")
async def get_conversation(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get conversation metadata and message count."""
    mids = await user_machine_ids(db, _user)

    result = await db.execute(
        select(Document)
        .options(load_only(
            Document.id,
            Document.machine_id,
            Document.tool_id,
            Document.title,
            Document.relative_path,
            Document.metadata_,
            Document.source_modified_at,
            Document.activity_at,
            Document.synced_at,
            Document.file_size_bytes,
        ))
        .where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404)
    if mids is not None and doc.machine_id not in mids:
        raise HTTPException(status_code=404)

    # Normalized rows are written transactionally during ingest and are the
    # viewer's indexed representation.  Prefer their cheap indexed count over
    # hydrating and reparsing a potentially hundreds-of-megabytes JSONL blob.
    count_result = await db.execute(
        select(func.count()).where(ConversationMessage.document_id == doc_id)
    )
    message_count = count_result.scalar() or 0
    if message_count == 0:
        raw_content = (
            await db.execute(select(Document.content).where(Document.id == doc_id))
        ).scalar_one_or_none()
        if raw_content:
            message_count = count_conversation_messages(raw_content, doc.tool_id)

    subagents: list[dict] = []
    is_subagent_orphan = False
    logical_activity: dict = {}
    if doc.tool_id in FOLDABLE_CONVERSATION_TOOLS:
        current_ref = ConversationRef(
            document_id=doc.id,
            tool_id=doc.tool_id,
            relative_path=doc.relative_path,
            metadata=doc.metadata_,
            title=doc.title,
            source_modified_at=doc.source_modified_at,
            activity_at=doc.activity_at,
            synced_at=doc.synced_at,
            file_size_bytes=doc.file_size_bytes,
        )
        roots_by_tool = group_conversation_root_thread_ids([current_ref])
        hierarchy_scope = or_(
            Document.id == doc.id,
            build_conversation_companion_filter(
                Document.tool_id,
                Document.metadata_,
                Document.relative_path,
                roots_by_tool,
            ),
        )
        hierarchy_q = (
            select(Document)
            .options(load_only(
                Document.id,
                Document.machine_id,
                Document.tool_id,
                Document.title,
                Document.relative_path,
                Document.metadata_,
                Document.source_modified_at,
                Document.activity_at,
                Document.synced_at,
                Document.file_size_bytes,
            ))
            .where(
                Document.tool_id == doc.tool_id,
                Document.category == "conversation",
                hierarchy_scope,
            )
        )
        if mids is not None:
            hierarchy_q = hierarchy_q.where(Document.machine_id.in_(mids))
        hierarchy_docs = (await db.execute(hierarchy_q)).scalars().all()
        hierarchy_refs = [
            ConversationRef(
                document_id=item.id,
                tool_id=item.tool_id,
                relative_path=item.relative_path,
                metadata=item.metadata_,
                title=item.title,
                source_modified_at=item.source_modified_at,
                activity_at=item.activity_at,
                synced_at=item.synced_at,
                file_size_bytes=item.file_size_bytes,
            )
            for item in hierarchy_docs
        ]
        hierarchy = fold_conversation_subagents(hierarchy_refs)
        logical_activity = build_logical_activity_map(
            hierarchy,
            hierarchy_refs,
        )
        subagents = build_subagent_summaries(
            hierarchy,
            hierarchy_refs,
        ).get(doc.id, [])
        is_subagent_orphan = doc.id in hierarchy.orphan_document_ids

    # Find related brain artifacts (same session_id)
    related_plans = []
    session_id = doc.metadata_.get("session_id") or doc.metadata_.get("cascade_id")
    if session_id and doc.tool_id == "antigravity":
        plans_q = (
            select(Document)
            .where(
                Document.tool_id == "antigravity",
                Document.category == "plan",
                Document.metadata_["session_id"].astext == session_id,
            )
            .order_by(Document.synced_at.desc())
        )
        # Scope related plans to same user — matching session_id alone could
        # surface another user's brain artifacts if they happened to share an ID.
        if mids is not None:
            plans_q = plans_q.where(Document.machine_id.in_(mids))
        plans_result = await db.execute(plans_q)
        for p in plans_result.scalars().all():
            # Skip resolved versions and metadata JSON
            if ".resolved" in p.relative_path or ".metadata.json" in p.relative_path:
                continue
            related_plans.append({
                "id": str(p.id),
                "title": p.title,
                "relative_path": p.relative_path,
                "category": p.category,
                "content_type": p.content_type,
                "content": p.content[:5000] if p.content else None,
                "file_size_bytes": p.file_size_bytes,
                "synced_at": p.synced_at.isoformat(),
            })

    activity_at = logical_activity.get(doc.id) or effective_conversation_timestamp(
        ConversationRef(
            document_id=doc.id,
            tool_id=doc.tool_id,
            relative_path=doc.relative_path,
            metadata=doc.metadata_,
            source_modified_at=doc.source_modified_at,
            activity_at=doc.activity_at,
            synced_at=doc.synced_at,
        )
    )

    return {
        "id": str(doc.id),
        "tool_id": doc.tool_id,
        "title": doc.title,
        "relative_path": doc.relative_path,
        "metadata": doc.metadata_,
        "message_count": message_count,
        "subagent_count": len(subagents),
        "is_subagent_orphan": is_subagent_orphan,
        "subagents": subagents,
        "activity_at": activity_at.isoformat() if activity_at else None,
        "synced_at": doc.synced_at.isoformat(),
        "related_plans": related_plans,
    }


@router.get("/{doc_id}/messages")
async def get_conversation_messages(
    doc_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    line_number: int | None = Query(None, ge=1),
    context_before: int = Query(0, ge=0, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get paginated, human-readable conversation messages."""
    mids = await user_machine_ids(db, _user)

    result = await db.execute(
        select(Document)
        .options(load_only(
            Document.id,
            Document.machine_id,
            Document.tool_id,
        ))
        .where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404)
    if mids is not None and doc.machine_id not in mids:
        raise HTTPException(status_code=404)

    # Prefer normalized rows. They are indexed by document and line number,
    # preserve the viewer fields, and avoid reparsing the raw transcript for
    # every initial page, prompt jump, and scroll page.
    base_filter = [ConversationMessage.document_id == doc_id]
    count_result = await db.execute(
        select(func.count()).where(*base_filter)
    )
    total = count_result.scalar() or 0
    if total > 0:
        message_query = (
            select(ConversationMessage)
            .where(*base_filter)
            .order_by(ConversationMessage.line_number)
            .limit(limit)
        )
        if line_number is not None:
            start_line = max(1, line_number - context_before)
            start_count = await db.execute(
                select(func.count()).where(
                    *base_filter,
                    ConversationMessage.line_number < start_line,
                )
            )
            offset = start_count.scalar() or 0
            message_query = message_query.where(
                ConversationMessage.line_number >= start_line
            )
        else:
            message_query = message_query.offset(offset)

        msgs_result = await db.execute(message_query)
        messages = msgs_result.scalars().all()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "messages": [
                {
                    "id": m.id,
                    "line_number": m.line_number,
                    "role": m.role or m.message_type,
                    "content": m.content,
                    "thinking": (
                        (m.metadata_ or {}).get("thinking")
                        if m.metadata_ else None
                    ),
                    "tool_name": (m.metadata_ or {}).get("tool_name", ""),
                    "tool_input": (m.metadata_ or {}).get("tool_input", ""),
                    "session_context": (m.metadata_ or {}).get(
                        "session_context", ""
                    ),
                    "tool_calls": _stored_tool_calls(m.metadata_),
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                    "raw_type": m.message_type or "",
                }
                for m in messages
            ],
        }

    # Legacy/imported documents without normalized rows retain the tolerant
    # raw parser as a compatibility fallback.
    raw_content = (
        await db.execute(select(Document.content).where(Document.id == doc_id))
    ).scalar_one_or_none()
    if line_number is not None:
        offset = max(0, line_number - 1 - context_before)
    if raw_content:
        total = count_conversation_messages(raw_content, doc.tool_id)
        page = parse_conversation(raw_content, doc.tool_id, offset=offset, limit=limit)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "messages": [
                {
                    "id": offset + i,
                    "line_number": offset + i + 1,
                    "role": m.role,
                    "content": m.content,
                    "thinking": m.thinking or None,
                    "tool_name": m.tool_name,
                    "tool_input": m.tool_input,
                    "session_context": m.session_context,
                    "tool_calls": _parsed_tool_calls(m),
                    "timestamp": m.timestamp or None,
                    "raw_type": m.raw_type,
                }
                for i, m in enumerate(page)
            ],
        }
    return {"total": 0, "offset": offset, "limit": limit, "messages": []}


@router.get("/{doc_id}/prompts")
async def get_conversation_prompts(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return a lightweight outline of every meaningful human prompt."""
    mids = await user_machine_ids(db, _user)

    result = await db.execute(
        select(Document)
        .options(load_only(
            Document.id,
            Document.machine_id,
            Document.tool_id,
        ))
        .where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404)
    if mids is not None and doc.machine_id not in mids:
        raise HTTPException(status_code=404)

    normalized_count = (
        await db.execute(
            select(func.count()).where(ConversationMessage.document_id == doc_id)
        )
    ).scalar() or 0
    prompts = []
    if normalized_count > 0:
        prompt_rows = await db.execute(
            select(
                ConversationMessage.id,
                ConversationMessage.line_number,
                ConversationMessage.content,
                ConversationMessage.timestamp,
            )
            .where(
                ConversationMessage.document_id == doc_id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.line_number)
            .limit(2000)
        )
        for message_id, line_number, content, timestamp in prompt_rows.all():
            clean = (content or "").strip()
            if not clean or clean.startswith("[Subagent Context]"):
                continue
            prompts.append({
                "id": message_id,
                "line_number": line_number,
                "content": clean[:500],
                "timestamp": timestamp.isoformat() if timestamp else None,
            })
    else:
        raw_content = (
            await db.execute(select(Document.content).where(Document.id == doc_id))
        ).scalar_one_or_none()
        if raw_content:
            parsed = parse_conversation(raw_content, doc.tool_id)
            prompts = [
                {
                    "id": index,
                    "line_number": index + 1,
                    "content": message.content.strip()[:500],
                    "timestamp": message.timestamp or None,
                }
                for index, message in enumerate(parsed)
                if message.role == "user"
                and message.content.strip()
                and not message.content.lstrip().startswith("[Subagent Context]")
            ][:2000]

    return {"prompts": prompts}
