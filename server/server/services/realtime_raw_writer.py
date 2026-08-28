"""Phase 2 raw asyncpg writer for synchronous conversation ingestion.

This module deliberately has no SQLAlchemy imports.  The old ingest service
selects it only for an explicit canary and falls back before commit when this
writer reports an unsupported semantic shape or a pre-commit failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import asyncpg
import orjson

from ..config import settings


class RawWriterUnsupported(RuntimeError):
    """The legacy writer must handle this semantic shape for this sync."""


class RawWriterFailure(RuntimeError):
    """A raw transaction failed before its commit and is safe to retry old."""


class RawWriterCommitUncertain(RuntimeError):
    """COMMIT was attempted but its durable outcome could not be proven."""


@dataclass(frozen=True, slots=True)
class RawDocument:
    id: uuid.UUID
    disposition: str = "committed"
    file_size_bytes: int = 0

    @property
    def _memento_ingest_disposition(self) -> str:
        return self.disposition


@dataclass(frozen=True, slots=True)
class WriterState:
    document: dict[str, Any] | None
    delivery: dict[str, Any] | None
    sync: dict[str, Any] | None
    read_model: dict[str, Any] | None
    task_state: dict[str, Any] | None
    dashboard: dict[str, Any] | None
    tail: tuple[SimpleNamespace, ...] = ()
    queued_claude: tuple[SimpleNamespace, ...] = ()
    cursor_sources: tuple[SimpleNamespace, ...] = ()
    recovered_history: tuple[SimpleNamespace, ...] = ()
    ordinary_user_rows: tuple[SimpleNamespace, ...] = ()


@dataclass(slots=True)
class MessageMutation:
    ordinal: int
    operation: str
    line_number: int
    message_type: str | None
    role: str | None
    content: str
    metadata: dict[str, Any]
    timestamp: datetime | None
    existing_id: int | None = None
    projection_dirty: bool = False
    previous_role: str | None = None
    previous_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class IngestMutation:
    document_id: uuid.UUID
    new_document: bool
    mode: str
    document_values: dict[str, Any]
    delivery_values: dict[str, Any]
    sync_values: dict[str, Any]
    messages: list[MessageMutation] = field(default_factory=list)
    usage_rows: list[dict[str, Any]] = field(default_factory=list)
    search_text: str = ""
    interactions_changed: bool = False
    title_changed: bool = False
    disposition: str = "committed"
    canvas_candidate: bool = False
    search_candidate: bool = False


_pools: dict[tuple[int, str], asyncpg.Pool] = {}
_pool_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _json_dumps(value: Any) -> str:
    return orjson.dumps(value, default=str).decode("utf-8")


async def _initialize_connection(connection: asyncpg.Connection) -> None:
    """Configure one pooled connection and its reusable message stage."""
    for type_name in ("json", "jsonb"):
        await connection.set_type_codec(
            type_name,
            schema="pg_catalog",
            encoder=_json_dumps,
            decoder=orjson.loads,
            format="text",
        )
    await connection.execute(
        """
        CREATE TEMP TABLE memento_raw_message_stage (
            ordinal integer NOT NULL, operation text NOT NULL, existing_id bigint,
            document_id uuid NOT NULL, line_number integer NOT NULL,
            message_type text, role text, content text NOT NULL,
            metadata_text text NOT NULL, timestamp timestamptz
        ) ON COMMIT DELETE ROWS
        """
    )


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _pool(database_url: str | None = None) -> asyncpg.Pool:
    loop = asyncio.get_running_loop()
    dsn = (database_url or _dsn()).replace("postgresql+asyncpg://", "postgresql://", 1)
    key = (id(loop), dsn)
    pool = _pools.get(key)
    if pool is None or pool._closed:  # asyncpg exposes this inexpensive state.
        lock = _pool_locks.setdefault(key, asyncio.Lock())
        async with lock:
            pool = _pools.get(key)
            if pool is None or pool._closed:
                pool = await asyncpg.create_pool(
                    dsn,
                    min_size=1,
                    max_size=24,
                    command_timeout=60,
                    init=_initialize_connection,
                )
                _pools[key] = pool
    return pool


async def _commit_converged(
    *,
    database_url: str | None,
    document_id: uuid.UUID,
    machine_id: uuid.UUID | None,
    tool_id: str,
    relative_path: str,
    content_hash: str,
    offset: int,
) -> bool:
    """Resolve an unknown commit outcome from durable revision fences only."""
    pool = await _pool(database_url)
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT delivery.revision_hash, sync.last_hash, sync.last_offset
            FROM document_delivery_state AS delivery
            JOIN sync_state AS sync
              ON sync.machine_id IS NOT DISTINCT FROM $2
             AND sync.tool_id = $3
             AND sync.relative_path = $4
            WHERE delivery.document_id = $1
            """,
            document_id,
            machine_id,
            tool_id,
            relative_path,
        )
    return bool(
        row is not None
        and row["revision_hash"] == content_hash
        and row["last_hash"] == content_hash
        and int(row["last_offset"] or 0) == int(offset)
    )


