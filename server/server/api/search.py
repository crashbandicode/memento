"""Search API — full-text search across all synced content."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, literal, or_, select
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
from ..services.message_search import (
    MAX_SEARCH_CONTENT_CHARS,
    MessageSearchCursor,
    build_message_search_expressions,
    cursor_after_predicate,
    decode_search_cursor,
    encode_search_cursor,
    make_search_snippet,
    normalize_search_query,
    suggest_corrected_query,
)
from ..services.user_filter import user_machine_ids, apply_user_filter

router = APIRouter(prefix="/api/search", tags=["search"])


def _conversation_ref(row) -> ConversationRef:
    return ConversationRef(
        document_id=row["document_id"],
        tool_id=row["tool_id"],
        relative_path=row["relative_path"],
        metadata=row["metadata"],
        title=row["title"],
        source_modified_at=row["source_modified_at"],
        activity_at=row["activity_at"],
        synced_at=row["synced_at"],
        file_size_bytes=row["file_size_bytes"],
    )


def _conversation_document_columns():
    return (
        Document.id.label("document_id"),
        Document.tool_id.label("tool_id"),
        Document.relative_path.label("relative_path"),
        Document.title.label("title"),
        Document.file_size_bytes.label("file_size_bytes"),
        Document.synced_at.label("synced_at"),
        Document.source_modified_at.label("source_modified_at"),
        Document.activity_at.label("activity_at"),
        Document.metadata_.label("metadata"),
    )


@router.get("/messages")
async def search_messages(
    q: str = Query(..., min_length=1, max_length=500),
    tool: str | None = None,
    device_id: str | None = None,
    project_id: uuid.UUID | None = None,
    days: int | None = Query(None, ge=1, le=3650),
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Search every normalized user/assistant message with bounded results.

    Message candidates are ranked in PostgreSQL through partial FTS/trigram
    GIN indexes, then the small top set is folded into canonical root threads.
    No transcript body or corpus-wide count is sent to the client.
    """
    query_text = normalize_search_query(q)
    if not query_text:
        return {
            "query": "",
            "results": [],
            "next_cursor": None,
            "has_more": False,
            "corrected_query": None,
        }
    mids = await user_machine_ids(db, _user)
    decoded_cursor = decode_search_cursor(cursor)
    sort_timestamp = func.coalesce(
        ConversationMessage.timestamp,
        Document.activity_at,
        Document.source_modified_at,
        Document.synced_at,
    )

    # Logical folding can collapse copies and subagents. Over-fetch only a
    # bounded top slice rather than ranking or hydrating the entire result set.
    candidate_limit = min(limit * 12 + 1, 401)

    def candidate_statement(search_text: str, *, corrected: bool = False):
        expressions = build_message_search_expressions(search_text)
        # Corrected terms are intentionally ranked below every literal/FTS
        # match for the user's original query. The compact lexicon resolves a
        # typo first; PostgreSQL then uses the same message FTS/trigram indexes
        # instead of fuzzy-scanning large transcript bodies.
        score = (
            literal(1.0) + func.least(expressions.score - 3.0, 0.999999)
            if corrected
            else expressions.score
        )
        match_type = literal("fuzzy") if corrected else expressions.match_type
        statement = (
            select(
                ConversationMessage.id.label("message_id"),
                ConversationMessage.line_number.label("line_number"),
                ConversationMessage.role.label("role"),
                func.left(
                    ConversationMessage.content,
                    MAX_SEARCH_CONTENT_CHARS,
                ).label("content"),
                ConversationMessage.timestamp.label("message_timestamp"),
                score.label("score"),
                match_type.label("match_type"),
                sort_timestamp.label("sort_timestamp"),
                *_conversation_document_columns(),
            )
            .join(Document, ConversationMessage.document_id == Document.id)
            .where(
                Document.category == "conversation",
                expressions.predicate,
            )
        )
        if tool:
            statement = statement.where(Document.tool_id == tool)
        if project_id:
            statement = statement.where(Document.project_id == project_id)
        if device_id:
            statement = statement.where(
                Document.machine_id.in_(
                    select(Machine.id).where(
                        Machine.collector_token_hash == device_id
                    )
                )
            )
        if days:
            statement = statement.where(
                sort_timestamp >= datetime.now(timezone.utc) - timedelta(days=days)
            )
        statement = apply_user_filter(statement, mids, Document.machine_id)
        if decoded_cursor:
            statement = statement.where(
                cursor_after_predicate(decoded_cursor, score, sort_timestamp)
            )
        return statement.order_by(
            score.desc(),
            sort_timestamp.desc(),
            ConversationMessage.id.desc(),
        )

    primary_rows = (
        await db.execute(candidate_statement(query_text).limit(candidate_limit))
    ).mappings().all()
    candidates = [dict(row, snippet_query=query_text) for row in primary_rows]
    corrected_query = None
    if len(candidates) < candidate_limit:
        corrected_query = await suggest_corrected_query(db, query_text)
        if corrected_query:
            remaining = candidate_limit - len(candidates)
            corrected_rows = (
                await db.execute(
                    candidate_statement(corrected_query, corrected=True).limit(
                        remaining
                    )
                )
            ).mappings().all()
            seen_message_ids = {row["message_id"] for row in candidates}
            candidates.extend(
                dict(row, snippet_query=corrected_query)
                for row in corrected_rows
                if row["message_id"] not in seen_message_ids
            )
    candidates.sort(
        key=lambda row: (
            float(row["score"] or 0.0),
            row["sort_timestamp"],
            row["message_id"],
        ),
        reverse=True,
    )
    if not candidates:
        return {
            "query": query_text,
            "results": [],
            "next_cursor": None,
            "has_more": False,
            "corrected_query": corrected_query,
        }

    matched_rows_by_id = {row["document_id"]: row for row in candidates}
    matched_refs = [_conversation_ref(row) for row in matched_rows_by_id.values()]
    roots_by_tool = group_conversation_root_thread_ids(
        matched_refs,
        path_children_only=True,
    )
    hierarchy_rows = dict(matched_rows_by_id)
    if roots_by_tool:
        companion_query = select(*_conversation_document_columns()).where(
            Document.category == "conversation",
            build_conversation_companion_filter(
                Document.tool_id,
                Document.metadata_,
                Document.relative_path,
                roots_by_tool,
            ),
        )
        if tool:
            companion_query = companion_query.where(Document.tool_id == tool)
        if project_id:
            companion_query = companion_query.where(Document.project_id == project_id)
        if device_id:
            companion_query = companion_query.where(
                Document.machine_id.in_(
                    select(Machine.id).where(
                        Machine.collector_token_hash == device_id
                    )
                )
            )
        companion_query = apply_user_filter(
            companion_query,
            mids,
            Document.machine_id,
        )
        companion_rows = (await db.execute(companion_query)).mappings().all()
        hierarchy_rows.update(
            {row["document_id"]: row for row in companion_rows}
        )

    refs = [_conversation_ref(row) for row in hierarchy_rows.values()]
    hierarchy = fold_conversation_subagents(refs)
    subagents_by_document = build_subagent_summaries(hierarchy, refs)
    logical_activity = build_logical_activity_map(hierarchy, refs)
    already_seen = set(
        decoded_cursor.seen_document_ids if decoded_cursor else ()
    )
    groups: dict[object, dict] = {}
    last_processed = None
    has_more = False

    for row in candidates:
        canonical_id = hierarchy.canonical_document_ids.get(
            row["document_id"],
            row["document_id"],
        )
        canonical_key = str(canonical_id)
        if canonical_key in already_seen:
            last_processed = row
            continue
        group = groups.get(canonical_id)
        if group is None:
            if len(groups) >= limit:
                has_more = True
                break
            presentation = hierarchy_rows.get(canonical_id, row)
            activity_at = (
                logical_activity.get(canonical_id)
                or presentation["activity_at"]
                or presentation["source_modified_at"]
                or presentation["synced_at"]
            )
            group = {
                "id": canonical_key,
                "tool_id": presentation["tool_id"],
                "relative_path": presentation["relative_path"],
                "title": presentation["title"],
                "activity_at": activity_at.isoformat() if activity_at else None,
                "subagent_count": hierarchy.subagent_counts.get(canonical_id, 0),
                "is_subagent_orphan": (
                    canonical_id in hierarchy.orphan_document_ids
                ),
                "subagents": subagents_by_document.get(canonical_id, []),
                "hits": [],
            }
            groups[canonical_id] = group
        if len(group["hits"]) < 3:
            group["hits"].append(
                {
                    "id": row["message_id"],
                    "line_number": row["line_number"],
                    "role": row["role"],
                    "snippet": make_search_snippet(
                        row["content"], row["snippet_query"]
                    ),
                    "timestamp": (
                        row["message_timestamp"].isoformat()
                        if row["message_timestamp"] else None
                    ),
                    "score": round(float(row["score"] or 0.0), 6),
                    "match_type": row["match_type"],
                    "matched_document_id": str(row["document_id"]),
                    "is_subagent_hit": row["document_id"] != canonical_id,
                }
            )
        last_processed = row

    if not has_more and len(candidates) == candidate_limit:
        has_more = True
    next_cursor = None
    if has_more and last_processed is not None:
        seen_ids = list(already_seen)
        seen_ids.extend(str(document_id) for document_id in groups)
        next_cursor = encode_search_cursor(
            MessageSearchCursor(
                score=float(last_processed["score"] or 0.0),
                timestamp=last_processed["sort_timestamp"],
                message_id=int(last_processed["message_id"]),
                seen_document_ids=tuple(dict.fromkeys(seen_ids)),
            )
        )

    return {
        "query": query_text,
        "results": list(groups.values()),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "corrected_query": corrected_query,
    }


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
