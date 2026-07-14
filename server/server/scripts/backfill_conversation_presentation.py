"""Idempotently normalize stored conversation presentation data.

The repair is deliberately batched: updating ``content_tsv`` touches large GIN
indexes and a single transaction over every transcript can exceed the normal
interactive statement timeout.  Applied runs commit each batch; dry runs use
the same work and roll each batch back while retaining accurate counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.orm import load_only

from server.db.models import ConversationMessage, Document, DocumentEmbedding
from server.db.session import async_session_factory, engine
from server.services.conversation_parser import (
    _extract_local_command,
    _iter_json_objects,
    extract_codex_session_metadata,
    has_cursor_session_context_prefix,
    is_codex_user_mirror_pair,
    is_claude_session_context_record,
    normalize_codex_user_payload,
    parse_conversation_line,
    split_cursor_user_payload,
    strip_terminal_sequences,
)
from server.services.ingest_service import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_STORED_MESSAGE_CHARS,
    _bounded_message_text,
    _conversation_title_needs_derivation,
    _friendly_codex_agent_title,
    _friendly_conversation_title,
)
from server.services.embedding_service import (
    CONVERSATION_EMBEDDING_MESSAGE_CHARS,
    CONVERSATION_EMBEDDING_MESSAGE_LIMIT,
    _chunk_text,
    conversation_embedding_content,
    embedding_input_hash,
)
from server.services.conversation_activity import refresh_document_activity_at
from server.services.large_content_store import (
    read_large_content,
    read_large_content_prefix,
)
from server.services.thread_metadata_service import _has_manual_title


LOCAL_COMMAND_PREFIXES = (
    "<command-name",
    "<command-message",
    "<command-args",
    "<local-command-caveat",
    "<local-command-stdout",
    "<local-command-stderr",
)
DEFAULT_BATCH_SIZE = 25
SESSION_META_PREFIX_CHARS = 1024 * 1024
CODEX_MIRROR_MESSAGE_TYPES = frozenset({"response_item", "user_message"})


@dataclass
class BackfillStats:
    converted_local_commands: int = 0
    removed_local_command_caveats: int = 0
    scanned_claude_context_records: int = 0
    reclassified_claude_context: int = 0
    existing_claude_context: int = 0
    unmatched_claude_context_records: int = 0
    normalized_codex_prompts: int = 0
    reclassified_codex_context: int = 0
    deduplicated_codex_prompts: int = 0
    renamed_conversations: int = 0
    backfilled_thread_metadata: int = 0
    refreshed_search_documents: int = 0
    invalidated_embedding_documents: int = 0
    cursor_candidate_documents: int = 0
    normalized_cursor_prompts: int = 0
    separated_cursor_context: int = 0
    removed_cursor_envelope_only_messages: int = 0
    backfilled_cursor_message_timestamps: int = 0
    preserved_cursor_message_timestamps: int = 0
    invalid_cursor_envelopes: int = 0
    rederived_cursor_titles: int = 0
    preserved_manual_cursor_titles: int = 0
    refreshed_activity_documents: int = 0
    updated_activity_documents: int = 0

    def add(self, other: "BackfillStats") -> None:
        for field in fields(self):
            setattr(self, field.name, getattr(self, field.name) + getattr(other, field.name))


def _normalize_codex_stored_message(message) -> tuple[bool, bool]:
    """Normalize one stored user row; return ``(changed, became_context)``."""
    role, content = normalize_codex_user_payload(message.content or "")
    changed = message.role != role or message.content != content
    became_context = role == "system"
    if changed:
        message.role = role
        message.content = content
    if became_context and message.message_type != "codex_context":
        message.message_type = "codex_context"
        changed = True
    return changed, became_context


def _has_leading_cursor_timestamp(value: str | None) -> bool:
    """Return whether text looks like a Cursor timestamp-envelope candidate."""
    return (value or "").lstrip().lower().startswith("<timestamp>")


def _has_leading_cursor_envelope(value: str | None) -> bool:
    """Return whether text begins with a known Cursor transport envelope."""
    return _has_leading_cursor_timestamp(value) or has_cursor_session_context_prefix(
        value
    )


def _normalize_cursor_stored_message(message) -> tuple[bool, bool, bool]:
    """Normalize one stored Cursor row.

    Returns ``(content_changed, timestamp_backfilled, timestamp_preserved)``.
    A valid envelope is the authority only when the historical row has no
    structured timestamp. Existing structured timestamps are never replaced.
    Invalid or user-authored ``<timestamp>`` text is an exact no-op.
    """
    content = message.content or ""
    normalized, envelope_timestamp, session_context = split_cursor_user_payload(
        content
    )
    if not envelope_timestamp and not session_context:
        return False, False, False

    content_changed = normalized != content
    if content_changed:
        message.content = normalized

    metadata_changed = False
    if session_context:
        metadata = dict(getattr(message, "metadata_", None) or {})
        if metadata.get("session_context") != session_context:
            metadata["session_context"] = session_context
            message.metadata_ = metadata
            metadata_changed = True

    if not envelope_timestamp:
        return content_changed or metadata_changed, False, False
    if message.timestamp is not None:
        return content_changed or metadata_changed, False, True

    try:
        message.timestamp = datetime.fromisoformat(envelope_timestamp)
    except (TypeError, ValueError):
        # The shared parser helper promises ISO output. Keep this guard so a
        # future parser regression cannot partially mutate production rows.
        if content_changed:
            message.content = content
        if metadata_changed:
            metadata = dict(getattr(message, "metadata_", None) or {})
            metadata.pop("session_context", None)
            message.metadata_ = metadata
        return False, False, False
    return content_changed or metadata_changed, True, False


def _cursor_title_from_messages(
    current_title: str | None,
    messages: list,
    metadata: dict | None = None,
) -> tuple[str | None, bool]:
    """Return ``(title, manual_title_preserved)`` for an affected Cursor row."""
    normalized_title, envelope_timestamp, session_context = (
        split_cursor_user_payload(current_title or "")
    )
    if (
        not envelope_timestamp
        and not session_context
        and not has_cursor_session_context_prefix(current_title)
    ):
        return current_title, False

    if _has_manual_title(metadata or {}):
        return current_title, True

    for message in sorted(messages, key=lambda item: item.line_number):
        if message.role != "user":
            continue
        title = _friendly_conversation_title(
            message.content or "",
            tool_id="cursor",
        )
        if title:
            return title, False

    fallback = _friendly_conversation_title(
        normalized_title,
        tool_id="cursor",
    )
    return fallback or current_title, False


def _is_codex_mirror_pair(first, second) -> bool:
    """Return whether two rows are the two known Codex copies of one prompt."""
    if first.role != "user" or second.role != "user":
        return False
    if {first.message_type, second.message_type} != CODEX_MIRROR_MESSAGE_TYPES:
        return False
    if first.timestamp is None or second.timestamp is None:
        return False
    if abs(first.line_number - second.line_number) > 1:
        return False
    return is_codex_user_mirror_pair(
        first.message_type,
        first.content,
        first.timestamp,
        second.message_type,
        second.content,
        second.timestamp,
    )


def _codex_title_from_messages(
    current_title: str | None,
    relative_path: str,
    messages: list,
    metadata: dict | None = None,
) -> str | None:
    """Return a repaired title while preserving legitimate/manual titles."""
    values = metadata or {}
    if (
        str(values.get("thread_source") or "").strip().lower() == "subagent"
    ):
        # Subagent transcripts inherit the root's opening prompts, so those
        # messages cannot identify the fork. The collector's task-oriented
        # agent path/nickname is the stable presentation contract instead.
        agent_title = _friendly_codex_agent_title(values)
        if agent_title:
            return agent_title
    if not _conversation_title_needs_derivation(current_title, "codex"):
        return current_title
    for message in sorted(messages, key=lambda item: item.line_number):
        if message.role != "user":
            continue
        title = _friendly_conversation_title(
            message.content or "",
            tool_id="codex",
        )
        if title:
            return title
    agent_title = _friendly_codex_agent_title(metadata)
    if agent_title:
        return agent_title
    return relative_path.rsplit("/", 1)[-1] or current_title


def _embedding_input_changed(
    *,
    has_inline_content: bool,
    previous_message_content: str,
    current_message_content: str,
) -> bool:
    """Return whether presentation repair changes this document's model input."""
    if has_inline_content:
        return False

    def model_input_hash(content: str) -> str:
        chunks = []
        if len(content) >= 100:
            chunks = _chunk_text(content, max_chunks=50)
        return embedding_input_hash(chunks)

    return model_input_hash(previous_message_content) != model_input_hash(
        current_message_content
    )