def _view(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _record(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _uuid(value: object | None) -> uuid.UUID | None:
    return value if isinstance(value, uuid.UUID) else (uuid.UUID(str(value)) if value else None)


def _projection_scalar(value: Any) -> Any:
    """Match PostgreSQL ``to_jsonb`` scalar formatting for comparisons."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def _load_state(
    connection: asyncpg.Connection,
    *,
    machine_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    tool_id: str,
    relative_path: str,
    cursor_state_delta: bool,
    load_recovered_history: bool = False,
) -> WriterState:
    # Raw selection intentionally retains the normal path's machine/owner
    # scope.  The source advisory lock has already serialized this identity.
    document = await connection.fetchrow(
        """
        SELECT d.id, d.tool_id, d.project_id, d.machine_id, d.relative_path,
               d.category, d.content_type, d.title, d.content_hash,
               d.file_size_bytes, d.metadata AS document_metadata,
               d.source_modified_at, d.activity_at, d.synced_at, d.visibility,
               d.needs_review, d.content_s3_key, d.content_object_sha256,
               d.content_object_size_bytes, d.content_object_verified_at,
               ds.revision_hash AS delivery_revision_hash,
               ds.file_size_bytes AS delivery_file_size_bytes,
               ds.delivery_metadata, ds.source_modified_at AS delivery_source_modified_at,
               ds.activity_at AS delivery_activity_at, ds.synced_at AS delivery_synced_at
               ,(
                   SELECT to_jsonb(sync_row)
                   FROM sync_state AS sync_row
                   WHERE sync_row.tool_id = $1
                     AND sync_row.relative_path = $2
                     AND sync_row.machine_id IS NOT DISTINCT FROM $3
                   FOR UPDATE
               ) AS sync_row
               ,(
                   SELECT to_jsonb(read_row)
                   FROM conversation_read_models AS read_row
                   WHERE read_row.document_id = d.id
                   FOR UPDATE
               ) AS read_model_row
               ,(
                   SELECT to_jsonb(task_row)
                   FROM conversation_task_states AS task_row
                   WHERE task_row.document_id = d.id
                   FOR UPDATE
               ) AS task_state_row
               ,(
                   SELECT to_jsonb(dashboard_row)
                   FROM dashboard_document_projections AS dashboard_row
                   WHERE dashboard_row.document_id = d.id
                   FOR UPDATE
               ) AS dashboard_row
        FROM documents AS d
        LEFT JOIN document_delivery_state AS ds ON ds.document_id = d.id
        WHERE d.tool_id = $1 AND d.relative_path = $2 AND d.machine_id = $3
          AND EXISTS (
              SELECT 1 FROM machines AS owner_machine
              WHERE owner_machine.id = d.machine_id AND owner_machine.user_id = $4
          )
        FOR UPDATE OF d
        """,
        tool_id,
        relative_path,
        machine_id,
        user_id,
    )
    if document is None:
        sync = await connection.fetchrow(
            """
            SELECT id, last_hash, last_offset, last_synced_at
            FROM sync_state
            WHERE tool_id = $1 AND relative_path = $2
              AND machine_id IS NOT DISTINCT FROM $3
            FOR UPDATE
            """,
            tool_id,
            relative_path,
            machine_id,
        )
        return WriterState(None, None, _record(sync), None, None, None)
    doc = dict(document)
    sync = doc.pop("sync_row")
    read_model = doc.pop("read_model_row")
    task_state = doc.pop("task_state_row")
    dashboard = doc.pop("dashboard_row")
    document_id = doc["id"]
    tail_rows = await connection.fetch(
        """
        SELECT id, document_id, line_number, message_type, role, content,
               metadata, timestamp
        FROM conversation_messages WHERE document_id = $1
        ORDER BY line_number DESC LIMIT 32
        """,
        document_id,
    )
    queued: list[SimpleNamespace] = []
    if tool_id == "claude_code":
        queue_rows = await connection.fetch(
            """
            SELECT id, document_id, line_number, message_type, role, content,
                   metadata, timestamp
            FROM conversation_messages
            WHERE document_id = $1
              AND message_type = ANY($2::text[])
            ORDER BY line_number
            """,
            document_id,
            ["queued_user_message", "queued_scheduled_automation"],
        )
        queued = [_message_view(row) for row in queue_rows]
    cursor_sources: list[SimpleNamespace] = []
    if cursor_state_delta:
        source_rows = await connection.fetch(
            """
            SELECT id, document_id, line_number, message_type, role, content,
                   metadata, timestamp
            FROM conversation_messages
            WHERE document_id = $1 AND metadata ? 'source_id'
            ORDER BY line_number
            """,
            document_id,
        )
        cursor_sources = [_message_view(row) for row in source_rows]
    recovered_history: tuple[SimpleNamespace, ...] = ()
    ordinary_user_rows: tuple[SimpleNamespace, ...] = ()
    if load_recovered_history:
        recovered_rows = await connection.fetch(
            """
            SELECT id, document_id, line_number, message_type, role, content,
                   metadata, timestamp
            FROM conversation_messages
            WHERE document_id = $1
              AND message_type = ANY($2::text[])
            ORDER BY line_number, id
            """,
            document_id,
            ["history_user_message", "first_user_message"],
        )
        recovered_history = tuple(_message_view(row) for row in recovered_rows)
        source_user_rows = await connection.fetch(
            """
            SELECT id, document_id, line_number, message_type, role, content,
                   metadata, timestamp
            FROM conversation_messages
            WHERE document_id = $1
              AND role = 'user'
              AND message_type IS DISTINCT FROM 'history_user_message'
            ORDER BY line_number, id
            """,
            document_id,
        )
        ordinary_user_rows = tuple(_message_view(row) for row in source_user_rows)
    return WriterState(
        doc,
        {
            "revision_hash": doc["delivery_revision_hash"],
            "file_size_bytes": doc["delivery_file_size_bytes"],
            "metadata": doc["delivery_metadata"],
            "source_modified_at": doc["delivery_source_modified_at"],
            "activity_at": doc["delivery_activity_at"],
            "synced_at": doc["delivery_synced_at"],
        } if doc["delivery_revision_hash"] is not None else None,
        sync,
        read_model,
        task_state,
        dashboard,
        tuple(_message_view(row) for row in reversed(tail_rows)),
        tuple(queued),
        tuple(cursor_sources),
        recovered_history,
        ordinary_user_rows,
    )


def _message_view(row: asyncpg.Record | dict[str, Any]) -> SimpleNamespace:
    value = dict(row)
    return _view(
        id=int(value["id"]),
        document_id=value["document_id"],
        line_number=int(value["line_number"]),
        message_type=value.get("message_type"),
        role=value.get("role"),
        content=value.get("content") or "",
        metadata_=dict(value.get("metadata") or {}),
        timestamp=value.get("timestamp"),
    )


def _document_view(
    *,
    document_id: uuid.UUID,
    tool_id: str,
    category: str,
    relative_path: str,
    machine_id: uuid.UUID | None,
    metadata: dict[str, Any],
    title: str | None,
    project_id: uuid.UUID | None,
    revision_hash: str,
    file_size_bytes: int,
    source_modified_at: datetime | None,
    activity_at: datetime | None,
    synced_at: datetime,
    visibility: str = "private",
) -> SimpleNamespace:
    return _view(
        id=document_id,
        tool_id=tool_id,
        category=category,
        relative_path=relative_path,
        machine_id=machine_id,
        project_id=project_id,
        metadata_=dict(metadata),
        title=title,
        content_hash=revision_hash,
        file_size_bytes=file_size_bytes,
        source_modified_at=source_modified_at,
        activity_at=activity_at,
        synced_at=synced_at,
        visibility=visibility,
        delivery_state=None,
    )


def _legacy_history_timestamp(value: object) -> datetime | None:
    """Normalize a history timestamp exactly as the legacy recovery branch."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _history_metadata_is_already_committed(
    state: WriterState,
    *,
    tool_id: str,
    history: list[dict[str, Any]],
    first_user_message: str,
    prospective_mutations: list[MessageMutation],
) -> bool:
    """Prove a recovery payload is a no-op before omitting it from raw ingest.

    Legacy first partitions missing history entries against ordinary user rows,
    then reconciles *all* stored recovered rows against those same source rows.
    This check runs after the raw frame was reduced, mirrors both partitions
    with the shared helper, and accepts only when neither legacy step mutates.
    """
    if history:
        from .ingest_service import MAX_STORED_MESSAGE_CHARS, _bounded_message_text
        from .history_recovery import UserOccurrence, partition_recovered_occurrences

        codex_normalizer = None
        if tool_id == "codex":
            from .conversation_parser import normalize_codex_user_payload

            codex_normalizer = normalize_codex_user_payload

        ordinary_by_id = {
            int(row.id): UserOccurrence(
                key=("stored-user", int(row.id)),
                content=row.content,
                timestamp=row.timestamp,
                line_number=row.line_number,
            )
            for row in state.ordinary_user_rows
        }
        for mutation in prospective_mutations:
            if mutation.operation == "update" and mutation.existing_id is not None:
                ordinary_by_id.pop(int(mutation.existing_id), None)
            if (
                mutation.role == "user"
                and mutation.message_type != "history_user_message"
            ):
                storage_key = (
                    int(mutation.existing_id)
                    if mutation.existing_id is not None
                    else ("new-user", mutation.ordinal)
                )
                occurrence_key = (
                    ("updated-user", int(mutation.existing_id))
                    if mutation.existing_id is not None
                    else storage_key
                )
                ordinary_by_id[storage_key] = UserOccurrence(
                    key=occurrence_key,
                    content=mutation.content,
                    timestamp=mutation.timestamp,
                    line_number=mutation.line_number,
                )
        ordinary_users = list(ordinary_by_id.values())
        stored_by_source_id = {
            str(row.metadata_.get("source_id") or ""): row
            for row in state.recovered_history
            if row.message_type == "history_user_message"
            and row.metadata_.get("source_id")
        }
        missing_history: list[UserOccurrence] = []
        usable_entries = 0
        for history_index, entry in enumerate(history):
            raw_text = str(entry.get("text", "") or "").strip()
            if codex_normalizer is not None:
                history_role, raw_text = codex_normalizer(raw_text)
                if history_role != "user":
                    continue
            if not raw_text:
                continue
            usable_entries += 1
            expected = _bounded_message_text(
                raw_text.replace("\x00", ""),
                MAX_STORED_MESSAGE_CHARS,
            )
            row = stored_by_source_id.get(f"codex-history:{history_index}")
            expected_timestamp = _legacy_history_timestamp(entry.get("ts", 0))
            if row is None:
                missing_history.append(
                    UserOccurrence(
                        key=history_index,
                        content=expected,
                        timestamp=expected_timestamp,
                    )
                )
                continue
            if row.content != expected or row.timestamp != expected_timestamp:
                return False
        # Legacy must not insert a history gap: every source-ID-missing entry
        # has to be one-to-one represented by an ordinary current/prospective
        # user occurrence under the same bounded transport matching helper.
        _matched_missing, unmatched_missing = partition_recovered_occurrences(
            ordinary_users,
            missing_history,
        )
        if not usable_entries or unmatched_missing:
            return False

        # The later legacy reconciliation independently reuses the source
        # occurrence set against all stored history rows. Any match deletes a
        # recovered row; any remaining negative row is repositioned. Either
        # mutation means this shortcut is not a no-op.
        stored_recovered = [
            UserOccurrence(
                key=int(row.id),
                content=row.content,
                timestamp=row.timestamp,
                line_number=row.line_number,
            )
            for row in state.recovered_history
            if row.message_type == "history_user_message"
        ]
        matched_recovered, _unmatched_recovered = partition_recovered_occurrences(
            ordinary_users,
            stored_recovered,
        )
        return not matched_recovered and not any(
            row.line_number < 1
            for row in state.recovered_history
            if row.message_type == "history_user_message"
        )
    first_user_msg = (first_user_message or "").strip()
    if tool_id == "codex" and first_user_msg:
        from .conversation_parser import normalize_codex_user_payload

        first_role, first_user_msg = normalize_codex_user_payload(first_user_msg)
        if first_role != "user":
            # Legacy discards Codex-injected context before its fallback gate.
            return True
    if not first_user_msg.strip():
        # Legacy does not inject an absent or whitespace-only fallback prompt.
        return True

    # Legacy's gate selects any persisted user row, including recovered rows;
    # non-delete user mutations commit in this raw transaction before legacy
    # fallback would run, so each proves its injection would be a no-op. A
    # committed user row only counts if this frame does not update it to a
    # non-user role: legacy re-checks AFTER the frame applies.
    superseded_ids = {
        int(mutation.existing_id)
        for mutation in prospective_mutations
        if mutation.operation == "update"
        and mutation.existing_id is not None
        and mutation.role != "user"
    }
    return (
        any(int(row.id) not in superseded_ids for row in state.ordinary_user_rows)
        or any(
            row.role == "user" and int(row.id) not in superseded_ids
            for row in state.recovered_history
        )
        or any(
            mutation.role == "user" and mutation.operation != "delete"
            for mutation in prospective_mutations
        )
    )


def reduce_writer_state(
    state: WriterState,
    *,
    tool_id: str,
    category: str,
    content_type: str,
    relative_path: str,
    content: str,
    content_hash: str,
    file_size: int,
    mode: str,
    offset: int,
    metadata: dict[str, Any],
    timestamp: float | None,
    machine_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    base_hash: str | None,
    base_offset: int | None,
    authoritative_rebase: bool,
    had_sensitive: bool,
) -> IngestMutation:
    """Pure normalizer/reducer: scalar state plus bytes -> plain mutations."""
    from .conversation_parser import (
        codex_assistant_transport_priority,
        is_codex_assistant_mirror_pair,
        is_codex_user_mirror_pair,
        pop_matching_claude_queue_user,
    )
    from .conversation_tasks import canonical_task_state
    from .embedding_service import desired_embedding_tier
    from .ingest_revision import bounded_source_timestamp, committed_full_supersedes
    from .ingest_service import (
        STORED_SOURCE_HASH_KEY,
        STORED_SOURCE_REVISION_KEY,
        STORED_SOURCE_SIZE_KEY,
        _PROTECTED_DOCUMENT_METADATA_KEYS,
        _assistant_identity_for_ingest,
        _bounded_message_text,
        _committed_delta_base,
        _drain_assistant_usage_rows,
        _latest_human_timestamp_for_ingest,
        _logical_document_file_size,
        _merge_delta_metadata,
        _normalized_interaction_ids,
        _normalized_terminal_tool_call_ids,
        _pending_question_ids_for_ingest,
        _pending_question_interactions,
        _prepare_document_metadata,
        _reconcile_live_interaction_signals,
        _reconcile_live_shell_activities,
        _select_updated_document_title,
        _store_assistant_identity,
        _store_latest_human_timestamp,
        _store_pending_question_ids,
        _update_pending_question_ids,
        iter_stored_conversation_messages,
    )

    if category != "conversation" or content_type != "jsonl":
        raise RawWriterUnsupported("raw writer is limited to conversation JSONL")
    if authoritative_rebase:
        raise RawWriterUnsupported("authoritative rebuild/history needs legacy reducer")
    if mode not in {"full", "delta"}:
        raise RawWriterUnsupported("unknown conversation mode")
    if mode == "delta" and (base_hash is None or base_offset is None):
        raise RawWriterUnsupported("raw DELTAs require an exact committed base")
    collector_metadata = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in _PROTECTED_DOCUMENT_METADATA_KEYS
    }
    stored_metadata, incoming_history, incoming_first_user_message = (
        _prepare_document_metadata(collector_metadata, tool_id=tool_id)
    )
    has_history_metadata = bool(
        metadata.get("user_history") or metadata.get("first_user_message")
    )
    if state.document is not None and mode == "full":
        current = state.delivery["revision_hash"] if state.delivery else state.document["content_hash"]
        if current == content_hash:
            if settings.document_content_minio_enabled:
                payload = content.encode("utf-8")
                if not (
                    state.document.get("content_s3_key")
                    and state.document.get("content_object_sha256")
                    == hashlib.sha256(payload).hexdigest()
                    and int(state.document.get("content_object_size_bytes") or -1)
                    == len(payload)
                    and state.document.get("content_object_verified_at") is not None
                ):
                    raise RawWriterUnsupported(
                        "an idempotent FULL with a missing pointer needs legacy repair"
                    )
            return IngestMutation(
                document_id=state.document["id"], new_document=False, mode=mode,
                document_values={}, delivery_values={}, sync_values={}, disposition="idempotent"
            )
        if not authoritative_rebase:
            existing_offset = int(state.sync["last_offset"] or 0) if state.sync and state.sync.get("last_hash") == current else 0
            existing_size = state.delivery["file_size_bytes"] if state.delivery else state.document["file_size_bytes"]
            source_at = state.delivery["source_modified_at"] if state.delivery else state.document["source_modified_at"]
            received = datetime.now(timezone.utc)
            incoming_at = bounded_source_timestamp(timestamp, received) or received
            if committed_full_supersedes(
                existing_hash=current, existing_timestamp=source_at,
                existing_offset=existing_offset, existing_size=int(existing_size or 0),
                incoming_hash=content_hash, incoming_timestamp=incoming_at,
                incoming_offset=offset, incoming_size=file_size,
            ):
                return IngestMutation(
                    document_id=state.document["id"], new_document=False, mode=mode,
                    document_values={}, delivery_values={}, sync_values={}, disposition="superseded"
                )
        raise RawWriterUnsupported("replacement FULL needs legacy reducer")
    if state.document is None and mode == "delta":
        raise RawWriterUnsupported("a DELTA requires an existing FULL document")

    received_at = datetime.now(timezone.utc)
    source_modified_at = bounded_source_timestamp(timestamp, received_at) or received_at
    doc_row = state.document
    delivery = state.delivery
    effective_metadata = dict(
        (delivery or {}).get("metadata")
        or (doc_row or {}).get("document_metadata")
        or {}
    )
    current_revision = (delivery or {}).get("revision_hash") or (doc_row or {}).get("content_hash")
    current_size = int((delivery or {}).get("file_size_bytes") or (doc_row or {}).get("file_size_bytes") or 0)
    if mode == "delta" and doc_row is not None:
        if (
            current_revision == content_hash
            and state.sync is not None
            and state.sync.get("last_hash") == content_hash
            and int(state.sync.get("last_offset") or 0) == int(offset)
        ):
            return IngestMutation(
                document_id=doc_row["id"], new_document=False, mode=mode,
                document_values={}, delivery_values={}, sync_values={}, disposition="idempotent"
            )
        if state.sync and state.sync.get("last_hash") == current_revision and int(state.sync.get("last_offset") or 0) >= offset:
            return IngestMutation(
                document_id=doc_row["id"], new_document=False, mode=mode,
                document_values={}, delivery_values={}, sync_values={}, disposition="stale_delta"
            )
        if base_hash is not None:
            legacy_doc = _view(content_hash=current_revision, file_size_bytes=current_size)
            legacy_sync = _view(**state.sync) if state.sync else None
            expected_hash, expected_offset = _committed_delta_base(legacy_doc, legacy_sync)
            if expected_hash != base_hash or base_offset is None or expected_offset != int(base_offset):
                from .ingest_service import DeltaBaseMismatch
                raise DeltaBaseMismatch(expected_hash=expected_hash, expected_offset=expected_offset)

    sanitized = content.replace("\x00", "")
    incoming_title_is_explicit = (
        stored_metadata.pop("source_title_kind", None) == "claude_ai_title"
    )
    incoming_title = stored_metadata.pop("title", None)
    if incoming_title_is_explicit:
        stored_metadata["memento_title_source"] = "claude_ai_title"
    if doc_row is None:
        if (
            stored_metadata.get("project_hash")
            or stored_metadata.get("project_path")
            or re.search(r'"cwd"\s*:\s*"[^"]+"', sanitized[:10_000])
        ):
            raise RawWriterUnsupported(
                "new-document project resolution needs the legacy reducer"
            )
        document_id = uuid.uuid4()
        document_metadata = dict(stored_metadata)
        new_document = True
        title = incoming_title or relative_path.split("/")[-1]
        project_id = None
        visibility = "private"
        document_source_at = source_modified_at
        previous_title = None
    else:
        document_id = doc_row["id"]
        document_metadata = _merge_delta_metadata(effective_metadata, stored_metadata) if mode == "delta" else dict(stored_metadata)
        new_document = False
        previous_title = doc_row["title"]
        if (
            tool_id == "claude_code"
            and previous_title
            and not incoming_title_is_explicit
            and not incoming_title
        ):
            # Legacy derives its friendly Claude title after applying the
            # filename fallback for an untitled DELTA. Raw has no derivation
            # pass, so preserve that net title here.
            title = previous_title
        else:
            title = _select_updated_document_title(
                previous_title,
                incoming_title or relative_path.split("/")[-1],
                category=category,
                tool_id=tool_id,
                metadata=document_metadata,
                incoming_title_is_explicit=incoming_title_is_explicit,
            )
        project_id = doc_row["project_id"]
        visibility = doc_row["visibility"] or "private"
        document_source_at = max(filter(None, ((delivery or {}).get("source_modified_at"), source_modified_at)))
    if mode == "full":
        encoded = sanitized.encode("utf-8")
        document_metadata[STORED_SOURCE_HASH_KEY] = hashlib.sha256(encoded).hexdigest()
        document_metadata[STORED_SOURCE_SIZE_KEY] = len(encoded)
        document_metadata[STORED_SOURCE_REVISION_KEY] = content_hash
    cursor_state_delta = (
        mode == "delta"
        and tool_id == "cursor"
        and document_metadata.get("source") == "cursor_state_v1"
    )
    logical_size = _logical_document_file_size(
        mode=mode,
        payload_size=file_size,
        offset=offset,
        existing_size=current_size,
        replace_offset=cursor_state_delta,
    )
    view = _document_view(
        document_id=document_id, tool_id=tool_id, category=category,
        relative_path=relative_path, machine_id=machine_id, metadata=document_metadata,
        title=title, project_id=project_id, revision_hash=content_hash,
        file_size_bytes=logical_size, source_modified_at=document_source_at,
        activity_at=(delivery or {}).get("activity_at") or (doc_row or {}).get("activity_at"),
        synced_at=received_at, visibility=visibility,
    )
    assistant_identity = _assistant_identity_for_ingest(view, mode)
    initial_task = canonical_task_state((state.task_state or {}).get("state"))
    tail = list(state.tail)
    initial_interactions = _pending_question_interactions(tail)
    start_line = (max((row.line_number for row in tail), default=0) + 1) if mode == "delta" else 1
    source_rows: dict[str, SimpleNamespace] = {}
    # The cursor reducer supplies the exact source rows from the preloaded
    # bounded tail for common updates.  Other sparse ordering shapes fall back.
    if cursor_state_delta:
        for row in state.cursor_sources:
            source_id = str(row.metadata_.get("source_id") or "")
            if source_id:
                source_rows[source_id] = row
    queued: dict[str, list[SimpleNamespace]] = {}
    if tool_id == "claude_code":
        for row in state.queued_claude:
            if not row.metadata_.get("canonical_source_id"):
                queued.setdefault(row.content.strip(), []).append(row)
    pending_ids = _pending_question_ids_for_ingest(view, mode)
    latest_human = _latest_human_timestamp_for_ingest(view, mode)
    mutations: list[MessageMutation] = []
    usage_rows: list[dict[str, Any]] = []
    line_number = start_line
    delta_tail = tail[-1] if tail else None
    canonical_interaction_ids: set[str] = set()
    terminal_tool_ids: set[str] = set()
    interactions_changed = mode == "full"
    has_search_text = False
    has_user_search_text = False
    ordinal = 0
    for normalized, clean_content, row_metadata, row_timestamp in iter_stored_conversation_messages(
        sanitized, tool_id,
        initial_question_interactions=initial_interactions,
        assistant_identity=assistant_identity,
        initial_task_state=initial_task,
        incremental=mode == "delta",
    ):
        usage_rows.extend(_drain_assistant_usage_rows(view, tool_id, assistant_identity))
        latest_human = _update_pending_question_ids(pending_ids, normalized, latest_human)
        canonical_interaction_ids.update(_normalized_interaction_ids(normalized))
        terminal_tool_ids.update(_normalized_terminal_tool_call_ids(normalized))
        interactions_changed = interactions_changed or bool(_normalized_interaction_ids(normalized))
        message_type = _bounded_message_text(normalized.raw_type or normalized.role, 50)
        incoming_source_id = str(row_metadata.get("source_id") or "")
        existing_cursor = source_rows.get(incoming_source_id) if cursor_state_delta else None
        if existing_cursor is not None:
            if normalized.role in {"user", "assistant"}:
                has_search_text = True
                has_user_search_text = has_user_search_text or normalized.role == "user"
            mutations.append(MessageMutation(
                ordinal=ordinal, operation="update", existing_id=existing_cursor.id,
                line_number=existing_cursor.line_number, message_type=message_type,
                role=normalized.role, content=clean_content, metadata=row_metadata,
                timestamp=row_timestamp, projection_dirty=True,
                previous_role=existing_cursor.role,
                previous_metadata=dict(existing_cursor.metadata_),
            ))
            ordinal += 1
            if delta_tail is existing_cursor:
                delta_tail = None
            continue
        if mode == "delta" and tool_id == "claude_code" and (
            (normalized.role == "user" and normalized.raw_type == "user")
            or (normalized.role == "system" and normalized.raw_type == "scheduled_automation")
        ):
            queue_row = pop_matching_claude_queue_user(queued, clean_content, row_timestamp)
            if queue_row is not None:
                next_metadata = dict(queue_row.metadata_)
                kind = "scheduled" if normalized.raw_type == "scheduled_automation" else "user"
                canonical = normalized.source_id or f"claude-{kind}:" + hashlib.sha256(
                    "\x1f".join((normalized.timestamp or "", clean_content)).encode("utf-8")
                ).hexdigest()
                next_metadata["canonical_source_id"] = _bounded_message_text(canonical, 256)
                mutations.append(MessageMutation(
                    ordinal=ordinal, operation="update", existing_id=queue_row.id,
                    line_number=queue_row.line_number, message_type=queue_row.message_type,
                    role=queue_row.role, content=queue_row.content, metadata=next_metadata,
                    timestamp=queue_row.timestamp,
                    previous_role=queue_row.role,
                    previous_metadata=dict(queue_row.metadata_),
                ))
                ordinal += 1
                continue
        if mode == "delta" and tool_id == "codex" and delta_tail is not None and delta_tail.line_number == line_number - 1:
            is_user_pair = normalized.role == "user" and normalized.raw_type == "user_message" and not normalized.source_paired and delta_tail.role == "user" and is_codex_user_mirror_pair(
                delta_tail.message_type, delta_tail.content, delta_tail.timestamp,
                normalized.raw_type, clean_content, row_timestamp,
            )
            is_assistant_pair = normalized.role == "assistant" and not normalized.source_paired and delta_tail.role == "assistant" and is_codex_assistant_mirror_pair(
                delta_tail.message_type, delta_tail.content, delta_tail.timestamp,
                normalized.raw_type, clean_content, row_timestamp,
            )
            if is_user_pair or is_assistant_pair:
                if normalized.role in {"user", "assistant"}:
                    has_search_text = True
                    has_user_search_text = has_user_search_text or normalized.role == "user"
                next_type = message_type
                next_content, next_metadata, next_timestamp = clean_content, row_metadata, row_timestamp
                if is_assistant_pair and codex_assistant_transport_priority(message_type) <= codex_assistant_transport_priority(delta_tail.message_type):
                    next_type, next_content, next_metadata, next_timestamp = delta_tail.message_type, delta_tail.content, delta_tail.metadata_, delta_tail.timestamp
                mutations.append(MessageMutation(
                    ordinal=ordinal, operation="update", existing_id=delta_tail.id,
                    line_number=delta_tail.line_number, message_type=next_type,
                    role=delta_tail.role, content=next_content, metadata=next_metadata,
                    timestamp=next_timestamp, projection_dirty=True,
                    previous_role=delta_tail.role,
                    previous_metadata=dict(delta_tail.metadata_),
                ))
                ordinal += 1
                delta_tail = None
                continue
        mutations.append(MessageMutation(
            ordinal=ordinal, operation="insert", line_number=line_number,
            message_type=message_type, role=normalized.role, content=clean_content,
            metadata=row_metadata, timestamp=row_timestamp,
        ))
        if normalized.role in {"user", "assistant"}:
            has_search_text = True
            has_user_search_text = has_user_search_text or normalized.role == "user"
        ordinal += 1
        line_number += 1
    usage_rows.extend(_drain_assistant_usage_rows(view, tool_id, assistant_identity))
    if new_document:
        from .conversation_hierarchy import conversation_briefing_kind

        first_user_content = next(
            (item.content for item in mutations if item.role == "user"),
            "",
        )
        if conversation_briefing_kind(first_user_content) == "delegate":
            # The normalized path reconciles a delegate marker with existing
            # orchestration records and mirrors that result into all three
            # projections.  The raw reducer has no equivalent relational
            # reconciliation, so it must decline this semantic shape before
            # any mutation rather than commit an unlinked child view.
            raise RawWriterUnsupported(
                "claw delegate markers need legacy orchestration reconciliation"
            )
    _reconcile_live_interaction_signals(view, canonical_interaction_ids, clear_all=False)
    _reconcile_live_shell_activities(view, terminal_tool_ids)
    _store_pending_question_ids(view, pending_ids)
    _store_latest_human_timestamp(view, latest_human)
    _store_assistant_identity(view, assistant_identity)
    if has_history_metadata and not _history_metadata_is_already_committed(
        state,
        tool_id=tool_id,
        history=incoming_history,
        first_user_message=incoming_first_user_message,
        prospective_mutations=mutations,
    ):
        raise RawWriterUnsupported("authoritative rebuild/history needs legacy reducer")
    from .canvas_artifacts import canvas_message_can_have_reference
    from .ingest_service import _conversation_search_index_needs_refresh
    from .realtime_ingest_projector import message_is_canvas_projection_candidate

    # Inserts enqueue only when the new body can name a Canvas. Updates also
    # retain the pre-update role/metadata so a replacement which removes a
    # Canvas-capable or indexed row still reconciles the stale projection.
    candidate_has_search_text = has_search_text or any(
        item.operation == "update"
        and item.previous_role in {"user", "assistant"}
        for item in mutations
    )
    candidate_has_user_search_text = has_user_search_text or any(
        item.operation == "update" and item.previous_role == "user"
        for item in mutations
    )
    canvas_candidate = any(
        message_is_canvas_projection_candidate(
            item.role, item.metadata, item.content
        )
        or (
            item.operation == "update"
            and (
                canvas_message_can_have_reference(item.role, item.metadata)
                or canvas_message_can_have_reference(
                    item.previous_role, item.previous_metadata
                )
            )
        )
        for item in mutations
    )
    search_text = "[user]" if has_user_search_text else ("[assistant]" if has_search_text else "")
    candidate_search_text = (
        "[user]"
        if candidate_has_user_search_text
        else ("[assistant]" if candidate_has_search_text else "")
    )
    search_candidate = _conversation_search_index_needs_refresh(
        is_new_document=new_document,
        mode=mode,
        new_search_text=candidate_search_text,
        previous_title=previous_title,
        current_title=view.title,
    )
    sync_values = {"last_hash": content_hash, "last_offset": offset, "last_synced_at": received_at}
    return IngestMutation(
        document_id=document_id, new_document=new_document, mode=mode,
        document_values={
            "metadata": dict(view.metadata_), "title": view.title,
            "project_id": view.project_id, "visibility": view.visibility,
            "source_modified_at": document_source_at, "synced_at": received_at,
            "file_size_bytes": logical_size,
            "needs_review": bool((doc_row or {}).get("needs_review")) or had_sensitive,
            "embedding_tier": desired_embedding_tier(category),
        },
        delivery_values={
            "revision_hash": content_hash, "file_size_bytes": logical_size,
            "metadata": dict(view.metadata_), "source_modified_at": document_source_at,
            "activity_at": view.activity_at, "synced_at": received_at,
        },
        sync_values=sync_values, messages=mutations, usage_rows=usage_rows,
        # The raw path currently needs only the event namespace signal, not a
        # second in-memory copy of the bounded search corpus.
        search_text=search_text,
        # Claude's raw UUID lineage may change on semantic records that do not
        # emit a visible message.  The existing writer includes that namespace
        # in the post-commit union; retain the same conservative invalidation.
        interactions_changed=interactions_changed or tool_id == "claude_code",
        title_changed=not new_document and previous_title != view.title,
        canvas_candidate=canvas_candidate,
        search_candidate=search_candidate,
    )


