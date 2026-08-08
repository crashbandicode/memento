"""Hierarchy API — Device → Tool → Project → Conversation drill-down."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Document, Machine, Project, Tool, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_activity import (
    ConversationActivitySummary,
    conversation_activity_summaries,
    conversation_list_timestamp_expression,
    effective_conversation_activity,
)
from ..services.conversation_hierarchy import (
    ConversationRef,
    FOLDABLE_CONVERSATION_TOOLS,
    build_logical_activity_map,
    fold_conversation_subagents,
)
from ..services.device_grouping import (
    accessible_machines,
    build_host_groups,
    resolve_device_scope_ids,
)
from ..services.document_delivery import (
    delivery_activity_expression,
    delivery_file_size_expression,
    delivery_metadata_expression,
    delivery_source_modified_expression,
    delivery_synced_expression,
)

router = APIRouter(prefix="/api/hierarchy", tags=["hierarchy"])


_DEVICE_FILE_COLUMNS = (
    Document.id,
    Document.title,
    Document.relative_path,
    Document.category,
    Document.content_type,
    delivery_file_size_expression().label("file_size_bytes"),
    delivery_activity_expression().label("activity_at"),
    delivery_source_modified_expression().label("source_modified_at"),
    delivery_synced_expression().label("synced_at"),
)

_CODEX_DEVICE_FILE_COLUMNS = (
    *_DEVICE_FILE_COLUMNS,
    delivery_metadata_expression().label("metadata"),
)


def _device_file_row(row) -> dict:
    (
        document_id,
        title,
        relative_path,
        category,
        content_type,
        file_size_bytes,
        raw_activity_at,
        source_modified_at,
        synced_at,
    ) = row
    activity_at = None
    if category == "conversation":
        effective_timestamp = effective_conversation_activity(
            raw_activity_at,
            source_modified_at,
            synced_at,
        )
        activity_at = (
            effective_timestamp.isoformat() if effective_timestamp else None
        )
    return {
        "id": str(document_id),
        "title": title,
        "relative_path": relative_path,
        "category": category,
        "content_type": content_type,
        "file_size_bytes": file_size_bytes,
        "activity_at": activity_at,
        "synced_at": synced_at.isoformat(),
    }


def _scope_row_sort_key(row) -> tuple:
    timestamp = (
        effective_conversation_activity(row[6], row[7], row[8])
        if row[3] == "conversation"
        else row[8]
    )
    return timestamp is not None, timestamp, str(row[0])


def _deduplicate_host_rows(rows: list) -> list:
    """Keep the newest representation of one logical path across runtimes."""
    newest_by_path: dict[tuple[str, str], tuple] = {}
    for row in rows:
        key = (row[3], row[2])
        previous = newest_by_path.get(key)
        if previous is None or _scope_row_sort_key(row) > _scope_row_sort_key(previous):
            newest_by_path[key] = row
    return sorted(newest_by_path.values(), key=_scope_row_sort_key, reverse=True)


def _fold_device_file_rows(
    rows: list,
    *,
    tool_id: str,
    offset: int,
    limit: int,
) -> tuple[int, list[dict]]:
    """Fold native child transcripts before sorting and paginating."""
    conversation_refs = [
        ConversationRef(
            document_id=row[0],
            tool_id=tool_id,
            relative_path=row[2],
            metadata=row[9],
            title=row[1],
            source_modified_at=row[7],
            activity_at=row[6],
            synced_at=row[8],
            file_size_bytes=row[5],
        )
        for row in rows
        if row[3] == "conversation"
    ]
    hierarchy = fold_conversation_subagents(conversation_refs)
    logical_activity = build_logical_activity_map(hierarchy, conversation_refs)
    visible_rows = [
        row
        for row in rows
        if row[3] != "conversation"
        or row[0] in hierarchy.visible_document_ids
    ]

    def sort_key(row) -> tuple:
        timestamp = (
            logical_activity.get(row[0])
            if row[3] == "conversation"
            else row[8]
        )
        return timestamp, str(row[0])

    visible_rows.sort(key=sort_key, reverse=True)
    page = visible_rows[offset:offset + limit]
    files = []
    for row in page:
        item = _device_file_row(row[:9])
        if row[3] == "conversation":
            timestamp = logical_activity.get(row[0])
            if timestamp is not None:
                item["activity_at"] = timestamp.isoformat()
            item["subagent_count"] = hierarchy.subagent_counts.get(row[0], 0)
            item["is_subagent_orphan"] = (
                row[0] in hierarchy.orphan_document_ids
            )
        files.append(item)
    return len(visible_rows), files


def _project_summary(row) -> dict:
    project_id, slug, title, tool_id, source_path = row
    return {
        "id": str(project_id),
        "slug": slug,
        "title": title,
        "tool_id": tool_id,
        "source_path": source_path,
    }


async def _annotate_conversation_activity(
    db: AsyncSession,
    files: list[dict],
) -> None:
    """Add the shared low-activity decision to one bounded hierarchy page."""
    conversation_ids = [
        uuid.UUID(item["id"])
        for item in files
        if item.get("category") == "conversation"
    ]
    summaries = await conversation_activity_summaries(db, conversation_ids)
    for item in files:
        if item.get("category") != "conversation":
            continue
        summary = summaries.get(
            uuid.UUID(item["id"]),
            ConversationActivitySummary(),
        )
        item["message_count"] = summary.message_count
        item["is_low_activity"] = summary.is_low_activity


@router.get("/devices")
async def list_devices_with_tools(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """Level 1: physical hosts with their selectable collector identities."""
    machines = await accessible_machines(db, _user)
    if not machines:
        return []
    machine_ids = [machine.id for machine in machines]
    document_rows = (
        await db.execute(
            select(
                Document.id,
                Document.machine_id,
                Document.tool_id,
                Document.relative_path,
            ).where(Document.machine_id.in_(machine_ids))
        )
    ).all()
    return build_host_groups(machines, document_rows)


@router.get("/devices/{device_id}/tools")
async def list_device_tools(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """Level 2: Tools for a machine identity or physical-host group."""
    machine_ids = await resolve_device_scope_ids(db, _user, device_id)

    # Count logical paths so replacement registrations do not inflate a host.
    tools_result = await db.execute(
        select(
            Document.tool_id,
            Tool.display_name,
            Document.category,
            func.count(func.distinct(Document.relative_path)).label("cnt"),
        )
        .outerjoin(Tool, Document.tool_id == Tool.id)
        .where(
            Document.machine_id.in_(machine_ids),
            Document.tool_id != "system",
        )
        .group_by(Document.tool_id, Tool.display_name, Document.category)
    )

    tool_data: dict[str, dict] = {}
    for tool_id, display_name, category, count in tools_result.all():
        if tool_id not in tool_data:
            tool_data[tool_id] = {
                "id": tool_id,
                "display_name": display_name or tool_id,
                "categories": {},
                "total_files": 0,
            }
        tool_data[tool_id]["categories"][category] = count
        tool_data[tool_id]["total_files"] += count

    return sorted(tool_data.values(), key=lambda t: t["total_files"], reverse=True)


@router.get("/devices/{device_id}/tools/{tool_id}/projects")
async def list_device_tool_projects(
    device_id: str, tool_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """Level 3: Projects for a machine or grouped host plus tool."""
    machine_ids = await resolve_device_scope_ids(db, _user, device_id)

    # Get logical documents for this scope+tool.
    rows = list((await db.execute(
        select(
            Document.project_id,
            func.count(func.distinct(Document.relative_path)).label("cnt"),
            func.max(delivery_synced_expression()).label("last"),
        )
        .where(
            Document.machine_id.in_(machine_ids),
            Document.tool_id == tool_id,
        )
        .group_by(Document.project_id)
    )).all())

    # Batch-fetch every referenced project in a single query instead of
    # looping N SELECT-by-id's (one per project).
    project_ids = [pid for pid, _c, _l in rows if pid]
    project_map: dict = {}
    if project_ids:
        proj_rows = await db.execute(
            select(Project.id, Project.title, Project.slug).where(Project.id.in_(project_ids))
        )
        project_map = {pid: (title, slug) for pid, title, slug in proj_rows.all()}

    items = []
    for project_id, count, last_sync in rows:
        if project_id:
            title, slug = project_map.get(project_id, ("Unknown", ""))
        else:
            title = "(No Project)"
            slug = ""
            project_id = "none"

        items.append({
            "id": str(project_id),
            "title": title,
            "slug": slug,
            "file_count": count,
            "last_sync": last_sync.isoformat() if last_sync else None,
        })

    return sorted(items, key=lambda p: p["file_count"], reverse=True)


@router.get("/devices/{device_id}/tools/{tool_id}/files")
async def list_device_tool_files(
    device_id: str, tool_id: str,
    project_id: str | None = None,
    category: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Level 4: Files for a machine or grouped physical-host scope."""
    machine_ids = await resolve_device_scope_ids(db, _user, device_id)

    criteria = [
        Document.machine_id.in_(machine_ids),
        Document.tool_id == tool_id,
    ]
    project = None
    if project_id and project_id != "none":
        try:
            resolved_project_id = uuid.UUID(project_id)
        except (AttributeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid project_id") from exc
        criteria.append(Document.project_id == resolved_project_id)

        # Only expose project metadata when the project is represented on the
        # already-authorized device+tool pair.  Looking the project up by UUID
        # alone would leak another user's project title/path to a caller who
        # can guess its identifier.
        project_result = await db.execute(
            select(
                Project.id,
                Project.slug,
                Project.title,
                Project.tool_id,
                Project.source_path,
            )
            .join(Document, Document.project_id == Project.id)
            .where(
                Project.id == resolved_project_id,
                Document.machine_id.in_(machine_ids),
                Document.tool_id == tool_id,
            )
            .limit(1)
        )
        project_row = project_result.first()
        if not project_row:
            raise HTTPException(status_code=404, detail="Project not found on this device")
        project = _project_summary(project_row)
    elif project_id == "none":
        criteria.append(Document.project_id.is_(None))
        project = {
            "id": "none",
            "slug": "",
            "title": "(No Project)",
            "tool_id": tool_id,
            "source_path": None,
        }
    if category:
        criteria.append(Document.category == category)

    # Agent-capable tools store each child as its own document. Load only the
    # lightweight list columns plus metadata, fold once, then page visible
    # logical files so a root with hundreds of children appears once.
    if tool_id in FOLDABLE_CONVERSATION_TOOLS and category in (None, "conversation"):
        folded_rows = (
            await db.execute(
                select(*_CODEX_DEVICE_FILE_COLUMNS).where(*criteria)
            )
        ).all()
        if len(machine_ids) > 1:
            folded_rows = _deduplicate_host_rows(list(folded_rows))
        total, files = _fold_device_file_rows(
            folded_rows,
            tool_id=tool_id,
            offset=offset,
            limit=limit,
        )
        await _annotate_conversation_activity(db, files)
        return {"total": total, "files": files, "project": project}

    if len(machine_ids) > 1:
        # A host group can contain old registrations of the same physical
        # file. Fetch lightweight rows once, collapse by logical path, then
        # paginate so total and page contents agree.
        grouped_rows = (
            await db.execute(
                select(*_DEVICE_FILE_COLUMNS).where(*criteria)
            )
        ).all()
        visible_rows = _deduplicate_host_rows(list(grouped_rows))
        files = [
            _device_file_row(row)
            for row in visible_rows[offset:offset + limit]
        ]
        await _annotate_conversation_activity(db, files)
        return {
            "total": len(visible_rows),
            "files": files,
            "project": project,
        }

    # Count directly against the filtered table.  Counting a subquery based
    # on ``select(Document)`` made PostgreSQL plan a projection containing the
    # multi-megabyte content/rendered payload columns even though the caller
    # only needed a row count.
    count_q = select(func.count(Document.id)).where(*criteria)
    total = (await db.execute(count_q)).scalar() or 0

    display_timestamp = conversation_list_timestamp_expression(
        Document.category,
        delivery_activity_expression(),
        delivery_source_modified_expression(),
        delivery_synced_expression(),
    )
    result = await db.execute(
        select(*_DEVICE_FILE_COLUMNS)
        .where(*criteria)
        .order_by(display_timestamp.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = result.all()

    files = [_device_file_row(row) for row in rows]
    await _annotate_conversation_activity(db, files)
    return {
        "total": total,
        "files": files,
        "project": project,
    }
