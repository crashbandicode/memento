"""Conversations API — paginated message viewer with normalized parsing."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, load_only

from ..db.models import (
    CanvasArtifact,
    CanvasArtifactReference,
    ConversationMessage,
    ConversationReadModel,
    ConversationTaskState,
    Document,
    Machine,
    Project,
    User,
)
from ..db.session import get_db, get_search_db
from ..middleware.auth import get_current_user
from ..services.canvas_artifact_store import normalized_path_hash
from ..services.canvas_artifacts import detect_message_canvases
from ..services.conversation_activity import conversation_activity_is_fresh
from ..services.conversation_hierarchy import (
    FOLDABLE_CONVERSATION_TOOLS,
    ConversationRef,
    build_conversation_companion_filter,
    build_logical_activity_map,
    build_subagent_summaries,
    conversation_display_title,
    conversation_root_thread_id,
    conversation_user_role_origin,
    current_thread_id,
    effective_conversation_timestamp,
    fold_conversation_subagents,
    group_conversation_root_thread_ids,
    is_conversation_subagent,
    merge_subagent_event_summaries,
)
from ..services.conversation_identity import (
    conversation_native_id,
    conversation_resume_id,
    native_conversation_tool_id,
    native_conversation_url,
    select_canonical_conversation_document,
)
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
from ..services.conversation_read_model import conversation_prompt_rows_statement
from ..services.claude_lineage import (
    INTERACTION_ORIGIN_KEY,
    history_entry_is_visible,
    load_lineage_active_states,
    normalize_interaction_origin,
)
from ..services.document_delivery import (
    delivery_metadata_expression,
    delivery_synced_expression,
    document_metadata,
)
from ..services.ingest_service import (
    INTERACTION_HISTORY_KEY,
    LIVE_INTERACTION_SIGNALS_KEY,
    LIVE_SHELL_ACTIVITIES_KEY,
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
    enrich_lifecycle_status,
    normalized_subagent_runtime,
    persisted_child_lifecycle,
    subagent_runtime_from_metadata,
)
from ..services.user_filter import user_machine_ids

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

_NATIVE_CONVERSATION_REF = re.compile(
    r"^(?P<tool>claude|codex|cursor)~"
    r"(?P<native>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)

_SHELL_TOOL_NAMES = {
    "bash",
    "execcommand",
    "powershell",
    "runterminalcommand",
    "runterminalcommandv2",
    "shell",
    "shellcommand",
    "terminal",
}


async def resolve_conversation_reference(
    conversation_ref: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> uuid.UUID:
    """Resolve a legacy document UUID or a scoped tool-native URL alias."""
    native_match = _NATIVE_CONVERSATION_REF.fullmatch(str(conversation_ref or ""))
    if native_match is not None:
        mids = await user_machine_ids(db, _user)
        tool_id = native_conversation_tool_id(native_match.group("tool"))
        try:
            native_id = str(uuid.UUID(native_match.group("native")))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=404) from None
        if tool_id is None:
            raise HTTPException(status_code=404)

        statement = (
            select(Document)
            .options(
                load_only(
                    Document.id,
                    Document.tool_id,
                    Document.relative_path,
                    Document.metadata_,
                    Document.source_modified_at,
                    Document.activity_at,
                    Document.synced_at,
                    Document.file_size_bytes,
                )
            )
            .where(
                Document.category == "conversation",
                Document.tool_id == tool_id,
                Document.metadata_["session_id"].astext == native_id,
            )
        )
        if mids is not None:
            statement = statement.where(Document.machine_id.in_(mids))
        candidates = list((await db.execute(statement)).scalars().all())
        selected = select_canonical_conversation_document(
            candidates,
            tool_id=tool_id,
            session_id=native_id,
        )
        if selected is None:
            raise HTTPException(status_code=404)
        return selected.id

    try:
        return uuid.UUID(str(conversation_ref))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404) from None


def _is_shell_tool_name(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    return normalized in _SHELL_TOOL_NAMES


def _shell_command_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if not isinstance(payload, dict):
        return text
    for key in ("command", "cmd", "script"):
        command = payload.get(key)
        if isinstance(command, list):
            candidate = " ".join(str(part) for part in command)
        elif command is not None:
            candidate = str(command)
        else:
            continue
        if candidate.strip():
            return candidate.strip()
    return text


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


async def _conversation_canvas_summaries(
    db: AsyncSession,
    document_id: uuid.UUID,
) -> list[dict]:
    """Return one current, authoritative Canvas descriptor per recorded path."""
    rows = (
        await db.execute(
            select(
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.recorded_path,
                CanvasArtifactReference.name,
                CanvasArtifactReference.status,
                CanvasArtifactReference.artifact_id,
                CanvasArtifactReference.updated_at,
                CanvasArtifact.render_mode,
                ConversationMessage.line_number,
            )
            .join(
                ConversationMessage,
                ConversationMessage.id == CanvasArtifactReference.message_id,
            )
            .outerjoin(
                CanvasArtifact,
                CanvasArtifact.id == CanvasArtifactReference.artifact_id,
            )
            .where(CanvasArtifactReference.document_id == document_id)
            .order_by(
                CanvasArtifactReference.updated_at.desc(),
                ConversationMessage.line_number.desc(),
            )
        )
    ).all()
    summaries: list[dict] = []
    seen: set[str] = set()
    for (
        path_hash,
        recorded_path,
        name,
        status,
        artifact_id,
        updated_at,
        render_mode,
        line_number,
    ) in rows:
        if path_hash in seen:
            continue
        seen.add(path_hash)
        descriptor = {
            "name": name,
            "path": recorded_path,
            "href": recorded_path,
            "source_kind": "unsupported",
            "capture_status": status,
            "line_number": line_number,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }
        if artifact_id:
            artifact_ref = str(artifact_id)
            descriptor.update(
                {
                    "artifact_id": artifact_ref,
                    "source_url": f"/api/canvas-artifacts/{artifact_ref}/source",
                    "source_kind": (
                        "interactive"
                        if render_mode == "interactive"
                        else "captured_source"
                    ),
                }
            )
            if render_mode == "interactive":
                descriptor["render_url"] = (
                    f"/api/canvas-artifacts/{artifact_ref}/render"
                )
        summaries.append(descriptor)
    return summaries

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


def _stored_interaction_history(metadata: object) -> list[dict]:
    if not isinstance(metadata, dict):
        return []
    value = metadata.get(INTERACTION_HISTORY_KEY)
    if isinstance(value, dict):
        entries = value.values()
    elif isinstance(value, list):
        entries = value
    else:
        return []
    return [entry for entry in entries if isinstance(entry, dict)][-32:]


def _lineage_visibility_entries(metadata: object) -> list[dict]:
    """Return retained history plus live signals that carry provenance."""
    entries = list(_stored_interaction_history(metadata))
    if not isinstance(metadata, dict):
        return entries
    signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
    if isinstance(signals, dict):
        entries.extend(
            signal for signal in signals.values() if isinstance(signal, dict)
        )
    return entries


def _current_interaction_history_anchor(
    entry: dict,
    projected_through_line: object,
) -> int | None:
    """Return an anchor only when it still belongs to this projection.

    Claude permission prompts arrive through a document-level hook side
    channel.  Rewinding/resuming a Claude session can replace the normalized
    branch while leaving an older interaction-history entry behind.  Its line
    number then refers to the superseded projection and must not be appended
    after the current branch's real final message.
    """
    try:
        anchor = max(0, int(entry.get("anchor_line_number") or 0))
        current_last_line = max(0, int(projected_through_line or 0))
    except (TypeError, ValueError):
        return None
    if anchor > current_last_line:
        return None
    return anchor


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
    *,
    read_model: ConversationReadModel | None = None,
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

    if read_model is not None and read_model.root_thread_id:
        projected_filters = []
        if tool_use_ids:
            projected_filters.append(
                ConversationReadModel.agent_tool_use_id.in_(tool_use_ids)
            )
        if thread_ids:
            projected_filters.extend([
                ConversationReadModel.agent_id.in_(thread_ids),
                ConversationReadModel.thread_id.in_(thread_ids),
            ])
        child_rows = (
            await db.execute(
                select(
                    ConversationReadModel.agent_tool_use_id,
                    ConversationReadModel.agent_id,
                    ConversationReadModel.thread_id,
                    ConversationReadModel.runtime,
                    ConversationReadModel.lifecycle,
                ).where(
                    ConversationReadModel.document_id != document.id,
                    ConversationReadModel.machine_id == document.machine_id,
                    ConversationReadModel.tool_id == document.tool_id,
                    ConversationReadModel.root_thread_id
                    == read_model.root_thread_id,
                    or_(*projected_filters),
                )
            )
        ).all()
        by_tool_use: dict[str, dict[str, str]] = {}
        by_thread: dict[str, dict[str, str]] = {}
        lifecycle_by_tool_use: dict[str, dict[str, str]] = {}
        lifecycle_by_thread: dict[str, dict[str, str]] = {}
        for tool_use_id, agent_id, thread_id, runtime, lifecycle in child_rows:
            safe_runtime = runtime if isinstance(runtime, dict) else {}
            safe_lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
            if tool_use_id and safe_runtime:
                by_tool_use[str(tool_use_id)] = safe_runtime
            if tool_use_id and safe_lifecycle:
                lifecycle_by_tool_use[str(tool_use_id)] = safe_lifecycle
            for value in (agent_id, thread_id):
                for alias in _agent_id_aliases(value):
                    if safe_runtime:
                        by_thread[alias] = safe_runtime
                    if safe_lifecycle:
                        lifecycle_by_thread[alias] = safe_lifecycle
    else:
        effective_metadata = delivery_metadata_expression()
        child_filters = []
        if tool_use_ids:
            child_filters.append(
                effective_metadata["agent_tool_use_id"].astext.in_(tool_use_ids)
            )
        if thread_ids:
            child_filters.extend([
                effective_metadata["agent_id"].astext.in_(thread_ids),
                effective_metadata["session_id"].astext.in_(thread_ids),
                effective_metadata["thread_id"].astext.in_(thread_ids),
            ])
        if not child_filters:
            return {}

        child_query = select(effective_metadata).where(
            Document.id != document.id,
            Document.machine_id == document.machine_id,
            Document.tool_id == document.tool_id,
            Document.category == "conversation",
            or_(*child_filters),
        )
        parent_root = conversation_root_thread_id(
            document.tool_id,
            document.relative_path,
            document.metadata_,
        )
        if parent_root:
            child_query = child_query.where(
                build_conversation_companion_filter(
                    Document.tool_id,
                    effective_metadata,
                    Document.relative_path,
                    {document.tool_id: {parent_root}},
                )
            )
        child_rows = (await db.execute(child_query)).scalars().all()

        by_tool_use = {}
        by_thread = {}
        lifecycle_by_tool_use = {}
        lifecycle_by_thread = {}
        for raw_metadata in child_rows:
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            runtime = subagent_runtime_from_metadata(metadata)
            lifecycle = persisted_child_lifecycle(metadata)
            tool_use_id = str(metadata.get("agent_tool_use_id") or "").strip()
            if tool_use_id and runtime:
                by_tool_use[tool_use_id] = runtime
            if tool_use_id and lifecycle:
                lifecycle_by_tool_use[tool_use_id] = lifecycle
            for key in ("agent_id", "session_id", "thread_id"):
                for alias in _agent_id_aliases(metadata.get(key)):
                    if runtime:
                        by_thread[alias] = runtime
                    if lifecycle:
                        lifecycle_by_thread[alias] = lifecycle

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
        lifecycle = lifecycle_by_tool_use.get(tool_use_id) or next(
            (
                lifecycle_by_thread[alias]
                for alias in _agent_id_aliases(thread_id)
                if alias in lifecycle_by_thread
            ),
            None,
        )
        normalized_event = enrich_lifecycle_status(
            enrich_lifecycle_runtime(
                event,
                runtime or normalized_subagent_runtime(
                    model=event.get("model"),
                    reasoning_effort=event.get("reasoning_effort"),
                ),
            ),
            lifecycle,
        )
        overrides[message_id] = normalized_event
    return overrides


async def _get_conversation_identity(
    db: AsyncSession,
    user: User,
    doc_id: uuid.UUID,
) -> tuple[Document, ConversationReadModel | None]:
    """Return one authorized document and its compact read projection."""
    statement = (
        select(Document, ConversationReadModel)
        .outerjoin(
            ConversationReadModel,
            ConversationReadModel.document_id == Document.id,
        )
        .options(
            load_only(
                Document.id,
                Document.machine_id,
                Document.tool_id,
                Document.title,
                Document.relative_path,
                Document.metadata_,
            ),
            joinedload(Document.delivery_state),
        )
        .where(Document.id == doc_id)
    )
    if user.role not in ("admin", "owner"):
        statement = statement.where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user.id)
            )
        )
    row = (await db.execute(statement)).first()
    if row is None:
        raise HTTPException(status_code=404)
    if hasattr(row, "_mapping") or isinstance(row, (tuple, list)):
        return row[0], row[1]
    # Lightweight unit doubles historically returned the scalar document.
    return row, None


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


@router.get("/{conversation_ref}")
async def get_conversation(
    doc_id: uuid.UUID = Depends(resolve_conversation_reference),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get conversation metadata and message count."""
    mids = await user_machine_ids(db, _user)

    result = await db.execute(
        select(Document, ConversationReadModel)
        .outerjoin(
            ConversationReadModel,
            ConversationReadModel.document_id == Document.id,
        )
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
    row = result.first()
    if row is not None and (
        hasattr(row, "_mapping") or isinstance(row, (tuple, list))
    ):
        doc, read_model = row[0], row[1]
    else:
        doc, read_model = row, None
    if not doc:
        raise HTTPException(status_code=404)
    if mids is not None and doc.machine_id not in mids:
        raise HTTPException(status_code=404)

    # Normalized rows are written transactionally during ingest and are the
    # viewer's indexed representation.  Prefer their cheap indexed count over
    # hydrating and reparsing a potentially hundreds-of-megabytes JSONL blob.
    if read_model is not None:
        message_count = int(read_model.message_count or 0)
    else:
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
    subagents_truncated = False
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
                delivery_metadata_expression(),
                Document.relative_path,
                roots_by_tool,
            ),
        )
        hierarchy_states_by_id: dict[uuid.UUID, ConversationReadModel] = {}
        if read_model is not None and read_model.root_thread_id:
            hierarchy_q = (
                select(Document, ConversationReadModel)
                .join(
                    ConversationReadModel,
                    ConversationReadModel.document_id == Document.id,
                )
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
                    Document.machine_id == doc.machine_id,
                    ConversationReadModel.tool_id == doc.tool_id,
                    ConversationReadModel.root_thread_id
                    == read_model.root_thread_id,
                )
                .order_by(Document.id)
                .limit(257)
            )
            hierarchy_rows = (await db.execute(hierarchy_q)).all()
            subagents_truncated = len(hierarchy_rows) > 256
            hierarchy_rows = hierarchy_rows[:256]
            hierarchy_docs = [item[0] for item in hierarchy_rows]
            hierarchy_states_by_id = {
                item[0].id: item[1] for item in hierarchy_rows
            }
        else:
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
        if hierarchy_states_by_id:
            lifecycle_rows = [
                (
                    source_document_id,
                    {"agent_event": item["event"]},
                    item.get("timestamp"),
                )
                for source_document_id in lifecycle_document_ids
                for item in (
                    hierarchy_states_by_id.get(source_document_id).agent_events
                    if hierarchy_states_by_id.get(source_document_id) is not None
                    else []
                )
                if isinstance(item, dict) and isinstance(item.get("event"), dict)
            ]
        else:
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
                    "timestamp": (
                        timestamp.isoformat()
                        if isinstance(timestamp, datetime)
                        else timestamp
                    ),
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
            .order_by(delivery_synced_expression().desc())
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

    conversation_canvases = await _conversation_canvas_summaries(db, doc.id)
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
        "native_id": conversation_native_id(
            doc.tool_id,
            "conversation",
            doc.metadata_,
        ),
        "resume_id": conversation_resume_id(
            doc.tool_id,
            "conversation",
            doc.metadata_,
        ),
        "canonical_url": native_conversation_url(
            doc.tool_id,
            "conversation",
            doc.metadata_,
        ),
        "canvases": conversation_canvases,
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
        "subagents_truncated": subagents_truncated,
        "is_subagent_orphan": is_subagent_orphan,
        "subagents": subagents,
        "activity_at": activity_at.isoformat() if activity_at else None,
        "synced_at": doc.synced_at.isoformat(),
        "related_plans": related_plans,
    }