async def _stage_messages(
    connection: asyncpg.Connection, document_id: uuid.UUID, mutations: list[MessageMutation]
) -> tuple[list[SimpleNamespace], set[int]]:
    if not mutations:
        return [], set()
    # The connection-local stage table is deliberately reused across pooled
    # transactions.  Phase 3 invokes this ordered reducer more than once
    # inside one coalesced transaction, so ``ON COMMIT DELETE ROWS`` alone is
    # insufficient; otherwise the prior frame's inserts are replayed.
    await connection.execute("TRUNCATE memento_raw_message_stage")
    await connection.copy_records_to_table(
        "memento_raw_message_stage",
        records=(
            (
                mutation.ordinal,
                mutation.operation,
                mutation.existing_id,
                document_id,
                mutation.line_number,
                mutation.message_type,
                mutation.role,
                mutation.content,
                _json_dumps(mutation.metadata),
                mutation.timestamp,
            )
            for mutation in mutations
        ),
        columns=("ordinal", "operation", "existing_id", "document_id", "line_number", "message_type", "role", "content", "metadata_text", "timestamp"),
    )
    applied = await connection.fetch(
        """
        WITH updated AS (
            UPDATE conversation_messages AS message SET
                message_type = stage.message_type, role = stage.role,
                content = stage.content, metadata = stage.metadata_text::jsonb,
                timestamp = stage.timestamp
            FROM memento_raw_message_stage AS stage
            WHERE stage.operation = 'update' AND message.id = stage.existing_id
            RETURNING message.id, stage.ordinal
        ), inserted AS (
            INSERT INTO conversation_messages
                (document_id, line_number, message_type, role, content, metadata, timestamp)
            SELECT document_id, line_number, message_type, role, content, metadata_text::jsonb, timestamp
            FROM memento_raw_message_stage WHERE operation = 'insert'
            ORDER BY ordinal
            RETURNING id, document_id, line_number
        )
        SELECT updated.id, updated.ordinal FROM updated
        UNION ALL
        SELECT inserted.id, stage.ordinal
        FROM inserted JOIN memento_raw_message_stage AS stage
          ON stage.operation = 'insert' AND stage.document_id = inserted.document_id
         AND stage.line_number = inserted.line_number
        ORDER BY ordinal
        """
    )
    returned_ids = {int(row["ordinal"]): int(row["id"]) for row in applied}
    if len(returned_ids) != len(mutations):
        raise RuntimeError("message stage did not apply every mutation")
    rows = [
        _view(
            id=returned_ids[mutation.ordinal],
            document_id=document_id,
            line_number=mutation.line_number,
            message_type=mutation.message_type,
            role=mutation.role,
            content=mutation.content,
            metadata_=mutation.metadata,
            timestamp=mutation.timestamp,
        )
        for mutation in mutations
    ]
    dirty = {mutation.line_number for mutation in mutations if mutation.projection_dirty}
    return rows, dirty