def _conversation_embedding_rows_query(document_ids: set):
    """Select only the bounded, deterministic fallback rows for each document."""
    ranked = select(
        ConversationMessage.document_id.label("document_id"),
        func.left(
            ConversationMessage.content,
            CONVERSATION_EMBEDDING_MESSAGE_CHARS,
        ).label("content"),
        func.row_number().over(
            partition_by=ConversationMessage.document_id,
            order_by=(
                ConversationMessage.line_number,
                ConversationMessage.id,
            ),
        ).label("row_number"),
    ).where(
        ConversationMessage.document_id.in_(document_ids),
        ConversationMessage.role.in_(("user", "assistant")),
    ).subquery()
    return (
        select(ranked.c.document_id, ranked.c.content)
        .where(ranked.c.row_number <= CONVERSATION_EMBEDDING_MESSAGE_LIMIT)
        .order_by(ranked.c.document_id, ranked.c.row_number)
    )


async def _conversation_embedding_content_by_document(
    db,
    document_ids: set,
) -> dict:
    """Load at most the runtime embedding limit for every requested document."""
    contents: dict = {document_id: [] for document_id in document_ids}
    if not document_ids:
        return {}
    rows = await db.execute(_conversation_embedding_rows_query(document_ids))
    for document_id, content in rows.all():
        contents[document_id].append(content)
    return {
        document_id: conversation_embedding_content(message_contents)
        for document_id, message_contents in contents.items()
    }


