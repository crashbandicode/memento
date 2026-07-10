"""Search API — full-text search across all synced content."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, Machine, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_hierarchy import (
    ConversationRef,
    build_conversation_companion_filter,
    build_logical_activity_map,
    build_subagent_summaries,
    fold_conversation_subagents,
    group_conversation_root_thread_ids,
)
from ..services.user_filter import user_machine_ids, apply_user_filter

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=500),
    tool: str | None = None,
    category: str | None = None,
    device_id: str | None = None,
    days: int | None = Query(None, ge=1, le=3650),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Full-text search across title, path, and content.

    Three index-backed branches in one OR:
    1. ``title.ilike`` + ``relative_path.ilike`` — trigram GIN, fast for
       path / filename lookups.
    2. ``content_tsv @@ to_tsquery('simple', ...)`` — jieba-tokenized
       full-text, fast for keyword-in-body matches (even Chinese) and
       avoids the TOAST-heap scan that raw ``content.ilike`` triggers.

    The content_tsv path is the keyword fallback when the BGE-M3 semantic
    endpoint is slow or unavailable — earlier revisions fell through to
    an ilike on ``content`` which pulled every matching doc's full body
    out of TOAST and blew past the MCP client's 30s ceiling.
    """
    from ..services.tokenize import tokenize_for_query

    mids = await user_machine_ids(db, _user)
    search_term = f"%{q}%"
    tsquery = tokenize_for_query(q)

    conds = [
        Document.title.ilike(search_term),
        Document.relative_path.ilike(search_term),
    ]
    if tsquery:
        conds.append(Document.content_tsv.op("@@")(func.to_tsquery("simple", tsquery)))

    content_match_position = func.strpos(func.lower(Document.content), q.lower())
    bounded_content_snippet = case(
        (
            and_(
                Document.category != "conversation",
                Document.content.is_not(None),
                content_match_position > 0,
            ),
            func.substr(
                Document.content,
                func.greatest(content_match_position - 100, 1),
                len(q) + 200,
            ),
        ),
        else_="",
    ).label("content_snippet")
    query = select(
        Document.id.label("id"),
        Document.tool_id.label("tool_id"),
        Document.relative_path.label("relative_path"),
        Document.category.label("category"),
        Document.title.label("title"),
        Document.file_size_bytes.label("file_size_bytes"),
        Document.synced_at.label("synced_at"),
        Document.source_modified_at.label("source_modified_at"),
        Document.activity_at.label("activity_at"),
        Document.metadata_.label("metadata"),
    ).where(or_(*conds))

    if tool:
        query = query.where(Document.tool_id == tool)
    if category:
        query = query.where(Document.category == category)
    if device_id:
        query = query.where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.collector_token_hash == device_id)
            )
        )
    if days:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.where(Document.synced_at >= cutoff)
    query = apply_user_filter(query, mids, Document.machine_id)

    # Fetch lightweight matching metadata first, fold logical agent threads,
    # then paginate. Applying OFFSET before folding made page totals wrong and
    # let multi-host copies leak back in at page boundaries. No transcript
    # content is selected in this phase; bounded snippets are fetched only for
    # the final page below.
    all_rows = (
        await db.execute(
            query.order_by(
                case(
                    (
                        Document.category == "conversation",
                        func.coalesce(
                            Document.activity_at,
                            Document.source_modified_at,
                            Document.synced_at,
                        ),
                    ),
                    else_=Document.synced_at,
                ).desc(),
                Document.id.desc(),
            )
        )
    ).mappings().all()
    matched_conversation_refs = [
        ConversationRef(
            document_id=row["id"],
            tool_id=row["tool_id"],
            relative_path=row["relative_path"],
            metadata=row["metadata"],
            title=row["title"],
            source_modified_at=row["source_modified_at"],
            activity_at=row["activity_at"],
            synced_at=row["synced_at"],
            file_size_bytes=row["file_size_bytes"],
        )
        for row in all_rows
        if row["category"] == "conversation"
    ]
    roots_by_tool = group_conversation_root_thread_ids(
        matched_conversation_refs,
        path_children_only=True,
    )

    hierarchy_rows = {row["id"]: row for row in all_rows}
    if roots_by_tool:
        companions_q = select(
            Document.id.label("id"),
            Document.tool_id.label("tool_id"),
            Document.relative_path.label("relative_path"),
            Document.category.label("category"),
            Document.title.label("title"),
            Document.file_size_bytes.label("file_size_bytes"),
            Document.synced_at.label("synced_at"),
            Document.source_modified_at.label("source_modified_at"),
            Document.activity_at.label("activity_at"),
            Document.metadata_.label("metadata"),
        ).where(
            Document.category == "conversation",
            build_conversation_companion_filter(
                Document.tool_id,
                Document.metadata_,
                Document.relative_path,
                roots_by_tool,
            ),
        )
        if device_id:
            companions_q = companions_q.where(
                Document.machine_id.in_(
                    select(Machine.id).where(
                        Machine.collector_token_hash == device_id
                    )
                )
            )
        companions_q = apply_user_filter(
            companions_q,
            mids,
            Document.machine_id,
        )
        companion_rows = (await db.execute(companions_q)).mappings().all()
        hierarchy_rows.update({row["id"]: row for row in companion_rows})

    conversation_refs = [
        ConversationRef(
            document_id=row["id"],
            tool_id=row["tool_id"],
            relative_path=row["relative_path"],
            metadata=row["metadata"],
            title=row["title"],
            source_modified_at=row["source_modified_at"],
            activity_at=row["activity_at"],
            synced_at=row["synced_at"],
            file_size_bytes=row["file_size_bytes"],
        )
        for row in hierarchy_rows.values()
        if row["category"] == "conversation"
    ]
    hierarchy = fold_conversation_subagents(conversation_refs)
    subagents_by_document = build_subagent_summaries(
        hierarchy,
        conversation_refs,
    )
    logical_activity_by_document = build_logical_activity_map(
        hierarchy,
        conversation_refs,
    )
    rows_by_id = hierarchy_rows
    folded_rows: list[tuple[dict, dict]] = []
    emitted_ids: set = set()
    for matching_row in all_rows:
        canonical_id = hierarchy.canonical_document_ids.get(
            matching_row["id"],
            matching_row["id"],
        )
        if canonical_id in emitted_ids:
            continue
        # Position the canonical card at its best/newest matching member while
        # keeping the card link/title pointed at the canonical document.
        folded_rows.append((rows_by_id.get(canonical_id, matching_row), matching_row))
        emitted_ids.add(canonical_id)
    folded_rows.sort(
        key=lambda pair: (
            (
                logical_activity_by_document.get(pair[0]["id"])
                or pair[0]["activity_at"]
                or pair[0]["source_modified_at"]
                or pair[0]["synced_at"]
            )
            if pair[0]["category"] == "conversation"
            else pair[0]["synced_at"],
            str(pair[0]["id"]),
        ),
        reverse=True,
    )
    total = len(folded_rows)
    page_rows = folded_rows[offset:offset + limit]
    rows = [presentation for presentation, _match in page_rows]

    non_conversation_ids = [
        row["id"] for row in rows if row["category"] != "conversation"
    ]
    content_snippets: dict = {}
    if non_conversation_ids:
        snippet_rows = (
            await db.execute(
                select(Document.id, bounded_content_snippet).where(
                    Document.id.in_(non_conversation_ids)
                )
            )
        ).all()
        content_snippets = {
            document_id: snippet or ""
            for document_id, snippet in snippet_rows
        }

    # Fetch at most one bounded matching normalized message per conversation
    # result. The main page query selects metadata plus a SQL-bounded snippet,
    # never a potentially 64 MiB Document.content value.
    conversation_ids = [
        match["id"]
        for _presentation, match in page_rows
        if match["category"] == "conversation"
    ]
    normalized_snippets: dict = {}
    if conversation_ids:
        ranked = (
            select(
                ConversationMessage.document_id.label("document_id"),
                func.left(ConversationMessage.content, 500).label("content"),
                func.row_number()
                .over(
                    partition_by=ConversationMessage.document_id,
                    order_by=(
                        ConversationMessage.line_number,
                        ConversationMessage.id,
                    ),
                )
                .label("row_number"),
            )
            .where(
                ConversationMessage.document_id.in_(conversation_ids),
                ConversationMessage.content.ilike(search_term),
            )
            .subquery()
        )
        snippet_rows = (
            await db.execute(
                select(ranked.c.document_id, ranked.c.content).where(
                    ranked.c.row_number == 1
                )
            )
        ).all()
        normalized_snippets = {
            document_id: content for document_id, content in snippet_rows
        }

    items = []
    for row, matching_row in page_rows:
        activity_at = (
            logical_activity_by_document.get(row["id"])
            or row["activity_at"]
        )
        snippet = normalized_snippets.get(
            matching_row["id"],
            content_snippets.get(matching_row["id"], ""),
        )
        matching_metadata = matching_row["metadata"] or {}
        matched_subagent_id = (
            str(matching_row["id"])
            if (
                str(matching_metadata.get("thread_source") or "").strip().lower()
                == "subagent"
                and matching_row["id"] != row["id"]
            )
            else None
        )

        items.append(
            {
                "id": str(row["id"]),
                "tool_id": row["tool_id"],
                "relative_path": row["relative_path"],
                "category": row["category"],
                "title": row["title"],
                "snippet": snippet,
                "file_size_bytes": row["file_size_bytes"],
                "activity_at": activity_at.isoformat() if activity_at else None,
                "synced_at": row["synced_at"].isoformat(),
                "subagent_count": hierarchy.subagent_counts.get(row["id"], 0),
                "is_subagent_orphan": (
                    row["id"] in hierarchy.orphan_document_ids
                ),
                "subagents": subagents_by_document.get(row["id"], []),
                "matched_subagent_id": matched_subagent_id,
            }
        )

    return {
        "query": q,
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": items,
    }