async def _upsert_usage(connection: asyncpg.Connection, rows: list[dict[str, Any]], *, accumulate: bool) -> None:
    if not rows:
        return
    from .ingest_service import _USAGE_COUNT_FIELDS, _merge_usage_rows
    rows = _merge_usage_rows(rows, accumulate=accumulate)
    query = """
        INSERT INTO conversation_usage_events
          (document_id, machine_id, tool_id, source_id, source, occurred_at,
           model, reasoning_effort, service_tier, attribution_status,
           input_tokens, uncached_input_tokens, cached_input_tokens,
           cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        ON CONFLICT (document_id, source_id) DO UPDATE SET
          machine_id=EXCLUDED.machine_id, tool_id=EXCLUDED.tool_id, source=EXCLUDED.source,
          occurred_at=GREATEST(conversation_usage_events.occurred_at, EXCLUDED.occurred_at),
          model=EXCLUDED.model, reasoning_effort=EXCLUDED.reasoning_effort,
          service_tier=EXCLUDED.service_tier, attribution_status=EXCLUDED.attribution_status,
          input_tokens={input_tokens}, uncached_input_tokens={uncached_input_tokens},
          cached_input_tokens={cached_input_tokens}, cache_write_input_tokens={cache_write_input_tokens},
          output_tokens={output_tokens}, reasoning_output_tokens={reasoning_output_tokens}, total_tokens={total_tokens}
    """.format(**{
        field: f"conversation_usage_events.{field} + EXCLUDED.{field}" if accumulate else f"EXCLUDED.{field}"
        for field in _USAGE_COUNT_FIELDS
    })
    await connection.executemany(query, [tuple(row.get(column) for column in (
        "document_id", "machine_id", "tool_id", "source_id", "source", "occurred_at",
        "model", "reasoning_effort", "service_tier", "attribution_status",
        "input_tokens", "uncached_input_tokens", "cached_input_tokens",
        "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
    )) for row in rows])


