"""Conversations API — paginated message viewer with normalized parsing."""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, load_only

from ..db.models import (
    CanvasArtifact,
    CanvasArtifactReference,
    ConversationMessage,
    ConversationTaskState,
    Document,
    Machine,
    Project,
    User,
)
from ..db.session import get_db, get_search_db
from ..middleware.auth import get_current_user
from ..services.conversation_hierarchy import (
    FOLDABLE_CONVERSATION_TOOLS,
    ConversationRef,
    build_conversation_companion_filter,
    build_logical_activity_map,
    build_subagent_summaries,
    conversation_display_title,
    conversation_user_role_origin,
    current_thread_id,
    effective_conversation_timestamp,
    fold_conversation_subagents,
    group_conversation_root_thread_ids,
    is_conversation_subagent,
    merge_subagent_event_summaries,
)
from ..services.canvas_artifacts import detect_message_canvases
from ..services.canvas_artifact_store import normalized_path_hash
from ..services.conversation_markdown import (
    is_meaningful_human_prompt,
    is_meaningful_human_turn,
)
from ..services.conversation_parser import (
    build_cursor_question_response,
    coerce_claude_live_interaction,
    count_conversation_messages,
    interaction_question_fingerprint,
    normalize_message_attachments,
    normalize_tool_calls,
    parse_conversation,
)
from ..services.ingest_service import (
    LIVE_INTERACTION_SIGNALS_KEY,
    interaction_at_or_before_human,
)
from ..services.message_search import (
    MAX_SEARCH_CONTENT_CHARS,
    build_message_search_expressions,
    make_search_snippet,
    normalize_search_query,
    suggest_corrected_query,
)
from ..services.subagent_lifecycle import (
    enrich_lifecycle_runtime,
    normalized_subagent_runtime,
    subagent_runtime_from_metadata,
)
from ..services.user_filter import user_machine_ids

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _canvas_field(content: str | None, links: dict[str, dict] | None = None) -> dict:
    """Attach validated Cursor Canvas descriptors only when a message has any."""
    canvases = detect_message_canvases(content)
    if links:
        for canvas in canvases:
            link = links.get(normalized_path_hash(str(canvas.get("path") or "")))
            if not link:
                continue
            canvas["capture_status"] = link["status"]
            if link.get("artifact_id"):
                artifact_id = link["artifact_id"]
                canvas["artifact_id"] = artifact_id
                canvas["source_url"] = f"/api/canvas-artifacts/{artifact_id}/source"
                if link.get("render_mode") == "interactive":
                    canvas["render_url"] = (
                        f"/api/canvas-artifacts/{artifact_id}/render"
                    )
                    canvas["source_kind"] = "interactive"
                else:
                    canvas["source_kind"] = "captured_source"
    return {"canvases": canvases} if canvases else {}


async def _canvas_links_for_messages(
    db: AsyncSession,
    message_ids: list[int],
) -> dict[int, dict[str, dict]]:
    if not message_ids:
        return {}
    rows = (
        await db.execute(
            select(
                CanvasArtifactReference.message_id,
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.artifact_id,
                CanvasArtifactReference.status,
                CanvasArtifact.render_mode,
            )
            .outerjoin(
                CanvasArtifact,
                CanvasArtifact.id == CanvasArtifactReference.artifact_id,
            )
            .where(CanvasArtifactReference.message_id.in_(message_ids))
        )
    ).all()
    links: dict[int, dict[str, dict]] = {}
    for message_id, path_hash, artifact_id, status, render_mode in rows:
        links.setdefault(message_id, {})[path_hash] = {
            "artifact_id": str(artifact_id) if artifact_id else None,
            "status": status,
            "render_mode": render_mode,
        }
    return links

