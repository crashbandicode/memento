"""Dashboard API — aggregated overview for the home page."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import (
    Date,
    Integer,
    String,
    and_,
    case,
    cast,
    func,
    literal,
    not_,
    or_,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ConversationMessage,
    ConversationRuntime,
    DashboardDocumentProjection,
    Document,
    Machine,
    PinnedThread,
    Project,
    Tool,
    User,
)
from ..db.session import get_search_db
from ..middleware.auth import get_current_user
from ..services.conversation_activity import (
    is_low_activity_summary,
)
from ..services.conversation_hierarchy import (
    ConversationRef,
    build_logical_activity_map,
    build_subagent_summaries,
    build_conversation_companion_filter,
    conversation_display_title,
    current_thread_id,
    fold_conversation_subagents,
    group_conversation_root_thread_ids,
    handoff_chain_title_overrides,
    resolve_conversation_briefing,
)
from ..services.conversation_identity import (
    conversation_resume_id,
    native_conversation_url,
)
from ..services.dashboard_projection import (
    ARCHIVED_METADATA_KEY,
    DASHBOARD_PROJECTION_VERSION,
    dashboard_projection_backfill_complete,
)
from ..services.dashboard_category_rollup import (
    dashboard_categories_from_rollup,
    dashboard_category_rollup_is_populated,
)
from ..services.dashboard_conversation_message_rollup import (
    dashboard_conversation_message_rollup_is_populated,
    dashboard_message_activity_from_rollup,
)
from ..services.device_grouping import resolve_device_scope_ids, split_device_name
from ..services.document_delivery import (
    delivery_activity_expression,
    delivery_file_size_expression,
    delivery_metadata_expression,
    delivery_source_modified_expression,
    delivery_synced_expression,
)
from ..services.spend_dashboard_proxy import spend_dashboard_proxy
from ..services.user_filter import user_machine_ids, apply_user_filter

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

DASHBOARD_CONVERSATION_CANDIDATE_LIMIT = 600
RECENT_PRIMARY_LIMIT = 20
RECENT_CLAW_SAMPLE_LIMIT = 20
OPEN_THREAD_ACTIVE_MINUTES = 20


@router.get("/spend")
async def get_spend_dashboard(
    refresh: bool = False,
    _user: User = Depends(get_current_user),
) -> dict:
    """Return the cached read-only spend-dashboard MCP snapshot."""
    return await spend_dashboard_proxy.get_snapshot(force_refresh=refresh)


def _apply_device_filter(query, machine_ids, machine_column):
    if machine_ids is None:
        return query
    return query.where(machine_column.in_(machine_ids))


def _effective_machine_scope(
    user_machine_ids,
    selected_machine_ids,
):
    """Return the exact intersection applied by ``scoped`` below."""
    if selected_machine_ids is None:
        return user_machine_ids
    if user_machine_ids is None:
        return selected_machine_ids
    allowed = set(user_machine_ids)
    return [
        machine_id
        for machine_id in selected_machine_ids
        if machine_id in allowed
    ]


def _legacy_is_archived_expression(metadata):
    archived_text = func.lower(
        func.coalesce(metadata[ARCHIVED_METADATA_KEY].astext, "")
    )
    return archived_text.in_(("true", "t", "1", "yes"))


def _unarchived_conversation_filter(source):
    return and_(
        source.c.category == "conversation",
        source.c.is_archived.is_(False),
    )


def _dashboard_projection_select(*, current_version_only: bool = False):
    projection = DashboardDocumentProjection
    statement = select(
        projection.document_id.label("id"),
        projection.tool_id.label("tool_id"),
        projection.title.label("title"),
        projection.synced_at.label("synced_at"),
        projection.project_id.label("project_id"),
        projection.file_size_bytes.label("file_size_bytes"),
        projection.relative_path.label("relative_path"),
        projection.hierarchy_metadata.label("hierarchy_metadata"),
        projection.source_modified_at.label("source_modified_at"),
        projection.activity_at.label("activity_at"),
        projection.machine_id.label("machine_id"),
        projection.category.label("category"),
        projection.visibility.label("visibility"),
        projection.session_id.label("session_id"),
        projection.root_thread_id.label("root_thread_id"),
        projection.parent_thread_id.label("parent_thread_id"),
        projection.is_subagent.label("is_subagent"),
        projection.is_archived.label("is_archived"),
        projection.message_count.label("message_count"),
        projection.user_message_count.label("user_message_count"),
        projection.assistant_message_count.label("assistant_message_count"),
        projection.human_character_count.label("human_character_count"),
        projection.pending_question_count.label("pending_question_count"),
        projection.agent_mode.label("agent_mode"),
        literal(True).label("projected"),
    )
    if current_version_only:
        statement = statement.where(
            projection.projection_version == DASHBOARD_PROJECTION_VERSION
        )
    return statement


def _legacy_dashboard_select():
    """Compatibility rows used only until the explicit backfill completes."""
    metadata = delivery_metadata_expression()
    session_id = func.coalesce(
        metadata["session_id"].astext,
        metadata["thread_id"].astext,
        metadata["cascade_id"].astext,
    )
    root_thread_id = func.coalesce(
        metadata["root_session_id"].astext,
        session_id,
    )
    pending_text = metadata["pending_question_count"].astext
    pending_count = case(
        (
            pending_text.op("~")(r"^[0-9]+$"),
            cast(pending_text, Integer),
        ),
        else_=0,
    )
    return (
        select(
            Document.id.label("id"),
            Document.tool_id.label("tool_id"),
            Document.title.label("title"),
            delivery_synced_expression().label("synced_at"),
            Document.project_id.label("project_id"),
            delivery_file_size_expression().label("file_size_bytes"),
            Document.relative_path.label("relative_path"),
            metadata.label("hierarchy_metadata"),
            delivery_source_modified_expression().label("source_modified_at"),
            delivery_activity_expression().label("activity_at"),
            Document.machine_id.label("machine_id"),
            Document.category.label("category"),
            Document.visibility.label("visibility"),
            session_id.label("session_id"),
            root_thread_id.label("root_thread_id"),
            metadata["parent_thread_id"].astext.label("parent_thread_id"),
            literal(False).label("is_subagent"),
            _legacy_is_archived_expression(metadata).label("is_archived"),
            literal(0).label("message_count"),
            literal(0).label("user_message_count"),
            literal(0).label("assistant_message_count"),
            literal(0).label("human_character_count"),
            pending_count.label("pending_question_count"),
            func.coalesce(
                metadata["_assistant_agent_mode"].astext,
                "",
            ).label("agent_mode"),
            literal(False).label("projected"),
        )
        .outerjoin(
            DashboardDocumentProjection,
            DashboardDocumentProjection.document_id == Document.id,
        )
        # A version bump must keep using normalized document metadata for an
        # old projection until its lightweight upgrade has completed.  In
        # particular, a newly added projected column has its database default
        # on old rows and must not leak archived conversations during that
        # compatibility window.
        .where(or_(
            DashboardDocumentProjection.document_id.is_(None),
            DashboardDocumentProjection.projection_version
            < DASHBOARD_PROJECTION_VERSION,
        ))
    )


def dashboard_source_statement(*, include_legacy: bool):
    """Return the narrow dashboard source, with a temporary legacy union."""
    projected = _dashboard_projection_select(
        current_version_only=include_legacy,
    )
    if not include_legacy:
        return projected
    return union_all(projected, _legacy_dashboard_select())


def _row_metadata(row) -> dict:
    metadata = (
        dict(row.hierarchy_metadata)
        if isinstance(row.hierarchy_metadata, dict)
        else {}
    )
    if row.session_id:
        metadata["session_id"] = row.session_id
        metadata["thread_id"] = row.session_id
    if row.root_thread_id:
        metadata["root_session_id"] = row.root_thread_id
    if row.parent_thread_id:
        metadata["parent_thread_id"] = row.parent_thread_id
    if row.is_subagent:
        metadata["is_subagent"] = True
        if row.tool_id == "codex":
            metadata["thread_source"] = "subagent"
    return metadata


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _absolute_project_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 4096
        or re.search(r"[\x00-\x1f\x7f-\x9f]", candidate)
    ):
        return None
    if (
        candidate.startswith("/")
        or candidate.startswith("\\\\")
        or _WINDOWS_ABSOLUTE_PATH_RE.match(candidate)
    ):
        return candidate
    return None


def _row_location(row) -> dict[str, str] | None:
    raw_host = str(getattr(row, "machine_name", "") or "").strip()
    host, runtime_platform = split_device_name(raw_host)
    if not host or re.search(r"[\x00-\x1f\x7f-\x9f]", host):
        return None
    metadata = _row_metadata(row)
    path = next(
        (
            candidate
            for candidate in (
                _absolute_project_path(metadata.get("project_path")),
                _absolute_project_path(metadata.get("cwd")),
                _absolute_project_path(getattr(row, "project_source_path", None)),
            )
            if candidate
        ),
        None,
    )
    if path is None:
        return None
    return {
        "host": host[:255],
        "path": path,
        "platform": runtime_platform,
    }


def _conversation_resume_command(
    tool_id: str,
    resume_id: str | None,
    location: dict[str, str] | None,
) -> str | None:
    """Build one copy-ready native resume command from verified identifiers."""
    if not resume_id:
        return None
    quoted_resume_id = f"'{resume_id}'"
    invocations = {
        "claude_code": f"claude --resume {quoted_resume_id}",
        "codex": f"codex resume {quoted_resume_id}",
        "cursor": f"cursor-agent --resume={quoted_resume_id}",
    }
    invocation = invocations.get(tool_id)
    if invocation is None:
        return None
    path = str((location or {}).get("path") or "").strip()
    if not path:
        return invocation
    platform = str((location or {}).get("platform") or "").casefold()
    if platform == "windows":
        return (
            f"Set-Location -LiteralPath '{path.replace(chr(39), chr(39) * 2)}' && "
            f"{invocation}"
        )
    escaped = path.replace("'", "'\"'\"'")
    return f"cd -- '{escaped}' && {invocation}"


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _thread_health_by_document(response: object) -> dict[str, dict]:
    """Index only observed per-thread health rows; absence stays unknown."""
    if not isinstance(response, dict) or response.get("available") is not True:
        return {}
    snapshot = response.get("snapshot")
    hygiene = snapshot.get("hygiene") if isinstance(snapshot, dict) else None
    hop_threads = hygiene.get("hopThreads") if isinstance(hygiene, dict) else None
    items = hop_threads.get("items") if isinstance(hop_threads, dict) else None
    if not isinstance(items, list):
        return {}
    result: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        document_id = str(
            item.get("documentId") or item.get("document_id") or ""
        ).strip()
        source = str(item.get("source") or "").strip()
        status = str(item.get("status") or "").strip()
        ratio = _finite_number(item.get("ratio"))
        hop_line = _finite_number(
            item.get("hopLine") if "hopLine" in item else item.get("hop_line")
        )
        hard_line = _finite_number(
            item.get("hardLine") if "hardLine" in item else item.get("hard_line")
        )
        if (
            not document_id
            or not source
            or not status
            or ratio is None
            or hop_line is None
            or hard_line is None
        ):
            continue
        result[document_id] = {
            "available": True,
            "stale": bool(response.get("stale")),
            "source": source,
            "ratio": ratio,
            "status": status,
            "hop_line": hop_line,
            "hard_line": hard_line,
        }
    return result


def _handoff_predecessor_document_ids(rows) -> set:
    """Return predecessors closed by one unambiguous explicit handoff marker."""
    rows = list(rows)
    rows_by_thread: dict[tuple[object, str], list] = {}
    for row in rows:
        if thread_id := current_thread_id(_row_metadata(row)):
            key = (getattr(row, "machine_id", None), thread_id)
            rows_by_thread.setdefault(key, []).append(row)
    predecessors = set()
    for row in rows:
        kind, predecessor_thread_id = resolve_conversation_briefing(
            metadata=_row_metadata(row),
        )
        if kind != "handoff" or predecessor_thread_id is None:
            continue
        predecessor_key = (
            getattr(row, "machine_id", None),
            predecessor_thread_id,
        )
        matches = [
            candidate
            for candidate in rows_by_thread.get(predecessor_key, [])
            if candidate.id != row.id
        ]
        # A UUID collision or duplicate source copy cannot be disambiguated
        # from this bounded projection. Keep both instead of hiding the wrong
        # conversation.
        if len(matches) == 1:
            predecessors.add(matches[0].id)
    return predecessors


def _select_open_thread_rows(
    rows,
    *,
    visible_document_ids: set,
    pinned_root_ids: set,
    handoff_predecessor_ids: set,
    logical_activity_by_document: dict,
    running_document_ids: set | None = None,
    now: datetime,
    active_minutes: int = OPEN_THREAD_ACTIVE_MINUTES,
):
    """Select recently active roots plus explicit pins without inferring closure."""
    cutoff = now - timedelta(minutes=active_minutes)
    running_document_ids = running_document_ids or set()
    selected = []
    for row in rows:
        if row.id not in visible_document_ids or row.is_subagent:
            continue
        if row.id in pinned_root_ids:
            selected.append(row)
            continue
        activity_at = (
            logical_activity_by_document.get(row.id)
            or row.activity_at
            or row.source_modified_at
            or row.synced_at
        )
        if (
            not row.is_archived
            and row.id not in handoff_predecessor_ids
            and (
                row.id in running_document_ids
                or (activity_at is not None and activity_at >= cutoff)
            )
        ):
            selected.append(row)
    return selected


def _fold_open_thread_hierarchy_by_machine(rows, conversation_refs):
    """Fold native hierarchy without deduplicating copies across machines."""

    refs_by_document = {
        ref.document_id: ref for ref in conversation_refs
    }
    visible_document_ids = set()
    canonical_document_ids = {}
    logical_activity_by_document = {}
    rows_by_machine = {}
    for row in rows:
        rows_by_machine.setdefault(row.machine_id, []).append(row)
    for machine_rows in rows_by_machine.values():
        machine_refs = [refs_by_document[row.id] for row in machine_rows]
        hierarchy = fold_conversation_subagents(machine_refs)
        visible_document_ids.update(hierarchy.visible_document_ids)
        canonical_document_ids.update(hierarchy.canonical_document_ids)
        logical_activity_by_document.update(
            build_logical_activity_map(hierarchy, machine_refs)
        )
    return (
        visible_document_ids,
        canonical_document_ids,
        logical_activity_by_document,
    )


def _is_unlinked_claw_row(row) -> bool:
    """True for orchestration=claw with no parent document id (Python twin of SQL).

    Path-linked children (``/subagents/`` transcripts) are excluded: the
    hierarchy fold links them under their parent regardless of orchestration
    metadata, so counting them in the unlinked aggregate double-counts.
    """
    metadata = _row_metadata(row)
    if str(metadata.get("orchestration") or "").strip().casefold() != "claw":
        return False
    if "/subagents/" in str(getattr(row, "relative_path", "") or "").replace("\\", "/"):
        return False
    return not str(metadata.get("orchestration_parent_document_id") or "").strip()


def _unlinked_claw_hierarchy_sql(hierarchy_metadata, relative_path):
    """Unlinked-claw predicate over dashboard hierarchy_metadata JSONB."""
    orchestration = func.lower(
        func.coalesce(hierarchy_metadata["orchestration"].astext, "")
    )
    parent = func.coalesce(
        hierarchy_metadata["orchestration_parent_document_id"].astext,
        "",
    )
    return and_(
        orchestration == "claw",
        parent == "",
        not_(func.coalesce(relative_path, "").like("%/subagents/%")),
    )


def corrected_claw_delegate_count(
    raw_count: int,
    loaded_rows,
    visible_document_ids,
    attention_ids: set,
) -> int:
    """Reconcile the independent SQL aggregate with rendered group membership.

    The COUNT(*) cannot see hierarchy folding (a delegate represented under
    its parent) or attention elevation (a delegate rendered as an Attention
    card); both remove rows from the rendered Claw group, so every loaded
    unlinked-claw row in either state is subtracted. Rows beyond the candidate
    cap are by construction unloaded and therefore neither folded nor
    elevated.
    """
    removed = sum(
        1
        for row in loaded_rows
        if _is_unlinked_claw_row(row)
        and (
            row.id not in visible_document_ids
            or row.id in attention_ids
        )
    )
    return max(0, int(raw_count) - removed)


def partition_dashboard_candidates_before_limit(
    rows,
    *,
    activity_key,
    candidate_limit: int = DASHBOARD_CONVERSATION_CANDIDATE_LIMIT,
    claw_sample_limit: int = RECENT_CLAW_SAMPLE_LIMIT,
):
    """Split unlinked claws from primaries before the 600-row candidate cap.

    Mirrors the SQL used by ``get_dashboard``: unlinked claws are counted and
    sampled independently so delegate volume cannot drown primary rows.
    """
    ordered = sorted(rows, key=activity_key, reverse=True)
    unlinked = [row for row in ordered if _is_unlinked_claw_row(row)]
    primaries = [row for row in ordered if not _is_unlinked_claw_row(row)]
    return {
        "primary_candidates": primaries[:candidate_limit],
        "claw_count": len(unlinked),
        "claw_sample": unlinked[:claw_sample_limit],
    }


def _select_recent_conversation_rows(
    visible_convo_rows,
    attention_ids: set,
    activity_key,
    *,
    primary_limit: int = RECENT_PRIMARY_LIMIT,
    claw_sample_limit: int = RECENT_CLAW_SAMPLE_LIMIT,
):
    """Partition unlinked Claw rows before applying the Recent primary budget."""
    attention_rows = [
        row for row in visible_convo_rows if row.id in attention_ids
    ]
    recent_rows = [
        row for row in visible_convo_rows if row.id not in attention_ids
    ]
    # Group membership must match the aggregate-count predicate exactly:
    # linked/path-linked claw rows that remain visible render as ordinary
    # rows, never as uncounted members of the aggregate group.
    claw_rows = [row for row in recent_rows if _is_unlinked_claw_row(row)]
    primary_rows = [
        row for row in recent_rows if not _is_unlinked_claw_row(row)
    ]
    convos_rows = (
        sorted(attention_rows, key=activity_key, reverse=True)
        + sorted(primary_rows, key=activity_key, reverse=True)[:primary_limit]
        + sorted(claw_rows, key=activity_key, reverse=True)[:claw_sample_limit]
    )
    return convos_rows, len(claw_rows)


@router.get("")
async def get_dashboard(
    device_id: str | None = None,
    tz_offset: int = Query(0),
    db: AsyncSession = Depends(get_search_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Aggregated dashboard data for home page."""
    mids = await user_machine_ids(db, _user)
    selected_machine_ids = (
        await resolve_device_scope_ids(db, _user, device_id)
        if device_id
        else None
    )

    # tz_offset: JS getTimezoneOffset() value (e.g. -480 for UTC+8)
    tz = timezone(timedelta(minutes=-tz_offset))
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    include_legacy = not await dashboard_projection_backfill_complete(db)
    source = dashboard_source_statement(
        include_legacy=include_legacy
    ).subquery("dashboard_source")

    def scoped(query):
        query = _apply_device_filter(
            query,
            selected_machine_ids,
            source.c.machine_id,
        )
        return apply_user_filter(query, mids, source.c.machine_id)

    tools_result = await db.execute(select(Tool).order_by(Tool.display_name))
    tool_records = list(tools_result.scalars().all())

    # The all-document tool/category GROUP BY was the dashboard's dominant
    # cold-request cost. Prefer its precomputed per-machine snapshot once the
    # dashboard projection itself is fully backfilled. During either rollout
    # (or before the first beat refresh), run the original live query so this
    # endpoint never serves a partial aggregate.
    if (
        not include_legacy
        and await dashboard_category_rollup_is_populated(db)
    ):
        categories_by_tool = await dashboard_categories_from_rollup(
            db,
            machine_ids=_effective_machine_scope(
                mids,
                selected_machine_ids,
            ),
        )
    else:
        cat_agg_q = scoped(
            select(source.c.tool_id, source.c.category, func.count().label("n"))
        ).group_by(source.c.tool_id, source.c.category)
        categories_by_tool: dict[str, dict[str, int]] = {}
        for tid, cat, count in (await db.execute(cat_agg_q)).all():
            categories_by_tool.setdefault(tid, {})[cat] = count

    today_q = scoped(
        select(source.c.tool_id, func.count().label("n")).where(
            source.c.synced_at >= today_start
        )
    ).group_by(source.c.tool_id)
    today_by_tool = {
        tool_id: count
        for tool_id, count in (await db.execute(today_q)).all()
    }
    tools = []
    for tool in tool_records:
        categories = categories_by_tool.get(tool.id, {})
        if (device_id or mids is not None) and not categories:
            continue
        tools.append({
            "id": tool.id,
            "display_name": tool.display_name,
            "total_files": sum(categories.values()),
            "last_sync_at": (
                tool.last_sync_at.isoformat() if tool.last_sync_at else None
            ),
            "categories": categories,
            "today_count": today_by_tool.get(tool.id, 0),
            "conversation_count": categories.get("conversation", 0),
        })

    conversation_columns = (
        source.c.id,
        source.c.tool_id,
        source.c.title,
        source.c.synced_at,
        source.c.project_id,
        source.c.file_size_bytes,
        Project.title.label("project_title"),
        Project.source_path.label("project_source_path"),
        select(Machine.name)
        .where(Machine.id == source.c.machine_id)
        .scalar_subquery()
        .label("machine_name"),
        source.c.relative_path,
        source.c.hierarchy_metadata,
        source.c.source_modified_at,
        source.c.activity_at,
        source.c.machine_id,
        source.c.session_id,
        source.c.root_thread_id,
        source.c.parent_thread_id,
        source.c.is_subagent,
        source.c.is_archived,
        source.c.message_count,
        source.c.user_message_count,
        source.c.assistant_message_count,
        source.c.human_character_count,
        source.c.pending_question_count,
        source.c.agent_mode,
        source.c.projected,
    )
    activity_expr = func.coalesce(
        source.c.activity_at,
        source.c.source_modified_at,
        source.c.synced_at,
    )
    unlinked_claw = _unlinked_claw_hierarchy_sql(
        source.c.hierarchy_metadata,
        source.c.relative_path,
    )
    recent_convos_q = (
        select(*conversation_columns)
        .outerjoin(Project, source.c.project_id == Project.id)
        .where(
            _unarchived_conversation_filter(source),
            not_(unlinked_claw),
        )
        .order_by(activity_expr.desc(), source.c.id.desc())
        .limit(DASHBOARD_CONVERSATION_CANDIDATE_LIMIT)
    )
    candidate_rows = list(
        (await db.execute(scoped(recent_convos_q))).all()
    )
    claw_delegate_count = int(
        (
            await db.execute(
                scoped(
                    select(func.count())
                    .select_from(source)
                    .where(
                        _unarchived_conversation_filter(source),
                        unlinked_claw,
                    )
                )
            )
        ).scalar()
        or 0
    )
    claw_sample_q = (
        select(*conversation_columns)
        .outerjoin(Project, source.c.project_id == Project.id)
        .where(
            _unarchived_conversation_filter(source),
            unlinked_claw,
        )
        .order_by(activity_expr.desc(), source.c.id.desc())
        .limit(RECENT_CLAW_SAMPLE_LIMIT)
    )
    claw_sample_rows = list(
        (await db.execute(scoped(claw_sample_q))).all()
    )

    attention_convos_q = (
        select(*conversation_columns)
        .outerjoin(Project, source.c.project_id == Project.id)
        .where(
            _unarchived_conversation_filter(source),
            source.c.pending_question_count > 0,
        )
    )
    rows_by_id = {row.id: row for row in candidate_rows}
    for row in (await db.execute(scoped(attention_convos_q))).all():
        rows_by_id[row.id] = row
    for row in claw_sample_rows:
        rows_by_id[row.id] = row
    # The Recent section remains bounded, but Open Threads must not inherit
    # that presentation budget. Fetch every source-active conversation in the
    # open window, including Claw rows, before hierarchy folding.
    open_activity_cutoff = now - timedelta(minutes=OPEN_THREAD_ACTIVE_MINUTES)
    active_convos_q = (
        select(*conversation_columns)
        .outerjoin(Project, source.c.project_id == Project.id)
        .where(
            _unarchived_conversation_filter(source),
            activity_expr >= open_activity_cutoff,
        )
    )
    for row in (await db.execute(scoped(active_convos_q))).all():
        rows_by_id[row.id] = row
    pinned_thread_rows = list(
        (
            await db.execute(
                select(PinnedThread.document_id, PinnedThread.created_at).where(
                    PinnedThread.user_id == _user.id
                )
            )
        ).all()
    )
    pinned_document_ids = {row.document_id for row in pinned_thread_rows}
    if pinned_document_ids:
        pinned_convos_q = (
            select(*conversation_columns)
            .outerjoin(Project, source.c.project_id == Project.id)
            .where(
                source.c.category == "conversation",
                source.c.id.in_(pinned_document_ids),
            )
        )
        for row in (await db.execute(scoped(pinned_convos_q))).all():
            rows_by_id[row.id] = row

    machine_scope = _effective_machine_scope(mids, selected_machine_ids)
    running_runtimes_q = select(ConversationRuntime).where(
        ConversationRuntime.state == "running"
    )
    if machine_scope is not None:
        running_runtimes_q = running_runtimes_q.where(
            ConversationRuntime.machine_id.in_(machine_scope)
        )
    running_runtimes = list(
        (await db.execute(running_runtimes_q)).scalars().all()
    )
    runtime_by_key = {
        (runtime.machine_id, runtime.tool_id, runtime.session_id): runtime
        for runtime in running_runtimes
    }
    runtime_by_document = {}
    running_document_ids = set()
    runtime_filters = [
        and_(
            source.c.machine_id == machine_id,
            source.c.tool_id == tool_id,
            source.c.session_id == session_id,
        )
        for machine_id, tool_id, session_id in runtime_by_key
    ]
    if runtime_filters:
        runtime_convos_q = (
            select(*conversation_columns)
            .outerjoin(Project, source.c.project_id == Project.id)
            .where(source.c.category == "conversation", or_(*runtime_filters))
        )
        for row in (await db.execute(scoped(runtime_convos_q))).all():
            runtime = runtime_by_key.get(
                (row.machine_id, row.tool_id, row.session_id)
            )
            if runtime is None:
                continue
            rows_by_id[row.id] = row
            running_document_ids.add(row.id)
            runtime_by_document[row.id] = runtime
    candidate_rows = list(rows_by_id.values())

    def conversation_ref(row) -> ConversationRef:
        metadata = _row_metadata(row)
        return ConversationRef(
            document_id=row.id,
            tool_id=row.tool_id,
            relative_path=row.relative_path,
            metadata=metadata,
            title=conversation_display_title(
                row.tool_id,
                row.relative_path,
                metadata,
                row.title,
            ),
            source_modified_at=row.source_modified_at,
            activity_at=row.activity_at,
            synced_at=row.synced_at,
            file_size_bytes=row.file_size_bytes,
        )

    roots_by_tool = group_conversation_root_thread_ids(
        [conversation_ref(row) for row in candidate_rows],
        path_children_only=True,
    )
    all_convo_rows_by_id = {row.id: row for row in candidate_rows}
    companion_filters = [
        and_(
            source.c.tool_id == tool_id,
            source.c.root_thread_id.in_(root_ids),
        )
        for tool_id, root_ids in roots_by_tool.items()
        if root_ids
    ]
    if companion_filters:
        companions_q = (
            select(*conversation_columns)
            .outerjoin(Project, source.c.project_id == Project.id)
            .where(
                _unarchived_conversation_filter(source),
                or_(
                    *companion_filters,
                    build_conversation_companion_filter(
                        source.c.tool_id,
                        source.c.hierarchy_metadata,
                        source.c.relative_path,
                        roots_by_tool,
                    ),
                ),
            )
        )
        for row in (await db.execute(scoped(companions_q))).all():
            all_convo_rows_by_id[row.id] = row

    represented_document_ids = {
        str(document_id) for document_id in all_convo_rows_by_id
    }
    explicit_orchestration_parent_ids = {
        str(parent_id)
        for row in all_convo_rows_by_id.values()
        if (
            parent_id := _row_metadata(row).get(
                "orchestration_parent_document_id"
            )
        )
    }
    orchestration_companions_q = (
        select(*conversation_columns)
        .outerjoin(Project, source.c.project_id == Project.id)
        .where(
            _unarchived_conversation_filter(source),
            or_(
                cast(source.c.id, String).in_(explicit_orchestration_parent_ids),
                source.c.hierarchy_metadata[
                    "orchestration_parent_document_id"
                ].astext.in_(represented_document_ids),
            ),
        )
    )
    if explicit_orchestration_parent_ids or represented_document_ids:
        for row in (
            await db.execute(scoped(orchestration_companions_q))
        ).all():
            all_convo_rows_by_id[row.id] = row

    all_convo_rows = list(all_convo_rows_by_id.values())
    conversation_refs = [conversation_ref(row) for row in all_convo_rows]
    title_overrides = handoff_chain_title_overrides(conversation_refs)
    conversation_refs = [
        replace(ref, title=title_overrides.get(ref.document_id, ref.title))
        for ref in conversation_refs
    ]
    conversation_titles = {
        ref.document_id: ref.title for ref in conversation_refs
    }
    conversation_hierarchy = fold_conversation_subagents(conversation_refs)
    subagents_by_document = build_subagent_summaries(
        conversation_hierarchy,
        conversation_refs,
    )
    logical_activity_by_document = build_logical_activity_map(
        conversation_hierarchy,
        conversation_refs,
    )
    # Open-thread cards represent physical collector copies. Fold children
    # independently inside each machine so a newer copy on one host cannot
    # hide the same native root on another host.
    (
        open_visible_document_ids,
        open_canonical_document_ids,
        open_logical_activity_by_document,
    ) = _fold_open_thread_hierarchy_by_machine(all_convo_rows, conversation_refs)
    pending_question_counts: dict = {}
    for row in all_convo_rows:
        count = max(0, int(row.pending_question_count or 0))
        if not count:
            continue
        canonical_id = conversation_hierarchy.canonical_document_ids.get(
            row.id,
            row.id,
        )
        pending_question_counts[canonical_id] = (
            pending_question_counts.get(canonical_id, 0) + count
        )

    visible_convo_rows = [
        row
        for row in all_convo_rows
        if row.id in conversation_hierarchy.visible_document_ids
        and not row.is_archived
    ]

    def activity_key(row):
        return (
            logical_activity_by_document.get(row.id)
            or row.activity_at
            or row.source_modified_at
            or row.synced_at,
            str(row.id),
        )

    pinned_root_ids = {
        open_canonical_document_ids.get(document_id, document_id)
        for document_id in pinned_document_ids
        if document_id in all_convo_rows_by_id
    }
    handoff_predecessor_ids = _handoff_predecessor_document_ids(all_convo_rows)
    open_thread_rows = _select_open_thread_rows(
        all_convo_rows,
        visible_document_ids=open_visible_document_ids,
        pinned_root_ids=pinned_root_ids,
        handoff_predecessor_ids=handoff_predecessor_ids,
        logical_activity_by_document=open_logical_activity_by_document,
        running_document_ids=running_document_ids,
        now=now,
    )

    attention_rows = [
        row
        for row in visible_convo_rows
        if pending_question_counts.get(row.id, 0) > 0
    ]
    attention_ids = {row.id for row in attention_rows}
    claw_delegate_count = corrected_claw_delegate_count(
        claw_delegate_count,
        all_convo_rows,
        conversation_hierarchy.visible_document_ids,
        attention_ids,
    )
    convos_rows, _sampled_claw_count = _select_recent_conversation_rows(
        visible_convo_rows,
        attention_ids,
        activity_key,
    )

    # Only temporary legacy rows fall back to message aggregation. Once the
    # backfill marker is complete, no dashboard query references message text.
    message_activity = {
        row.id: (
            int(row.message_count or 0),
            int(row.user_message_count or 0),
            int(row.assistant_message_count or 0),
            int(row.human_character_count or 0),
        )
        for row in convos_rows
        if row.projected
    }
    legacy_ids = [row.id for row in convos_rows if not row.projected]
    if legacy_ids:
        if await dashboard_conversation_message_rollup_is_populated(db):
            message_activity.update(
                await dashboard_message_activity_from_rollup(
                    db,
                    document_ids=legacy_ids,
                )
            )
        else:
            legacy_message_q = (
                select(
                    ConversationMessage.document_id,
                    func.count().label("message_count"),
                    func.count().filter(
                        ConversationMessage.role == "user"
                    ).label("user_count"),
                    func.count().filter(
                        ConversationMessage.role == "assistant"
                    ).label("assistant_count"),
                    func.coalesce(
                        func.sum(func.length(ConversationMessage.content)).filter(
                            ConversationMessage.role.in_(("user", "assistant"))
                        ),
                        0,
                    ).label("human_character_count"),
                )
                .where(ConversationMessage.document_id.in_(legacy_ids))
                .group_by(ConversationMessage.document_id)
            )
            message_activity.update({
                document_id: (total, users, assistants, characters)
                for document_id, total, users, assistants, characters
                in (await db.execute(legacy_message_q)).all()
            })

    recent_conversations = []
    for row in convos_rows:
        activity_at = (
            logical_activity_by_document.get(row.id) or row.activity_at
        )
        total, users, assistants, characters = message_activity.get(
            row.id,
            (0, 0, 0, 0),
        )
        recent_conversations.append({
            "id": str(row.id),
            "tool_id": row.tool_id,
            "title": conversation_titles.get(row.id) or row.title,
            "activity_at": activity_at.isoformat() if activity_at else None,
            "synced_at": row.synced_at.isoformat(),
            "project_title": row.project_title,
            "message_count": total,
            "pending_question_count": pending_question_counts.get(row.id, 0),
            "agent_mode": row.agent_mode or "",
            "orchestration": (
                str(_row_metadata(row).get("orchestration") or "").strip()
                or None
            ),
            # Group membership flag: matches the aggregate-count predicate
            # exactly (unlinked, not path-linked). Linked claw rows render as
            # ordinary rows and must not be re-grouped client-side.
            "claw_delegate": _is_unlinked_claw_row(row),
            "subagent_count": conversation_hierarchy.subagent_counts.get(
                row.id,
                0,
            ),
            "is_subagent_orphan": (
                row.id in conversation_hierarchy.orphan_document_ids
            ),
            "subagents": subagents_by_document.get(row.id, []),
            "is_low_activity": is_low_activity_summary(
                users,
                assistants,
                characters,
            ),
        })

    cutoff = now - timedelta(days=7)
    tz_adjusted_synced = source.c.synced_at + timedelta(minutes=-tz_offset)
    daily_q = scoped(
        select(
            cast(tz_adjusted_synced, Date).label("day"),
            func.count().label("count"),
        ).where(source.c.synced_at >= cutoff)
    ).group_by("day").order_by("day")
    daily = [
        {"date": str(row.day), "count": row.count}
        for row in (await db.execute(daily_q)).all()
    ]

    tool_daily_q = scoped(
        select(
            source.c.tool_id,
            cast(tz_adjusted_synced, Date).label("day"),
            func.count().label("count"),
        ).where(source.c.synced_at >= cutoff)
    ).group_by(source.c.tool_id, "day").order_by("day")
    tool_daily: dict[str, list] = {}
    for row in (await db.execute(tool_daily_q)).all():
        tool_daily.setdefault(row.tool_id, []).append({
            "date": str(row.day),
            "count": row.count,
        })

    devices_q = select(Machine).order_by(Machine.name).limit(10)
    if selected_machine_ids is not None:
        # The dashboard selector scopes the whole page, including the device
        # summary. Previously every document-backed section changed while the
        # device count/list continued to show all machines, making a valid
        # selection look only partially applied.
        devices_q = devices_q.where(Machine.id.in_(selected_machine_ids))
    if mids is not None:
        devices_q = devices_q.where(Machine.id.in_(mids))
    machine_rows = list((await db.execute(devices_q)).scalars().all())
    device_counts: dict = {}
    if machine_rows:
        device_count_q = (
            select(source.c.machine_id, func.count())
            .where(source.c.machine_id.in_([machine.id for machine in machine_rows]))
            .group_by(source.c.machine_id)
        )
        device_counts = {
            machine_id: count
            for machine_id, count in (await db.execute(device_count_q)).all()
        }
    devices = [{
        "id": str(machine.id),
        "device_id": machine.collector_token_hash,
        "name": machine.name,
        "last_heartbeat": (
            machine.last_heartbeat.isoformat()
            if machine.last_heartbeat
            else None
        ),
        "collector_version": machine.collector_version,
        "total_files": device_counts.get(machine.id, 0),
    } for machine in machine_rows]

    # Never make the primary dashboard wait for external telemetry. Trigger a
    # non-blocking background warm so this ~13 s open-threads poll keeps the
    # in-memory snapshot cache fresh instead of relying on a Spend-panel mount;
    # this removes the post-restart cold window where every thread reads
    # "health not reported". The read below stays cache-only and never awaits
    # spend-provider I/O, so per-thread health is truthfully unknown only until
    # the first background warm lands.
    if open_thread_rows:
        spend_dashboard_proxy.prime_cache()
        health_by_document = _thread_health_by_document(
            spend_dashboard_proxy.get_cached_snapshot()
        )
    else:
        health_by_document = {}
    open_rows_by_machine: dict[str, list] = {}
    for row in open_thread_rows:
        open_rows_by_machine.setdefault(str(row.machine_id or "unknown"), []).append(row)

    open_thread_groups = []
    open_threads_truncated = False
    open_thread_cutoff = now - timedelta(minutes=OPEN_THREAD_ACTIVE_MINUTES)

    def open_activity_key(row):
        return (
            open_logical_activity_by_document.get(row.id)
            or row.activity_at
            or row.source_modified_at
            or row.synced_at,
            str(row.id),
        )

    for machine_id, rows in open_rows_by_machine.items():
        pinned_rows = sorted(
            (row for row in rows if row.id in pinned_root_ids),
            key=open_activity_key,
            reverse=True,
        )
        unpinned_rows = sorted(
            (row for row in rows if row.id not in pinned_root_ids),
            key=open_activity_key,
            reverse=True,
        )
        ordered_rows = pinned_rows + unpinned_rows
        group_truncated = False
        raw_machine_name = str(ordered_rows[0].machine_name or "").strip()
        machine_host, machine_platform = split_device_name(raw_machine_name)
        threads = []
        for row in ordered_rows:
            metadata = _row_metadata(row)
            location = _row_location(row)
            resume_id = conversation_resume_id(
                row.tool_id,
                "conversation",
                metadata,
            )
            activity_at = open_activity_key(row)[0]
            observed_runtime = runtime_by_document.get(row.id)
            threads.append({
                "document_id": str(row.id),
                "tool_id": row.tool_id,
                "title": conversation_titles.get(row.id) or row.title,
                "canonical_url": (
                    native_conversation_url(
                        row.tool_id,
                        "conversation",
                        metadata,
                    )
                    or f"/conversations/{row.id}"
                ),
                "resume_id": resume_id,
                "resume_command": _conversation_resume_command(
                    row.tool_id,
                    resume_id,
                    location,
                ),
                "location": location,
                "project_title": row.project_title,
                "activity_at": activity_at.isoformat() if activity_at else None,
                "is_open": (
                    row.id in running_document_ids
                    or (
                        not row.is_archived
                        and row.id not in handoff_predecessor_ids
                        and activity_at is not None
                        and activity_at >= open_thread_cutoff
                    )
                ),
                "runtime_state": (
                    observed_runtime.state if observed_runtime is not None else None
                ),
                "runtime_privilege": (
                    observed_runtime.privilege
                    if observed_runtime is not None
                    else None
                ),
                "pinned": row.id in pinned_root_ids,
                "health": health_by_document.get(str(row.id)),
            })
        open_thread_groups.append({
            "machine": {
                "id": machine_id,
                "name": raw_machine_name or machine_host,
                "host": machine_host,
                "platform": machine_platform,
            },
            "total_threads": len(rows),
            "truncated": group_truncated,
            "threads": threads,
        })
    open_thread_groups.sort(key=lambda group: (
        group["machine"]["host"].casefold(),
        group["machine"]["platform"].casefold(),
        group["machine"]["id"],
    ))

    today_total_q = scoped(
        select(func.count()).select_from(source).where(
            source.c.synced_at >= today_start
        )
    )
    today_total = (await db.execute(today_total_q)).scalar() or 0
    today_conversation_q = scoped(
        select(func.count()).select_from(source).where(
            source.c.synced_at >= today_start,
            source.c.category == "conversation",
        )
    )
    today_conversations = (
        await db.execute(today_conversation_q)
    ).scalar() or 0

    document_count_q = scoped(select(func.count()).select_from(source))
    total_documents = (await db.execute(document_count_q)).scalar() or 0
    project_count_q = scoped(
        select(func.count(func.distinct(source.c.project_id)))
        .select_from(source)
        .where(source.c.project_id.isnot(None))
    )
    total_projects = (await db.execute(project_count_q)).scalar() or 0

    return {
        "tools": tools,
        "recent_conversations": recent_conversations,
        "claw_delegate_count": claw_delegate_count,
        "open_thread_groups": open_thread_groups,
        "open_thread_active_minutes": OPEN_THREAD_ACTIVE_MINUTES,
        "open_threads_truncated": open_threads_truncated,
        "daily": daily,
        "tool_daily": tool_daily,
        "devices": devices,
        "stats": {
            "total_documents": total_documents,
            "total_projects": total_projects,
            "total_tools": len(tools),
            "total_devices": len(devices),
            "today_total": today_total,
            "today_conversations": today_conversations,
        },
    }