async def _refresh_projections(
    connection: asyncpg.Connection, *, state: WriterState, mutation: IngestMutation,
    document: SimpleNamespace, rows: list[SimpleNamespace], dirty: set[int], user_id: uuid.UUID | None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    from .conversation_read_model import READ_MODEL_VERSION, _Accumulator, _identity_values, _prompt_projection_value
    from .conversation_tasks import _document_identity, _state_from_metadata, task_state_counts, task_state_hash
    from .dashboard_projection import dashboard_projection_values

    existing = _view(**state.read_model) if state.read_model else None
    incremental = mutation.mode == "delta" and existing is not None and int(existing.projection_version or 0) == READ_MODEL_VERSION
    previous_through = int(existing.projected_through_line or 0) if incremental else 0
    inserted = [row for row, source in zip(rows, mutation.messages) if source.operation == "insert"]
    if incremental and dirty:
        stats = await connection.fetchrow(
            """SELECT count(*) AS messages, count(*) FILTER (WHERE role='user') AS users,
                      count(*) FILTER (WHERE role='assistant') AS assistants,
                      coalesce(sum(length(content)) FILTER (WHERE role = ANY($2::text[])), 0) AS characters
               FROM conversation_messages WHERE document_id=$1""",
            mutation.document_id, ["user", "assistant"],
        )
        message_count, user_count, assistant_count, character_count = (int(stats[key] or 0) for key in ("messages", "users", "assistants", "characters"))
    elif incremental:
        message_count = int(existing.message_count or 0) + len(inserted)
        user_count = int(existing.user_message_count or 0) + sum(row.role == "user" for row in inserted)
        assistant_count = int(existing.assistant_message_count or 0) + sum(row.role == "assistant" for row in inserted)
        character_count = int(existing.human_character_count or 0) + sum(len(row.content or "") for row in inserted if row.role in {"user", "assistant"})
    else:
        message_count = len(rows)
        user_count = sum(row.role == "user" for row in rows)
        assistant_count = sum(row.role == "assistant" for row in rows)
        character_count = sum(len(row.content or "") for row in rows if row.role in {"user", "assistant"})
    accumulator = _Accumulator(existing if incremental else None)
    for row in rows:
        accumulator.observe(row)
    projected = max([previous_through, *(row.line_number for row in rows)], default=previous_through)
    latest_assistant = existing.latest_assistant_line if incremental else None
    for row in rows:
        if (row.role or row.message_type) == "assistant":
            latest_assistant = max(int(latest_assistant or 0), row.line_number)
    previous_generation = int(existing.generation or 0) if existing is not None else 0
    accumulator_values = accumulator.values()
    # The count is derived from currently live state for API consumers.  It is
    # deliberately not a persisted read-model column.
    accumulator_values.pop("background_running_count", None)
    read_values = {
        **_identity_values(document), "message_count": message_count,
        "user_message_count": user_count, "assistant_message_count": assistant_count,
        "human_character_count": character_count, "projected_through_line": projected,
        "latest_assistant_line": latest_assistant,
        "generation": previous_generation if incremental and not dirty else previous_generation + 1,
        "projection_version": READ_MODEL_VERSION, **accumulator_values,
    }
    read_columns = list(read_values)
    read_placeholders = [
        (
            f"COALESCE(${index}::jsonb, 'null'::jsonb)"
            if column == "lifecycle"
            else f"${index}"
        )
        for index, column in enumerate(read_columns, start=2)
    ]
    await connection.execute(
        "INSERT INTO conversation_read_models (document_id," + ",".join(read_columns) + ") VALUES ($1," + ",".join(read_placeholders) + ") ON CONFLICT (document_id) DO UPDATE SET " + ",".join(f"{column}=EXCLUDED.{column}" for column in read_columns),
        mutation.document_id, *(read_values[column] for column in read_columns),
    )
    updated_ids = [
        row.id
        for row, source in zip(rows, mutation.messages)
        if source.operation == "update"
    ]
    if not incremental and not mutation.new_document:
        await connection.execute("DELETE FROM conversation_prompt_projections WHERE document_id=$1", mutation.document_id)
    elif updated_ids:
        await connection.execute("DELETE FROM conversation_prompt_projections WHERE document_id=$1 AND message_id = ANY($2::bigint[])", mutation.document_id, updated_ids)
    prompt_rows = [_prompt_projection_value(row) for row in rows]
    prompt_rows = [row for row in prompt_rows if row is not None]
    if prompt_rows:
        await connection.executemany(
            "INSERT INTO conversation_prompt_projections (document_id,message_id,line_number,content,timestamp) VALUES ($1,$2,$3,$4,$5)",
            [(row["document_id"], row["message_id"], row["line_number"], row["content"], row["timestamp"]) for row in prompt_rows],
        )
    # Task projection is derived from the same staged rows and generated IDs.
    candidates = [row for row in rows if _state_from_metadata(row.metadata_) is not None]
    if candidates:
        source = max(candidates, key=lambda row: (
            int(((_state_from_metadata(row.metadata_) or {}).get("is_current")) and ((_state_from_metadata(row.metadata_) or {}).get("quality") != "partial")), row.line_number, row.id
        ))
        task = _state_from_metadata(source.metadata_)
        assert task is not None
        source_ids = list(task["source_ids"])
        for value in (source.metadata_.get("source_id"), source.metadata_.get("tool_call_id")):
            if value and str(value) not in source_ids:
                source_ids.append(str(value)[:256])
        task["source_ids"] = source_ids[-64:]
        counts = task_state_counts(task)
        identity = _document_identity(document)
        explicit = bool(task["is_current"] and task["quality"] != "partial")
        observed = source.timestamp or datetime.now(timezone.utc)
        task_values = {
            "machine_id": document.machine_id, "user_id": user_id, "tool_id": document.tool_id,
            **identity, "source_message_id": source.id, "source_line_number": source.line_number,
            "source_ids": task["source_ids"], "revision": task["revision"], "state": task,
            "state_hash": task_state_hash(task), "explicit_current": explicit, "quality": task["quality"],
            "projection_version": 1, "pending_count": counts["pending"], "in_progress_count": counts["in_progress"],
            "blocked_count": counts["blocked"], "completed_count": counts["completed"], "cancelled_count": counts["cancelled"],
            "outstanding_count": counts["outstanding"], "total_count": counts["total"], "observed_at": observed,
            "verified_at": observed if explicit else None,
        }
        columns = list(task_values)
        await connection.execute(
            "INSERT INTO conversation_task_states (document_id," + ",".join(columns) + ") VALUES (" + ",".join(f"${index}" for index in range(1, len(columns) + 2)) + ") ON CONFLICT (document_id) DO UPDATE SET " + ",".join(f"{column}=EXCLUDED.{column}" for column in columns),
            mutation.document_id, *(task_values[column] for column in columns),
        )
    read_view = _view(**read_values)
    dashboard_values = dashboard_projection_values(document, read_view)
    previous_dashboard = state.dashboard or {}
    dashboard_changed = not previous_dashboard or any(
        _projection_scalar(previous_dashboard.get(key))
        != _projection_scalar(value)
        for key, value in dashboard_values.items()
    )
    if previous_dashboard and dashboard_changed:
        dashboard_changed = any(
            _projection_scalar(previous_dashboard.get(key))
            != _projection_scalar(dashboard_values[key])
            for key in (
                "title",
                "category",
                "activity_at",
                "is_archived",
                "pending_question_count",
            )
        ) or (int(previous_dashboard.get("message_count") or 0) // 20 != int(dashboard_values["message_count"] or 0) // 20)
    columns = list(dashboard_values)
    await connection.execute(
        "INSERT INTO dashboard_document_projections (document_id," + ",".join(columns) + ") VALUES (" + ",".join(f"${index}" for index in range(1, len(columns) + 2)) + ") ON CONFLICT (document_id) DO UPDATE SET " + ",".join(f"{column}=EXCLUDED.{column}" for column in columns),
        mutation.document_id, *(dashboard_values[column] for column in columns),
    )
    return read_values, dashboard_values, dashboard_changed


async def _apply(
    connection: asyncpg.Connection, *, state: WriterState, mutation: IngestMutation,
    tool_id: str, category: str, content_type: str, relative_path: str,
    content_hash: str, machine_id: uuid.UUID | None, user_id: uuid.UUID | None,
    content: str,
) -> tuple[RawDocument, dict[str, Any] | None]:
    if mutation.disposition != "committed":
        if state.sync is not None:
            await connection.execute("UPDATE sync_state SET last_synced_at=$1 WHERE id=$2", datetime.now(timezone.utc), state.sync["id"])
        current_size = int(
            (state.delivery or {}).get("file_size_bytes")
            or (state.document or {}).get("file_size_bytes")
            or 0
        )
        return RawDocument(mutation.document_id, mutation.disposition, current_size), None
    pointer = None
    if mutation.mode == "full" and settings.document_content_minio_enabled:
        from .large_content_store import finalize_document_content
        pointer = await finalize_document_content(document_id=mutation.document_id, content=content, connection=connection)
    if mutation.new_document:
        await connection.execute(
            """
            INSERT INTO tools
              (id, display_name, total_files, total_size_bytes, last_sync_at)
            VALUES ($1,$2,1,0,$3)
            ON CONFLICT (id) DO UPDATE SET
              last_sync_at=EXCLUDED.last_sync_at,
              total_files=coalesce(tools.total_files,0)+1
            """,
            tool_id,
            tool_id,
            mutation.document_values["synced_at"],
        )
        await connection.execute(
            """
            INSERT INTO documents
              (id,tool_id,project_id,machine_id,relative_path,category,content_type,title,
               content_hash,file_size_bytes,metadata,needs_review,visibility,embedding_status,
               embedding_attempts,embedding_tier,knowledge_status,knowledge_attempts,synced_at,
               source_modified_at,activity_at,content_s3_key,content_object_sha256,
               content_object_size_bytes,content_object_verified_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'pending',0,$20,'pending',0,$14,$15,NULL,$16,$17,$18,$19)
            """,
            mutation.document_id, tool_id, mutation.document_values["project_id"], machine_id,
            relative_path, category, content_type, mutation.document_values["title"], content_hash,
            mutation.document_values["file_size_bytes"], mutation.document_values["metadata"],
            mutation.document_values["needs_review"], mutation.document_values["visibility"],
            mutation.document_values["synced_at"], mutation.document_values["source_modified_at"],
            pointer.key if pointer else None,
            pointer.sha256 if pointer else None, pointer.size_bytes if pointer else None,
            pointer.verified_at if pointer else None,
            mutation.document_values["embedding_tier"],
        )
    elif mutation.mode == "full":
        await connection.execute(
            "UPDATE documents SET metadata=$2, source_modified_at=$3, synced_at=$4, content_hash=$5, file_size_bytes=$6, content_s3_key=$7, content_object_sha256=$8, content_object_size_bytes=$9, content_object_verified_at=$10 WHERE id=$1",
            mutation.document_id, mutation.document_values["metadata"], mutation.document_values["source_modified_at"], mutation.document_values["synced_at"], content_hash, mutation.document_values["file_size_bytes"], pointer.key if pointer else None, pointer.sha256 if pointer else None, pointer.size_bytes if pointer else None, pointer.verified_at if pointer else None,
        )
    if mutation.title_changed:
        await connection.execute(
            "UPDATE documents SET title=$2 WHERE id=$1 AND title IS DISTINCT FROM $2",
            mutation.document_id,
            mutation.document_values["title"],
        )
    rows, dirty = await _stage_messages(connection, mutation.document_id, mutation.messages)
    if mutation.mode == "full" and not mutation.new_document:
        raise RawWriterUnsupported("replacement FULL must be rejected before staging")
    previous_activity = (
        (state.delivery or {}).get("activity_at")
        or (state.document or {}).get("activity_at")
    )
    if dirty:
        activity_at = await connection.fetchval(
            "SELECT max(timestamp) FROM conversation_messages WHERE document_id=$1 AND role = ANY($2::text[]) AND timestamp IS NOT NULL",
            mutation.document_id,
            ["user", "assistant"],
        )
    else:
        activity_at = max(
            (
                value
                for value in (
                    previous_activity,
                    *(row.timestamp for row in rows if row.role in {"user", "assistant"}),
                )
                if value is not None
            ),
            default=None,
        )
    activity_advanced = activity_at is not None and (
        previous_activity is None or activity_at > previous_activity
    )
    project_activity_changed = (
        mutation.mode == "full"
        or activity_advanced
        or mutation.interactions_changed
    )
    mutation.delivery_values["activity_at"] = activity_at
    document = _document_view(
        document_id=mutation.document_id, tool_id=tool_id, category=category,
        relative_path=relative_path, machine_id=machine_id, metadata=mutation.delivery_values["metadata"],
        title=mutation.document_values["title"], project_id=mutation.document_values["project_id"],
        revision_hash=content_hash, file_size_bytes=mutation.delivery_values["file_size_bytes"],
        source_modified_at=mutation.delivery_values["source_modified_at"], activity_at=activity_at,
        synced_at=mutation.delivery_values["synced_at"], visibility=mutation.document_values["visibility"],
    )
    await _upsert_usage(connection, mutation.usage_rows, accumulate=tool_id == "codex")
    _read, _dashboard, dashboard_changed = await _refresh_projections(
        connection, state=state, mutation=mutation, document=document, rows=rows, dirty=dirty, user_id=user_id,
    )
    await connection.execute(
        """
        WITH tool_write AS (
          INSERT INTO tools (id,display_name,total_files,total_size_bytes,last_sync_at)
          VALUES ($9,$9,0,0,$8)
          ON CONFLICT (id) DO UPDATE SET last_sync_at=EXCLUDED.last_sync_at
          RETURNING id
        ), document_review AS (
          UPDATE documents SET
            needs_review=needs_review OR $10,
            embedding_status=CASE WHEN $17 THEN 'pending' ELSE embedding_status END,
            embedding_attempts=CASE WHEN $17 THEN 0 ELSE embedding_attempts END,
            embedding_claim_token=CASE WHEN $17 THEN NULL ELSE embedding_claim_token END,
            embedding_claimed_at=CASE WHEN $17 THEN NULL ELSE embedding_claimed_at END,
            embedding_content_hash=CASE WHEN $17 THEN NULL ELSE embedding_content_hash END
          WHERE id=$1 AND ($10 OR $17)
          RETURNING id
        ), project_activity AS (
          UPDATE projects SET updated_at=now()
          WHERE id=$2 AND $16
          RETURNING id
        ), delivery_write AS (
        INSERT INTO document_delivery_state
          (document_id,project_id,revision_hash,file_size_bytes,delivery_metadata,source_modified_at,activity_at,synced_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (document_id) DO UPDATE SET
          project_id=EXCLUDED.project_id, revision_hash=EXCLUDED.revision_hash,
          file_size_bytes=EXCLUDED.file_size_bytes, delivery_metadata=EXCLUDED.delivery_metadata,
          source_modified_at=EXCLUDED.source_modified_at, activity_at=EXCLUDED.activity_at,
          synced_at=EXCLUDED.synced_at
        RETURNING document_id
        )
        INSERT INTO sync_state (machine_id,tool_id,relative_path,last_hash,last_offset,last_synced_at)
        VALUES ($11,$9,$12,$13,$14,$15)
        ON CONFLICT (machine_id,tool_id,relative_path) DO UPDATE SET
          last_hash=EXCLUDED.last_hash,
          last_offset=EXCLUDED.last_offset,
          last_synced_at=EXCLUDED.last_synced_at
        """,
        mutation.document_id, mutation.document_values["project_id"], content_hash,
        mutation.delivery_values["file_size_bytes"], mutation.delivery_values["metadata"],
        mutation.delivery_values["source_modified_at"], activity_at, mutation.delivery_values["synced_at"],
        tool_id, mutation.document_values["needs_review"], machine_id, relative_path,
        mutation.sync_values["last_hash"], mutation.sync_values["last_offset"],
        mutation.sync_values["last_synced_at"], project_activity_changed,
        bool(mutation.search_text),
    )
    from .ingest_service import _conversation_event_changes
    changes = _conversation_event_changes(
        mode=mutation.mode, search_text=mutation.search_text,
        title_changed=mutation.title_changed,
        interactions_changed=mutation.interactions_changed,
        dashboard_changed=dashboard_changed,
    )
    if mutation.document_values["project_id"] is None:
        changes = [change for change in changes if change != "project"]
    if settings.realtime_ingest_deferred_projections:
        from .realtime_ingest_projector import enqueue_projection_candidates_raw

        await enqueue_projection_candidates_raw(
            connection,
            document_id=mutation.document_id,
            revision_hash=content_hash,
            canvas=mutation.canvas_candidate,
            search=mutation.search_candidate,
        )
    return RawDocument(
        mutation.document_id,
        file_size_bytes=mutation.delivery_values["file_size_bytes"],
    ), {
        "event_type": "file_synced", "user_id": str(user_id) if user_id else None,
        # The legacy reconciler republishes a delegate's hierarchy metadata on
        # every advancing revision. Preserve that cache invalidation when a
        # later DELTA is safely handled by the raw writer.
        "claw_delegate_metadata": (
            str(
                mutation.delivery_values["metadata"].get("orchestration")
                or ""
            ).strip()
            == "claw"
        ),
        "cache": {
            "daily": mutation.mode == "full" or activity_advanced,
            "project": project_activity_changed,
            "project_id": mutation.document_values["project_id"],
        },
        "data": {"document_id": str(mutation.document_id), "tool_id": tool_id,
                 "category": category, "relative_path": relative_path,
                 "title": mutation.document_values["title"],
                 "project_id": str(mutation.document_values["project_id"]) if mutation.document_values["project_id"] else None,
                 "changes": changes},
    }


async def _ensure_supported_identity_path(
    connection: asyncpg.Connection,
    *,
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_id: str,
    relative_path: str,
    source_identity: str | None,
) -> None:
    """Reject relocations/aliases before the raw writer mutates anything."""
    if source_identity is None:
        return
    rows = await connection.fetch(
        """
        SELECT d.id, d.relative_path
        FROM documents AS d
        LEFT JOIN document_delivery_state AS delivery
          ON delivery.document_id = d.id
        WHERE d.tool_id = $1 AND d.category = 'conversation'
          AND d.machine_id = $2
          AND EXISTS (
              SELECT 1 FROM machines AS owner_machine
              WHERE owner_machine.id = d.machine_id
                AND owner_machine.user_id = $3
          )
          AND coalesce(delivery.delivery_metadata, d.metadata, '{}'::jsonb)
                ->> 'session_id' = $4
          AND ($1 <> 'codex' OR
               coalesce(delivery.delivery_metadata, d.metadata, '{}'::jsonb)
                 ->> 'thread_id' = $4)
        FOR UPDATE OF d
        """,
        tool_id,
        machine_id,
        user_id,
        source_identity,
    )
    if len(rows) > 1 or (
        rows and str(rows[0]["relative_path"]) != relative_path
    ):
        raise RawWriterUnsupported(
            "stable-identity relocation/alias selection needs the legacy reducer"
        )


async def ingest_conversation_raw(
    *, tool_id: str, category: str, content_type: str, relative_path: str,
    content: str, content_hash: str, file_size: int, mode: str, offset: int,
    metadata: dict[str, Any], timestamp: float | None, machine_id: str | uuid.UUID | None,
    user_id: str | uuid.UUID | None, base_hash: str | None, base_offset: int | None,
    authoritative_rebase: bool = False, database_url: str | None = None,
    simulate_ambiguous_commit: bool = False,
    content_already_sanitized: bool = False,
    content_had_sensitive: bool = False,
) -> tuple[RawDocument, dict[str, Any] | None]:
    """Run one fully raw, atomic writer transaction and return its SSE payload."""
    machine = _uuid(machine_id)
    owner = _uuid(user_id)
    if machine is None or owner is None:
        raise RawWriterUnsupported(
            "raw conversation ingest requires an authenticated owner and machine"
        )
    if category != "conversation" or content_type != "jsonl":
        raise RawWriterUnsupported("raw writer is limited to conversation JSONL")
    metadata = dict(metadata or {})
    from .ingest_service import (
        CURSOR_PROJECTION_ORDER_KEY,
        _claude_subagent_pair_transcript_path,
        _resanitize,
        _source_lock_id,
    )
    if metadata.get(CURSOR_PROJECTION_ORDER_KEY) is not None:
        raise RawWriterUnsupported(
            "Cursor projection reordering needs the legacy reducer"
        )
    if _claude_subagent_pair_transcript_path(relative_path, category) is not None and (
        not settings.realtime_ingest_raw_subagent_transcripts or mode != "delta"
    ):
        raise RawWriterUnsupported(
            "Claude transcript/sidecar pairing needs the legacy reducer"
        )
    content = content.replace("\x00", "")
    if content_already_sanitized:
        had_sensitive = content_had_sensitive
    else:
        content, detected_sensitive = _resanitize(content)
        had_sensitive = content_had_sensitive or detected_sensitive
    if tool_id == "codex" and content:
        from .conversation_parser import extract_codex_session_metadata

        metadata.update(extract_codex_session_metadata(content))
    if tool_id in {"cursor", "claude_code"}:
        from .conversation_hierarchy import path_linked_subagent_identity

        for key, value in path_linked_subagent_identity(relative_path).items():
            metadata.setdefault(key, value)
    from .conversation_identity import conversation_session_id

    stable_source_identity = conversation_session_id(tool_id, category, metadata)
    cursor_state_delta = (
        mode == "delta"
        and tool_id == "cursor"
        and metadata.get("source") == "cursor_state_v1"
    )
    try:
        pool = await _pool(database_url)
    except Exception as exc:
        raise RawWriterFailure(f"raw pool unavailable before transaction: {exc}") from exc

    commit_started = False
    commit_error: Exception | None = None
    result: RawDocument
    event: dict[str, Any] | None
    mutation: IngestMutation
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            try:
                await transaction.start()
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1::bigint)",
                    _source_lock_id(
                        str(machine),
                        str(owner),
                        tool_id,
                        relative_path,
                        source_identity=stable_source_identity,
                    ),
                )
                await _ensure_supported_identity_path(
                    connection,
                    machine_id=machine,
                    user_id=owner,
                    tool_id=tool_id,
                    relative_path=relative_path,
                    source_identity=stable_source_identity,
                )
                state = await _load_state(
                    connection,
                    machine_id=machine,
                    user_id=owner,
                    tool_id=tool_id,
                    relative_path=relative_path,
                    cursor_state_delta=cursor_state_delta,
                    load_recovered_history=bool(
                        metadata.get("user_history")
                        or metadata.get("first_user_message")
                    ),
                )
                mutation = reduce_writer_state(
                    state, tool_id=tool_id, category=category, content_type=content_type,
                    relative_path=relative_path, content=content, content_hash=content_hash,
                    file_size=file_size, mode=mode, offset=offset, metadata=metadata,
                    timestamp=timestamp, machine_id=machine, user_id=owner, base_hash=base_hash,
                    base_offset=base_offset, authoritative_rebase=authoritative_rebase,
                    had_sensitive=had_sensitive,
                )
                result, event = await _apply(
                    connection, state=state, mutation=mutation, tool_id=tool_id,
                    category=category, content_type=content_type, relative_path=relative_path,
                    content_hash=content_hash, machine_id=machine, user_id=owner,
                    content=content,
                )
            except Exception:
                try:
                    await transaction.rollback()
                except Exception:
                    pass
                raise

            commit_started = True
            try:
                await transaction.commit()
            except Exception as exc:
                commit_error = exc
    except (RawWriterUnsupported, RawWriterFailure):
        raise
    except Exception as exc:
        from .ingest_service import DeltaBaseMismatch

        if isinstance(exc, DeltaBaseMismatch):
            raise
        if commit_started:
            commit_error = commit_error or exc
        else:
            raise RawWriterFailure(
                f"raw transaction failed before commit: {exc}"
            ) from exc

    if commit_error is not None or simulate_ambiguous_commit:
        try:
            converged = await _commit_converged(
                database_url=database_url,
                document_id=result.id,
                machine_id=machine,
                tool_id=tool_id,
                relative_path=relative_path,
                content_hash=content_hash,
                offset=offset,
            )
        except Exception as exc:
            raise RawWriterCommitUncertain(
                "raw COMMIT outcome could not be reread; retry the raw delivery"
            ) from exc
        if converged:
            return result, event
        raise RawWriterCommitUncertain(
            "raw COMMIT was attempted but its revision fence did not converge"
        ) from commit_error
    return result, event


