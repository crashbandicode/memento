"""Projects API — browse projects and their documents."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..db.models import (
    ConversationMessage, Document, KnowledgeEntity, KnowledgeObservation,
    KnowledgeRelation, Machine, Project, Tool, User,
)
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_activity import (
    ConversationActivitySummary,
    conversation_activity_summaries,
    historical_conversation_activity_query,
    is_low_activity_messages,
)
from ..services.conversation_hierarchy import (
    ConversationRef,
    FOLDABLE_CONVERSATION_TOOLS,
    build_logical_activity_map,
    build_subagent_summaries,
    conversation_root_thread_id,
    current_thread_id,
    effective_conversation_timestamp,
    explicit_subagent_parent_thread_id,
    fold_conversation_subagents,
)
from ..services.user_filter import user_machine_ids, apply_user_filter

router = APIRouter(prefix="/api/projects", tags=["projects"])


async def _resolve_project_device_id(
    db: AsyncSession,
    user: User,
    device_id: str,
) -> uuid.UUID:
    """Resolve a public collector device ID, with a database UUID fallback.

    The device selector exposes ``Machine.collector_token_hash`` as
    ``device_id``.  Older callers can still send the database UUID.  Resolve
    the public ID first so a UUID-shaped collector ID cannot accidentally
    select a different machine through the fallback.
    """
    machine = (
        await db.execute(
            select(Machine)
            .options(load_only(Machine.id, Machine.user_id))
            .where(Machine.collector_token_hash == device_id)
        )
    ).scalar_one_or_none()

    if machine is None:
        try:
            machine_uuid = uuid.UUID(device_id)
        except ValueError:
            machine_uuid = None
        if machine_uuid is not None:
            machine = (
                await db.execute(
                    select(Machine)
                    .options(load_only(Machine.id, Machine.user_id))
                    .where(Machine.id == machine_uuid)
                )
            ).scalar_one_or_none()

    if (
        machine is None
        or (
            user.role not in ("admin", "owner")
            and machine.user_id != user.id
        )
    ):
        # Do not disclose whether an inaccessible device exists.
        raise HTTPException(status_code=404, detail="Device not found")
    return machine.id


@router.get("")
async def list_projects(
    tool_id: str | None = None,
    device_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    selected_machine_id = (
        await _resolve_project_device_id(db, _user, device_id)
        if device_id
        else None
    )
    # A selected machine was already ownership-checked above.  Preserve the
    # existing all-device user filter when no explicit device is selected.
    mids = (
        None
        if selected_machine_id is not None
        else await user_machine_ids(db, _user)
    )

    # Single query: projects LEFT JOIN documents, GROUP BY, count documents
    doc_count_col = func.count(Document.id).label("doc_count")
    join_cond = Document.project_id == Project.id
    if selected_machine_id is not None:
        join_cond = join_cond & (Document.machine_id == selected_machine_id)
    elif mids is not None:
        join_cond = join_cond & Document.machine_id.in_(mids)

    query = (
        select(Project, doc_count_col)
        .outerjoin(Document, join_cond)
        .group_by(Project.id)
        .order_by(Project.updated_at.desc(), Project.id)
    )
    if tool_id:
        query = query.where(Project.tool_id == tool_id)
    if selected_machine_id is not None or mids is not None:
        # Exclude projects with zero visible docs for the selected scope.
        query = query.having(doc_count_col > 0)

    result = await db.execute(query)
    rows = result.all()

    # The aggregate above intentionally stays cheap, but its raw document
    # count includes every native subagent transcript. Reconcile agent-capable
    # project counts with the same root/subagent presentation used by project
    # detail and search so each card agrees with its drill-down.
    logical_document_counts = {p.id: count or 0 for p, count in rows}
    foldable_project_ids = [
        p.id
        for p, _count in rows
        if p.tool_id in FOLDABLE_CONVERSATION_TOOLS
    ]
    if foldable_project_ids:
        foldable_conversations_q = (
            select(Document)
            .options(load_only(
                Document.id,
                Document.project_id,
                Document.tool_id,
                Document.title,
                Document.relative_path,
                Document.metadata_,
                Document.source_modified_at,
                Document.activity_at,
                Document.synced_at,
                Document.file_size_bytes,
                Document.machine_id,
            ))
            .where(
                Document.project_id.in_(foldable_project_ids),
                Document.tool_id.in_(FOLDABLE_CONVERSATION_TOOLS),
                Document.category == "conversation",
            )
        )
        if selected_machine_id is not None:
            foldable_conversations_q = foldable_conversations_q.where(
                Document.machine_id == selected_machine_id
            )
        else:
            foldable_conversations_q = apply_user_filter(
                foldable_conversations_q,
                mids,
                Document.machine_id,
            )
        foldable_documents = (
            await db.execute(foldable_conversations_q)
        ).scalars().all()
        conversations_by_project: dict[uuid.UUID, list[Document]] = {}
        for document in foldable_documents:
            if document.project_id is not None:
                conversations_by_project.setdefault(
                    document.project_id,
                    [],
                ).append(document)
        for project_id, documents in conversations_by_project.items():
            refs = [
                ConversationRef(
                    document_id=document.id,
                    tool_id=document.tool_id,
                    relative_path=document.relative_path,
                    metadata=document.metadata_,
                    title=document.title,
                    source_modified_at=document.source_modified_at,
                    activity_at=document.activity_at,
                    synced_at=document.synced_at,
                    file_size_bytes=document.file_size_bytes,
                )
                for document in documents
            ]
            hierarchy = fold_conversation_subagents(refs)
            logical_document_counts[project_id] = (
                logical_document_counts.get(project_id, 0)
                - len(documents)
                + len(hierarchy.visible_document_ids)
            )

    return [
        {
            "id": str(p.id),
            "slug": p.slug,
            "title": p.title,
            "tool_id": p.tool_id,
            "source_path": p.source_path,
            "visibility": p.visibility,
            "document_count": logical_document_counts[p.id],
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p, count in rows
    ]


@router.get("/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    include_content: bool = Query(
        False,
        description="If true, inline full content for curated categories "
                    "(identity/memory/plan/learning/note). Conversation docs "
                    "stay metadata-only to keep payloads sane.",
    ),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    mids = await user_machine_ids(db, _user)

    project_q = select(Project).where(Project.id == project_id)
    if mids is not None:
        # Project rows are shared metadata and do not carry user_id.  Require
        # at least one document on a machine visible to this user so a guessed
        # UUID cannot disclose another user's title or source path.
        project_q = (
            project_q
            .join(Document, Document.project_id == Project.id)
            .where(Document.machine_id.in_(mids))
            .distinct()
        )
    result = await db.execute(project_q)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404)

    # Determine conversation visibility from the complete project before the
    # 50-row document window is applied.  A root can be older than its newest
    # child, and folding against only the page would incorrectly expose that
    # child as an orphan.
    conv_meta_q = (
        select(Document)
        .options(load_only(
            Document.id,
            Document.tool_id,
            Document.title,
            Document.relative_path,
            Document.metadata_,
            Document.source_modified_at,
            Document.activity_at,
            Document.synced_at,
            Document.file_size_bytes,
            Document.machine_id,
        ))
        .where(
            Document.project_id == project_id,
            Document.category == "conversation",
        )
    )
    conv_meta_q = apply_user_filter(conv_meta_q, mids, Document.machine_id)
    conv_meta = (await db.execute(conv_meta_q)).scalars().all()
    conversation_refs = [
        ConversationRef(
            document_id=d.id,
            tool_id=d.tool_id,
            relative_path=d.relative_path,
            metadata=d.metadata_,
            title=d.title,
            source_modified_at=d.source_modified_at,
            activity_at=d.activity_at,
            synced_at=d.synced_at,
            file_size_bytes=d.file_size_bytes,
        )
        for d in conv_meta
    ]
    conversation_hierarchy = fold_conversation_subagents(conversation_refs)
    subagents_by_document = build_subagent_summaries(
        conversation_hierarchy,
        conversation_refs,
    )
    logical_activity_by_document = build_logical_activity_map(
        conversation_hierarchy,
        conversation_refs,
    )

    # A reply in a folded subagent belongs to the root card's logical thread.
    # Feed that aggregate into SQL before LIMIT so a recently active root
    # cannot be omitted merely because its own transcript is older.
    logical_activity_order = (
        case(
            logical_activity_by_document,
            value=Document.id,
            else_=None,
        )
        if logical_activity_by_document
        else Document.activity_at
    )

    docs_q = (
        select(Document)
        .where(
            Document.project_id == project_id,
            or_(
                Document.category != "conversation",
                Document.id.in_(list(conversation_hierarchy.visible_document_ids)),
            ),
        )
        .order_by(
            case(
                (
                    Document.category == "conversation",
                    func.coalesce(
                        logical_activity_order,
                        Document.activity_at,
                        Document.source_modified_at,
                        Document.synced_at,
                    ),
                ),
                else_=Document.synced_at,
            ).desc(),
            Document.id.desc(),
        )
        .limit(50)
    )
    if not include_content:
        # The default endpoint is a metadata view.  Document.content and
        # rendered_html can each be megabytes, and selecting the ORM entity
        # without load_only hydrated those payloads only to discard them in
        # _doc_row below.
        docs_q = docs_q.options(load_only(
            Document.id,
            Document.relative_path,
            Document.category,
            Document.title,
            Document.file_size_bytes,
            Document.activity_at,
            Document.synced_at,
        ))
    docs_q = apply_user_filter(docs_q, mids, Document.machine_id)
    docs_result = await db.execute(docs_q)
    docs = docs_result.scalars().all()

    conversation_ids = [d.id for d in docs if d.category == "conversation"]
    activity_by_document = await conversation_activity_summaries(
        db,
        conversation_ids,
    )

    # Categories whose content is small + curated + worth inlining so a
    # single fetch is enough for an AI to "know the project". Conversation
    # docs are deliberately excluded — they can be MBs and the caller can
    # always paginate via memory_conversation(doc_id).
    INLINE_CATEGORIES = {"identity", "memory", "plan", "learning", "note"}

    def _doc_row(d: Document) -> dict:
        row = {
            "id": str(d.id),
            "relative_path": d.relative_path,
            "category": d.category,
            "title": d.title,
            "file_size_bytes": d.file_size_bytes,
            "synced_at": d.synced_at.isoformat(),
        }
        if include_content and d.category in INLINE_CATEGORIES:
            row["content"] = d.content
            if d.ai_summary:
                row["ai_summary"] = d.ai_summary
        if d.category == "conversation":
            activity_at = logical_activity_by_document.get(d.id) or d.activity_at
            row["activity_at"] = (
                activity_at.isoformat() if activity_at else None
            )
            activity = activity_by_document.get(
                d.id,
                ConversationActivitySummary(),
            )
            row["message_count"] = activity.message_count
            row["is_low_activity"] = activity.is_low_activity
            row["subagent_count"] = conversation_hierarchy.subagent_counts.get(
                d.id,
                0,
            )
            row["is_subagent_orphan"] = (
                d.id in conversation_hierarchy.orphan_document_ids
            )
            row["subagents"] = subagents_by_document.get(d.id, [])
        return row

    return {
        "id": str(project.id),
        "slug": project.slug,
        "title": project.title,
        "tool_id": project.tool_id,
        "source_path": project.source_path,
        "visibility": project.visibility,
        "documents": [_doc_row(d) for d in docs],
    }


@router.get("/{project_id}/timeline")
async def get_project_timeline(
    project_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    category: str | None = None,
    order: str = Query("desc", regex="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return a session-grouped timeline for a project.

    Groups documents by session_id (cascade_id), showing each session as a unit:
    - conversation with message preview
    - related brain artifacts (task.md, implementation_plan.md, walkthrough.md)
    Filters out .resolved versions and .metadata.json noise.
    """
    mids = await user_machine_ids(db, _user)

    proj_result = await db.execute(select(Project).where(Project.id == project_id))
    project = proj_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404)

    tool_map: dict[str, dict] = {}
    tools_result = await db.execute(select(Tool))
    for t in tools_result.scalars().all():
        tool_map[t.id] = {"display_name": t.display_name, "icon": t.icon}

    # Phase 1: lightweight scan — only metadata columns, NO content/rendered_html
    # content can be up to 1MB per doc; loading every doc's content is the main cost.
    meta_cols = (
        Document.id, Document.tool_id, Document.category, Document.content_type,
        Document.relative_path, Document.title, Document.file_size_bytes,
        Document.ai_summary, Document.metadata_,
        Document.source_modified_at, Document.synced_at, Document.machine_id,
        Document.activity_at,
    )
    q = (
        select(*meta_cols)
        .where(Document.project_id == project_id)
        .order_by(
            case(
                (
                    Document.category == "conversation",
                    func.coalesce(
                        Document.activity_at,
                        Document.source_modified_at,
                        Document.synced_at,
                    ),
                ),
                else_=func.coalesce(
                    Document.source_modified_at,
                    Document.synced_at,
                ),
            ).desc(),
            Document.id.desc(),
        )
    )
    if category:
        q = q.where(Document.category == category)
    q = apply_user_filter(q, mids, Document.machine_id)
    all_rows = (await db.execute(q)).all()

    conversation_refs = [
        ConversationRef(
            document_id=row[0],
            tool_id=row[1],
            relative_path=row[4],
            metadata=row[8],
            title=row[5],
            source_modified_at=row[9],
            activity_at=row[12],
            synced_at=row[10],
            file_size_bytes=row[6],
        )
        for row in all_rows
        if row[2] == "conversation"
    ]
    conversation_hierarchy = fold_conversation_subagents(conversation_refs)
    subagents_by_document = build_subagent_summaries(
        conversation_hierarchy,
        conversation_refs,
    )
    logical_activity_by_document = build_logical_activity_map(
        conversation_hierarchy,
        conversation_refs,
    )

    # Group by session_id — build sessions (no content yet)
    sessions: dict[str, dict] = {}
    standalone: list[dict] = []

    for r in all_rows:
        d_id, d_tool_id, d_category, d_ctype, d_path, d_title, d_size, \
            d_ai_summary, d_meta, d_src_mod, d_synced, _d_mid, _d_activity = r
        # Skip noise files
        # `.meta.json` is the Claude Code subagent sidecar (just type +
        # description, no conversation content). `.metadata.json` and
        # `.resolved` are transient versions. All are noise on a timeline.
        if ".resolved" in d_path or ".meta.json" in d_path or ".metadata.json" in d_path:
            continue
        if (
            d_category == "conversation"
            and d_id not in conversation_hierarchy.visible_document_ids
        ):
            continue

        session_id = current_thread_id(d_meta)
        logical_session_id = str(
            (d_meta or {}).get("root_session_id") or session_id or ""
        )
        effective_activity = (
            logical_activity_by_document.get(d_id)
            if d_category == "conversation"
            else None
        )
        timestamp = effective_activity or d_src_mod or d_synced
        ts = timestamp.isoformat()
        tool_info = tool_map.get(d_tool_id, {})

        if not session_id:
            event: dict = {
                "id": str(d_id),
                "type": d_category,
                "tool_id": d_tool_id,
                "tool_name": tool_info.get("display_name", d_tool_id),
                "title": d_title or d_path.split("/")[-1],
                "relative_path": d_path,
                "content_type": d_ctype,
                "timestamp": ts,
                "logical_session_id": logical_session_id or None,
                "file_size_bytes": d_size,
                "ai_summary": d_ai_summary,
            }
            if d_category == "conversation":
                event["activity_at"] = ts
                event["synced_at"] = d_synced.isoformat()
                event["preview_messages"] = []
                event["message_count"] = 0
                event["subagent_count"] = conversation_hierarchy.subagent_counts.get(
                    d_id,
                    0,
                )
                event["is_subagent_orphan"] = (
                    d_id in conversation_hierarchy.orphan_document_ids
                )
                event["subagents"] = subagents_by_document.get(d_id, [])
            standalone.append(event)
            continue

        if session_id not in sessions:
            sessions[session_id] = {
                "session_id": session_id,
                "logical_session_id": logical_session_id,
                "type": "session",
                "tool_id": d_tool_id,
                "tool_name": tool_info.get("display_name", d_tool_id),
                "timestamp": ts,
                "activity_at": None,
                "synced_at": None,
                "conversation": None,
                "artifacts": [],
                "subagent_count": 0,
                "is_subagent_orphan": False,
            }
        session = sessions[session_id]
        if ts > session["timestamp"]:
            session["timestamp"] = ts

        if d_category == "conversation":
            session["logical_session_id"] = logical_session_id
            session["timestamp"] = ts
            session["activity_at"] = ts
            session["synced_at"] = d_synced.isoformat()
            session["conversation"] = {
                "id": str(d_id),
                "title": d_title or d_path.split("/")[-1],
                "message_count": 0,
                "preview_messages": [],
                "file_size_bytes": d_size,
                "activity_at": ts,
                "synced_at": d_synced.isoformat(),
                "subagent_count": conversation_hierarchy.subagent_counts.get(
                    d_id,
                    0,
                ),
                "is_subagent_orphan": (
                    d_id in conversation_hierarchy.orphan_document_ids
                ),
                "subagents": subagents_by_document.get(d_id, []),
            }
            session["subagent_count"] = conversation_hierarchy.subagent_counts.get(
                d_id,
                0,
            )
            session["is_subagent_orphan"] = (
                d_id in conversation_hierarchy.orphan_document_ids
            )
            session["subagents"] = subagents_by_document.get(d_id, [])
        elif d_category == "plan":
            doc_type = d_path.split("/")[-1].split(".")[0]
            session["artifacts"].append({
                "id": str(d_id),
                "title": d_title or doc_type,
                "doc_type": doc_type,
                "content_preview": None,
                "file_size_bytes": d_size,
            })
        else:
            session["artifacts"].append({
                "id": str(d_id),
                "title": d_title or d_path.split("/")[-1],
                "doc_type": d_category,
                "content_preview": None,
                "file_size_bytes": d_size,
            })

    # Artifacts can be synced after their conversation.  A conversation card
    # remains anchored to effective thread activity; artifact-only sessions
    # keep their prior latest-document behavior.
    for session in sessions.values():
        if session.get("conversation") and session.get("activity_at"):
            session["timestamp"] = session["activity_at"]

    # Merge + sort + paginate BEFORE touching content
    all_events = list(sessions.values()) + standalone
    all_events.sort(
        key=lambda e: (
            e.get("timestamp", ""),
            e.get("session_id") or e.get("id") or "",
        ),
        reverse=(order == "desc"),
    )
    total = len(all_events)
    page = all_events[offset:offset + limit]

    # Set session title from conversation or first artifact
    for ev in page:
        if ev.get("type") == "session":
            if ev.get("conversation"):
                ev["title"] = ev["conversation"]["title"]
            elif ev.get("artifacts"):
                ev["title"] = ev["artifacts"][0]["title"]
            else:
                ev["title"] = ev["session_id"][:8]

    # Phase 2: collect only doc_ids referenced on THIS page
    page_conv_ids: set = set()
    page_plan_ids: set = set()
    for ev in page:
        if ev.get("type") == "session":
            if ev.get("conversation"):
                page_conv_ids.add(uuid.UUID(ev["conversation"]["id"]))
            for a in ev.get("artifacts") or []:
                page_plan_ids.add(uuid.UUID(a["id"]))
        elif ev.get("type") == "conversation":
            page_conv_ids.add(uuid.UUID(ev["id"]))
        else:
            page_plan_ids.add(uuid.UUID(ev["id"]))

    # Message counts for paginated conversations — one GROUP BY query
    msg_counts: dict = {}
    if page_conv_ids:
        cnt_result = await db.execute(
            select(ConversationMessage.document_id, func.count())
            .where(ConversationMessage.document_id.in_(page_conv_ids))
            .where(ConversationMessage.role.in_(("user", "assistant")))
            .group_by(ConversationMessage.document_id)
        )
        msg_counts = {row[0]: row[1] for row in cnt_result.all()}

    # Preview messages — fetch first 6 user/assistant messages per conv doc
    previews: dict = {}
    if page_conv_ids:
        # Use a window function via a lateral-like approach: just fetch first 10 rows per doc
        # by line_number, then filter in Python. Small constant per doc.
        msg_rows = await db.execute(
            select(ConversationMessage.id, ConversationMessage.document_id, ConversationMessage.role,
                   ConversationMessage.content, ConversationMessage.timestamp,
                   ConversationMessage.line_number)
            .where(ConversationMessage.document_id.in_(page_conv_ids))
            .where(ConversationMessage.role.in_(("user", "assistant")))
            .order_by(
                ConversationMessage.document_id,
                ConversationMessage.line_number,
                ConversationMessage.id,
            )
        )
        for message_id, doc_id, role, content, ts_val, line_number in msg_rows.all():
            lst = previews.setdefault(doc_id, [])
            if len(lst) < 4:
                lst.append({
                    "id": message_id,
                    "line_number": line_number,
                    "role": role,
                    "content": (content or "")[:300],
                    "tool_name": "",
                    "timestamp": ts_val.isoformat() if ts_val else None,
                })

    # Plan/other content previews — fetch only content for page plan docs
    plan_previews: dict = {}
    if page_plan_ids:
        plan_rows = await db.execute(
            select(Document.id, Document.category, Document.content)
            .where(Document.id.in_(page_plan_ids))
        )
        for pid, pcat, pcontent in plan_rows.all():
            if pcontent:
                cap = 500 if pcat == "plan" else 300
                plan_previews[pid] = pcontent[:cap]

    # Stitch back onto page
    for ev in page:
        if ev.get("type") == "session":
            conv = ev.get("conversation")
            if conv:
                cid = uuid.UUID(conv["id"])
                conv["message_count"] = msg_counts.get(cid, 0)
                conv["preview_messages"] = previews.get(cid, [])
            for a in ev.get("artifacts") or []:
                a["content_preview"] = plan_previews.get(uuid.UUID(a["id"]))
        elif ev.get("type") == "conversation":
            cid = uuid.UUID(ev["id"])
            ev["message_count"] = msg_counts.get(cid, 0)
            ev["preview_messages"] = previews.get(cid, [])
        else:
            ev["content_preview"] = plan_previews.get(uuid.UUID(ev["id"]))

    return {
        "project": {
            "id": str(project.id),
            "slug": project.slug,
            "title": project.title,
            "tool_id": project.tool_id,
            "source_path": project.source_path,
        },
        "total": total,
        "offset": offset,
        "limit": limit,
        "events": page,
    }