_MACHINE_PLATFORM_SUFFIX_RE = re.compile(
    r"\s+\((?:darwin|linux|windows)\)\s*$",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _absolute_project_path(value: object) -> str | None:
    """Return an existing absolute path without decoding project hashes."""
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


def _conversation_location(document: Document) -> dict[str, str] | None:
    """Build the compact, authorized host/path pair for one document."""
    machine = getattr(document, "machine", None)
    raw_host = str(getattr(machine, "name", "") or "").strip()
    host = _MACHINE_PLATFORM_SUFFIX_RE.sub("", raw_host).strip()
    if not host or re.search(r"[\x00-\x1f\x7f-\x9f]", host):
        return None

    metadata = (
        document.metadata_
        if isinstance(getattr(document, "metadata_", None), dict)
        else {}
    )
    project = getattr(document, "project", None)
    path = next(
        (
            candidate
            for candidate in (
                _absolute_project_path(metadata.get("project_path")),
                _absolute_project_path(metadata.get("cwd")),
                _absolute_project_path(getattr(project, "source_path", None)),
            )
            if candidate
        ),
        None,
    )
    return {"host": host[:255], "path": path} if path else None


def _parsed_tool_calls(message: object) -> list[dict[str, object]]:
    return normalize_tool_calls(getattr(message, "tool_calls", None))


def _stored_tool_calls(metadata: object) -> list[dict[str, object]]:
    """Read the same bounded tool-call shape used by raw-content parsing."""
    if not isinstance(metadata, dict):
        return []
    return normalize_tool_calls(metadata.get("tool_calls"))


def _stored_attachments(metadata: object) -> list[dict[str, str]]:
    """Read the same bounded attachment shape used by raw-content parsing."""
    if not isinstance(metadata, dict):
        return []
    return normalize_message_attachments(metadata.get("attachments"))


def _stored_interaction(metadata: object, key: str) -> dict | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    return value if isinstance(value, dict) else None


def _stored_task_state(metadata: object) -> dict | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("task_state")
    return value if isinstance(value, dict) else None


def _pending_question_count(metadata: object) -> int:
    if not isinstance(metadata, dict):
        return 0
    try:
        return max(0, int(metadata.get("pending_question_count") or 0))
    except (TypeError, ValueError):
        return 0


def _stored_agent_event(metadata: object) -> dict | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("agent_event")
    return value if isinstance(value, dict) else None


def _agent_id_aliases(value: object) -> set[str]:
    identity = str(value or "").strip()
    if not identity:
        return set()
    aliases = {identity}
    if identity.startswith("agent-"):
        aliases.add(identity[len("agent-"):])
    else:
        aliases.add(f"agent-{identity}")
    return aliases


async def _subagent_event_runtime_overrides(
    db: AsyncSession,
    document: Document,
    messages: list[ConversationMessage],
) -> dict[int, dict]:
    """Resolve actual child runtime identity for lifecycle rows on one page."""
    event_rows: list[tuple[int, dict]] = []
    tool_use_ids: set[str] = set()
    thread_ids: set[str] = set()
    for message in messages:
        event = _stored_agent_event(message.metadata_)
        if event is None or event.get("activity_type", "subagent") != "subagent":
            continue
        tool_use_id = str(event.get("agent_tool_use_id") or "").strip()
        thread_id = str(event.get("agent_thread_id") or "").strip()
        if not tool_use_id and not thread_id:
            continue
        event_rows.append((message.id, event))
        if tool_use_id:
            tool_use_ids.add(tool_use_id)
        thread_ids.update(_agent_id_aliases(thread_id))
    if not event_rows:
        return {}

    child_filters = []
    if tool_use_ids:
        child_filters.append(
            Document.metadata_["agent_tool_use_id"].astext.in_(tool_use_ids)
        )
    if thread_ids:
        child_filters.extend([
            Document.metadata_["agent_id"].astext.in_(thread_ids),
            Document.metadata_["session_id"].astext.in_(thread_ids),
            Document.metadata_["thread_id"].astext.in_(thread_ids),
        ])
    if not child_filters:
        return {}

    child_rows = (
        await db.execute(
            select(Document.metadata_)
            .where(
                Document.id != document.id,
                Document.machine_id == document.machine_id,
                Document.tool_id == document.tool_id,
                Document.category == "conversation",
                or_(*child_filters),
            )
        )
    ).scalars().all()

    by_tool_use: dict[str, dict[str, str]] = {}
    by_thread: dict[str, dict[str, str]] = {}
    for raw_metadata in child_rows:
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        runtime = subagent_runtime_from_metadata(metadata)
        if not runtime:
            continue
        tool_use_id = str(metadata.get("agent_tool_use_id") or "").strip()
        if tool_use_id:
            by_tool_use[tool_use_id] = runtime
        for key in ("agent_id", "session_id", "thread_id"):
            for alias in _agent_id_aliases(metadata.get(key)):
                by_thread[alias] = runtime

    overrides: dict[int, dict] = {}
    for message_id, event in event_rows:
        tool_use_id = str(event.get("agent_tool_use_id") or "").strip()
        thread_id = str(event.get("agent_thread_id") or "").strip()
        runtime = by_tool_use.get(tool_use_id) or next(
            (
                by_thread[alias]
                for alias in _agent_id_aliases(thread_id)
                if alias in by_thread
            ),
            None,
        )
        normalized_event = enrich_lifecycle_runtime(
            event,
            runtime or normalized_subagent_runtime(
                model=event.get("model"),
                reasoning_effort=event.get("reasoning_effort"),
            ),
        )
        overrides[message_id] = normalized_event
    return overrides


async def _get_conversation_identity(
    db: AsyncSession,
    user: User,
    doc_id: uuid.UUID,
) -> Document:
    """Return the minimal authorized document shape used by message APIs."""
    mids = await user_machine_ids(db, user)
    doc = (
        await db.execute(
            select(Document)
            .options(
                load_only(
                    Document.id,
                    Document.machine_id,
                    Document.tool_id,
                    Document.title,
                    Document.relative_path,
                    Document.metadata_,
                )
            )
            .where(Document.id == doc_id)
        )
    ).scalar_one_or_none()
    if not doc or (mids is not None and doc.machine_id not in mids):
        raise HTTPException(status_code=404)
    return doc


def _message_question_interactions(metadata: object) -> list[dict]:
    """Return every normalized question carried by one message row."""
    if not isinstance(metadata, dict):
        return []
    interactions: list[dict] = []
    direct = metadata.get("interaction")
    if isinstance(direct, dict):
        interactions.append(direct)
    for call in _stored_tool_calls(metadata):
        interaction = call.get("interaction")
        if isinstance(interaction, dict):
            interactions.append(interaction)
    return interactions


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
        .options(
            load_only(
                Document.id,
                Document.machine_id,
                Document.project_id,
                Document.tool_id,
                Document.title,
                Document.relative_path,
                Document.metadata_,
                Document.source_modified_at,
                Document.activity_at,
                Document.synced_at,
                Document.file_size_bytes,
            ),
            joinedload(Document.machine).load_only(Machine.name),
            joinedload(Document.project).load_only(Project.source_path),
        )
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
    active_task_state = (
        await db.execute(
            select(ConversationTaskState.state).where(
                ConversationTaskState.document_id == doc_id
            )
        )
    ).scalar_one_or_none()
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
            .options(
                load_only(
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
                )
            )
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
        subagents_by_parent = build_subagent_summaries(
            hierarchy,
            hierarchy_refs,
        )
        summary_parent_id = hierarchy.canonical_document_ids.get(doc.id, doc.id)
        subagents = subagents_by_parent.get(summary_parent_id, [])
        current_is_subagent = is_conversation_subagent(
            doc.tool_id,
            doc.relative_path,
            doc.metadata_,
        )
        lifecycle_document_ids = (
            [doc.id]
            if current_is_subagent
            else [item.id for item in hierarchy_docs]
        )
        lifecycle_rows = (
            await db.execute(
                select(
                    ConversationMessage.document_id,
                    ConversationMessage.metadata_,
                    ConversationMessage.timestamp,
                )
                .where(
                    ConversationMessage.document_id.in_(lifecycle_document_ids),
                    ConversationMessage.metadata_.op("?")("agent_event"),
                    or_(
                        func.coalesce(
                            func.jsonb_extract_path_text(
                                ConversationMessage.metadata_,
                                "agent_event",
                                "agent_thread_id",
                            ),
                            "",
                        )
                        != "",
                        func.coalesce(
                            func.jsonb_extract_path_text(
                                ConversationMessage.metadata_,
                                "agent_event",
                                "agent_tool_use_id",
                            ),
                            "",
                        )
                        != "",
                    ),
                )
                .order_by(
                    ConversationMessage.timestamp,
                    ConversationMessage.document_id,
                    ConversationMessage.line_number,
                )
            )
        ).all()
        refs_by_id = {item.document_id: item for item in hierarchy_refs}
        lifecycle_events: list[dict] = []
        for source_document_id, metadata, timestamp in lifecycle_rows:
            event = _stored_agent_event(metadata)
            if event is None:
                continue
            source_ref = refs_by_id.get(source_document_id)
            source_metadata = source_ref.metadata if source_ref is not None else {}
            try:
                source_depth = int((source_metadata or {}).get("agent_depth") or 0)
            except (TypeError, ValueError):
                source_depth = 0
            lifecycle_events.append(
                {
                    **event,
                    "parent_thread_id": current_thread_id(source_metadata),
                    "agent_depth": source_depth + 1,
                    "user_role_origin": (
                        "parent_agent"
                        if doc.tool_id == "claude_code"
                        else None
                    ),
                    "timestamp": timestamp.isoformat() if timestamp else None,
                }
            )
        subagents = merge_subagent_event_summaries(
            subagents,
            lifecycle_events,
        )
        if current_is_subagent:
            current_thread = current_thread_id(doc.metadata_)
            subagents = [
                child
                for child in subagents
                if child.get("parent_thread_id") == current_thread
            ]
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
            related_plans.append(
                {
                    "id": str(p.id),
                    "title": p.title,
                    "relative_path": p.relative_path,
                    "category": p.category,
                    "content_type": p.content_type,
                    "content": p.content[:5000] if p.content else None,
                    "file_size_bytes": p.file_size_bytes,
                    "synced_at": p.synced_at.isoformat(),
                }
            )

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
        "title": conversation_display_title(
            doc.tool_id,
            doc.relative_path,
            doc.metadata_,
            doc.title,
        ),
        "relative_path": doc.relative_path,
        "metadata": doc.metadata_,
        "user_role_origin": conversation_user_role_origin(
            doc.tool_id,
            doc.relative_path,
            doc.metadata_,
        ),
        "location": _conversation_location(doc),
        "active_task_state": active_task_state,
        "pending_question_count": _pending_question_count(doc.metadata_),
        "agent_mode": (
            str((doc.metadata_ or {}).get("_assistant_agent_mode") or "")
            if isinstance(doc.metadata_, dict)
            else ""
        ),
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
    tail: bool = Query(False),
    line_number: int | None = Query(None, ge=1),
    context_before: int = Query(0, ge=0, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get paginated, human-readable conversation messages."""
    doc = await _get_conversation_identity(db, _user, doc_id)
    user_role_origin = conversation_user_role_origin(
        doc.tool_id,
        doc.relative_path,
        doc.metadata_,
    )

    # Prefer normalized rows. They are indexed by document and line number,
    # preserve the viewer fields, and avoid reparsing the raw transcript for
    # every initial page, prompt jump, and scroll page.
    base_filter = [ConversationMessage.document_id == doc_id]
    count_result = await db.execute(select(func.count()).where(*base_filter))
    total = count_result.scalar() or 0
    if total > 0:
        if tail is True and line_number is None:
            offset = max(0, total - limit)
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
        agent_event_overrides = await _subagent_event_runtime_overrides(
            db,
            doc,
            messages,
        )
        canvas_links = await _canvas_links_for_messages(
            db,
            [
                message.id
                for message in messages
                if ".canvas.tsx" in (message.content or "").casefold()
            ],
        )
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "messages": [
                {
                    "id": m.id,
                    "line_number": m.line_number,
                    "role": m.role or m.message_type,
                    "origin": (
                        user_role_origin
                        if (m.role or m.message_type) == "user"
                        else None
                    ),
                    "content": m.content,
                    "thinking": (
                        (m.metadata_ or {}).get("thinking") if m.metadata_ else None
                    ),
                    "model": (m.metadata_ or {}).get("model", ""),
                    "reasoning_effort": (m.metadata_ or {}).get("reasoning_effort", ""),
                    "service_tier": (m.metadata_ or {}).get("service_tier", ""),
                    "agent_mode": (m.metadata_ or {}).get("agent_mode", ""),
                    "tool_name": (m.metadata_ or {}).get("tool_name", ""),
                    "tool_input": (m.metadata_ or {}).get("tool_input", ""),
                    "session_context": (m.metadata_ or {}).get("session_context", ""),
                    "attachments": _stored_attachments(m.metadata_),
                    "tool_calls": _stored_tool_calls(m.metadata_),
                    "interaction": _stored_interaction(m.metadata_, "interaction"),
                    "interaction_response": _stored_interaction(
                        m.metadata_,
                        "interaction_response",
                    ),
                    "task_state": _stored_task_state(m.metadata_),
                    "agent_event": (
                        agent_event_overrides.get(m.id)
                        or _stored_agent_event(m.metadata_)
                    ),
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                    "raw_type": m.message_type or "",
                    **_canvas_field(m.content, canvas_links.get(m.id)),
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
        if tail is True and line_number is None:
            offset = max(0, total - limit)
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
                    "origin": user_role_origin if m.role == "user" else None,
                    "content": m.content,
                    "thinking": m.thinking or None,
                    "model": m.model,
                    "reasoning_effort": m.reasoning_effort,
                    "service_tier": m.service_tier,
                    "agent_mode": m.agent_mode,
                    "tool_name": m.tool_name,
                    "tool_input": m.tool_input,
                    "session_context": m.session_context,
                    "attachments": normalize_message_attachments(m.attachments),
                    "tool_calls": _parsed_tool_calls(m),
                    "interaction": m.interaction,
                    "interaction_response": m.interaction_response,
                    "task_state": m.task_state,
                    "agent_event": m.agent_event,
                    "timestamp": m.timestamp or None,
                    "raw_type": m.raw_type,
                    **_canvas_field(m.content),
                }
                for i, m in enumerate(page)
            ],
        }
    return {"total": 0, "offset": offset, "limit": limit, "messages": []}


@router.get("/{doc_id}/pending-interactions")
async def get_pending_conversation_interactions(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return unresolved questions independently of transcript pagination."""
    doc = await _get_conversation_identity(db, _user, doc_id)
    source_documents = {
        doc.id: conversation_display_title(
            doc.tool_id,
            doc.relative_path,
            doc.metadata_,
            doc.title,
        )
    }
    source_metadata = {doc.id: doc.metadata_}
    source_origins = {
        doc.id: conversation_user_role_origin(
            doc.tool_id,
            doc.relative_path,
            doc.metadata_,
        )
    }

    if doc.tool_id in FOLDABLE_CONVERSATION_TOOLS:
        current_ref = ConversationRef(
            document_id=doc.id,
            tool_id=doc.tool_id,
            relative_path=doc.relative_path,
            metadata=doc.metadata_,
            title=doc.title,
        )
        roots_by_tool = group_conversation_root_thread_ids([current_ref])
        companion_filter = build_conversation_companion_filter(
            Document.tool_id,
            Document.metadata_,
            Document.relative_path,
            roots_by_tool,
        )
        companion_rows = (
            await db.execute(
                select(Document.id, Document.title, Document.metadata_).where(
                    Document.machine_id == doc.machine_id,
                    Document.tool_id == doc.tool_id,
                    Document.category == "conversation",
                    or_(Document.id == doc.id, companion_filter),
                )
            )
        ).all()
        for companion_id, companion_title, companion_metadata in companion_rows:
            source_documents[companion_id] = conversation_display_title(
                doc.tool_id,
                None,
                companion_metadata,
                companion_title,
            )
            source_metadata[companion_id] = companion_metadata
            source_origins[companion_id] = conversation_user_role_origin(
                doc.tool_id,
                None,
                companion_metadata,
            )

    rows = (
        (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.document_id.in_(source_documents),
                    or_(
                        ConversationMessage.role == "user",
                        ConversationMessage.metadata_.op("?")("interaction"),
                        ConversationMessage.metadata_.op("?")("interaction_response"),
                        ConversationMessage.metadata_.op("?")("tool_calls"),
                    ),
                )
                .order_by(
                    ConversationMessage.document_id,
                    ConversationMessage.line_number,
                )
            )
        )
        .scalars()
        .all()
    )

    questions: dict[str, tuple[ConversationMessage, dict]] = {}
    resolved_ids: set[str] = set()
    open_ids: dict[uuid.UUID, set[str]] = {}
    latest_human_timestamps: dict[uuid.UUID, object] = {}
    inferred_responses: dict[str, tuple[ConversationMessage, dict]] = {}
    for message in rows:
        document_open_ids = open_ids.setdefault(message.document_id, set())
        message_metadata = dict(message.metadata_ or {})
        if source_origins.get(message.document_id):
            message_metadata["message_origin"] = source_origins[message.document_id]
        message_interactions = _message_question_interactions(message.metadata_)
        for interaction in message_interactions:
            interaction_id = str(interaction.get("id") or "").strip()
            if interaction_id:
                if interaction_at_or_before_human(
                    message.timestamp,
                    latest_human_timestamps.get(message.document_id),
                ):
                    resolved_ids.add(interaction_id)
                    document_open_ids.discard(interaction_id)
                    continue
                questions[interaction_id] = (message, interaction)
                document_open_ids.add(interaction_id)
        response = _stored_interaction(message.metadata_, "interaction_response")
        if response is not None:
            interaction_id = str(response.get("interaction_id") or "").strip()
            if interaction_id:
                resolved_ids.add(interaction_id)
                document_open_ids.discard(interaction_id)
        else:
            for interaction in message_interactions:
                inferred = build_cursor_question_response(
                    interaction,
                    message.content,
                )
                if inferred is None:
                    continue
                interaction_id = str(interaction.get("id") or "").strip()
                if interaction_id:
                    resolved_ids.add(interaction_id)
                    document_open_ids.discard(interaction_id)
        if response is None and is_meaningful_human_prompt(
            message.content,
            message_metadata,
            message.role,
        ):
            for interaction_id in document_open_ids:
                resolved_ids.add(interaction_id)
                inferred_responses[interaction_id] = (
                    message,
                    {
                        "kind": "question_response",
                        "interaction_id": interaction_id,
                        "status": "answered",
                        "answers": [],
                        "raw_text": message.content[:4000],
                    },
                )
            document_open_ids.clear()
        if is_meaningful_human_turn(
            message.content,
            message_metadata,
            message.role,
        ):
            if response is not None:
                resolved_ids.update(document_open_ids)
                document_open_ids.clear()
            current_human_timestamp = latest_human_timestamps.get(message.document_id)
            if (
                current_human_timestamp is None
                or not interaction_at_or_before_human(
                    message.timestamp,
                    current_human_timestamp,
                )
            ):
                latest_human_timestamps[message.document_id] = message.timestamp

    pending = [
        (message, interaction)
        for interaction_id, (message, interaction) in questions.items()
        if interaction_id not in resolved_ids
    ]
    pending.sort(
        key=lambda item: (
            item[0].timestamp.isoformat() if item[0].timestamp else "",
            str(item[0].document_id),
            item[0].line_number,
        )
    )
    pending = pending[-64:]
    live_pending: list[dict] = []
    seen_question_fingerprints = {
        interaction_question_fingerprint(interaction)
        for _message, interaction in pending
        if interaction_question_fingerprint(interaction)
    }
    for source_document_id, metadata in source_metadata.items():
        if not isinstance(metadata, dict):
            continue
        signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
        if not isinstance(signals, dict):
            continue
        for interaction_id, signal in signals.items():
            if (
                interaction_id in resolved_ids
                or interaction_id in questions
                or not isinstance(signal, dict)
                or interaction_at_or_before_human(
                    signal.get("timestamp"),
                    latest_human_timestamps.get(source_document_id),
                )
            ):
                continue
            interaction = coerce_claude_live_interaction(signal.get("interaction"))
            if not isinstance(interaction, dict):
                continue
            fingerprint = interaction_question_fingerprint(interaction)
            if fingerprint and fingerprint in seen_question_fingerprints:
                continue
            if fingerprint:
                seen_question_fingerprints.add(fingerprint)
            live_pending.append(
                {
                    "document_id": str(source_document_id),
                    "source_title": source_documents.get(source_document_id),
                    "message_id": 0,
                    "line_number": 0,
                    "interaction": interaction,
                    "model": signal.get("model", ""),
                    "reasoning_effort": signal.get("reasoning_effort", ""),
                    "service_tier": signal.get("service_tier", ""),
                    "agent_mode": signal.get("agent_mode", ""),
                    "timestamp": signal.get("timestamp") or None,
                }
            )
    live_pending.sort(key=lambda item: str(item.get("timestamp") or ""))
    live_pending = live_pending[-64:]
    return {
        "count": len(pending) + len(live_pending),
        "interactions": [
            {
                "document_id": str(message.document_id),
                "source_title": source_documents.get(message.document_id),
                "message_id": message.id,
                "line_number": message.line_number,
                "interaction": interaction,
                "model": (message.metadata_ or {}).get("model", ""),
                "reasoning_effort": (message.metadata_ or {}).get(
                    "reasoning_effort", ""
                ),
                "service_tier": (message.metadata_ or {}).get("service_tier", ""),
                "agent_mode": (message.metadata_ or {}).get("agent_mode", ""),
                "timestamp": (
                    message.timestamp.isoformat() if message.timestamp else None
                ),
            }
            for message, interaction in pending
        ]
        + live_pending,
        "inferred_responses": [
            {
                "document_id": str(message.document_id),
                "message_id": message.id,
                "line_number": message.line_number,
                "response": response,
                "timestamp": (
                    message.timestamp.isoformat() if message.timestamp else None
                ),
            }
            for message, response in inferred_responses.values()
        ],
    }