async def ingest_conversation_raw_chain(
    *,
    frames: list[dict[str, Any]],
    database_url: str | None = None,
    simulate_ambiguous_commit: bool = False,
) -> tuple[RawDocument, dict[str, Any] | None]:
    """Commit an ordered guarded DELTA chain in one raw asyncpg transaction.

    The reducer intentionally runs once per constituent envelope.  This keeps
    frame metadata and the existing stateful tool semantics ordered exactly as
    the synchronous writer, while the advisory lock, PostgreSQL transaction,
    and post-commit SSE are amortized over the chain.
    """
    if not frames:
        raise RawWriterUnsupported("raw chain requires at least one frame")
    first = dict(frames[0])
    machine = _uuid(first.get("machine_id"))
    owner = _uuid(first.get("user_id"))
    if machine is None or owner is None:
        raise RawWriterUnsupported(
            "raw conversation ingest requires an authenticated owner and machine"
        )
    tool_id = str(first.get("tool_id") or "")
    category = str(first.get("category") or "")
    content_type = str(first.get("content_type") or "")
    relative_path = str(first.get("relative_path") or "")
    if category != "conversation" or content_type != "jsonl":
        raise RawWriterUnsupported("raw writer is limited to conversation JSONL")

    from .ingest_service import (
        CURSOR_PROJECTION_ORDER_KEY,
        _claude_subagent_pair_transcript_path,
        _resanitize,
        _source_lock_id,
    )
    from .conversation_identity import conversation_session_id

    prepared: list[dict[str, Any]] = []
    stable_source_identity: str | None = None
    for source_frame in frames:
        frame = dict(source_frame)
        if (
            str(frame.get("tool_id") or "") != tool_id
            or str(frame.get("category") or "") != category
            or str(frame.get("content_type") or "") != content_type
            or str(frame.get("relative_path") or "") != relative_path
            or _uuid(frame.get("machine_id")) != machine
            or _uuid(frame.get("user_id")) != owner
            or frame.get("mode") != "delta"
        ):
            raise RawWriterUnsupported("raw chain has mixed source identities")
        metadata = dict(frame.get("metadata") or {})
        if metadata.get(CURSOR_PROJECTION_ORDER_KEY) is not None:
            raise RawWriterUnsupported(
                "Cursor projection reordering needs the legacy reducer"
            )
        if (
            not settings.realtime_ingest_raw_subagent_transcripts
            and _claude_subagent_pair_transcript_path(relative_path, category) is not None
        ):
            raise RawWriterUnsupported(
                "Claude transcript/sidecar pairing needs the legacy reducer"
            )
        content = str(frame.get("content") or "").replace("\x00", "")
        if frame.get("content_already_sanitized"):
            had_sensitive = bool(frame.get("content_had_sensitive"))
        else:
            content, detected_sensitive = _resanitize(content)
            had_sensitive = bool(frame.get("content_had_sensitive")) or detected_sensitive
        if tool_id == "codex" and content:
            from .conversation_parser import extract_codex_session_metadata

            metadata.update(extract_codex_session_metadata(content))
        if tool_id in {"cursor", "claude_code"}:
            from .conversation_hierarchy import path_linked_subagent_identity

            for key, value in path_linked_subagent_identity(relative_path).items():
                metadata.setdefault(key, value)
        identity = conversation_session_id(tool_id, category, metadata)
        if stable_source_identity is None:
            stable_source_identity = identity
        elif identity != stable_source_identity:
            raise RawWriterUnsupported("raw chain changes stable source identity")
        frame.update(content=content, metadata=metadata, had_sensitive=had_sensitive)
        prepared.append(frame)

    cursor_state_delta = (
        tool_id == "cursor" and prepared[0]["metadata"].get("source") == "cursor_state_v1"
    )
    load_recovered_history = any(
        frame["metadata"].get("user_history")
        or frame["metadata"].get("first_user_message")
        for frame in prepared
    )
    # Most collector DELTAs in one quiet window have identical source
    # metadata.  They are plain append JSONL, so one reducer/application is
    # semantically equivalent to applying each envelope in order while it
    # removes repeated scalar loads, stage transfers, and projection writes.
    # Metadata-bearing shapes deliberately retain the sequential path below:
    # a later frame must never erase a prior frame's distinct metadata.
    reducer_frames = prepared

    def combined_frame(source_frames: list[dict[str, Any]]) -> dict[str, Any]:
        combined_content = ""
        for frame in source_frames:
            content = frame["content"]
            if (
                combined_content
                and content
                and not combined_content.endswith("\n")
                and not content.startswith("\n")
            ):
                combined_content += "\n"
            combined_content += content
        combined = dict(source_frames[-1])
        combined.update(
            content=combined_content,
            file_size=sum(int(frame["file_size"]) for frame in source_frames),
            base_hash=source_frames[0].get("base_hash"),
            base_offset=source_frames[0].get("base_offset"),
            had_sensitive=any(bool(frame["had_sensitive"]) for frame in source_frames),
        )
        return combined

    can_combine = not load_recovered_history and all(
        frame["metadata"] == prepared[0]["metadata"] for frame in prepared[1:]
    )
    if can_combine:
        reducer_frames = [combined_frame(prepared)]
    try:
        pool = await _pool(database_url)
    except Exception as exc:
        raise RawWriterFailure(f"raw pool unavailable before transaction: {exc}") from exc

    commit_started = False
    commit_error: Exception | None = None
    result: RawDocument | None = None
    events: list[dict[str, Any]] = []
    last_state: WriterState | None = None
    try:
        async with pool.acquire() as connection:
            transaction = connection.transaction()
            try:
                await transaction.start()
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1::bigint)",
                    _source_lock_id(
                        str(machine), str(owner), tool_id, relative_path,
                        source_identity=stable_source_identity,
                    ),
                )
                await _ensure_supported_identity_path(
                    connection,
                    machine_id=machine,
                    user_id=owner,
                    tool_id=tool_id,
                    relative_path=relative_path,
                    source_identity=stable_source_identity,
                )
                preloaded_state: WriterState | None = None
                if can_combine and len(prepared) > 1:
                    initial_state = await _load_state(
                        connection,
                        machine_id=machine,
                        user_id=owner,
                        tool_id=tool_id,
                        relative_path=relative_path,
                        cursor_state_delta=cursor_state_delta,
                        load_recovered_history=load_recovered_history,
                    )
                    preloaded_state = initial_state
                    current_hash = (
                        (initial_state.delivery or {}).get("revision_hash")
                        or (initial_state.document or {}).get("content_hash")
                    )
                    current_offset = (
                        int(initial_state.sync.get("last_offset") or 0)
                        if initial_state.sync
                        and initial_state.sync.get("last_hash") == current_hash
                        else 0
                    )
                    # A crash can commit the head before removing its marker;
                    # successors admitted during that window must resume after
                    # the already-committed prefix, not fail the combined base.
                    for index, frame in enumerate(prepared[:-1]):
                        if (
                            str(frame["content_hash"]) == current_hash
                            and int(frame["offset"]) == current_offset
                        ):
                            reducer_frames = [combined_frame(prepared[index + 1 :])]
                            break
                for frame in reducer_frames:
                    # Re-read scalar state inside the still-open transaction.
                    # PostgreSQL exposes our own writes, preserving exact
                    # one-by-one reducer semantics without ORM hydration.
                    if preloaded_state is not None:
                        state = preloaded_state
                        preloaded_state = None
                    else:
                        state = await _load_state(
                            connection,
                            machine_id=machine,
                            user_id=owner,
                            tool_id=tool_id,
                            relative_path=relative_path,
                            cursor_state_delta=cursor_state_delta,
                            load_recovered_history=load_recovered_history,
                        )
                    last_state = state
                    mutation = reduce_writer_state(
                        state,
                        tool_id=tool_id,
                        category=category,
                        content_type=content_type,
                        relative_path=relative_path,
                        content=frame["content"],
                        content_hash=str(frame["content_hash"]),
                        file_size=int(frame["file_size"]),
                        mode="delta",
                        offset=int(frame["offset"]),
                        metadata=frame["metadata"],
                        timestamp=frame.get("timestamp"),
                        machine_id=machine,
                        user_id=owner,
                        base_hash=frame.get("base_hash"),
                        base_offset=frame.get("base_offset"),
                        authoritative_rebase=False,
                        had_sensitive=bool(frame["had_sensitive"]),
                    )
                    result, event = await _apply(
                        connection,
                        state=state,
                        mutation=mutation,
                        tool_id=tool_id,
                        category=category,
                        content_type=content_type,
                        relative_path=relative_path,
                        content_hash=str(frame["content_hash"]),
                        machine_id=machine,
                        user_id=owner,
                        content=frame["content"],
                    )
                    if event is not None:
                        events.append(event)
            except Exception:
                try:
                    await transaction.rollback()
                except Exception:
                    pass
                raise
            commit_started = True
            try:
                await transaction.commit()
            except Exception as exc:
                commit_error = exc
    except (RawWriterUnsupported, RawWriterFailure):
        raise
    except Exception as exc:
        from .ingest_service import DeltaBaseMismatch

        if isinstance(exc, DeltaBaseMismatch):
            raise
        if commit_started:
            commit_error = commit_error or exc
        else:
            raise RawWriterFailure(
                f"raw chain transaction failed before commit: {exc}"
            ) from exc

    if result is None:
        raise RawWriterFailure("raw chain completed without a result")
    final = prepared[-1]
    if commit_error is not None or simulate_ambiguous_commit:
        try:
            converged = await _commit_converged(
                database_url=database_url,
                document_id=result.id,
                machine_id=machine,
                tool_id=tool_id,
                relative_path=relative_path,
                content_hash=str(final["content_hash"]),
                offset=int(final["offset"]),
            )
        except Exception as exc:
            raise RawWriterCommitUncertain(
                "raw chain COMMIT outcome could not be reread; retry delivery"
            ) from exc
        if not converged:
            raise RawWriterCommitUncertain(
                "raw chain COMMIT was attempted but revision fence did not converge"
            ) from commit_error

    if not events and result.disposition == "idempotent" and last_state is not None:
        document = last_state.document or {}
        project_id = document.get("project_id")
        changes = {
            "conversation.messages",
            "conversation.metadata",
            "conversation.pending_interactions",
            "conversation.prompts",
            "conversation.search",
            "dashboard",
            "project",
        }
        if project_id is None:
            changes.discard("project")
        return result, {
            "event_type": "file_synced",
            "user_id": str(owner),
            "cache": {"daily": False, "project": False, "project_id": project_id},
            "data": {
                "document_id": str(result.id),
                "tool_id": tool_id,
                "category": category,
                "relative_path": relative_path,
                "title": document.get("title"),
                "project_id": str(project_id) if project_id else None,
                "changes": sorted(changes),
            },
        }
    if not events:
        return result, None
    event = dict(events[-1])
    changes: set[str] = set()
    daily = False
    project = False
    for candidate in events:
        data = candidate.get("data")
        if isinstance(data, dict):
            changes.update(str(value) for value in data.get("changes", []))
        cache = candidate.get("cache")
        if isinstance(cache, dict):
            daily = daily or bool(cache.get("daily"))
            project = project or bool(cache.get("project"))
    data = dict(event.get("data") or {})
    data["changes"] = sorted(changes)
    event["data"] = data
    cache = dict(event.get("cache") or {})
    cache["daily"] = daily
    cache["project"] = project
    event["cache"] = cache
    return result, event