@router.get("/{project_id}/conversations")
async def get_project_conversations(
    project_id: uuid.UUID,
    session_offset: int = Query(0, ge=0),
    session_limit: int = Query(10, ge=1, le=50),
    max_messages_per_session: int = Query(0, ge=0, le=10000),
    order: str = Query("asc", regex="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
    as_of: datetime | None = None,  # internal — passed by share.py to cap visibility
) -> dict:
    """Return all conversations for a project merged into a continuous flow.

    Groups by session, each session contains:
    - conversation messages (parsed from JSONL)
    - brain artifacts (task.md, implementation_plan.md, walkthrough.md)
    Paginated by session (not by message).

    ``as_of`` (not a public query param — passed by share.py) caps
    docs to those synced and messages to those timestamped on or
    before that instant. Share-link snapshot semantics.
    """
    from ..services.cache import cache_get, cache_set
    # Cache by full query shape — different pages / orderings / preview
    # caps all need separate entries. 30s TTL: short enough that fresh
    # ingest shows up quickly, long enough to hide back/forward
    # navigation hits. ``as_of`` participates in the key so share
    # traffic doesn't poison the owner-UI cache.
    as_of_key = as_of.isoformat() if as_of else "live"
    cache_key = (
        f"project:conv:{_user.id}:{project_id}:v5:"
        f"{session_offset}:{session_limit}:{max_messages_per_session}:{order}:{as_of_key}"
    )
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    mids = await user_machine_ids(db, _user)

    proj_result = await db.execute(select(Project).where(Project.id == project_id))
    project = proj_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404)

    # Phase 1: scan only metadata columns to figure out pagination + subagent
    # grouping. Pulling Document.content (TOAST'd, can be 500 KB+ per doc)
    # for every doc in the project just to discard 90% of them blew up
    # cold-start time on chunky projects (favorite_chat hit 24 s with 152
    # conversations). load_only here ~halves wall time on big projects and
    # dramatically reduces TOAST reads.
    conv_q = (
        select(Document)
        .options(load_only(
            Document.id, Document.relative_path, Document.machine_id,
            Document.tool_id, Document.title, Document.metadata_,
            Document.source_modified_at, Document.activity_at,
            Document.synced_at,
            Document.file_size_bytes,
        ))
        .where(
            Document.project_id == project_id,
            Document.category == "conversation",
            Document.content_type.in_(("jsonl", "json")),
        )
        .order_by(
            func.coalesce(
                Document.activity_at,
                Document.source_modified_at,
                Document.synced_at,
            ).desc(),
            Document.id.desc(),
        )
    )
    conv_q = apply_user_filter(conv_q, mids, Document.machine_id)
    if as_of is not None:
        conv_q = conv_q.where(Document.synced_at <= as_of)
    all_convs = (await db.execute(conv_q)).scalars().all()

    activity_at_by_document = {d.id: d.activity_at for d in all_convs}
    if as_of is not None and all_convs:
        # Shared snapshots must not inherit a message timestamp added after
        # the link's cutoff.  Derive the persisted field's historical view in
        # one bounded GROUP BY, then feed it through normal logical folding.
        historical_activity_rows = await db.execute(
            historical_conversation_activity_query(
                (d.id for d in all_convs),
                as_of,
            )
        )
        historical_activity = dict(historical_activity_rows.all())
        activity_at_by_document = {
            d.id: historical_activity.get(d.id)
            for d in all_convs
        }

    main_convs: list = []
    explicit_children: list = []

    for d in all_convs:
        rp = d.relative_path or ""
        # Skip the sidecar noise that isn't real conversation content.
        if ".meta.json" in rp or ".metadata.json" in rp or ".resolved" in rp:
            continue
        if explicit_subagent_parent_thread_id(rp):
            explicit_children.append(d)
        else:
            main_convs.append(d)

    conversation_refs = [
        ConversationRef(
            document_id=d.id,
            tool_id=d.tool_id,
            relative_path=d.relative_path,
            metadata=d.metadata_,
            title=d.title,
            source_modified_at=d.source_modified_at,
            activity_at=activity_at_by_document.get(d.id),
            synced_at=d.synced_at,
            file_size_bytes=d.file_size_bytes,
        )
        for d in main_convs
    ]
    conversation_hierarchy = fold_conversation_subagents(conversation_refs)
    subagents_by_document = build_subagent_summaries(
        conversation_hierarchy,
        conversation_refs,
    )
    logical_activity_by_document = build_logical_activity_map(
        conversation_hierarchy,
        conversation_refs,
    )
    main_convs = [
        d
        for d in main_convs
        if d.id in conversation_hierarchy.visible_document_ids
    ]

    # Reuse the native root-ID resolver for both Claude Code's
    # `<root>.jsonl` layout and Cursor's `<root>/<root>.jsonl` layout.
    roots_by_tool_and_thread = {
        (d.tool_id, current_thread_id(d.metadata_)): d.id
        for d in main_convs
        if current_thread_id(d.metadata_)
    }
    subagent_map: dict[uuid.UUID, list] = {}
    for child in explicit_children:
        root_thread_id = conversation_root_thread_id(
            child.tool_id,
            child.relative_path,
            child.metadata_,
        )
        root_document_id = roots_by_tool_and_thread.get(
            (child.tool_id, root_thread_id)
        )
        if root_document_id is not None:
            subagent_map.setdefault(root_document_id, []).append(child)

    # Legacy ``/subagents/`` children are presented inline rather than folded
    # by metadata.  Their latest real turn still contributes to the parent
    # card's logical activity.
    group_activity_by_document = dict(logical_activity_by_document)
    for d in main_convs:
        candidates = [
            value
            for value in (
                group_activity_by_document.get(d.id),
                *(
                    effective_conversation_timestamp(
                        ConversationRef(
                            document_id=child.id,
                            tool_id=child.tool_id,
                            relative_path=child.relative_path,
                            metadata=child.metadata_,
                            source_modified_at=child.source_modified_at,
                            activity_at=activity_at_by_document.get(child.id),
                            synced_at=child.synced_at,
                        )
                    )
                    for child in subagent_map.get(d.id, [])
                ),
            )
            if value is not None
        ]
        if candidates:
            group_activity_by_document[d.id] = max(candidates)

    main_convs.sort(
        key=lambda d: (
            group_activity_by_document.get(d.id)
            or effective_conversation_timestamp(
                ConversationRef(
                    document_id=d.id,
                    tool_id=d.tool_id,
                    relative_path=d.relative_path,
                    metadata=d.metadata_,
                    source_modified_at=d.source_modified_at,
                    activity_at=activity_at_by_document.get(d.id),
                    synced_at=d.synced_at,
                )
            ),
            str(d.id),
        ),
        reverse=(order != "asc"),
    )
    total_sessions = len(main_convs)

    # Paginate by main session (subagents folded into parents)
    page_convs = main_convs[session_offset:session_offset + session_limit]

    # Get all plan docs for this project (for artifact embedding)
    plans_q = (
        select(Document)
        .where(Document.project_id == project_id, Document.category == "plan")
        .order_by(Document.synced_at, Document.id)
    )
    plans_q = apply_user_filter(plans_q, mids, Document.machine_id)
    plans_result = await db.execute(plans_q)
    all_plans = plans_result.scalars().all()

    # Index plans by session_id
    plans_by_session: dict[str, list] = {}
    for p in all_plans:
        rp = p.relative_path or ""
        if ".resolved" in rp or ".meta.json" in rp or ".metadata.json" in rp:
            continue
        sid = current_thread_id(p.metadata_) or ""
        if sid:
            plans_by_session.setdefault(sid, []).append(p)

    # Phase 2: read pre-parsed messages from conversation_messages instead of
    # re-walking the JSONL content per request. The original implementation
    # called parse_conversation(d.content, ...) per doc — for a project with
    # 5 sessions × 1 MB of JSONL each (favorite_chat), that is 5+ MB of
    # JSON.loads() + dataclass building per request, which dominated cold
    # latency (30 s observed).
    needed_ids: list = [d.id for d in page_convs]
    for d in page_convs:
        for child in subagent_map.get(d.id, []):
            needed_ids.append(child.id)
    msgs_by_doc: dict = {}
    if needed_ids:
        msg_q = (
            select(
                ConversationMessage.id,
                ConversationMessage.document_id,
                ConversationMessage.role,
                ConversationMessage.content,
                ConversationMessage.message_type,
                ConversationMessage.timestamp,
                ConversationMessage.line_number,
            )
            .where(ConversationMessage.document_id.in_(needed_ids))
            .order_by(
                ConversationMessage.document_id,
                ConversationMessage.line_number,
                ConversationMessage.id,
            )
        )
        if as_of is not None:
            # Either no timestamp recorded (legacy / parser miss — keep
            # those; the doc itself already passed the synced_at cap) or
            # timestamp before/at the snapshot moment.
            from sqlalchemy import or_ as _or
            msg_q = msg_q.where(_or(
                ConversationMessage.timestamp.is_(None),
                ConversationMessage.timestamp <= as_of,
            ))
        rows = await db.execute(msg_q)
        for message_id, did, role, content, mtype, ts, line_number in rows.all():
            if role not in ("user", "assistant"):
                continue
            if role == "user" and (
                content.startswith("[Result]")
                or content.startswith("[Tool:")
                or content.startswith('{"tool_use_id"')
            ):
                continue
            if role == "assistant" and (
                content.startswith("[Tool:")
                and "\n" not in content.split("[Tool:")[0]
            ):
                continue
            msgs_by_doc.setdefault(did, []).append({
                "id": message_id,
                "line_number": line_number,
                "role": role,
                "content": content,
                "thinking": None,
                "tool_name": "",
                "tool_input": "",
                "raw_type": mtype or "",
                "timestamp": ts.isoformat() if ts else None,
            })

    def _parse_doc_messages(d: Document) -> list[dict]:
        return msgs_by_doc.get(d.id, [])

    # Build session list — merge subagent messages into parent by timestamp
    sessions = []
    for d in page_convs:
        session_id = current_thread_id(d.metadata_) or ""
        logical_session_id = str(
            (d.metadata_ or {}).get("root_session_id") or session_id
        )
        group_activity_at = group_activity_by_document.get(d.id)
        effective_timestamp = (
            group_activity_at
            or effective_conversation_timestamp(
                ConversationRef(
                    document_id=d.id,
                    tool_id=d.tool_id,
                    relative_path=d.relative_path,
                    metadata=d.metadata_,
                    source_modified_at=d.source_modified_at,
                    activity_at=activity_at_by_document.get(d.id),
                    synced_at=d.synced_at,
                )
            )
        )
        ts = effective_timestamp.isoformat()

        # Parse main conversation messages
        messages = list(_parse_doc_messages(d))

        # Merge subagent messages inline, marked with subagent_name
        child_docs = subagent_map.get(d.id, [])
        for child in child_docs:
            child_msgs = [dict(message) for message in _parse_doc_messages(child)]
            child_name = child.title or (child.relative_path or "").split("/")[-1].replace(".jsonl", "")
            for m in child_msgs:
                m["subagent_name"] = child_name
            messages.extend(child_msgs)

        # Sort all messages by timestamp (interleaves main + subagent)
        if messages and messages[0].get("timestamp"):
            messages.sort(
                key=lambda x: (
                    x.get("timestamp") or "",
                    x.get("id") or 0,
                )
            )

        is_low_activity = is_low_activity_messages(messages)

        # Preview mode: clip to N messages per session (0 = no limit).
        # When the user is reading oldest-first (order=asc, default on /timeline),
        # show the *first* N so they see how the session started. When reading
        # newest-first (desc), show the *last* N — the latest state matters
        # more. Picking the wrong end is what made a user see a session
        # "start at 19:50" when the first real prompt was at 19:24 and
        # there were simply more than N messages in between.
        total_msgs = len(messages)
        if max_messages_per_session and total_msgs > max_messages_per_session:
            if order == "asc":
                messages = messages[:max_messages_per_session]
            else:
                messages = messages[-max_messages_per_session:]

        # Get artifacts for this session
        artifacts = []
        for p in plans_by_session.get(session_id, []):
            doc_type = p.relative_path.split("/")[-1].split(".")[0]
            artifacts.append({
                "id": str(p.id),
                "title": p.title or doc_type,
                "doc_type": doc_type,
                "content": p.content[:5000] if p.content else None,
                "file_size_bytes": p.file_size_bytes,
            })

        sessions.append({
            "session_id": session_id,
            "logical_session_id": logical_session_id,
            "title": d.title or session_id[:8],
            "conversation_id": str(d.id),
            "timestamp": ts,
            "activity_at": (
                group_activity_at.isoformat() if group_activity_at else None
            ),
            "synced_at": d.synced_at.isoformat(),
            "message_count": total_msgs,  # true total, not the clipped count
            "is_low_activity": is_low_activity,
            "subagent_count": (
                conversation_hierarchy.subagent_counts.get(d.id, 0)
                + len(child_docs)
            ),
            "is_subagent_orphan": (
                d.id in conversation_hierarchy.orphan_document_ids
            ),
            "subagents": subagents_by_document.get(d.id, []),
            "messages": messages,
            "truncated": bool(max_messages_per_session and total_msgs > max_messages_per_session),
            "artifacts": artifacts,
        })

    payload = {
        "project": {
            "id": str(project.id),
            "slug": project.slug,
            "title": project.title,
            "source_path": project.source_path,
        },
        "total_sessions": total_sessions,
        "session_offset": session_offset,
        "session_limit": session_limit,
        "order": order,
        "sessions": sessions,
    }
    await cache_set(cache_key, payload, ttl_seconds=30)
    return payload


