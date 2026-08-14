"""Apply trusted, lightweight source metadata without re-ingesting content."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, Machine
from .cache import (
    daily_cache_namespace,
    project_conversations_cache_namespace,
    stage_cache_invalidation,
)
from .conversation_parser import (
    coerce_claude_live_interaction,
    interaction_question_fingerprint,
    is_claude_ask_user_permission_wrapper,
    normalize_interaction,
    strip_terminal_sequences,
)
from .conversation_activity import (
    ACTIVITY_FUTURE_CLOCK_SKEW,
    parse_conversation_activity_timestamp,
)
from .claude_lineage import (
    INTERACTION_ORIGIN_KEY,
    normalize_interaction_origin,
    origin_matches_permission_interaction,
)
from .document_delivery import (
    attach_document_delivery,
    delivery_metadata_expression,
    document_metadata,
    ensure_document_delivery_state,
    store_document_metadata,
)
from .ingest_service import (
    CURRENT_PENDING_QUESTIONS_KEY,
    INTERACTION_HISTORY_KEY,
    LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY,
    LIVE_INTERACTION_SIGNALS_KEY,
    LIVE_SHELL_ACTIVITIES_KEY,
    MAX_SEARCH_TEXT_CHARS,
    PENDING_QUESTION_COUNT_KEY,
    _bounded_message_text,
    _conversation_title_needs_derivation,
    _normalized_interaction_timestamp,
    _publish_file_synced_event,
    interaction_at_or_before_human,
)
from .tokenize import tokenize_for_index

_MANUAL_TITLE_SOURCES = {
    "manual",
    "user",
    "memento_manual",
    "memento_user",
}
_TITLE_REVISION_MAP_LIMIT = 32
_INTERACTION_HISTORY_LIMIT = 32
_LIVE_SHELL_ACTIVITY_LIMIT = 16
_TERMINAL_ACTIVITY_MESSAGE_TYPES = {
    "question_tool_output",
    "tool_output",
    "tool_result",
}
_TERMINAL_ACTIVITY_TOOL_STATUSES = {
    "aborted",
    "cancelled",
    "canceled",
    "complete",
    "completed",
    "done",
    "error",
    "failed",
    "interrupted",
    "success",
    "succeeded",
}
# Kept only so older tests/extensions that monkeypatch the removed SCAN hook
# fail closed instead of importing a missing symbol. Production code never
# calls this sentinel.
cache_delete_prefix = None


async def _interaction_anchor_line(
    db: AsyncSession,
    document_id: uuid.UUID,
    timestamp: object,
) -> int:
    """Return the last transcript line that existed when an interaction opened."""
    interaction_at = _normalized_interaction_timestamp(timestamp)
    statement = select(func.max(ConversationMessage.line_number)).where(
        ConversationMessage.document_id == document_id,
    )
    if interaction_at is not None:
        statement = statement.where(
            ConversationMessage.timestamp <= interaction_at,
        )
    line_number = (await db.execute(statement)).scalar_one_or_none()
    try:
        return max(0, int(line_number or 0))
    except (TypeError, ValueError):
        return 0


async def _canonical_activity_is_terminal(
    db: AsyncSession,
    document_id: uuid.UUID,
    activity_id: str,
) -> bool:
    """Return whether normalized history has already retired this activity."""
    terminal_id = (
        await db.execute(
            select(ConversationMessage.id)
            .where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.metadata_["tool_call_id"].astext == activity_id,
                or_(
                    ConversationMessage.message_type.in_(
                        _TERMINAL_ACTIVITY_MESSAGE_TYPES
                    ),
                    ConversationMessage.metadata_["tool_status"].astext.in_(
                        _TERMINAL_ACTIVITY_TOOL_STATUSES
                    ),
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return terminal_id is not None


@dataclass(frozen=True)
class ThreadTitleUpdateResult:
    matched: int
    updated: int
    ignored: int
    valid: bool = True


def sanitize_explicit_codex_title(title: str) -> str | None:
    """Return a safe one-line title, rejecting wrapper/instruction payloads."""
    candidate = strip_terminal_sequences(title).replace("\x00", "")
    candidate = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate or len(candidate) > 500:
        return None
    if _conversation_title_needs_derivation(candidate, "codex"):
        return None
    return candidate


def codex_thread_documents_select(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
):
    """Lock every owner-visible copy in deterministic document-id order."""
    thread_value = str(thread_id)
    effective_metadata = delivery_metadata_expression()
    return (
        select(Document)
        .where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            ),
            Document.tool_id == "codex",
            Document.category == "conversation",
            effective_metadata["thread_id"].astext == thread_value,
        )
        .order_by(Document.id.asc())
        .with_for_update(of=Document)
    )


def _codex_source_thread_select(
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
):
    effective_metadata = delivery_metadata_expression()
    return (
        select(Document.id)
        .where(
            Document.machine_id == machine_id,
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            ),
            Document.tool_id == "codex",
            Document.category == "conversation",
            effective_metadata["thread_id"].astext == str(thread_id),
        )
        .limit(1)
    )


def _codex_document_path_select(
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    relative_path: str,
):
    return (
        select(Document)
        .where(
            Document.machine_id == machine_id,
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            ),
            Document.tool_id == "codex",
            Document.category == "conversation",
            Document.relative_path == relative_path,
        )
        .order_by(Document.id.asc())
        .limit(2)
        .with_for_update(of=Document)
    )


def _has_manual_title(metadata: dict) -> bool:
    if metadata.get("title_is_manual") is True:
        return True
    sources = {
        str(metadata.get("title_source") or "").strip().lower(),
        str(metadata.get("memento_title_source") or "").strip().lower(),
    }
    return bool(sources & _MANUAL_TITLE_SOURCES)


def _has_explicit_codex_title(metadata: dict) -> bool:
    return (
        str(metadata.get("memento_title_source") or "").strip().lower()
        == "codex_explicit_rename"
    )


def _interaction_history(metadata: dict) -> dict[str, dict]:
    raw_history = metadata.get(INTERACTION_HISTORY_KEY)
    if isinstance(raw_history, dict):
        candidates = raw_history.items()
    elif isinstance(raw_history, list):
        candidates = (("", entry) for entry in raw_history)
    else:
        return {}

    history: dict[str, dict] = {}
    for key, entry in candidates:
        if not isinstance(entry, dict):
            continue
        interaction = entry.get("interaction")
        if not isinstance(interaction, dict):
            continue
        interaction_id = _bounded_message_text(
            str(interaction.get("id") or key or ""),
            512,
        )
        if interaction_id:
            history[interaction_id] = entry
    return dict(list(history.items())[-_INTERACTION_HISTORY_LIMIT:])


def _upsert_interaction_history(
    metadata: dict,
    interaction_id: str,
    entry: dict,
) -> None:
    """Store one recent interaction state, replacing any older state by id."""
    history = _interaction_history(metadata)
    history.pop(interaction_id, None)
    history[interaction_id] = entry
    metadata[INTERACTION_HISTORY_KEY] = list(history.values())[
        -_INTERACTION_HISTORY_LIMIT:
    ]


def _oldest_interaction_timestamp(*values: object) -> str:
    candidates = [
        (parsed, str(value))
        for value in values
        if (parsed := _normalized_interaction_timestamp(value)) is not None
    ]
    if not candidates:
        return ""
    return min(candidates, key=lambda item: item[0])[1]


def _title_revision_map(metadata: dict) -> dict[str, int]:
    raw = metadata.get("codex_title_revisions")
    if not isinstance(raw, dict):
        return {}
    revisions: dict[str, int] = {}
    for key, value in raw.items():
        try:
            machine_key = str(uuid.UUID(str(key)))
            revision = int(value)
        except (TypeError, ValueError, AttributeError):
            continue
        if revision > 0:
            revisions[machine_key] = revision
    return revisions


def _bounded_revision_map(
    revisions: dict[str, int],
    *,
    source_machine: str,
    revision: int,
) -> dict[str, int]:
    """Keep per-source clocks bounded without comparing clocks across hosts."""
    revisions = dict(revisions)
    revisions.pop(source_machine, None)
    retained_keys = sorted(revisions)[-_TITLE_REVISION_MAP_LIMIT + 1 :]
    bounded = {key: revisions[key] for key in retained_keys}
    bounded[source_machine] = revision
    return bounded


async def _refresh_title_search_index(
    db: AsyncSession,
    document: Document,
) -> None:
    """Replace title lexemes using bounded normalized conversation rows."""
    latest_rows = (
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
    searchable_content = _bounded_message_text(
        "\n".join(row for row in reversed(latest_rows) if row),
        MAX_SEARCH_TEXT_CHARS,
    )
    tsv_input = tokenize_for_index(f"{document.title or ''} {searchable_content}")
    await db.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(content_tsv=func.to_tsvector("simple", tsv_input))
    )


async def apply_codex_thread_title_update(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    thread_id: uuid.UUID,
    title: str,
    revision: int,
    user_id: uuid.UUID,
    title_kind: str = "unknown",
    relative_path: str | None = None,
) -> ThreadTitleUpdateResult:
    """Apply a classified Codex source-title update without content ingest."""
    title_kind = str(title_kind or "unknown").strip().lower()
    if title_kind not in {"custom", "fallback", "unknown"}:
        title_kind = "unknown"
    clean_title = sanitize_explicit_codex_title(title)
    if clean_title is None:
        return ThreadTitleUpdateResult(0, 0, 1, valid=False)

    source_match = (
        await db.execute(_codex_source_thread_select(machine_id, user_id, thread_id))
    ).scalar_one_or_none()
    if source_match is not None:
        result = await db.execute(codex_thread_documents_select(user_id, thread_id))
        documents = list(result.scalars().all())
        # Re-check after acquiring all locks: the unlocked routing probe above
        # must never authorize propagation if its source row disappeared.
        if not any(document.machine_id == machine_id for document in documents):
            return ThreadTitleUpdateResult(0, 0, 0)
    elif relative_path:
        fallback_result = await db.execute(
            _codex_document_path_select(machine_id, user_id, relative_path)
        )
        path_documents = list(fallback_result.scalars().all())
        documents = path_documents if len(path_documents) == 1 else []
    else:
        documents = []
    if not documents:
        return ThreadTitleUpdateResult(0, 0, 0)

    source_machine = str(machine_id)
    updated = 0
    ignored = 0
    title_changed_documents: list[Document] = []
    for document in documents:
        metadata = document_metadata(document)
        revisions = _title_revision_map(metadata)
        current_revision = revisions.get(source_machine, 0)
        is_source_document = document.machine_id == machine_id
        if not current_revision and is_source_document:
            try:
                current_revision = int(metadata.get("codex_title_revision") or 0)
            except (TypeError, ValueError):
                current_revision = 0
        if revision < current_revision and document.title == clean_title:
            # A restored state_5 database can legitimately reset its timestamp
            # clock. The durable queue serializes/coalesces each source thread,
            # so a different authenticated title is the latest observation and
            # must converge even when its numeric revision decreased. A lower
            # revision carrying the already-applied title is merely idempotent.
            ignored += 1
            continue

        next_revisions = _bounded_revision_map(
            revisions,
            source_machine=source_machine,
            revision=revision,
        )
        preserve_title = _has_manual_title(metadata) or (
            title_kind != "custom" and _has_explicit_codex_title(metadata)
        )
        if preserve_title:
            if next_revisions != revisions:
                metadata["codex_title_revisions"] = next_revisions
                if is_source_document:
                    metadata["codex_title_revision"] = revision
                delivery_state = await ensure_document_delivery_state(db, document)
                attach_document_delivery(
                    document,
                    delivery_state,
                    runtime_only=True,
                )
                store_document_metadata(document, metadata)
            ignored += 1
            continue

        metadata_changed = next_revisions != revisions
        if is_source_document and metadata.get("codex_title_revision") != revision:
            metadata_changed = True
        title_changed = document.title != clean_title
        if not title_changed and not metadata_changed:
            continue

        document.title = clean_title
        metadata["codex_title_revisions"] = next_revisions
        if is_source_document:
            # Backward compatibility for rows written before per-source clocks.
            metadata["codex_title_revision"] = revision
        metadata["memento_title_source"] = {
            "custom": "codex_explicit_rename",
            "fallback": "codex_source_fallback",
        }.get(title_kind, "codex_source_unknown")
        delivery_state = await ensure_document_delivery_state(db, document)
        attach_document_delivery(document, delivery_state, runtime_only=True)
        store_document_metadata(document, metadata)
        updated += 1
        if title_changed:
            title_changed_documents.append(document)

    for document in title_changed_documents:
        await _refresh_title_search_index(db, document)
        if isinstance(document, Document):
            from .dashboard_projection import (
                refresh_dashboard_document_projection,
            )

            await refresh_dashboard_document_projection(db, document)
        _publish_file_synced_event(
            db,
            document,
            str(user_id),
            changes={
                "conversation.metadata",
                "conversation.search",
                "dashboard",
                "project",
            },
        )

    if title_changed_documents:
        stage_cache_invalidation(db, daily_cache_namespace(user_id))
        project_ids = {
            document.project_id
            for document in title_changed_documents
            if document.project_id
        }
        for project_id in project_ids:
            stage_cache_invalidation(
                db,
                project_conversations_cache_namespace(user_id, project_id),
            )

    return ThreadTitleUpdateResult(
        matched=len(documents),
        updated=updated,
        ignored=ignored,
    )


async def apply_conversation_interaction_update(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_id: str,
    relative_path: str,
    interaction_id: str,
    interaction_status: str,
    question_tool: str,
    interaction_input: object,
    interaction_origin: object = None,
    timestamp: str = "",
) -> ThreadTitleUpdateResult:
    """Apply a live question preview without waiting for its transcript delta."""
    interaction_id = _bounded_message_text(str(interaction_id or ""), 512)
    relative_path = str(relative_path or "").replace("\\", "/").lstrip("/")
    status = str(interaction_status or "").strip().lower()
    if (
        tool_id not in {"claude_code", "codex", "cursor"}
        or not relative_path
        or not interaction_id
        or status not in {"pending", "answered", "cancelled"}
    ):
        return ThreadTitleUpdateResult(0, 0, 1, valid=False)

    document = (
        await db.execute(
            select(Document)
            .where(
                Document.machine_id == machine_id,
                Document.machine_id.in_(
                    select(Machine.id).where(Machine.user_id == user_id)
                ),
                Document.tool_id == tool_id,
                Document.category == "conversation",
                Document.relative_path == relative_path,
            )
            .limit(1)
            .with_for_update(of=Document)
        )
    ).scalar_one_or_none()
    if document is None:
        return ThreadTitleUpdateResult(0, 0, 0)

    original_metadata = document_metadata(document)
    metadata = dict(original_metadata)
    raw_signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
    signals = (
        {
            str(key): value
            for key, value in raw_signals.items()
            if isinstance(value, dict)
        }
        if isinstance(raw_signals, dict)
        else {}
    )
    raw_pending_ids = metadata.get(CURRENT_PENDING_QUESTIONS_KEY)
    pending_ids = (
        {
            _bounded_message_text(str(value), 512)
            for value in raw_pending_ids[:64]
            if value
        }
        if isinstance(raw_pending_ids, list)
        else set()
    )

    previous_signal = signals.get(interaction_id)
    previous_history = _interaction_history(metadata).get(interaction_id)
    previous_history_origin = (
        normalize_interaction_origin(
            previous_history.get(INTERACTION_ORIGIN_KEY)
        )
        if isinstance(previous_history, dict)
        else None
    )
    origin = (
        normalize_interaction_origin(interaction_origin)
        if tool_id == "claude_code" and status == "pending"
        else None
    )
    stale_pending = status == "pending" and interaction_at_or_before_human(
        timestamp,
        metadata.get(LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY),
    )
    if status == "pending" and not stale_pending:
        interaction = normalize_interaction(
            question_tool,
            interaction_input,
            source=tool_id,
            interaction_id=interaction_id,
        )
        if interaction is None:
            return ThreadTitleUpdateResult(1, 0, 1, valid=False)
        # Recover already-normalized AskUserQuestion permission wrappers when a
        # collector re-emits them before the side-file rewrite lands.
        if is_claude_ask_user_permission_wrapper(interaction):
            recovered = coerce_claude_live_interaction(interaction)
            if recovered is None:
                return ThreadTitleUpdateResult(1, 0, 1, valid=False)
            interaction = recovered
        if origin is not None and not origin_matches_permission_interaction(
            origin,
            interaction,
        ):
            # Provenance is an authority to hide only when it is bound to this
            # exact normalized PermissionRequest. A mismatched collector value
            # remains an ordinary fail-open live interaction.
            origin = None
        fingerprint = interaction_question_fingerprint(interaction)
        normalized_question_tool = re.sub(
            r"[^a-z0-9]",
            "",
            str(question_tool or "").casefold(),
        )
        normalized_interaction_tool = re.sub(
            r"[^a-z0-9]",
            "",
            str(interaction.get("tool_name") or "").casefold(),
        )
        duplicate_question_ids: list[str] = []
        if (
            tool_id == "claude_code"
            and fingerprint
            and normalized_interaction_tool == "askuserquestion"
        ):
            for sibling_id, sibling in list(signals.items()):
                if sibling_id == interaction_id or not isinstance(sibling, dict):
                    continue
                sibling_interaction = sibling.get("interaction")
                recovered_sibling = coerce_claude_live_interaction(
                    sibling_interaction,
                )
                sibling_fp = interaction_question_fingerprint(
                    recovered_sibling or sibling_interaction,
                )
                if sibling_fp == fingerprint:
                    duplicate_question_ids.append(sibling_id)

        # Claude can emit a real AskUserQuestion followed by a synthetic
        # PermissionRequest wrapper for the exact same prompt. Keep the
        # canonical tool-use id when it already exists; if the wrapper arrived
        # first, replace it when the canonical event follows.
        skip_duplicate_wrapper = (
            normalized_question_tool != "askuserquestion"
            and bool(duplicate_question_ids)
        )
        if not skip_duplicate_wrapper:
            if normalized_question_tool == "askuserquestion":
                for sibling_id in duplicate_question_ids:
                    signals.pop(sibling_id, None)
                    pending_ids.discard(sibling_id)
            signal = {
                "interaction": interaction,
                "timestamp": _bounded_message_text(str(timestamp or ""), 128),
                "tool_name": _bounded_message_text(
                    str(interaction.get("tool_name") or question_tool or ""),
                    256,
                ),
            }
            if origin is not None:
                signal[INTERACTION_ORIGIN_KEY] = origin
            else:
                previous_origin = (
                    normalize_interaction_origin(
                        previous_signal.get(INTERACTION_ORIGIN_KEY)
                    )
                    if isinstance(previous_signal, dict)
                    else None
                )
                retained_origin = previous_origin or previous_history_origin
                if retained_origin is not None:
                    signal[INTERACTION_ORIGIN_KEY] = retained_origin
            signals[interaction_id] = signal
            pending_ids.add(interaction_id)
            if interaction.get("interaction_type") == "permission_request":
                anchor_line_number = await _interaction_anchor_line(
                    db,
                    document.id,
                    timestamp,
                )
                history_entry = {
                    "interaction": interaction,
                    "timestamp": _bounded_message_text(
                        str(timestamp or ""),
                        128,
                    ),
                    "status": "pending",
                    "response": None,
                    "anchor_line_number": anchor_line_number,
                }
                retained_origin = origin or previous_history_origin
                if retained_origin is not None:
                    history_entry[INTERACTION_ORIGIN_KEY] = retained_origin
                _upsert_interaction_history(
                    metadata,
                    interaction_id,
                    history_entry,
                )

            # Also prune legacy malformed wrappers that cannot be recovered
            # well enough to fingerprint.
            if fingerprint and not is_claude_ask_user_permission_wrapper(interaction):
                for sibling_id, sibling in list(signals.items()):
                    if sibling_id == interaction_id or not isinstance(sibling, dict):
                        continue
                    sibling_interaction = sibling.get("interaction")
                    if not is_claude_ask_user_permission_wrapper(sibling_interaction):
                        continue
                    recovered_sibling = coerce_claude_live_interaction(
                        sibling_interaction,
                    )
                    sibling_fp = interaction_question_fingerprint(
                        recovered_sibling or sibling_interaction,
                    )
                    if sibling_fp == fingerprint or recovered_sibling is None:
                        signals.pop(sibling_id, None)
                        pending_ids.discard(sibling_id)
    else:
        signals.pop(interaction_id, None)
        pending_ids.discard(interaction_id)
        if status in {"answered", "cancelled"}:
            history_entry = _interaction_history(metadata).get(interaction_id)
            previous_interaction = (
                previous_signal.get("interaction")
                if isinstance(previous_signal, dict)
                else None
            )
            history_interaction = (
                history_entry.get("interaction")
                if isinstance(history_entry, dict)
                else None
            )
            interaction = next(
                (
                    candidate
                    for candidate in (previous_interaction, history_interaction)
                    if isinstance(candidate, dict)
                    and candidate.get("interaction_type") == "permission_request"
                    and not is_claude_ask_user_permission_wrapper(candidate)
                ),
                None,
            )
            if interaction is not None:
                history_timestamp = (
                    history_entry.get("timestamp")
                    if isinstance(history_entry, dict)
                    else None
                )
                signal_timestamp = (
                    previous_signal.get("timestamp")
                    if isinstance(previous_signal, dict)
                    else None
                )
                resolved_timestamp = _oldest_interaction_timestamp(
                    history_timestamp,
                    signal_timestamp,
                    timestamp,
                )
                anchor_line_number = (
                    history_entry.get("anchor_line_number", 0)
                    if isinstance(history_entry, dict)
                    else 0
                )
                try:
                    anchor_line_number = max(0, int(anchor_line_number or 0))
                except (TypeError, ValueError):
                    anchor_line_number = 0
                history_at = _normalized_interaction_timestamp(history_timestamp)
                resolved_at = _normalized_interaction_timestamp(resolved_timestamp)
                if anchor_line_number == 0 or (
                    history_at is not None
                    and resolved_at is not None
                    and resolved_at < history_at
                ):
                    anchor_line_number = await _interaction_anchor_line(
                        db,
                        document.id,
                        resolved_timestamp,
                    )
                resolved_entry = {
                    "interaction": interaction,
                    "timestamp": _bounded_message_text(
                        resolved_timestamp,
                        128,
                    ),
                    "status": status,
                    "response": {
                        "kind": "question_response",
                        "interaction_id": interaction_id,
                        "status": status,
                        "answers": [],
                        "raw_text": "",
                    },
                    "anchor_line_number": anchor_line_number,
                }
                retained_origin = origin or (
                    normalize_interaction_origin(
                        history_entry.get(INTERACTION_ORIGIN_KEY)
                    )
                    if isinstance(history_entry, dict)
                    else None
                )
                if retained_origin is not None:
                    resolved_entry[INTERACTION_ORIGIN_KEY] = retained_origin
                _upsert_interaction_history(
                    metadata,
                    interaction_id,
                    resolved_entry,
                )

    bounded_ids = sorted(pending_ids)[:64]
    if signals:
        metadata[LIVE_INTERACTION_SIGNALS_KEY] = dict(list(signals.items())[-64:])
    else:
        metadata.pop(LIVE_INTERACTION_SIGNALS_KEY, None)
    if bounded_ids:
        metadata[CURRENT_PENDING_QUESTIONS_KEY] = bounded_ids
        metadata[PENDING_QUESTION_COUNT_KEY] = len(bounded_ids)
    else:
        metadata.pop(CURRENT_PENDING_QUESTIONS_KEY, None)
        metadata.pop(PENDING_QUESTION_COUNT_KEY, None)

    if previous_signal == signals.get(interaction_id) and metadata == original_metadata:
        return ThreadTitleUpdateResult(1, 0, 0)
    delivery_state = await ensure_document_delivery_state(db, document)
    attach_document_delivery(document, delivery_state, runtime_only=True)
    store_document_metadata(document, metadata)
    if isinstance(document, Document):
        from .dashboard_projection import refresh_dashboard_document_projection

        await refresh_dashboard_document_projection(db, document)
    if document.project_id:
        stage_cache_invalidation(
            db,
            project_conversations_cache_namespace(user_id, document.project_id),
        )
    _publish_file_synced_event(
        db,
        document,
        str(user_id),
        changes={
            "conversation.metadata",
            "conversation.pending_interactions",
            "dashboard",
        },
    )
    return ThreadTitleUpdateResult(1, 1, 0)


async def apply_conversation_activity_update(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_id: str,
    relative_path: str,
    activity_id: str,
    activity_status: str,
    activity_tool: str,
    command: object,
    timestamp: str = "",
) -> ThreadTitleUpdateResult:
    """Apply a transient shell-command lifecycle update."""
    relative_path = str(relative_path or "").replace("\\", "/").lstrip("/")
    activity_id = _bounded_message_text(str(activity_id or ""), 512)
    status = str(activity_status or "").strip().lower()
    clean_tool = _bounded_message_text(
        strip_terminal_sequences(str(activity_tool or "")).strip(),
        256,
    )
    clean_command = _bounded_message_text(
        strip_terminal_sequences(str(command or "")).replace("\x00", "").strip(),
        8_000,
    )
    event_at = parse_conversation_activity_timestamp(timestamp)
    if (
        tool_id not in {"claude_code", "codex", "cursor"}
        or not relative_path
        or not activity_id
        or status not in {"running", "completed", "failed", "cancelled"}
        or (status == "running" and not clean_command)
        or event_at is None
        or event_at
        > datetime.now(timezone.utc) + ACTIVITY_FUTURE_CLOCK_SKEW
    ):
        return ThreadTitleUpdateResult(0, 0, 1, valid=False)
    event_timestamp = event_at.isoformat()

    document = (
        await db.execute(
            select(Document)
            .where(
                Document.machine_id == machine_id,
                Document.machine_id.in_(
                    select(Machine.id).where(Machine.user_id == user_id)
                ),
                Document.tool_id == tool_id,
                Document.category == "conversation",
                Document.relative_path == relative_path,
            )
            .limit(1)
            .with_for_update(of=Document)
        )
    ).scalar_one_or_none()
    if document is None:
        return ThreadTitleUpdateResult(0, 0, 0)

    original_metadata = document_metadata(document)
    metadata = dict(original_metadata)
    raw_activities = metadata.get(LIVE_SHELL_ACTIVITIES_KEY)
    activities = (
        {
            str(key): value
            for key, value in raw_activities.items()
            if isinstance(value, dict)
        }
        if isinstance(raw_activities, dict)
        else {}
    )
    previous = activities.get(activity_id)
    previous_status = (
        str(previous.get("status") or "").strip().casefold()
        if isinstance(previous, dict)
        else ""
    )
    previous_at = parse_conversation_activity_timestamp(
        (
            previous.get("updated_at")
            or previous.get("started_at")
            if isinstance(previous, dict)
            else None
        )
    )
    if isinstance(previous, dict):
        if previous_status != "running" and status == "running":
            # Activity IDs are immutable lifecycle identities. A completed
            # command cannot legitimately begin running again.
            return ThreadTitleUpdateResult(1, 0, 0)
        if previous_at is not None and (
            event_at < previous_at
            or (
                event_at == previous_at
                and (
                    status == previous_status
                    or previous_status != "running"
                    or status == "running"
                )
            )
        ):
            return ThreadTitleUpdateResult(1, 0, 0)
    if status != "running" and not isinstance(previous, dict):
        # A canonical result may already have retired the live card. Never
        # resurrect it when a delayed terminal metadata update arrives.
        return ThreadTitleUpdateResult(1, 0, 0)
    if (
        status == "running"
        and not isinstance(previous, dict)
        and await _canonical_activity_is_terminal(db, document.id, activity_id)
    ):
        # Canonical reconciliation removes the transient card. A delayed start
        # must not recreate it after the normalized terminal row has committed.
        return ThreadTitleUpdateResult(1, 0, 0)

    started_at = (
        str(previous.get("started_at") or "")
        if isinstance(previous, dict)
        else ""
    ) or event_timestamp
    anchor_line_number = (
        previous.get("anchor_line_number", 0)
        if isinstance(previous, dict)
        else 0
    )
    try:
        anchor_line_number = max(0, int(anchor_line_number or 0))
    except (TypeError, ValueError):
        anchor_line_number = 0
    if anchor_line_number == 0:
        anchor_line_number = await _interaction_anchor_line(
            db,
            document.id,
            started_at or timestamp,
        )

    activity = {
        "id": activity_id,
        "activity_type": "shell",
        "status": status,
        "tool_name": clean_tool or (
            str(previous.get("tool_name") or "")
            if isinstance(previous, dict)
            else "Shell"
        ),
        "command": clean_command or (
            str(previous.get("command") or "")
            if isinstance(previous, dict)
            else ""
        ),
        "started_at": _bounded_message_text(started_at, 128),
        "updated_at": _bounded_message_text(event_timestamp, 128),
        "anchor_line_number": anchor_line_number,
    }
    activities.pop(activity_id, None)
    activities[activity_id] = activity
    metadata[LIVE_SHELL_ACTIVITIES_KEY] = dict(
        list(activities.items())[-_LIVE_SHELL_ACTIVITY_LIMIT:]
    )
    if metadata == original_metadata:
        return ThreadTitleUpdateResult(1, 0, 0)

    delivery_state = await ensure_document_delivery_state(db, document)
    attach_document_delivery(document, delivery_state, runtime_only=True)
    store_document_metadata(document, metadata)
    if document.project_id:
        stage_cache_invalidation(
            db,
            project_conversations_cache_namespace(user_id, document.project_id),
        )
    _publish_file_synced_event(
        db,
        document,
        str(user_id),
        changes={
            "conversation.metadata",
            "conversation.pending_interactions",
        },
    )
    return ThreadTitleUpdateResult(1, 1, 0)