async def _invalidate_changed_embeddings(db, document_ids: set) -> None:
    """Discard vectors only when their actual model input changed."""
    if not document_ids:
        return
    await db.execute(
        delete(DocumentEmbedding).where(
            DocumentEmbedding.document_id.in_(document_ids)
        )
    )
    await db.execute(
        update(Document)
        .where(Document.id.in_(document_ids))
        .values(
            embedding_status="pending",
            embedding_attempts=0,
            embedding_claim_token=None,
            embedding_claimed_at=None,
        )
    )


async def _refresh_document_search(db, document: Document) -> None:
    rows = (
        (
            await db.execute(
                select(func.left(ConversationMessage.content, 2_048))
                .where(
                    ConversationMessage.document_id == document.id,
                    ConversationMessage.role.in_(("user", "assistant")),
                )
                .order_by(ConversationMessage.line_number.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    search_text = _bounded_message_text(
        "\n".join(row for row in reversed(rows) if row),
        MAX_SEARCH_TEXT_CHARS,
    )
    from server.services.tokenize import tokenize_for_index

    tsv_input = tokenize_for_index(f"{document.title or ''} {search_text}")
    await db.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(content_tsv=func.to_tsvector("simple", tsv_input))
    )


async def _repair_claude_commands(db) -> BackfillStats:
    stats = BackfillStats()
    claude_document_ids = select(Document.id).where(Document.tool_id == "claude_code")
    local_messages = await db.execute(
        select(ConversationMessage).where(
            ConversationMessage.document_id.in_(claude_document_ids),
            ConversationMessage.role == "user",
            or_(*(
                func.lower(func.ltrim(ConversationMessage.content)).like(f"{prefix}%")
                for prefix in LOCAL_COMMAND_PREFIXES
            )),
        )
    )
    for message in local_messages.scalars():
        command = _extract_local_command(message.content or "")
        if command is None:
            await db.execute(
                delete(ConversationMessage).where(ConversationMessage.id == message.id)
            )
            stats.removed_local_command_caveats += 1
            continue

        tool_name, tool_input, output = command
        message.role = "tool"
        message.message_type = "local_command"
        message.content = output or f"[{tool_name}]"
        message.metadata_ = {
            **(message.metadata_ or {}),
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        stats.converted_local_commands += 1
    return stats


async def _externalized_prefix(key: str) -> str:
    try:
        return await asyncio.to_thread(
            read_large_content_prefix,
            key,
            max_bytes=SESSION_META_PREFIX_CHARS,
        )
    except Exception as exc:
        raise RuntimeError(
            f"failed to range-read externalized transcript prefix: {key}"
        ) from exc


async def _externalized_content(key: str) -> str:
    try:
        return await asyncio.to_thread(read_large_content, key)
    except Exception as exc:
        raise RuntimeError(
            f"failed to read externalized transcript for context repair: {key}"
        ) from exc


def _timestamp_bucket(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.isoformat()
    return str(value).replace("Z", "+00:00")[:19]


def _claude_context_identities(raw_content: str) -> tuple[set[tuple[str, str]], int]:
    """Return stored-row identities for Claude's authoritative context flags."""
    identities: set[tuple[str, str]] = set()
    records = 0
    for raw_object in _iter_json_objects(raw_content):
        try:
            obj = json.loads(raw_object)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict) or not is_claude_session_context_record(obj):
            continue
        parsed = parse_conversation_line(raw_object, "claude_code")
        if (
            parsed is None
            or parsed.role != "system"
            or parsed.raw_type != "claude_context"
        ):
            # Local-command records can also be isMeta=true; the shared parser
            # intentionally keeps those as compact tool rows instead.
            continue
        clean_content = _bounded_message_text(
            strip_terminal_sequences(parsed.content).replace("\x00", ""),
            MAX_STORED_MESSAGE_CHARS,
        )
        if not clean_content.strip():
            continue
        identities.add((clean_content, _timestamp_bucket(parsed.timestamp)))
        records += 1
    return identities, records


async def _repair_claude_context_batch(
    db,
    document_ids: list,
) -> BackfillStats:
    """Reclassify Claude's raw-metadata context rows without guessing by text."""
    stats = BackfillStats()
    rows = await db.execute(
        select(
            Document,
            (func.coalesce(func.length(Document.content), 0) > 0).label(
                "has_inline_embedding_content"
            ),
        )
        .options(load_only(
            Document.id,
            Document.title,
            Document.content,
            Document.content_s3_key,
        ))
        .where(Document.id.in_(document_ids))
        .with_for_update(of=Document)
    )
    documents: dict = {}
    identities_by_document: dict = {}
    inline_embedding_documents: set = set()
    for document, has_inline_embedding_content in rows.all():
        documents[document.id] = document
        if has_inline_embedding_content:
            inline_embedding_documents.add(document.id)
        raw_content = document.content or ""
        if not raw_content and document.content_s3_key:
            raw_content = await _externalized_content(document.content_s3_key)
        identities, record_count = _claude_context_identities(raw_content)
        identities_by_document[document.id] = identities
        stats.scanned_claude_context_records += record_count

    fallback_embedding_documents = set(documents) - inline_embedding_documents
    embedding_content_before = await _conversation_embedding_content_by_document(
        db,
        fallback_embedding_documents,
    )

    message_rows = await db.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.document_id.in_(document_ids),
            ConversationMessage.role.in_(("user", "system")),
        )
        .order_by(
            ConversationMessage.document_id,
            ConversationMessage.line_number,
            ConversationMessage.id,
        )
        .with_for_update(of=ConversationMessage)
    )
    matched_by_document: dict = defaultdict(set)
    changed_documents: set = set()
    for message in message_rows.scalars():
        identity = (
            message.content or "",
            _timestamp_bucket(message.timestamp),
        )
        if identity not in identities_by_document.get(message.document_id, set()):
            continue
        matched_by_document[message.document_id].add(identity)
        if message.role == "system" and message.message_type == "claude_context":
            stats.existing_claude_context += 1
            continue
        message.role = "system"
        message.message_type = "claude_context"
        changed_documents.add(message.document_id)
        stats.reclassified_claude_context += 1

    stats.unmatched_claude_context_records = sum(
        len(identities - matched_by_document.get(document_id, set()))
        for document_id, identities in identities_by_document.items()
    )
    await db.flush()

    for document_id in sorted(changed_documents, key=str):
        await _refresh_document_search(db, documents[document_id])
        stats.refreshed_search_documents += 1

    embedding_content_after = await _conversation_embedding_content_by_document(
        db,
        fallback_embedding_documents,
    )
    embedding_changed_documents = {
        document_id
        for document_id in fallback_embedding_documents
        if _embedding_input_changed(
            has_inline_content=False,
            previous_message_content=embedding_content_before.get(document_id, ""),
            current_message_content=embedding_content_after.get(document_id, ""),
        )
    }
    await _invalidate_changed_embeddings(db, embedding_changed_documents)
    stats.invalidated_embedding_documents += len(embedding_changed_documents)
    return stats


async def _set_batch_timeouts(db) -> None:
    await db.execute(text("SET LOCAL statement_timeout = '25min'"))
    await db.execute(
        text("SET LOCAL idle_in_transaction_session_timeout = '25min'")
    )


async def _repair_codex_batch(db, document_ids: list) -> BackfillStats:
    stats = BackfillStats()
    rows = await db.execute(
        select(
            Document,
            func.left(Document.content, SESSION_META_PREFIX_CHARS).label("content_prefix"),
            (
                func.coalesce(func.length(Document.content), 0) > 0
            ).label("has_inline_embedding_content"),
        )
        .options(load_only(
            Document.id,
            Document.tool_id,
            Document.category,
            Document.relative_path,
            Document.title,
            Document.metadata_,
            Document.content_s3_key,
        ))
        .where(Document.id.in_(document_ids))
    )
    documents: dict = {}
    content_prefixes: dict = {}
    inline_embedding_documents: set = set()
    for document, content_prefix, has_inline_embedding_content in rows.all():
        documents[document.id] = document
        content_prefixes[document.id] = content_prefix or ""
        if has_inline_embedding_content:
            inline_embedding_documents.add(document.id)

    message_rows = await db.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.document_id.in_(document_ids),
            ConversationMessage.role == "user",
        )
        .order_by(
            ConversationMessage.document_id,
            ConversationMessage.line_number,
        )
    )
    messages_by_document: dict = defaultdict(list)
    for message in message_rows.scalars():
        messages_by_document[message.document_id].append(message)

    # Inline transcripts are embedded directly from Document.content, which
    # this presentation repair never changes. For externalized/empty
    # conversations, read only the same first 100 bounded rows used at runtime.
    fallback_embedding_documents = set(documents) - inline_embedding_documents
    embedding_content_before = await _conversation_embedding_content_by_document(
        db,
        fallback_embedding_documents,
    )

    changed_documents: set = set()
    for document_id, messages in messages_by_document.items():
        retained_messages = []
        previous_mirror_candidate = None
        for message in messages:
            if message.role == "user":
                changed, became_context = _normalize_codex_stored_message(message)
                if changed:
                    changed_documents.add(document_id)
                    if became_context:
                        stats.reclassified_codex_context += 1
                    else:
                        stats.normalized_codex_prompts += 1

            if (
                previous_mirror_candidate is not None
                and _is_codex_mirror_pair(previous_mirror_candidate, message)
            ):
                if message.message_type == "user_message":
                    await db.delete(previous_mirror_candidate)
                    if (
                        retained_messages
                        and retained_messages[-1] is previous_mirror_candidate
                    ):
                        retained_messages.pop()
                    retained_messages.append(message)
                else:
                    await db.delete(message)
                previous_mirror_candidate = None
                changed_documents.add(document_id)
                stats.deduplicated_codex_prompts += 1
                continue

            if (
                message.role == "user"
                and message.message_type in CODEX_MIRROR_MESSAGE_TYPES
                and message.timestamp is not None
            ):
                previous_mirror_candidate = message
            else:
                previous_mirror_candidate = None
            retained_messages.append(message)
        messages_by_document[document_id] = retained_messages

    await db.flush()

    for document_id, document in documents.items():
        messages = messages_by_document.get(document_id, [])
        prefix = content_prefixes.get(document_id, "")
        if not prefix and document.content_s3_key:
            prefix = await _externalized_prefix(document.content_s3_key)
        identity = extract_codex_session_metadata(prefix)
        existing_metadata = dict(document.metadata_ or {})
        if identity:
            merged_metadata = {**existing_metadata, **identity}
            if merged_metadata != existing_metadata:
                document.metadata_ = merged_metadata
                existing_metadata = merged_metadata
                stats.backfilled_thread_metadata += 1

        repaired_title = _codex_title_from_messages(
            document.title,
            document.relative_path,
            messages,
            existing_metadata,
        )
        if repaired_title and repaired_title != document.title:
            document.title = repaired_title
            changed_documents.add(document_id)
            stats.renamed_conversations += 1

    await db.flush()
    for document_id in changed_documents:
        document = documents.get(document_id)
        if document is None:
            continue
        await _refresh_document_search(db, document)
        stats.refreshed_search_documents += 1

    embedding_content_after = await _conversation_embedding_content_by_document(
        db,
        fallback_embedding_documents,
    )
    embedding_changed_documents = {
        document_id
        for document_id in documents
        if _embedding_input_changed(
            has_inline_content=document_id in inline_embedding_documents,
            previous_message_content=embedding_content_before.get(document_id, ""),
            current_message_content=embedding_content_after.get(document_id, ""),
        )
    }
    await _invalidate_changed_embeddings(db, embedding_changed_documents)
    stats.invalidated_embedding_documents += len(embedding_changed_documents)
    return stats


