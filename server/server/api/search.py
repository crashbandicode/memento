"""Search API — full-text search across all synced content."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, Machine, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
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
        bounded_content_snippet,
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

    # Fetch page + total in one query. COUNT(*) OVER () reuses the same bitmap
    # index plan as the page query, avoiding a separate seq-scan-based count.
    total_col = func.count().over().label("_total")
    paged = (
        query.add_columns(total_col)
        .order_by(Document.synced_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(paged)).mappings().all()
    total = rows[0]["_total"] if rows else 0

    # Fetch at most one bounded matching normalized message per conversation
    # result. The main page query selects metadata plus a SQL-bounded snippet,
    # never a potentially 64 MiB Document.content value.
    conversation_ids = [row["id"] for row in rows if row["category"] == "conversation"]
    normalized_snippets: dict = {}
    if conversation_ids:
        ranked = (
            select(
                ConversationMessage.document_id.label("document_id"),
                func.left(ConversationMessage.content, 500).label("content"),
                func.row_number()
                .over(
                    partition_by=ConversationMessage.document_id,
                    order_by=ConversationMessage.line_number,
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
    for row in rows:
        snippet = normalized_snippets.get(row["id"], row["content_snippet"] or "")

        items.append(
            {
                "id": str(row["id"]),
                "tool_id": row["tool_id"],
                "relative_path": row["relative_path"],
                "category": row["category"],
                "title": row["title"],
                "snippet": snippet,
                "file_size_bytes": row["file_size_bytes"],
                "synced_at": row["synced_at"].isoformat(),
            }
        )

    return {
        "query": q,
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": items,
    }