@router.get("/{doc_id}/latest-agent-message")
async def get_latest_agent_message(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return the latest assistant line without loading a transcript window."""
    doc = await _get_conversation_identity(db, _user, doc_id)
    latest_line = (
        await db.execute(
            select(ConversationMessage.line_number)
            .where(
                ConversationMessage.document_id == doc_id,
                func.coalesce(
                    ConversationMessage.role,
                    ConversationMessage.message_type,
                )
                == "assistant",
            )
            .order_by(
                ConversationMessage.line_number.desc(),
                ConversationMessage.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_line is not None:
        return {"line_number": latest_line}

    normalized_count = (
        await db.execute(
            select(func.count()).where(ConversationMessage.document_id == doc_id)
        )
    ).scalar() or 0
    if normalized_count > 0:
        return {"line_number": None}

    raw_content = (
        await db.execute(select(Document.content).where(Document.id == doc_id))
    ).scalar_one_or_none()
    if not raw_content:
        return {"line_number": None}
    messages = parse_conversation(raw_content, doc.tool_id)
    latest_line = next(
        (
            index
            for index in range(len(messages), 0, -1)
            if messages[index - 1].role == "assistant"
        ),
        None,
    )
    return {"line_number": latest_line}


@router.get("/{doc_id}/search")
async def search_conversation_messages(
    doc_id: uuid.UUID,
    q: str = Query(..., min_length=1, max_length=500),
    after_line: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_search_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Search one normalized transcript without loading it into the client.

    Results are chronological so next/previous navigation behaves like an
    editor search. The existing ``messages?line_number=`` endpoint loads the
    bounded rendering window when a hit is selected.
    """
    doc = await _get_conversation_identity(db, _user, doc_id)
    user_role_origin = conversation_user_role_origin(
        doc.tool_id,
        doc.relative_path,
        doc.metadata_,
    )

    query_text = normalize_search_query(q)
    if not query_text:
        return {
            "query": "",
            "results": [],
            "next_after_line": None,
            "has_more": False,
            "corrected_query": None,
        }

    def search_statement(search_text: str):
        expressions = build_message_search_expressions(
            search_text,
            allow_short_substring=True,
        )
        statement = (
            select(
                ConversationMessage.id,
                ConversationMessage.line_number,
                ConversationMessage.role,
                func.left(
                    ConversationMessage.content,
                    MAX_SEARCH_CONTENT_CHARS,
                ).label("content"),
                ConversationMessage.timestamp,
                expressions.score.label("score"),
                expressions.match_type.label("match_type"),
            )
            .where(
                ConversationMessage.document_id == doc_id,
                expressions.predicate,
            )
            .order_by(ConversationMessage.line_number, ConversationMessage.id)
            .limit(limit + 1)
        )
        if after_line is not None:
            statement = statement.where(ConversationMessage.line_number > after_line)
        return statement

    primary_rows = (await db.execute(search_statement(query_text))).mappings().all()
    rows = [dict(row, snippet_query=query_text) for row in primary_rows]
    corrected_query = await suggest_corrected_query(db, query_text)
    corrected_count = 0
    if corrected_query:
        corrected_rows = (
            (await db.execute(search_statement(corrected_query))).mappings().all()
        )
        corrected_count = len(corrected_rows)
        seen_message_ids = {row["id"] for row in rows}
        for corrected_row in corrected_rows:
            if corrected_row["id"] in seen_message_ids:
                continue
            row = dict(corrected_row, snippet_query=corrected_query)
            row["score"] = 1.0 + min(
                max(float(row["score"] or 0.0) - 3.0, 0.0),
                0.999999,
            )
            row["match_type"] = "fuzzy"
            rows.append(row)
    rows.sort(key=lambda row: (row["line_number"], row["id"]))
    has_more = len(rows) > limit or len(primary_rows) > limit or corrected_count > limit
    page = rows[:limit]
    return {
        "query": query_text,
        "results": [
            {
                "id": row["id"],
                "line_number": row["line_number"],
                "role": row["role"],
                "origin": (
                    user_role_origin
                    if row["role"] == "user"
                    else None
                ),
                "snippet": make_search_snippet(row["content"], row["snippet_query"]),
                "timestamp": (
                    row["timestamp"].isoformat() if row["timestamp"] else None
                ),
                "score": round(float(row["score"] or 0.0), 6),
                "match_type": row["match_type"],
            }
            for row in page
        ],
        "next_after_line": page[-1]["line_number"] if has_more and page else None,
        "has_more": has_more,
        "corrected_query": corrected_query,
    }


@router.get("/{doc_id}/prompts")
async def get_conversation_prompts(
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return a lightweight outline of every meaningful human prompt."""
    doc = await _get_conversation_identity(db, _user, doc_id)
    if conversation_user_role_origin(
        doc.tool_id,
        doc.relative_path,
        doc.metadata_,
    ) == "parent_agent":
        return {"prompts": []}

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
                ConversationMessage.metadata_,
            )
            .where(
                ConversationMessage.document_id == doc_id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.line_number)
        )
        for message_id, line_number, content, timestamp, metadata in prompt_rows.all():
            clean = (content or "").strip()
            if not is_meaningful_human_prompt(clean, metadata):
                continue
            prompts.append(
                {
                    "id": message_id,
                    "line_number": line_number,
                    "content": clean[:500],
                    "timestamp": timestamp.isoformat() if timestamp else None,
                }
            )
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
                if is_meaningful_human_prompt(
                    message.content,
                    {"interaction_response": message.interaction_response}
                    if message.interaction_response
                    else {},
                    message.role,
                )
            ]

    return {"prompts": prompts}