def _cursor_repair_document_ids_query():
    """Select only Cursor conversations with a leading envelope candidate."""
    message_candidate = exists(
        select(1)
        .select_from(ConversationMessage)
        .where(
            ConversationMessage.document_id == Document.id,
            ConversationMessage.role == "user",
            ConversationMessage.content.op("~*")(
                r"^\s*<(timestamp|external_links|plugin_info|uploaded_documents)"
                r"([[:space:]>])"
            ),
        )
        .correlate(Document)
    )
    return (
        select(Document.id)
        .where(
            Document.tool_id == "cursor",
            Document.category == "conversation",
            or_(
                Document.title.op("~*")(r"^\s*<timestamp>"),
                message_candidate,
            ),
        )
        .order_by(Document.id)
    )


async def _repair_cursor_batch(db, document_ids: list) -> BackfillStats:
    """Normalize one guarded batch of historical Cursor conversations.

    Apply mode requires ingestion and embedding workers to be quiesced. Row
    locks protect the document/message repair itself, but an embedding worker
    must not publish vectors between the before/after input snapshots.
    """
    stats = BackfillStats(cursor_candidate_documents=len(document_ids))
    rows = await db.execute(
        select(
            Document,
            (
                func.coalesce(func.length(Document.content), 0) > 0
            ).label("has_inline_embedding_content"),
        )
        .options(load_only(
            Document.id,
            Document.tool_id,
            Document.category,
            Document.title,
            Document.metadata_,
            Document.activity_at,
        ))
        .where(Document.id.in_(document_ids))
        .with_for_update(of=Document)
    )
    documents: dict = {}
    inline_embedding_documents: set = set()
    for document, has_inline_embedding_content in rows.all():
        documents[document.id] = document
        if has_inline_embedding_content:
            inline_embedding_documents.add(document.id)

    message_rows = await db.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.document_id.in_(document_ids),
            ConversationMessage.role == "user",
        )
        .order_by(
            ConversationMessage.document_id,
            ConversationMessage.line_number,
            ConversationMessage.id,
        )
        .with_for_update(of=ConversationMessage)
    )
    messages_by_document: dict = defaultdict(list)
    for message in message_rows.scalars():
        messages_by_document[message.document_id].append(message)

    fallback_embedding_documents = set(documents) - inline_embedding_documents
    embedding_content_before = await _conversation_embedding_content_by_document(
        db,
        fallback_embedding_documents,
    )

    search_changed_documents: set = set()
    activity_refresh_documents: set = set()
    for document_id, messages in messages_by_document.items():
        retained_messages = []
        for message in messages:
            if not _has_leading_cursor_envelope(message.content):
                retained_messages.append(message)
                continue

            had_context = bool(
                split_cursor_user_payload(message.content or "")[2]
            )
            content_changed, timestamp_backfilled, timestamp_preserved = (
                _normalize_cursor_stored_message(message)
            )
            if not (content_changed or timestamp_backfilled or timestamp_preserved):
                stats.invalid_cursor_envelopes += 1
                retained_messages.append(message)
                continue

            activity_refresh_documents.add(document_id)
            if content_changed:
                search_changed_documents.add(document_id)
                stats.normalized_cursor_prompts += 1
                if had_context:
                    stats.separated_cursor_context += 1
            if not (message.content or "").strip():
                metadata = dict(message.metadata_ or {})
                context = str(metadata.pop("session_context", "")).strip()
                if context:
                    message.role = "system"
                    message.message_type = "cursor_context"
                    message.content = context
                    message.metadata_ = metadata
                    retained_messages.append(message)
                    continue
                await db.delete(message)
                search_changed_documents.add(document_id)
                stats.removed_cursor_envelope_only_messages += 1
                continue
            if timestamp_backfilled:
                stats.backfilled_cursor_message_timestamps += 1
            elif timestamp_preserved:
                stats.preserved_cursor_message_timestamps += 1
            retained_messages.append(message)
        messages_by_document[document_id] = retained_messages

    await db.flush()

    for document_id, document in documents.items():
        if _has_leading_cursor_envelope(document.title):
            normalized_title, _envelope_timestamp, _session_context = (
                split_cursor_user_payload(document.title or "")
            )
            activity_refresh_documents.add(document_id)
            repaired_title, manual_preserved = _cursor_title_from_messages(
                document.title,
                messages_by_document.get(document_id, []),
                document.metadata_,
            )
            if manual_preserved:
                stats.preserved_manual_cursor_titles += 1
            elif repaired_title and repaired_title != document.title:
                document.title = repaired_title
                search_changed_documents.add(document_id)
                stats.renamed_conversations += 1
                stats.rederived_cursor_titles += 1
            elif normalized_title != document.title:
                # This should only be reachable for an empty normalized title.
                stats.invalid_cursor_envelopes += 1

    await db.flush()

    for document_id in sorted(activity_refresh_documents, key=str):
        document = documents.get(document_id)
        if document is None:
            continue
        previous_activity = document.activity_at
        current_activity = await refresh_document_activity_at(db, document)
        stats.refreshed_activity_documents += 1
        if current_activity != previous_activity:
            stats.updated_activity_documents += 1

    for document_id in sorted(search_changed_documents, key=str):
        document = documents.get(document_id)
        if document is None:
            continue
        await _refresh_document_search(db, document)
        stats.refreshed_search_documents += 1

    embedding_content_after = await _conversation_embedding_content_by_document(
        db,
        fallback_embedding_documents,
    )
    embedding_changed_documents = {
        document_id
        for document_id in fallback_embedding_documents
        if _embedding_input_changed(
            has_inline_content=False,
            previous_message_content=embedding_content_before.get(document_id, ""),
            current_message_content=embedding_content_after.get(document_id, ""),
        )
    }
    await _invalidate_changed_embeddings(db, embedding_changed_documents)
    stats.invalidated_embedding_documents += len(embedding_changed_documents)
    return stats