@router.get("/{conversation_ref}/messages")
async def get_conversation_messages(
    doc_id: uuid.UUID = Depends(resolve_conversation_reference),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    tail: bool = Query(False),
    line_number: int | None = Query(None, ge=1),
    context_before: int = Query(0, ge=0, le=200),
    after_line: int | None = None,
    before_line: int | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get paginated, human-readable conversation messages."""
    doc, read_model = await _get_conversation_identity(db, _user, doc_id)
    user_role_origin = conversation_user_role_origin(
        doc.tool_id,
        doc.relative_path,
        doc.metadata_,
    )

    # Prefer normalized rows. They are indexed by document and line number,
    # preserve the viewer fields, and avoid reparsing the raw transcript for
    # every initial page, prompt jump, and scroll page.
    base_filter = [ConversationMessage.document_id == doc_id]
    if read_model is not None:
        total = int(read_model.message_count or 0)
    else:
        count_result = await db.execute(select(func.count()).where(*base_filter))
        total = count_result.scalar() or 0
    if total > 0:
        message_query = (
            select(ConversationMessage)
            .where(*base_filter)
            .order_by(ConversationMessage.line_number)
            .limit(limit + 1)
        )
        descending = False
        if line_number is not None:
            start_line = max(1, line_number - context_before)
            offset = max(0, start_line - 1)
            message_query = message_query.where(
                ConversationMessage.line_number >= start_line
            )
        elif tail is True:
            descending = True
            message_query = message_query.order_by(
                None
            ).order_by(
                ConversationMessage.line_number.desc(),
                ConversationMessage.id.desc(),
            )
        elif before_line is not None:
            descending = True
            message_query = (
                message_query.where(
                    ConversationMessage.line_number < before_line
                )
                .order_by(None)
                .order_by(
                    ConversationMessage.line_number.desc(),
                    ConversationMessage.id.desc(),
                )
            )
        elif after_line is not None:
            message_query = message_query.where(
                ConversationMessage.line_number > after_line
            )
        else:
            if offset:
                message_query = message_query.offset(offset)

        msgs_result = await db.execute(message_query)
        fetched = msgs_result.scalars().all()
        page_has_extra = len(fetched) > limit
        messages = fetched[:limit]
        if descending:
            messages.reverse()
        if messages:
            offset = max(0, int(messages[0].line_number or 1) - 1)
        has_earlier = bool(messages) and (
            page_has_extra if before_line is not None else offset > 0
        )
        if tail is True:
            has_more = False
        elif before_line is not None:
            has_more = True
        else:
            has_more = page_has_extra
        agent_event_overrides = await _subagent_event_runtime_overrides(
            db,
            doc,
            messages,
            read_model=read_model,
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
            "has_more": has_more,
            "has_earlier": has_earlier,
            "next_after_line": (
                messages[-1].line_number if has_more and messages else None
            ),
            "previous_before_line": (
                messages[0].line_number if has_earlier and messages else None
            ),
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
                    "tool_call_id": (m.metadata_ or {}).get("tool_call_id", ""),
                    "tool_status": (m.metadata_ or {}).get("tool_status", ""),
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
                    "tool_call_id": m.tool_call_id,
                    "tool_status": m.tool_status,
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


async def _projected_pending_interactions(
    db: AsyncSession,
    document: Document,
    projection: ConversationReadModel,
) -> dict:
    """Assemble bounded interaction state without replaying message history."""
    statement = (
        select(Document, ConversationReadModel)
        .join(
            ConversationReadModel,
            ConversationReadModel.document_id == Document.id,
        )
        .where(Document.machine_id == document.machine_id)
        .order_by(Document.id)
        .limit(257)
        .options(joinedload(Document.delivery_state))
    )
    if projection.root_thread_id:
        statement = statement.where(
            ConversationReadModel.tool_id == document.tool_id,
            ConversationReadModel.root_thread_id == projection.root_thread_id,
        )
    else:
        statement = statement.where(Document.id == document.id)
    rows = (await db.execute(statement)).all()
    rows = rows[:256]
    if not rows:
        rows = [(document, projection)]

    lineage_states = await load_lineage_active_states(
        db,
        (
            (source_document.id, entry)
            for source_document, _state in rows
            for entry in _lineage_visibility_entries(
                document_metadata(source_document)
            )
        ),
    )

    interactions: list[dict] = []
    inline_by_id: dict[str, dict] = {}
    inferred: list[dict] = []
    activities_by_id: dict[tuple[str, str], dict] = {}
    seen_interaction_ids: set[str] = set()
    activity_now = datetime.now(timezone.utc)
    for source_document, state in rows:
        metadata = document_metadata(source_document)
        source_is_subagent = is_conversation_subagent(
            source_document.tool_id,
            source_document.relative_path,
            metadata,
        )
        title = conversation_display_title(
            source_document.tool_id,
            source_document.relative_path,
            metadata,
            source_document.title,
        )
        for raw_item in state.pending_interactions or []:
            if not isinstance(raw_item, dict):
                continue
            item = {**raw_item, "source_title": title}
            interaction = item.get("interaction")
            interaction_id = (
                str(interaction.get("id") or "").strip()
                if isinstance(interaction, dict)
                else ""
            )
            if not interaction_id or interaction_id in seen_interaction_ids:
                continue
            seen_interaction_ids.add(interaction_id)
            interactions.append(item)
        for item in state.inferred_responses or []:
            if isinstance(item, dict):
                inferred.append(dict(item))
        for raw_activity in state.live_activities or []:
            if (
                not isinstance(raw_activity, dict)
                or not conversation_activity_is_fresh(
                    raw_activity,
                    now=activity_now,
                )
            ):
                continue
            activity_id = str(raw_activity.get("activity_id") or "").strip()
            if activity_id:
                activities_by_id[(str(source_document.id), activity_id)] = {
                    **raw_activity,
                    "source_title": title,
                }

        signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
        if isinstance(signals, dict):
            for interaction_id, signal in signals.items():
                if (
                    interaction_id in seen_interaction_ids
                    or not isinstance(signal, dict)
                ):
                    continue
                interaction = coerce_claude_live_interaction(
                    signal.get("interaction")
                )
                if not isinstance(interaction, dict):
                    continue
                if interaction.get("interaction_type") == "permission_request":
                    origin = normalize_interaction_origin(
                        signal.get(INTERACTION_ORIGIN_KEY)
                    )
                    lineage_state = (
                        lineage_states.get(
                            (
                                str(source_document.id),
                                str(origin.get("record_uuid") or ""),
                            )
                        )
                        if origin is not None
                        else None
                    )
                    if not history_entry_is_visible(
                        signal,
                        projected_through_line=state.projected_through_line,
                        lineage_state=lineage_state,
                        document_is_subagent=source_is_subagent,
                    ):
                        continue
                canonical_id = str(
                    interaction.get("id") or interaction_id
                ).strip()
                if not canonical_id or canonical_id in seen_interaction_ids:
                    continue
                seen_interaction_ids.add(canonical_id)
                item = {
                    "document_id": str(source_document.id),
                    "source_title": title,
                    "message_id": 0,
                    "line_number": 0,
                    "interaction": interaction,
                    "model": signal.get("model", ""),
                    "reasoning_effort": signal.get("reasoning_effort", ""),
                    "service_tier": signal.get("service_tier", ""),
                    "agent_mode": signal.get("agent_mode", ""),
                    "timestamp": signal.get("timestamp") or None,
                }
                interactions.append(item)
                if interaction.get("interaction_type") == "permission_request":
                    inline_by_id[canonical_id] = {**item, "status": "pending"}

        for entry in _stored_interaction_history(metadata):
            interaction = coerce_claude_live_interaction(entry.get("interaction"))
            if (
                not isinstance(interaction, dict)
                or interaction.get("interaction_type") != "permission_request"
            ):
                continue
            interaction_id = str(interaction.get("id") or "").strip()
            status = str(entry.get("status") or "").strip().lower()
            if not interaction_id or status not in {
                "pending",
                "answered",
                "cancelled",
            }:
                continue
            origin = normalize_interaction_origin(
                entry.get(INTERACTION_ORIGIN_KEY)
            )
            lineage_state = (
                lineage_states.get(
                    (str(source_document.id), str(origin.get("record_uuid") or ""))
                )
                if origin is not None
                else None
            )
            if not history_entry_is_visible(
                entry,
                projected_through_line=state.projected_through_line,
                lineage_state=lineage_state,
                document_is_subagent=source_is_subagent,
            ):
                continue
            anchor_line_number = max(
                0,
                int(entry.get("anchor_line_number") or 0),
            )
            response = entry.get("response")
            inline_by_id[interaction_id] = {
                "document_id": str(source_document.id),
                "source_title": title,
                "message_id": 0,
                "line_number": anchor_line_number,
                "interaction": interaction,
                **({"response": response} if isinstance(response, dict) else {}),
                "model": entry.get("model", ""),
                "reasoning_effort": entry.get("reasoning_effort", ""),
                "service_tier": entry.get("service_tier", ""),
                "agent_mode": entry.get("agent_mode", ""),
                "timestamp": entry.get("timestamp") or None,
                "status": status,
            }

        raw_activities = metadata.get(LIVE_SHELL_ACTIVITIES_KEY)
        if isinstance(raw_activities, dict):
            for activity_id, raw_activity in raw_activities.items():
                if not isinstance(raw_activity, dict):
                    continue
                canonical_id = str(
                    raw_activity.get("id") or activity_id
                ).strip()
                command = str(raw_activity.get("command") or "").strip()
                status = str(raw_activity.get("status") or "").strip().lower()
                if (
                    not canonical_id
                    or not command
                    or status not in {
                        "running",
                        "completed",
                        "failed",
                        "cancelled",
                    }
                ):
                    continue
                activity = {
                    "document_id": str(source_document.id),
                    "source_title": title,
                    "message_id": 0,
                    "line_number": max(
                        0,
                        int(raw_activity.get("anchor_line_number") or 0),
                    ),
                    "activity_id": canonical_id,
                    "activity_type": "shell",
                    "status": status,
                    "tool_name": str(raw_activity.get("tool_name") or "Shell"),
                    "command": command,
                    "started_at": raw_activity.get("started_at") or None,
                    "updated_at": raw_activity.get("updated_at") or None,
                }
                if conversation_activity_is_fresh(activity, now=activity_now):
                    activities_by_id[(str(source_document.id), canonical_id)] = (
                        activity
                    )

    interactions.sort(
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("document_id") or ""),
            int(item.get("line_number") or 0),
        )
    )
    inferred.sort(
        key=lambda item: (
            str(item.get("timestamp") or ""),
            int(item.get("line_number") or 0),
        )
    )
    inline = sorted(
        inline_by_id.values(),
        key=lambda item: (
            int(item.get("line_number") or 0) <= 0,
            int(item.get("line_number") or 0),
            str(item.get("timestamp") or ""),
        ),
    )
    activities = sorted(
        activities_by_id.values(),
        key=lambda item: (
            int(item.get("line_number") or 0) <= 0,
            int(item.get("line_number") or 0),
            str(item.get("started_at") or ""),
        ),
    )
    return {
        "count": len(interactions[-64:]),
        "interactions": interactions[-64:],
        "inline_interactions": inline[-64:],
        "live_activities": activities[-64:],
        "inferred_responses": inferred[-64:],
    }


@router.get("/{conversation_ref}/pending-interactions")
async def get_pending_conversation_interactions(
    doc_id: uuid.UUID = Depends(resolve_conversation_reference),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return unresolved questions independently of transcript pagination."""
    doc, read_model = await _get_conversation_identity(db, _user, doc_id)
    if read_model is not None:
        return await _projected_pending_interactions(db, doc, read_model)
    effective_doc_metadata = document_metadata(doc)
    source_documents = {
        doc.id: conversation_display_title(
            doc.tool_id,
            doc.relative_path,
            effective_doc_metadata,
            doc.title,
        )
    }
    source_metadata = {doc.id: effective_doc_metadata}
    source_origins = {
        doc.id: conversation_user_role_origin(
            doc.tool_id,
            doc.relative_path,
            effective_doc_metadata,
        )
    }
    source_is_subagent = {
        doc.id: is_conversation_subagent(
            doc.tool_id,
            doc.relative_path,
            effective_doc_metadata,
        )
    }

    if doc.tool_id in FOLDABLE_CONVERSATION_TOOLS:
        current_ref = ConversationRef(
            document_id=doc.id,
            tool_id=doc.tool_id,
            relative_path=doc.relative_path,
            metadata=effective_doc_metadata,
            title=doc.title,
        )
        roots_by_tool = group_conversation_root_thread_ids([current_ref])
        companion_filter = build_conversation_companion_filter(
            Document.tool_id,
            delivery_metadata_expression(),
            Document.relative_path,
            roots_by_tool,
        )
        effective_metadata = delivery_metadata_expression()
        companion_rows = (
            await db.execute(
                select(Document.id, Document.title, effective_metadata).where(
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
            source_is_subagent[companion_id] = is_conversation_subagent(
                doc.tool_id,
                None,
                companion_metadata,
            )

    # The fallback must apply the same range guard as a read model. Its later
    # interaction scan intentionally selects only relevant semantic rows, so
    # it cannot safely infer the transcript high-water mark from that subset.
    history_document_ids = [
        document_id
        for document_id, metadata in source_metadata.items()
        if _stored_interaction_history(metadata)
    ]
    projected_through_lines: dict[uuid.UUID, int] = {}
    if history_document_ids:
        projected_rows = await db.execute(
            select(
                ConversationMessage.document_id,
                func.max(ConversationMessage.line_number),
            )
            .where(ConversationMessage.document_id.in_(history_document_ids))
            .group_by(ConversationMessage.document_id)
        )
        projected_through_lines = {
            document_id: max(0, int(last_line or 0))
            for document_id, last_line in projected_rows.all()
        }

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
    lineage_states = await load_lineage_active_states(
        db,
        (
            (source_document_id, entry)
            for source_document_id, metadata in source_metadata.items()
            for entry in _lineage_visibility_entries(metadata)
        ),
    )
    recent_tool_rows = (
        (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.document_id == doc.id,
                    ConversationMessage.metadata_.op("?")("tool_call_id"),
                )
                .order_by(ConversationMessage.line_number.desc())
                .limit(512)
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
    live_activities_by_key: dict[tuple[uuid.UUID, str], dict] = {}
    activity_now = datetime.now(timezone.utc)
    for message in reversed(recent_tool_rows):
        message_metadata = (
            message.metadata_ if isinstance(message.metadata_, dict) else {}
        )
        tool_call_id = str(message_metadata.get("tool_call_id") or "").strip()
        if not tool_call_id:
            continue
        activity_key = (message.document_id, tool_call_id)
        raw_type = str(message.message_type or "").strip().casefold()
        tool_status = str(
            message_metadata.get("tool_status") or ""
        ).strip().casefold()
        if raw_type in {
            "tool_result",
            "tool_output",
            "question_tool_output",
        } or tool_status in {
            "cancelled",
            "canceled",
            "completed",
            "done",
            "error",
            "failed",
            "interrupted",
            "success",
        }:
            live_activities_by_key.pop(activity_key, None)
            continue
        tool_name = str(message_metadata.get("tool_name") or "").strip()
        command = _shell_command_text(message_metadata.get("tool_input"))
        if not _is_shell_tool_name(tool_name) or not command:
            continue
        timestamp = message.timestamp.isoformat() if message.timestamp else None
        activity = {
            "document_id": str(message.document_id),
            "source_title": source_documents.get(message.document_id),
            "message_id": message.id,
            "line_number": message.line_number,
            "activity_id": tool_call_id,
            "activity_type": "shell",
            "status": "running",
            "tool_name": tool_name,
            "command": command,
            "started_at": timestamp,
            "updated_at": timestamp,
        }
        if conversation_activity_is_fresh(activity, now=activity_now):
            live_activities_by_key[activity_key] = activity
    inline_interactions_by_id: dict[str, dict] = {}
    seen_question_fingerprints = {
        interaction_question_fingerprint(interaction)
        for _message, interaction in pending
        if interaction_question_fingerprint(interaction)
    }
    for source_document_id, metadata in source_metadata.items():
        if not isinstance(metadata, dict):
            continue
        signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
        if isinstance(signals, dict):
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
                interaction = coerce_claude_live_interaction(
                    signal.get("interaction")
                )
                if not isinstance(interaction, dict):
                    continue
                if interaction.get("interaction_type") == "permission_request":
                    origin = normalize_interaction_origin(
                        signal.get(INTERACTION_ORIGIN_KEY)
                    )
                    lineage_state = (
                        lineage_states.get(
                            (
                                str(source_document_id),
                                str(origin.get("record_uuid") or ""),
                            )
                        )
                        if origin is not None
                        else None
                    )
                    if not history_entry_is_visible(
                        signal,
                        projected_through_line=projected_through_lines.get(
                            source_document_id,
                            0,
                        ),
                        lineage_state=lineage_state,
                        document_is_subagent=source_is_subagent.get(
                            source_document_id,
                            False,
                        ),
                    ):
                        continue
                fingerprint = interaction_question_fingerprint(interaction)
                if fingerprint and fingerprint in seen_question_fingerprints:
                    continue
                if fingerprint:
                    seen_question_fingerprints.add(fingerprint)
                live_item = {
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
                live_pending.append(live_item)
                canonical_id = str(interaction.get("id") or interaction_id).strip()
                if (
                    canonical_id
                    and canonical_id not in questions
                    and interaction.get("interaction_type") == "permission_request"
                ):
                    inline_interactions_by_id[canonical_id] = {
                        **live_item,
                        "status": "pending",
                    }

    for source_document_id, metadata in source_metadata.items():
        for entry in _stored_interaction_history(metadata):
            interaction = coerce_claude_live_interaction(entry.get("interaction"))
            if (
                not isinstance(interaction, dict)
                or interaction.get("interaction_type") != "permission_request"
            ):
                continue
            interaction_id = str(interaction.get("id") or "").strip()
            if not interaction_id or interaction_id in questions:
                continue
            status = str(entry.get("status") or "").strip().lower()
            if status not in {"pending", "answered", "cancelled"}:
                continue
            origin = normalize_interaction_origin(
                entry.get(INTERACTION_ORIGIN_KEY)
            )
            lineage_state = (
                lineage_states.get(
                    (str(source_document_id), str(origin.get("record_uuid") or ""))
                )
                if origin is not None
                else None
            )
            if not history_entry_is_visible(
                entry,
                projected_through_line=projected_through_lines.get(
                    source_document_id,
                    0,
                ),
                lineage_state=lineage_state,
                document_is_subagent=source_is_subagent.get(
                    source_document_id,
                    False,
                ),
            ):
                continue
            if (
                status == "pending"
                and interaction_id in inline_interactions_by_id
            ):
                continue
            fingerprint = interaction_question_fingerprint(interaction)
            if fingerprint and fingerprint in seen_question_fingerprints:
                continue
            if fingerprint:
                seen_question_fingerprints.add(fingerprint)
            anchor_line_number = max(
                0,
                int(entry.get("anchor_line_number") or 0),
            )
            response = entry.get("response")
            inline_interactions_by_id[interaction_id] = {
                "document_id": str(source_document_id),
                "source_title": source_documents.get(source_document_id),
                "message_id": 0,
                "line_number": anchor_line_number,
                "interaction": interaction,
                **({"response": response} if isinstance(response, dict) else {}),
                "model": entry.get("model", ""),
                "reasoning_effort": entry.get("reasoning_effort", ""),
                "service_tier": entry.get("service_tier", ""),
                "agent_mode": entry.get("agent_mode", ""),
                "timestamp": entry.get("timestamp") or None,
                "status": status,
            }
    for source_document_id, metadata in source_metadata.items():
        if not isinstance(metadata, dict):
            continue
        raw_activities = metadata.get(LIVE_SHELL_ACTIVITIES_KEY)
        if not isinstance(raw_activities, dict):
            continue
        for activity_id, raw_activity in raw_activities.items():
            if not isinstance(raw_activity, dict):
                continue
            canonical_id = str(raw_activity.get("id") or activity_id).strip()
            status = str(raw_activity.get("status") or "").strip().lower()
            command = str(raw_activity.get("command") or "").strip()
            if (
                not canonical_id
                or status not in {
                    "running",
                    "completed",
                    "failed",
                    "cancelled",
                }
                or not command
            ):
                continue
            activity_at = raw_activity.get("updated_at") or raw_activity.get(
                "started_at"
            )
            if not conversation_activity_is_fresh(
                {
                    "status": status,
                    "updated_at": activity_at,
                },
                now=activity_now,
            ):
                continue
            try:
                anchor_line_number = max(
                    0,
                    int(raw_activity.get("anchor_line_number") or 0),
                )
            except (TypeError, ValueError):
                anchor_line_number = 0
            live_activities_by_key[(source_document_id, canonical_id)] = {
                "document_id": str(source_document_id),
                "source_title": source_documents.get(source_document_id),
                "message_id": 0,
                "line_number": anchor_line_number,
                "activity_id": canonical_id,
                "activity_type": "shell",
                "status": status,
                "tool_name": str(raw_activity.get("tool_name") or "Shell"),
                "command": command,
                "started_at": raw_activity.get("started_at") or None,
                "updated_at": raw_activity.get("updated_at") or None,
            }
    live_pending.sort(key=lambda item: str(item.get("timestamp") or ""))
    live_pending = live_pending[-64:]
    inline_interactions = list(inline_interactions_by_id.values())
    inline_interactions.sort(
        key=lambda item: (
            int(item.get("line_number") or 0) <= 0,
            int(item.get("line_number") or 0),
            str(item.get("timestamp") or ""),
        )
    )
    live_activities = list(live_activities_by_key.values())
    live_activities.sort(
        key=lambda item: (
            int(item.get("line_number") or 0) <= 0,
            int(item.get("line_number") or 0),
            str(item.get("started_at") or ""),
        )
    )
    live_activities = live_activities[-64:]
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
        "inline_interactions": inline_interactions,
        "live_activities": live_activities,
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


@router.get("/{conversation_ref}/latest-agent-message")
async def get_latest_agent_message(
    doc_id: uuid.UUID = Depends(resolve_conversation_reference),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return the latest assistant line without loading a transcript window."""
    doc, read_model = await _get_conversation_identity(db, _user, doc_id)
    if read_model is not None:
        return {"line_number": read_model.latest_assistant_line}
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

    normalized_exists = (
        await db.execute(
            select(ConversationMessage.id)
            .where(ConversationMessage.document_id == doc_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if normalized_exists is not None:
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


@router.get("/{conversation_ref}/search")
async def search_conversation_messages(
    doc_id: uuid.UUID = Depends(resolve_conversation_reference),
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
    doc, _read_model = await _get_conversation_identity(db, _user, doc_id)
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


@router.get("/{conversation_ref}/prompts")
async def get_conversation_prompts(
    doc_id: uuid.UUID = Depends(resolve_conversation_reference),
    after_line: int | None = None,
    generation: int | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Return a lightweight outline of every meaningful human prompt."""
    doc, read_model = await _get_conversation_identity(db, _user, doc_id)
    if conversation_user_role_origin(
        doc.tool_id,
        doc.relative_path,
        doc.metadata_,
    ) == "parent_agent":
        return {
            "prompts": [],
            **(
                {
                    "generation": read_model.generation,
                    "projected_through_line": read_model.projected_through_line,
                    "reset": generation != read_model.generation
                    if generation is not None
                    else False,
                }
                if read_model is not None
                else {}
            ),
        }

    if read_model is not None:
        reset = generation is not None and generation != read_model.generation
        minimum_line = None if reset else after_line
        prompt_rows = (
            await db.execute(
                conversation_prompt_rows_statement(
                    doc_id,
                    after_line=minimum_line,
                )
            )
        ).scalars().all()
        prompts = [
            {
                "id": item.message_id,
                "line_number": item.line_number,
                "content": item.content,
                "timestamp": (
                    item.timestamp.isoformat() if item.timestamp else None
                ),
            }
            for item in prompt_rows
        ]
        return {
            "prompts": prompts,
            "generation": read_model.generation,
            "projected_through_line": read_model.projected_through_line,
            "reset": reset,
        }

    normalized_exists = (
        await db.execute(
            select(ConversationMessage.id)
            .where(ConversationMessage.document_id == doc_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    prompts = []
    if normalized_exists is not None:
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
