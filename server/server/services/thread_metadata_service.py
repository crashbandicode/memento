"""Apply trusted, lightweight source metadata without re-ingesting content."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, Document, Machine
from .cache import cache_delete_prefix
from .conversation_parser import strip_terminal_sequences
from .ingest_service import (
    MAX_SEARCH_TEXT_CHARS,
    _bounded_message_text,
    _conversation_title_needs_derivation,
)
from .tokenize import tokenize_for_index


_MANUAL_TITLE_SOURCES = {
    "manual",
    "user",
    "memento_manual",
    "memento_user",
}
_TITLE_REVISION_MAP_LIMIT = 32


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
    return (
        select(Document)
        .where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            ),
            Document.tool_id == "codex",
            Document.category == "conversation",
            Document.metadata_["thread_id"].astext == thread_value,
        )
        .order_by(Document.id.asc())
        .with_for_update(of=Document)
    )


def _codex_source_thread_select(
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
):
    return (
        select(Document.id)
        .where(
            Document.machine_id == machine_id,
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            ),
            Document.tool_id == "codex",
            Document.category == "conversation",
            Document.metadata_["thread_id"].astext == str(thread_id),
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
    retained_keys = sorted(revisions)[-_TITLE_REVISION_MAP_LIMIT + 1:]
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
    tsv_input = tokenize_for_index(
        f"{document.title or ''} {searchable_content}"
    )
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
        await db.execute(
            _codex_source_thread_select(machine_id, user_id, thread_id)
        )
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
        metadata = dict(document.metadata_ or {})
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
                document.metadata_ = metadata
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
        document.metadata_ = metadata
        updated += 1
        if title_changed:
            title_changed_documents.append(document)

    for document in title_changed_documents:
        await _refresh_title_search_index(db, document)

    if updated:
        await cache_delete_prefix(f"daily:detail:{user_id}:")
        project_ids = {document.project_id for document in documents if document.project_id}
        for project_id in project_ids:
            await cache_delete_prefix(f"project:conv:{user_id}:{project_id}:")

    return ThreadTitleUpdateResult(
        matched=len(documents),
        updated=updated,
        ignored=ignored,
    )
