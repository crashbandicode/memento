"""Download conversation transcripts as faithful, filtered Markdown."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import uuid
import zipfile
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TextIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..db.models import (
    ConversationMessage,
    Document,
    Machine,
    Project,
    User,
)
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_activity import is_low_activity_summary
from ..services.conversation_hierarchy import (
    ConversationRef,
    build_logical_activity_map,
    conversation_root_thread_id,
    current_thread_id,
    fold_conversation_subagents,
    is_conversation_subagent,
)
from ..services.conversation_markdown import (
    ConversationMarkdownInfo,
    ExportMessage,
    MarkdownExportOptions,
    PromptSelection,
    parse_prompt_selection,
    safe_markdown_filename,
    write_conversation_markdown,
)
from ..services.conversation_parser import parse_conversation
from ..services.message_search import (
    build_message_search_expressions,
    normalize_search_query,
)
from ..services.user_filter import user_machine_ids


logger = logging.getLogger("memento.conversation_exports")
router = APIRouter(prefix="/api/exports/conversations", tags=["exports"])

MAX_MARKDOWN_EXPORT_BYTES = int(os.environ.get(
    "MEMENTO_MARKDOWN_EXPORT_MAX_BYTES",
    str(2 * 1024 * 1024 * 1024),
))

_export_locks: dict[str, asyncio.Lock] = {}


class ConversationExportRequest(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    prompt_range: str = Field(default="", max_length=512)
    query: str = Field(default="", max_length=500)
    tool_ids: list[str] = Field(default_factory=list, max_length=20)
    project_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    include_subagents: bool = False
    include_low_activity: bool = False
    include_tools: bool = True
    include_thinking: bool = True
    include_session_context: bool = True
    include_timestamps: bool = True
    output: Literal["zip", "combined"] = "zip"
    max_threads: int = Field(default=250, ge=1, le=1000)


@dataclass(frozen=True, slots=True)
class _ExportDocument:
    id: uuid.UUID
    tool_id: str
    title: str
    relative_path: str
    metadata: Mapping[str, Any]
    activity_at: datetime | None
    source_modified_at: datetime | None
    synced_at: datetime | None
    project_title: str | None
    machine_name: str | None
    project_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _SelectionPlan:
    spans: tuple[tuple[int, int | None], ...] | None
    prompt_numbers: Mapping[int, int]


@dataclass(slots=True)
class _ByteBudget:
    maximum: int
    used: int = 0

    def add(self, value: str) -> None:
        self.used += len(value.encode("utf-8"))
        if self.used > self.maximum:
            raise ValueError(
                f"Markdown export exceeds the {self.maximum // (1024 * 1024)} MiB safety cap"
            )


class _BudgetWriter:
    def __init__(self, target: TextIO, budget: _ByteBudget) -> None:
        self.target = target
        self.budget = budget

    def write(self, value: str) -> int:
        self.budget.add(value)
        return self.target.write(value)


def _export_lock(user_id: str) -> asyncio.Lock:
    lock = _export_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _export_locks[user_id] = lock
    return lock


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _options(
    prompt_range: str,
    start_at: datetime | None,
    end_at: datetime | None,
    include_tools: bool,
    include_thinking: bool,
    include_session_context: bool,
    include_timestamps: bool,
) -> MarkdownExportOptions:
    if start_at is not None and end_at is not None and start_at > end_at:
        raise HTTPException(status_code=422, detail="start_at must not be after end_at")
    try:
        prompt_selection = parse_prompt_selection(prompt_range)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MarkdownExportOptions(
        prompt_selection=prompt_selection,
        start_at=start_at,
        end_at=end_at,
        include_tools=include_tools,
        include_thinking=include_thinking,
        include_session_context=include_session_context,
        include_timestamps=include_timestamps,
    )


def _parse_stored_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _prompt_date_matches(
    timestamp: datetime | None,
    options: MarkdownExportOptions,
) -> bool:
    if options.start_at is None and options.end_at is None:
        return True
    current = _aware(timestamp)
    if current is None:
        return False
    start = _aware(options.start_at)
    end = _aware(options.end_at)
    return bool(
        (start is None or current >= start)
        and (end is None or current <= end)
    )


def _line_span_predicate(line_number, spans: tuple[tuple[int, int | None], ...]):
    return or_(*[
        line_number >= start if end is None else line_number.between(start, end)
        for start, end in spans
    ])


async def _selection_plan(
    db: AsyncSession,
    document_id: uuid.UUID,
    normalized_count: int,
    options: MarkdownExportOptions,
) -> _SelectionPlan:
    filtered = bool(
        options.prompt_selection is not None
        or options.start_at is not None
        or options.end_at is not None
    )
    if not filtered or normalized_count == 0:
        return _SelectionPlan(None, {})

    rows = (
        await db.execute(
            select(
                ConversationMessage.line_number,
                ConversationMessage.timestamp,
            )
            .where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.role == "user",
                func.length(func.trim(ConversationMessage.content)) > 0,
                ~ConversationMessage.content.startswith("[Subagent Context]"),
                ConversationMessage.metadata_["interaction_response"].astext.is_(None),
            )
            .order_by(ConversationMessage.line_number, ConversationMessage.id)
        )
    ).all()
    prompt_numbers = {
        line_number: index
        for index, (line_number, _timestamp) in enumerate(rows, start=1)
    }
    selected: list[tuple[int, int | None]] = []
    for index, (line_number, timestamp) in enumerate(rows, start=1):
        if (
            options.prompt_selection is not None
            and not options.prompt_selection.includes(index)
        ):
            continue
        if not _prompt_date_matches(timestamp, options):
            continue
        next_line = rows[index][0] if index < len(rows) else None
        selected.append((line_number, next_line - 1 if next_line is not None else None))

    merged: list[tuple[int, int | None]] = []
    for start, end in selected:
        if merged and merged[-1][1] is not None and start <= merged[-1][1] + 1:
            prior_start, prior_end = merged[-1]
            merged[-1] = (
                prior_start,
                None if end is None else max(prior_end, end),
            )
        else:
            merged.append((start, end))
    return _SelectionPlan(tuple(merged), prompt_numbers)


async def _message_stream(
    db: AsyncSession,
    document: _ExportDocument,
    normalized_count: int,
    plan: _SelectionPlan,
) -> AsyncIterator[ExportMessage]:
    if normalized_count:
        if plan.spans == ():
            return
        query = (
            select(ConversationMessage)
            .where(ConversationMessage.document_id == document.id)
            .order_by(ConversationMessage.line_number, ConversationMessage.id)
            .execution_options(yield_per=1000)
        )
        if plan.spans is not None:
            query = query.where(_line_span_predicate(
                ConversationMessage.line_number,
                plan.spans,
            ))
        stream = await db.stream_scalars(query)
        async for message in stream:
            yield ExportMessage(
                line_number=message.line_number,
                role=message.role or message.message_type or "system",
                content=message.content or "",
                metadata=message.metadata_ or {},
                timestamp=message.timestamp,
                message_type=message.message_type or "",
                prompt_number=plan.prompt_numbers.get(message.line_number),
            )
        return

    raw_content = (
        await db.execute(
            select(Document.content).where(Document.id == document.id)
        )
    ).scalar_one_or_none()
    if not raw_content:
        return
    for index, message in enumerate(parse_conversation(raw_content, document.tool_id), start=1):
        metadata: dict[str, Any] = {}
        if message.thinking:
            metadata["thinking"] = message.thinking
        if message.tool_name:
            metadata["tool_name"] = message.tool_name
        if message.tool_input:
            metadata["tool_input"] = message.tool_input
        if message.session_context:
            metadata["session_context"] = message.session_context
        if message.attachments:
            metadata["attachments"] = message.attachments
        if message.tool_calls:
            metadata["tool_calls"] = message.tool_calls
        if message.interaction:
            metadata["interaction"] = message.interaction
        if message.interaction_response:
            metadata["interaction_response"] = message.interaction_response
        yield ExportMessage(
            line_number=index,
            role=message.role,
            content=message.content,
            metadata=metadata,
            timestamp=_parse_stored_timestamp(message.timestamp),
            message_type=message.raw_type,
        )


async def _interaction_responses(
    db: AsyncSession,
    document_id: uuid.UUID,
    plan: _SelectionPlan,
) -> dict[str, Mapping[str, Any]]:
    if plan.spans == ():
        return {}
    query = select(ConversationMessage.metadata_).where(
        ConversationMessage.document_id == document_id,
        ConversationMessage.metadata_["interaction_response"].astext.is_not(None),
    )
    if plan.spans is not None:
        query = query.where(_line_span_predicate(
            ConversationMessage.line_number,
            plan.spans,
        ))
    rows = (await db.execute(query)).scalars().all()
    responses: dict[str, Mapping[str, Any]] = {}
    for metadata in rows:
        response = (metadata or {}).get("interaction_response")
        if not isinstance(response, dict):
            continue
        interaction_id = str(response.get("interaction_id") or "")
        if interaction_id:
            responses[interaction_id] = response
    return responses


async def _message_count(db: AsyncSession, document_id: uuid.UUID) -> int:
    return int((
        await db.execute(
            select(func.count()).where(
                ConversationMessage.document_id == document_id
            )
        )
    ).scalar() or 0)


def _markdown_info(
    document: _ExportDocument,
    message_count: int,
) -> ConversationMarkdownInfo:
    return ConversationMarkdownInfo(
        title=document.title or document.relative_path or "Untitled conversation",
        tool_id=document.tool_id,
        document_id=str(document.id),
        relative_path=document.relative_path,
        activity_at=(
            document.activity_at
            or document.source_modified_at
            or document.synced_at
        ),
        message_count=message_count,
        project_title=document.project_title,
        machine_name=document.machine_name,
        is_subagent=is_conversation_subagent(
            document.tool_id,
            document.relative_path,
            document.metadata,
        ),
    )


async def _write_document(
    db: AsyncSession,
    writer: TextIO,
    document: _ExportDocument,
    options: MarkdownExportOptions,
    message_count: int | None = None,
) -> None:
    if message_count is None:
        message_count = await _message_count(db, document.id)
    plan = await _selection_plan(db, document.id, message_count, options)
    await write_conversation_markdown(
        writer,
        _markdown_info(document, message_count),
        _message_stream(db, document, message_count, plan),
        options,
        await _interaction_responses(db, document.id, plan),
    )


async def _load_document(
    db: AsyncSession,
    document_id: uuid.UUID,
    machine_ids: list[uuid.UUID] | None,
) -> _ExportDocument:
    query = (
        select(
            Document.id,
            Document.tool_id,
            Document.title,
            Document.relative_path,
            Document.metadata_,
            Document.activity_at,
            Document.source_modified_at,
            Document.synced_at,
            Document.project_id,
            Project.title.label("project_title"),
            Machine.name.label("machine_name"),
        )
        .outerjoin(Project, Project.id == Document.project_id)
        .outerjoin(Machine, Machine.id == Document.machine_id)
        .where(
            Document.id == document_id,
            Document.category == "conversation",
        )
    )
    if machine_ids is not None:
        query = query.where(Document.machine_id.in_(machine_ids))
    row = (await db.execute(query)).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _row_document(row)


def _row_document(row: Mapping[str, Any]) -> _ExportDocument:
    return _ExportDocument(
        id=row["id"],
        tool_id=row["tool_id"],
        title=row["title"] or row["relative_path"] or "Untitled conversation",
        relative_path=row["relative_path"],
        metadata=row["metadata_"] or {},
        activity_at=row["activity_at"],
        source_modified_at=row["source_modified_at"],
        synced_at=row["synced_at"],
        project_title=row["project_title"],
        machine_name=row["machine_name"],
        project_id=row["project_id"],
    )


async def _all_documents(
    db: AsyncSession,
    machine_ids: list[uuid.UUID] | None,
    tool_ids: list[str],
    project_ids: list[uuid.UUID],
) -> list[_ExportDocument]:
    query = (
        select(
            Document.id,
            Document.tool_id,
            Document.title,
            Document.relative_path,
            Document.metadata_,
            Document.activity_at,
            Document.source_modified_at,
            Document.synced_at,
            Document.project_id,
            Project.title.label("project_title"),
            Machine.name.label("machine_name"),
        )
        .outerjoin(Project, Project.id == Document.project_id)
        .outerjoin(Machine, Machine.id == Document.machine_id)
        .where(Document.category == "conversation")
    )
    if machine_ids is not None:
        query = query.where(Document.machine_id.in_(machine_ids))
    if tool_ids:
        query = query.where(Document.tool_id.in_(tool_ids))
    if project_ids:
        query = query.where(Document.project_id.in_(project_ids))
    return [
        _row_document(row)
        for row in (await db.execute(query)).mappings().all()
    ]


def _references(documents: list[_ExportDocument]) -> list[ConversationRef]:
    return [
        ConversationRef(
            document_id=document.id,
            tool_id=document.tool_id,
            relative_path=document.relative_path,
            metadata=document.metadata,
            title=document.title,
            source_modified_at=document.source_modified_at,
            activity_at=document.activity_at,
            synced_at=document.synced_at,
        )
        for document in documents
    ]


def _representatives(
    documents: list[_ExportDocument],
    include_subagents: bool,
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, datetime | None]]:
    refs = _references(documents)
    hierarchy = fold_conversation_subagents(refs)
    logical_activity = build_logical_activity_map(hierarchy, refs)
    if not include_subagents:
        return dict(hierarchy.canonical_document_ids), logical_activity

    refs_by_id = {ref.document_id: ref for ref in refs}
    child_representatives: dict[tuple[str, str, str], uuid.UUID] = {}
    for child_ids in hierarchy.subagent_document_ids.values():
        for child_id in child_ids:
            child = refs_by_id.get(child_id)
            if child is None:
                continue
            key = (
                child.tool_id or "",
                conversation_root_thread_id(
                    child.tool_id,
                    child.relative_path,
                    child.metadata,
                ) or "",
                current_thread_id(child.metadata) or str(child.document_id),
            )
            child_representatives[key] = child_id

    representatives: dict[uuid.UUID, uuid.UUID] = {}
    for ref in refs:
        if is_conversation_subagent(ref.tool_id, ref.relative_path, ref.metadata):
            key = (
                ref.tool_id or "",
                conversation_root_thread_id(
                    ref.tool_id,
                    ref.relative_path,
                    ref.metadata,
                ) or "",
                current_thread_id(ref.metadata) or str(ref.document_id),
            )
            representatives[ref.document_id] = child_representatives.get(
                key,
                ref.document_id,
            )
        else:
            representatives[ref.document_id] = hierarchy.canonical_document_ids.get(
                ref.document_id,
                ref.document_id,
            )
    return representatives, logical_activity


def _prompt_number_predicate(
    prompt_number,
    selection: PromptSelection | None,
):
    if selection is None:
        return None
    return or_(*[
        prompt_number >= start if end is None else prompt_number.between(start, end)
        for start, end in selection.intervals
    ])


async def _prompt_matching_ids(
    db: AsyncSession,
    selection: PromptSelection | None,
    start_at: datetime | None,
    end_at: datetime | None,
    allowed_document_ids: set[uuid.UUID],
) -> set[uuid.UUID] | None:
    if selection is None and start_at is None and end_at is None:
        return None
    response_missing = ConversationMessage.metadata_["interaction_response"].astext.is_(None)
    prompt_rows = (
        select(
            ConversationMessage.document_id.label("document_id"),
            ConversationMessage.timestamp.label("timestamp"),
            func.row_number().over(
                partition_by=ConversationMessage.document_id,
                order_by=(
                    ConversationMessage.line_number,
                    ConversationMessage.id,
                ),
            ).label("prompt_number"),
        )
        .where(
            ConversationMessage.role == "user",
            ConversationMessage.document_id.in_(allowed_document_ids),
            func.length(func.trim(ConversationMessage.content)) > 0,
            ~ConversationMessage.content.startswith("[Subagent Context]"),
            response_missing,
        )
        .subquery()
    )
    conditions = []
    number_predicate = _prompt_number_predicate(
        prompt_rows.c.prompt_number,
        selection,
    )
    if number_predicate is not None:
        conditions.append(number_predicate)
    if start_at is not None:
        conditions.append(prompt_rows.c.timestamp >= start_at)
    if end_at is not None:
        conditions.append(prompt_rows.c.timestamp <= end_at)
    return set((
        await db.execute(
            select(prompt_rows.c.document_id).where(*conditions).distinct()
        )
    ).scalars().all())


async def _query_matching_ids(
    db: AsyncSession,
    query: str,
    allowed_document_ids: set[uuid.UUID],
) -> set[uuid.UUID] | None:
    normalized = normalize_search_query(query)
    if not normalized:
        return None
    expressions = build_message_search_expressions(
        normalized,
        allow_short_substring=True,
    )
    return set((
        await db.execute(
            select(ConversationMessage.document_id)
            .where(
                ConversationMessage.document_id.in_(allowed_document_ids),
                expressions.predicate,
            )
            .distinct()
        )
    ).scalars().all())


async def _message_summaries(
    db: AsyncSession,
    document_ids: set[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int, int, int]]:
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(
                ConversationMessage.document_id,
                func.count().label("total"),
                func.count().filter(ConversationMessage.role == "user").label("users"),
                func.count().filter(ConversationMessage.role == "assistant").label("assistants"),
                func.coalesce(
                    func.sum(func.length(ConversationMessage.content)).filter(
                        ConversationMessage.role.in_(("user", "assistant"))
                    ),
                    0,
                ).label("characters"),
            )
            .where(ConversationMessage.document_id.in_(document_ids))
            .group_by(ConversationMessage.document_id)
        )
    ).all()
    return {
        document_id: (int(total), int(users), int(assistants), int(characters))
        for document_id, total, users, assistants, characters in rows
    }


async def _select_global_documents(
    db: AsyncSession,
    machine_ids: list[uuid.UUID] | None,
    request: ConversationExportRequest,
    options: MarkdownExportOptions,
) -> tuple[list[_ExportDocument], dict[uuid.UUID, int], int]:
    documents = await _all_documents(
        db,
        machine_ids,
        request.tool_ids,
        request.project_ids,
    )
    if not documents:
        return [], {}, 0
    documents_by_id = {document.id: document for document in documents}
    representatives, logical_activity = _representatives(
        documents,
        request.include_subagents,
    )
    matched_ids: set[uuid.UUID] = set(documents_by_id)
    prompt_ids = await _prompt_matching_ids(
        db,
        options.prompt_selection,
        options.start_at,
        options.end_at,
        set(documents_by_id),
    )
    if prompt_ids is not None:
        matched_ids.intersection_update(prompt_ids)
    query_ids = await _query_matching_ids(
        db,
        request.query,
        set(documents_by_id),
    )
    if query_ids is not None:
        matched_ids.intersection_update(query_ids)

    selected_representatives = {
        representatives[document_id]
        for document_id in matched_ids
        if document_id in representatives
    }
    member_ids = {
        document_id
        for document_id, representative in representatives.items()
        if representative in selected_representatives
    }
    summaries = await _message_summaries(db, member_ids)
    logical_summaries: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for document_id in member_ids:
        representative = representatives[document_id]
        values = summaries.get(document_id, (0, 0, 0, 0))
        for index, value in enumerate(values):
            logical_summaries[representative][index] += value

    if not request.include_low_activity:
        selected_representatives = {
            representative
            for representative in selected_representatives
            if not is_low_activity_summary(
                logical_summaries[representative][1],
                logical_summaries[representative][2],
                logical_summaries[representative][3],
            )
        }
    ordered = [
        documents_by_id[document_id]
        for document_id in selected_representatives
        if document_id in documents_by_id
    ]
    ordered.sort(
        key=lambda document: (
            logical_activity.get(document.id)
            or document.activity_at
            or document.source_modified_at
            or document.synced_at
            or datetime.min.replace(tzinfo=timezone.utc),
            str(document.id),
        ),
        reverse=True,
    )
    total = len(ordered)
    ordered = ordered[:request.max_threads]
    counts = {
        document.id: summaries.get(document.id, (0, 0, 0, 0))[0]
        for document in ordered
    }
    return ordered, counts, total


def _temp_path(suffix: str) -> str:
    handle, path = tempfile.mkstemp(prefix="memento-conversations-", suffix=suffix)
    os.close(handle)
    return path


def _global_intro(
    writer: TextIO,
    request: ConversationExportRequest,
    exported: int,
    available: int,
) -> None:
    writer.write("# Memento conversation export\n\n")
    writer.write(
        f"Exported {exported} of {available} matching threads on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
    )
    writer.write("## Filters\n\n")
    writer.write(f"- Date range: `{request.start_at or 'any'}` to `{request.end_at or 'any'}`\n")
    writer.write(f"- Prompt range: `{request.prompt_range or 'all'}`\n")
    writer.write(f"- Message query: `{request.query or 'none'}`\n")
    writer.write(f"- Tools: `{', '.join(request.tool_ids) if request.tool_ids else 'all'}`\n")
    writer.write(f"- Projects: `{', '.join(str(value) for value in request.project_ids) if request.project_ids else 'all'}`\n")
    writer.write(f"- Include subagents: `{request.include_subagents}`\n")
    writer.write(f"- Include low-activity threads: `{request.include_low_activity}`\n\n")


async def _build_global_export(
    db: AsyncSession,
    machine_ids: list[uuid.UUID] | None,
    request: ConversationExportRequest,
    options: MarkdownExportOptions,
) -> tuple[str, str, int, int]:
    documents, counts, available = await _select_global_documents(
        db,
        machine_ids,
        request,
        options,
    )
    budget = _ByteBudget(MAX_MARKDOWN_EXPORT_BYTES)
    if request.output == "combined":
        path = _temp_path(".md")
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as raw_writer:
                writer = _BudgetWriter(raw_writer, budget)
                _global_intro(writer, request, len(documents), available)
                for index, document in enumerate(documents):
                    if index:
                        writer.write("\n---\n\n")
                    await _write_document(
                        db,
                        writer,
                        document,
                        options,
                        counts.get(document.id, 0),
                    )
        except BaseException:
            _unlink(path)
            raise
        return path, "memento-conversations.md", len(documents), available

    path = _temp_path(".zip")
    try:
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            readme = io.StringIO()
            _global_intro(readme, request, len(documents), available)
            readme.write("## Threads\n\n")
            filenames = [
                safe_markdown_filename(document.title, str(document.id))
                for document in documents
            ]
            for document, filename in zip(documents, filenames, strict=True):
                readme.write(f"- [{document.title}]({quote(filename)})\n")
            readme_value = readme.getvalue()
            budget.add(readme_value)
            archive.writestr("README.md", readme_value)
            for document, filename in zip(documents, filenames, strict=True):
                with archive.open(filename, "w") as binary:
                    with io.TextIOWrapper(
                        binary,
                        encoding="utf-8",
                        newline="\n",
                        write_through=True,
                    ) as raw_writer:
                        await _write_document(
                            db,
                            _BudgetWriter(raw_writer, budget),
                            document,
                            options,
                            counts.get(document.id, 0),
                        )
    except BaseException:
        _unlink(path)
        raise
    return path, "memento-conversations.zip", len(documents), available


def _download_response(
    path: str,
    filename: str,
    media_type: str,
    lock: asyncio.Lock,
    headers: dict[str, str] | None = None,
) -> FileResponse:
    def cleanup() -> None:
        _unlink(path)
        try:
            lock.release()
        except RuntimeError:
            pass

    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        background=BackgroundTask(cleanup),
        headers={"X-Accel-Buffering": "no", **(headers or {})},
    )


@router.get("/{document_id}")
async def export_one_conversation(
    document_id: uuid.UUID,
    start_at: datetime | None = Query(None),
    end_at: datetime | None = Query(None),
    prompt_range: str = Query("", max_length=512),
    include_tools: bool = Query(True),
    include_thinking: bool = Query(True),
    include_session_context: bool = Query(True),
    include_timestamps: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    options = _options(
        prompt_range,
        start_at,
        end_at,
        include_tools,
        include_thinking,
        include_session_context,
        include_timestamps,
    )
    lock = _export_lock(str(user.id))
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Another conversation export is already running",
        )
    await lock.acquire()
    path = ""
    try:
        machine_ids = await user_machine_ids(db, user)
        document = await _load_document(db, document_id, machine_ids)
        path = _temp_path(".md")
        budget = _ByteBudget(MAX_MARKDOWN_EXPORT_BYTES)
        with open(path, "w", encoding="utf-8", newline="\n") as raw_writer:
            await _write_document(
                db,
                _BudgetWriter(raw_writer, budget),
                document,
                options,
            )
    except ValueError as exc:
        if path:
            _unlink(path)
        lock.release()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except BaseException:
        if path:
            _unlink(path)
        lock.release()
        raise
    return _download_response(
        path,
        safe_markdown_filename(document.title, str(document.id)),
        "text/markdown; charset=utf-8",
        lock,
    )


@router.post("")
async def export_conversations(
    request: ConversationExportRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    options = _options(
        request.prompt_range,
        request.start_at,
        request.end_at,
        request.include_tools,
        request.include_thinking,
        request.include_session_context,
        request.include_timestamps,
    )
    lock = _export_lock(str(user.id))
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Another conversation export is already running",
        )
    await lock.acquire()
    path = ""
    try:
        machine_ids = await user_machine_ids(db, user)
        path, filename, exported, available = await _build_global_export(
            db,
            machine_ids,
            request,
            options,
        )
    except HTTPException:
        lock.release()
        raise
    except ValueError as exc:
        if path:
            _unlink(path)
        lock.release()
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception:
        if path:
            _unlink(path)
        lock.release()
        logger.exception("conversation export failed for user %s", user.id)
        raise HTTPException(status_code=500, detail="conversation export failed")
    return _download_response(
        path,
        filename,
        "application/zip" if filename.endswith(".zip") else "text/markdown; charset=utf-8",
        lock,
        {
            "X-Memento-Exported-Threads": str(exported),
            "X-Memento-Matching-Threads": str(available),
            "X-Memento-Truncated": "true" if exported < available else "false",
        },
    )
