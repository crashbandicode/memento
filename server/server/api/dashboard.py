"""Dashboard API — aggregated overview for the home page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, Machine, Project, Tool, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_activity import (
    effective_conversation_activity_expression,
    is_low_activity_summary,
)
from ..services.conversation_hierarchy import (
    ConversationRef,
    build_conversation_companion_filter,
    build_logical_activity_map,
    build_subagent_summaries,
    fold_conversation_subagents,
    group_conversation_root_thread_ids,
)
from ..services.user_filter import user_machine_ids, apply_user_filter

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

DASHBOARD_CONVERSATION_CANDIDATE_LIMIT = 600


def _apply_device_filter(query, device_id: str | None):
    if not device_id:
        return query
    return query.where(Document.machine_id.in_(
        select(Machine.id).where(Machine.collector_token_hash == device_id)
    ))


@router.get("")
async def get_dashboard(
    device_id: str | None = None,
    tz_offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Aggregated dashboard data for home page."""
    mids = await user_machine_ids(db, _user)

    # tz_offset: JS getTimezoneOffset() value (e.g. -480 for UTC+8)
    tz = timezone(timedelta(minutes=-tz_offset))
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Tools with stats — one-shot aggregation instead of 3 COUNT queries per
    # tool. With N tools, this was 3N+1 round-trips; now it's 2 (tool list +
    # single GROUP BY).
    tools_result = await db.execute(select(Tool).order_by(Tool.display_name))
    tool_records = list(tools_result.scalars().all())

    cat_agg_q = select(Document.tool_id, Document.category, func.count().label("n"))
    cat_agg_q = _apply_device_filter(cat_agg_q, device_id)
    cat_agg_q = apply_user_filter(cat_agg_q, mids, Document.machine_id)
    cat_agg_q = cat_agg_q.group_by(Document.tool_id, Document.category)
    categories_by_tool: dict[str, dict[str, int]] = {}
    for tid, cat, n in (await db.execute(cat_agg_q)).all():
        categories_by_tool.setdefault(tid, {})[cat] = n

    today_q = (
        select(Document.tool_id, func.count().label("n"))
        .where(Document.synced_at >= today_start)
    )
    today_q = _apply_device_filter(today_q, device_id)
    today_q = apply_user_filter(today_q, mids, Document.machine_id)
    today_q = today_q.group_by(Document.tool_id)
    today_by_tool: dict[str, int] = {tid: n for tid, n in (await db.execute(today_q)).all()}

    tools = []
    for t in tool_records:
        categories = categories_by_tool.get(t.id, {})
        if (device_id or mids is not None) and not categories:
            continue
        tools.append({
            "id": t.id,
            "display_name": t.display_name,
            "total_files": sum(categories.values()) if (device_id or mids is not None) else t.total_files,
            "last_sync_at": t.last_sync_at.isoformat() if t.last_sync_at else None,
            "categories": categories,
            "today_count": today_by_tool.get(t.id, 0),
            "conversation_count": categories.get("conversation", 0),
        })

    # Fetch a bounded, activity-ordered candidate set instead of folding every
    # visible conversation on each dashboard refresh.  If a candidate is a
    # child transcript, pull its logical companions so the visible root still
    # absorbs the child in dashboard presentation for every supported tool.
    activity_expr = effective_conversation_activity_expression(
        Document.activity_at,
        Document.source_modified_at,
        Document.synced_at,
    )
    recent_convos_q = (
        select(Document.id, Document.tool_id, Document.title,
               Document.synced_at, Document.project_id, Document.file_size_bytes,
               Project.title.label("project_title"), Document.relative_path,
               Document.metadata_, Document.source_modified_at,
               Document.activity_at)
        .outerjoin(Project, Document.project_id == Project.id)
        .where(Document.category == "conversation")
        .order_by(activity_expr.desc(), Document.id.desc())
        .limit(DASHBOARD_CONVERSATION_CANDIDATE_LIMIT)
    )
    recent_convos_q = _apply_device_filter(recent_convos_q, device_id)
    recent_convos_q = apply_user_filter(recent_convos_q, mids, Document.machine_id)
    candidate_rows = list((await db.execute(recent_convos_q)).all())

    candidate_refs = [
        ConversationRef(
            document_id=row[0],
            tool_id=row[1],
            relative_path=row[7],
            metadata=row[8],
            title=row[2],
            source_modified_at=row[9],
            activity_at=row[10],
            synced_at=row[3],
            file_size_bytes=row[5],
        )
        for row in candidate_rows
    ]
    roots_by_tool = group_conversation_root_thread_ids(
        candidate_refs,
        path_children_only=True,
    )

    all_convo_rows_by_id = {row[0]: row for row in candidate_rows}
    if roots_by_tool:
        companions_q = (
            select(Document.id, Document.tool_id, Document.title,
                   Document.synced_at, Document.project_id, Document.file_size_bytes,
                   Project.title.label("project_title"), Document.relative_path,
                   Document.metadata_, Document.source_modified_at,
                   Document.activity_at)
            .outerjoin(Project, Document.project_id == Project.id)
            .where(
                Document.category == "conversation",
                build_conversation_companion_filter(
                    Document.tool_id,
                    Document.metadata_,
                    Document.relative_path,
                    roots_by_tool,
                ),
            )
        )
        companions_q = _apply_device_filter(companions_q, device_id)
        companions_q = apply_user_filter(companions_q, mids, Document.machine_id)
        for row in (await db.execute(companions_q)).all():
            all_convo_rows_by_id[row[0]] = row

    all_convo_rows = list(all_convo_rows_by_id.values())
    conversation_refs = [
        ConversationRef(
            document_id=row[0],
            tool_id=row[1],
            relative_path=row[7],
            metadata=row[8],
            title=row[2],
            source_modified_at=row[9],
            activity_at=row[10],
            synced_at=row[3],
            file_size_bytes=row[5],
        )
        for row in all_convo_rows
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
    convos_rows = sorted(
        (
            row
            for row in all_convo_rows
            if row.id in conversation_hierarchy.visible_document_ids
        ),
        key=lambda row: (
            logical_activity_by_document.get(row.id)
            or row.activity_at
            or row.source_modified_at
            or row.synced_at,
            str(row.id),
        ),
        reverse=True,
    )[:20]

    # Batch both display counts and meaningful human/assistant activity in one
    # GROUP BY instead of one query per document.
    msg_activity: dict = {}
    if convos_rows:
        msg_count_q = (
            select(
                ConversationMessage.document_id,
                func.count().label("message_count"),
                func.count().filter(ConversationMessage.role == "user").label("user_count"),
                func.count().filter(ConversationMessage.role == "assistant").label("assistant_count"),
                func.coalesce(
                    func.sum(func.length(ConversationMessage.content)).filter(
                        ConversationMessage.role.in_(("user", "assistant"))
                    ),
                    0,
                ).label("human_character_count"),
            )
            .where(ConversationMessage.document_id.in_([r.id for r in convos_rows]))
            .group_by(ConversationMessage.document_id)
        )
        msg_activity = {
            did: (total, users, assistants, characters)
            for did, total, users, assistants, characters
            in (await db.execute(msg_count_q)).all()
        }

    recent_conversations = []
    for r in convos_rows:
        activity_at = logical_activity_by_document.get(r.id) or r.activity_at
        total, users, assistants, characters = msg_activity.get(
            r.id,
            (0, 0, 0, 0),
        )
        recent_conversations.append({
            "id": str(r.id),
            "tool_id": r.tool_id,
            "title": r.title,
            "activity_at": activity_at.isoformat() if activity_at else None,
            "synced_at": r.synced_at.isoformat(),
            "project_title": r.project_title,
            "message_count": total,
            "subagent_count": conversation_hierarchy.subagent_counts.get(r.id, 0),
            "is_subagent_orphan": (
                r.id in conversation_hierarchy.orphan_document_ids
            ),
            "subagents": subagents_by_document.get(r.id, []),
            "is_low_activity": is_low_activity_summary(
                users,
                assistants,
                characters,
            ),
        })

    # Recent activity (last 7 days by date, timezone-adjusted)
    cutoff = now - timedelta(days=7)
    tz_adjusted_synced = Document.synced_at + timedelta(minutes=-tz_offset)
    daily_q = (
        select(cast(tz_adjusted_synced, Date).label("day"), func.count().label("count"))
        .where(Document.synced_at >= cutoff)
    )
    daily_q = _apply_device_filter(daily_q, device_id)
    daily_q = apply_user_filter(daily_q, mids, Document.machine_id)
    daily_result = await db.execute(daily_q.group_by("day").order_by("day"))
    daily = [{"date": str(r.day), "count": r.count} for r in daily_result.all()]

    # Activity by tool (last 7 days)
    tool_daily_q = (
        select(Document.tool_id,
               cast(tz_adjusted_synced, Date).label("day"),
               func.count().label("count"))
        .where(Document.synced_at >= cutoff)
    )
    tool_daily_q = _apply_device_filter(tool_daily_q, device_id)
    tool_daily_q = apply_user_filter(tool_daily_q, mids, Document.machine_id)
    tool_daily_result = await db.execute(
        tool_daily_q.group_by(Document.tool_id, "day").order_by("day")
    )
    tool_daily: dict[str, list] = {}
    for r in tool_daily_result.all():
        tool_daily.setdefault(r.tool_id, []).append({"date": str(r.day), "count": r.count})

    # Active devices — batch per-device document counts in a single GROUP BY
    # instead of N+1.
    devices_q = select(Machine).order_by(Machine.name).limit(10)
    if mids is not None:
        devices_q = devices_q.where(Machine.id.in_(mids))
    machine_rows = list((await db.execute(devices_q)).scalars().all())

    dev_counts: dict = {}
    if machine_rows:
        dev_count_q = (
            select(Document.machine_id, func.count())
            .where(Document.machine_id.in_([m.id for m in machine_rows]))
            .group_by(Document.machine_id)
        )
        dev_counts = {mid: n for mid, n in (await db.execute(dev_count_q)).all()}

    devices = []
    for m in machine_rows:
        devices.append({
            "id": str(m.id),
            "device_id": m.collector_token_hash,
            "name": m.name,
            "last_heartbeat": m.last_heartbeat.isoformat() if m.last_heartbeat else None,
            "collector_version": m.collector_version,
            "total_files": dev_counts.get(m.id, 0),
        })

    # Today's stats
    today_total_q = select(func.count()).where(Document.synced_at >= today_start)
    today_total_q = _apply_device_filter(today_total_q, device_id)
    today_total_q = apply_user_filter(today_total_q, mids, Document.machine_id)
    today_total = (await db.execute(today_total_q)).scalar() or 0

    today_conv_q = select(func.count()).where(
        Document.synced_at >= today_start, Document.category == "conversation",
    )
    today_conv_q = _apply_device_filter(today_conv_q, device_id)
    today_conv_q = apply_user_filter(today_conv_q, mids, Document.machine_id)
    today_conversations = (await db.execute(today_conv_q)).scalar() or 0

    # Total stats
    doc_count_q = select(func.count()).select_from(Document)
    doc_count_q = _apply_device_filter(doc_count_q, device_id)
    doc_count_q = apply_user_filter(doc_count_q, mids, Document.machine_id)
    total_docs = (await db.execute(doc_count_q)).scalar() or 0
    # Count only projects the user has ingested into — Project has no user_id,
    # so scope via Document.machine_id → Machine.user_id (same path as mids).
    proj_count_q = (
        select(func.count(func.distinct(Document.project_id)))
        .where(Document.project_id.isnot(None))
    )
    proj_count_q = _apply_device_filter(proj_count_q, device_id)
    proj_count_q = apply_user_filter(proj_count_q, mids, Document.machine_id)
    total_projects = (await db.execute(proj_count_q)).scalar() or 0

    return {
        "tools": tools,
        "recent_conversations": recent_conversations,
        "daily": daily,
        "tool_daily": tool_daily,
        "devices": devices,
        "stats": {
            "total_documents": total_docs,
            "total_projects": total_projects,
            "total_tools": len(tools),
            "total_devices": len(devices),
            "today_total": today_total,
            "today_conversations": today_conversations,
        },
    }
