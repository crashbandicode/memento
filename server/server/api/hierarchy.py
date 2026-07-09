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
    conversation_list_timestamp_expression,
    effective_conversation_activity,
)

router = APIRouter(prefix="/api/hierarchy", tags=["hierarchy"])


_DEVICE_FILE_COLUMNS = (
    Document.id,
    Document.title,
    Document.relative_path,
    Document.category,
    Document.content_type,
    Document.file_size_bytes,
    Document.activity_at,
    Document.source_modified_at,
    Document.synced_at,
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


def _project_summary(row) -> dict:
    project_id, slug, title, tool_id, source_path = row
    return {
        "id": str(project_id),
        "slug": slug,
        "title": title,
        "tool_id": tool_id,
        "source_path": source_path,
    }


def _check_machine_access(machine: Machine | None, user: User) -> Machine | None:
    """Return None if user has no access to the machine."""
    if machine is None:
        return None
    if user.role in ("admin", "owner"):
        return machine
    if machine.user_id != user.id:
        return None
    return machine


async def _find_machine(db: AsyncSession, device_id: str) -> Machine | None:
    """Find machine by collector_token_hash OR by primary key UUID."""
    result = await db.execute(
        select(Machine).where(Machine.collector_token_hash == device_id)
    )
    m = result.scalar_one_or_none()
    if m:
        return m
    # Fallback: try as UUID primary key
    try:
        import uuid as _uuid
        uid = _uuid.UUID(device_id)
        result = await db.execute(select(Machine).where(Machine.id == uid))
        return result.scalar_one_or_none()
    except (ValueError, AttributeError):
        return None


@router.get("/devices")
async def list_devices_with_tools(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """Level 1: All devices with tool counts."""
    machines_q = select(Machine).order_by(Machine.name)
    if _user.role not in ("admin", "owner"):
        machines_q = machines_q.where(Machine.user_id == _user.id)
    machines = await db.execute(machines_q)
    items = []
    for m in machines.scalars().all():
        tools_result = await db.execute(
            select(Document.tool_id, func.count().label("cnt"))
            .where(Document.machine_id == m.id, Document.tool_id != "system")
            .group_by(Document.tool_id)
        )
        tool_counts = {r[0]: r[1] for r in tools_result.all()}
        total = sum(tool_counts.values())
        items.append({
            "id": str(m.id),
            "device_id": m.collector_token_hash,
            "name": m.name,
            "last_heartbeat": m.last_heartbeat.isoformat() if m.last_heartbeat else None,
            "total_files": total,
            "tools": [{"id": tid, "file_count": cnt} for tid, cnt in sorted(tool_counts.items())],
        })
    return items


@router.get("/devices/{device_id}/tools")
async def list_device_tools(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """Level 2: Tools for a specific device, with category breakdown."""
    m = _check_machine_access(await _find_machine(db, device_id), _user)
    if not m:
        return []

    # Single query with JOIN — no N+1, exclude "system" pseudo-tool
    tools_result = await db.execute(
        select(Document.tool_id, Tool.display_name, Document.category, func.count().label("cnt"))
        .outerjoin(Tool, Document.tool_id == Tool.id)
        .where(Document.machine_id == m.id, Document.tool_id != "system")
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
    """Level 3: Projects for a device+tool, with recent file info."""
    m = _check_machine_access(await _find_machine(db, device_id), _user)
    if not m:
        return []

    # Get documents for this device+tool
    rows = list((await db.execute(
        select(Document.project_id, func.count().label("cnt"), func.max(Document.synced_at).label("last"))
        .where(Document.machine_id == m.id, Document.tool_id == tool_id)
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
    """Level 4: Files (conversations/docs) for a device+tool, with optional project/category filter."""
    m = _check_machine_access(await _find_machine(db, device_id), _user)
    if not m:
        if project_id is not None:
            raise HTTPException(status_code=404, detail="Device not found")
        return {"total": 0, "files": [], "project": None}

    criteria = [Document.machine_id == m.id, Document.tool_id == tool_id]
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
                Document.machine_id == m.id,
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

    # Count directly against the filtered table.  Counting a subquery based
    # on ``select(Document)`` made PostgreSQL plan a projection containing the
    # multi-megabyte content/rendered payload columns even though the caller
    # only needed a row count.
    count_q = select(func.count(Document.id)).where(*criteria)
    total = (await db.execute(count_q)).scalar() or 0

    display_timestamp = conversation_list_timestamp_expression(
        Document.category,
        Document.activity_at,
        Document.source_modified_at,
        Document.synced_at,
    )
    result = await db.execute(
        select(*_DEVICE_FILE_COLUMNS)
        .where(*criteria)
        .order_by(display_timestamp.desc(), Document.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = result.all()

    return {
        "total": total,
        "files": [_device_file_row(row) for row in rows],
        "project": project,
    }