# Heuristic: documents we want to drop on the floor when bootstrapping a
# new AI session. .meta.json is the Claude Code subagent sidecar (no
# conversation content); .metadata.json and .resolved are transient
# variants. Same noise filter the timeline uses.
def _is_export_noise(path: str) -> bool:
    return (
        ".resolved" in path
        or ".meta.json" in path
        or ".metadata.json" in path
    )


# Category order in the rendered Markdown. Memory / plan / identity go
# first because that's the "give the new AI context" payload; long
# conversation transcripts last because they're the bulk. Anything not
# in this list is appended in alphabetical order under a "Other" group.
_CATEGORY_ORDER = ["memory", "plan", "identity", "config", "skill", "learning", "note", "conversation"]
_CATEGORY_HEADERS_ZH = {
    "memory": "记忆", "plan": "计划", "identity": "身份卡",
    "config": "配置", "skill": "技能", "learning": "学习笔记",
    "note": "笔记", "conversation": "对话",
}


@router.get("/{project_id}/export.md")
async def export_project_markdown(
    project_id: uuid.UUID,
    include_conversations: bool = Query(True, description="Include the full conversation transcripts (can be very long)"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> Response:
    """Render a single project's documents + knowledge-graph context as a
    Markdown blob that can be dropped into a fresh AI project as
    ``MEMENTO-CONTEXT.md``. This is the "one-shot dump" alternative to
    the live MCP path — useful for portable / offline handoff.

    Same ownership filter as the rest of the project routes (admin /
    owner see everything via apply_user_filter shortcut).
    """
    mids = await user_machine_ids(db, _user)

    project = (await db.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404)

    # Pull all docs for this project (no 50-row cap; this is an export).
    docs_q = (
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.synced_at.desc())
    )
    docs_q = apply_user_filter(docs_q, mids, Document.machine_id)
    docs = [d for d in (await db.execute(docs_q)).scalars().all()
            if not _is_export_noise(d.relative_path)]

    # Knowledge-graph: any entity whose name fuzzy-matches the project
    # title. Mirrors what memory_context does for MCP, so the Markdown
    # carries the same context an AI calling memory_context would see.
    ent_q = (
        select(KnowledgeEntity)
        .where(or_(
            KnowledgeEntity.name.ilike(f"%{project.title}%"),
            KnowledgeEntity.name.ilike(f"%{project.slug}%"),
        ))
        .limit(20)
    )
    if mids is not None:
        # Non-admin: only entities owned by the caller.
        ent_q = ent_q.where(KnowledgeEntity.user_id == _user.id)
    entities = (await db.execute(ent_q)).scalars().all()

    lines: list[str] = []
    title = project.title or project.slug or "Untitled project"
    lines.append(f"# Memento context — {title}")
    lines.append("")
    lines.append(
        f"**Tool**: `{project.tool_id or 'unknown'}`  ·  "
        f"**Documents**: {len(docs)}  ·  "
        f"**Exported**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    if project.source_path:
        lines.append(f"**Source path**: `{project.source_path}`")
    lines.append("")
    lines.append(
        "> This file was exported from Memento as a one-shot bootstrap of\n"
        "> a project's prior context. Drop it into a fresh project as\n"
        "> `MEMENTO-CONTEXT.md` and add to your `CLAUDE.md` / `AGENTS.md`:\n"
        "> `Read MEMENTO-CONTEXT.md for prior conversations and decisions on this project.`"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Group docs by category.
    by_cat: dict[str, list[Document]] = {}
    for d in docs:
        by_cat.setdefault(d.category or "other", []).append(d)

    ordered_cats = (
        [c for c in _CATEGORY_ORDER if c in by_cat]
        + sorted([c for c in by_cat if c not in _CATEGORY_ORDER])
    )

    for cat in ordered_cats:
        if cat == "conversation" and not include_conversations:
            continue
        bucket = by_cat[cat]
        if not bucket:
            continue
        zh = _CATEGORY_HEADERS_ZH.get(cat, cat)
        lines.append(f"## {cat.title()} / {zh}  ({len(bucket)})")
        lines.append("")
        for d in bucket:
            doc_title = d.title or (d.relative_path.rsplit("/", 1)[-1] if d.relative_path else d.category)
            stamp = d.synced_at.strftime("%Y-%m-%d") if d.synced_at else ""
            lines.append(f"### {doc_title}")
            lines.append(f"*{d.relative_path}*  ·  *{stamp}*")
            lines.append("")
            # For conversations, prefer the AI summary if we have one —
            # the raw JSONL is too long to inline. For everything else,
            # the full content is the point.
            if cat == "conversation" and d.ai_summary:
                lines.append(d.ai_summary)
            elif d.content:
                # Fence as plain text. Don't trust the source for
                # well-formed markdown — but we DO trust it not to be
                # binary because the model rejects binary content_types
                # upstream. Cap per-doc to 200 KB so a single 1 MB
                # conversation log doesn't dominate the file; the AI
                # can call memory_open(id) for the rest.
                blob = d.content if len(d.content) < 200_000 else d.content[:200_000] + "\n\n…(truncated; call memory_open() for full text)"
                lines.append(blob)
            else:
                lines.append("*(empty)*")
            lines.append("")
            lines.append("---")
            lines.append("")

    if entities:
        lines.append("## Knowledge graph")
        lines.append("")
        ent_ids = [e.id for e in entities]
        # Fetch relations + observations in two batched queries.
        rels = (await db.execute(
            select(KnowledgeRelation, KnowledgeEntity)
            .join(KnowledgeEntity, KnowledgeRelation.target_id == KnowledgeEntity.id)
            .where(KnowledgeRelation.source_id.in_(ent_ids))
        )).all()
        rels_by_source: dict[uuid.UUID, list[tuple[str, str]]] = {}
        for r, target in rels:
            rels_by_source.setdefault(r.source_id, []).append((r.relation_type, target.name))

        obs = (await db.execute(
            select(KnowledgeObservation)
            .where(KnowledgeObservation.entity_id.in_(ent_ids))
            .order_by(KnowledgeObservation.observed_at.desc())
        )).scalars().all()
        obs_by_entity: dict[uuid.UUID, list[KnowledgeObservation]] = {}
        for o in obs:
            obs_by_entity.setdefault(o.entity_id, []).append(o)

        for e in entities:
            lines.append(f"### {e.name}  *({e.entity_type})*")
            if e.summary:
                lines.append("")
                lines.append(e.summary)
            ent_rels = rels_by_source.get(e.id, [])
            if ent_rels:
                lines.append("")
                lines.append("**Relations**:")
                for rel_type, target_name in ent_rels[:10]:
                    lines.append(f"- {rel_type} → **{target_name}**")
            ent_obs = obs_by_entity.get(e.id, [])
            if ent_obs:
                lines.append("")
                lines.append("**Recent observations**:")
                for o in ent_obs[:10]:
                    date_str = o.observed_at.strftime("%Y-%m-%d") if o.observed_at else ""
                    prefix = f"[{date_str}] " if date_str else ""
                    lines.append(f"- {prefix}{o.content}")
            lines.append("")
            lines.append("---")
            lines.append("")

    md = "\n".join(lines)
    # Per RFC 6266: file name with simple ASCII fallback + filename* for
    # unicode. The slug is already URL-safe; project.title may have
    # CJK so we just send ASCII fallback derived from slug.
    safe = "".join(c for c in (project.slug or "project") if c.isalnum() or c in "-_")[:64]
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="memento-context-{safe}.md"',
            # nginx-friendly: don't buffer multi-MB markdown.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{project_id}/blueprint")
async def get_project_blueprint(
    project_id: uuid.UUID,
    recent_convs: int = Query(10, ge=0, le=50,
        description="How many recent conversations to include (AI summary only, not full body)"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """One-shot "give me the project blueprint" for an AI agent.

    Combines into a single response everything you'd otherwise need
    ``memory_context`` + N×``memory_open`` + ``memory_graph`` + a
    handful of ``memory_recall`` calls to assemble:

      * project meta (title / tool / source_path)
      * full content of all identity / memory / plan / learning files
        (these are the curated "what is this project" docs)
      * recent conversation AI summaries (titles + ai_summary; no full body)
      * knowledge-graph entities + observations + relations whose name
        fuzzy-matches the project title or slug

    Powers the MCP ``memory_blueprint`` tool — replaces 5-10 sequential
    RPCs with one.
    """
    mids = await user_machine_ids(db, _user)

    project = (await db.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404)

    # 1. Curated docs full content (identity / memory / plan / learning)
    CURATED = ("identity", "memory", "plan", "learning")
    curated_q = (
        select(Document)
        .where(
            Document.project_id == project_id,
            Document.category.in_(CURATED),
        )
        .order_by(Document.synced_at.desc())
        .limit(100)
    )
    curated_q = apply_user_filter(curated_q, mids, Document.machine_id)
    curated_docs = [
        d for d in (await db.execute(curated_q)).scalars().all()
        if not _is_export_noise(d.relative_path)
    ]

    # 2. Recent conversations — title + ai_summary only (full body via memory_conversation)
    if recent_convs > 0:
        conv_q = (
            select(Document)
            .where(
                Document.project_id == project_id,
                Document.category == "conversation",
            )
            .order_by(Document.synced_at.desc())
            .limit(recent_convs)
        )
        conv_q = apply_user_filter(conv_q, mids, Document.machine_id)
        convs = [
            d for d in (await db.execute(conv_q)).scalars().all()
            if not _is_export_noise(d.relative_path)
        ]
    else:
        convs = []

    # 3. Knowledge graph: entities fuzzy-matching project name/slug
    ent_q = (
        select(KnowledgeEntity)
        .where(or_(
            KnowledgeEntity.name.ilike(f"%{project.title}%"),
            KnowledgeEntity.name.ilike(f"%{project.slug}%"),
        ))
        .limit(20)
    )
    if mids is not None:
        ent_q = ent_q.where(KnowledgeEntity.user_id == _user.id)
    entities = (await db.execute(ent_q)).scalars().all()
    ent_ids = [e.id for e in entities]

    # Batch-fetch observations + outgoing relations for those entities
    observations_by_entity: dict[str, list[dict]] = {}
    relations_by_entity: dict[str, list[dict]] = {}
    if ent_ids:
        obs = (await db.execute(
            select(KnowledgeObservation)
            .where(KnowledgeObservation.entity_id.in_(ent_ids))
            .order_by(KnowledgeObservation.observed_at.desc())
        )).scalars().all()
        for o in obs:
            observations_by_entity.setdefault(str(o.entity_id), []).append({
                "content": o.content,
                "observed_at": o.observed_at.isoformat() if o.observed_at else None,
            })

        rels = (await db.execute(
            select(KnowledgeRelation, KnowledgeEntity)
            .join(KnowledgeEntity, KnowledgeRelation.target_id == KnowledgeEntity.id)
            .where(KnowledgeRelation.source_id.in_(ent_ids))
        )).all()
        for rel, target in rels:
            relations_by_entity.setdefault(str(rel.source_id), []).append({
                "relation": rel.relation_type,
                "target_name": target.name,
                "target_type": target.entity_type,
            })

    return {
        "project": {
            "id": str(project.id),
            "title": project.title,
            "slug": project.slug,
            "tool_id": project.tool_id,
            "source_path": project.source_path,
        },
        "curated_docs": [
            {
                "id": str(d.id),
                "category": d.category,
                "title": d.title,
                "relative_path": d.relative_path,
                "synced_at": d.synced_at.isoformat(),
                "content": d.content,
                "ai_summary": d.ai_summary,
            }
            for d in curated_docs
        ],
        "recent_conversations": [
            {
                "id": str(d.id),
                "title": d.title or d.relative_path.rsplit("/", 1)[-1],
                "tool_id": d.tool_id,
                "synced_at": d.synced_at.isoformat(),
                "ai_summary": d.ai_summary,
            }
            for d in convs
        ],
        "knowledge_graph": {
            "entities": [
                {
                    "id": str(e.id),
                    "name": e.name,
                    "entity_type": e.entity_type,
                    "summary": e.summary,
                    "observations": observations_by_entity.get(str(e.id), [])[:15],
                    "relations": relations_by_entity.get(str(e.id), [])[:15],
                }
                for e in entities
            ],
        },
        "counts": {
            "curated_docs": len(curated_docs),
            "recent_conversations": len(convs),
            "entities": len(entities),
            "total_observations": sum(len(v) for v in observations_by_entity.values()),
            "total_relations": sum(len(v) for v in relations_by_entity.values()),
        },
    }