async def backfill(
    dry_run: bool,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cursor_only: bool = False,
) -> BackfillStats:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    stats = BackfillStats()
    async with async_session_factory() as db:
        if not cursor_only:
            await _set_batch_timeouts(db)
            stats.add(await _repair_claude_commands(db))
            if dry_run:
                await db.rollback()
            else:
                await db.commit()

            claude_document_ids = list((await db.execute(
                select(Document.id)
                .where(
                    Document.tool_id == "claude_code",
                    Document.category == "conversation",
                )
                .order_by(Document.id)
            )).scalars())
            for offset in range(0, len(claude_document_ids), batch_size):
                await _set_batch_timeouts(db)
                batch = claude_document_ids[offset:offset + batch_size]
                stats.add(await _repair_claude_context_batch(db, batch))
                if dry_run:
                    await db.rollback()
                else:
                    await db.commit()

            document_ids = list((await db.execute(
                select(Document.id)
                .where(
                    Document.tool_id == "codex",
                    Document.category == "conversation",
                )
                .order_by(Document.id)
            )).scalars())

            for offset in range(0, len(document_ids), batch_size):
                await _set_batch_timeouts(db)
                batch = document_ids[offset:offset + batch_size]
                stats.add(await _repair_codex_batch(db, batch))
                if dry_run:
                    await db.rollback()
                else:
                    await db.commit()

        cursor_document_ids = list((await db.execute(
            _cursor_repair_document_ids_query()
        )).scalars())
        for offset in range(0, len(cursor_document_ids), batch_size):
            await _set_batch_timeouts(db)
            batch = cursor_document_ids[offset:offset + batch_size]
            stats.add(await _repair_cursor_batch(db, batch))
            if dry_run:
                await db.rollback()
            else:
                await db.commit()

    await engine.dispose()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate changes and roll each repair batch back",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "commit each guarded repair batch; ingestion and embedding "
            "workers must be quiesced"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="documents per transaction (default: 25)",
    )
    parser.add_argument(
        "--cursor-only",
        action="store_true",
        help="skip the older Claude/Codex repairs and process Cursor only",
    )
    args = parser.parse_args()

    stats = asyncio.run(backfill(
        args.dry_run,
        batch_size=args.batch_size,
        cursor_only=args.cursor_only,
    ))
    mode = "dry-run" if args.dry_run else "applied"
    values = " ".join(
        f"{field.name}={getattr(stats, field.name)}"
        for field in fields(stats)
    )
    print(f"{mode}: {values}")


if __name__ == "__main__":
    main()
