"""Ingest service — processes incoming files from the collector."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import orjson

from sqlalchemy import delete, func, inspect, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..config import settings
from ..db.models import (
    ConversationMessage,
    ConversationUsageEvent,
    DashboardDocumentProjection,
    Document,
    DocumentVersion,
    Machine,
    Project,
    SyncState,
    Tool,
)
from ..tool_catalog import tool_display_name
from .claude_lineage import EXACT_PERMISSION_RESPONSE_BACKFILL
from .conversation_identity import (
    conversation_session_id,
    select_canonical_conversation_document,
    should_relocate_conversation_document,
)
from .conversation_stream import ConversationFileSource
from .conversation_usage import (
    LAST_ACTIVITY_AT_METADATA_KEY,
    STARTED_AT_METADATA_KEY,
    TOKEN_USAGE_METADATA_KEY,
    USAGE_SEGMENT_METADATA_KEY,
    normalize_token_usage,
    subtract_token_usage,
    token_usage_from_metadata,
    usage_observation_values,
)
from .document_delivery import (
    attach_document_delivery,
    delivery_file_size_expression,
    delivery_metadata_expression,
    delivery_revision_expression,
    document_metadata,
    ensure_document_delivery_state,
    outerjoin_document_delivery,
    store_document_metadata,
    update_document_delivery,
    update_document_source_modified,
)
from .history_recovery import (
    UserOccurrence,
    partition_recovered_occurrences,
    recovered_occurrence_anchors,
)
from .ingest_revision import bounded_source_timestamp, committed_full_supersedes
from .large_content_store import document_content, finalize_document_content
from .subagent_lifecycle import (
    SUBAGENT_LIFECYCLE_AT_KEY,
    SUBAGENT_LIFECYCLE_EVIDENCE_KEY,
    SUBAGENT_LIFECYCLE_SOURCE_KEY,
    SUBAGENT_LIFECYCLE_STATUS_KEY,
    child_lifecycle_evidence,
    child_lifecycle_evidence_from_objects,
    lifecycle_event_identity,
    merge_duplicate_lifecycle_events,
    reconcile_child_lifecycle_metadata,
)

# Set of background tasks — prevents GC from collecting them before completion
_background_tasks: set = set()
# Cap concurrent post-ingest work (each holds a DB connection + a BGE-M3 slot
# for ~10s). Without this, a re-sync storm exhausts the connection pool and
# user web requests time out.
_post_ingest_semaphore: asyncio.Semaphore | None = None
# Cap concurrent ingest endpoint handlers: each holds a main-pool connection
# for the entire write transaction (documents + conversation_messages +
# tsvector update). 16 leaves headroom in the 32-slot main pool for login,
# dashboard, search, etc. — collector storms can't starve the web UI.
_ingest_semaphore: asyncio.Semaphore | None = None
# Compatibility sentinel for callers that used to monkeypatch prefix-SCAN
# invalidation. No production path calls it.
_invalidate_ingest_read_caches = None


def _get_post_ingest_semaphore() -> asyncio.Semaphore:
    global _post_ingest_semaphore
    if _post_ingest_semaphore is None:
        import asyncio as _asyncio

        _post_ingest_semaphore = _asyncio.Semaphore(8)
    return _post_ingest_semaphore


def _get_ingest_semaphore() -> asyncio.Semaphore:
    global _ingest_semaphore
    if _ingest_semaphore is None:
        import asyncio as _asyncio

        _ingest_semaphore = _asyncio.Semaphore(24)
    return _ingest_semaphore


MAX_STORED_MESSAGE_CHARS = 256 * 1024
MAX_STORED_AUXILIARY_CHARS = 128 * 1024
MAX_STORED_TOOL_NAME_CHARS = 256
MAX_STORED_IDENTITY_CHARS = 128
MAX_MESSAGE_BATCH_CHARS = 4 * 1024 * 1024
MAX_SEARCH_TEXT_CHARS = 200 * 1024
MAX_DOCUMENT_METADATA_BYTES = 256 * 1024
MAX_METADATA_STRING_CHARS = 16 * 1024
MAX_USER_HISTORY_ENTRIES = 2_000
MAX_USER_HISTORY_BYTES = 4 * 1024 * 1024
MAX_CURSOR_PROJECTION_BASELINE_RECORDS = 100_000
MAX_CURSOR_PROJECTION_INSERTION_TAIL_RECORDS = 32
MAX_CURSOR_PROJECTION_INSERTION_GROUPS = 16
MAX_CURSOR_PROJECTION_INSERTED_RECORDS = 64
CURSOR_PROJECTION_ORDER_KEY = "_cursor_projection_order_v1"
STORED_SOURCE_REVISION_KEY = "_stored_source_revision_hash"
STORED_SOURCE_HASH_KEY = "_stored_source_hash"
STORED_SOURCE_SIZE_KEY = "_stored_source_size"
CURRENT_ASSISTANT_MODEL_KEY = "_assistant_model"
CURRENT_ASSISTANT_REASONING_KEY = "_assistant_reasoning_effort"
CURRENT_ASSISTANT_SERVICE_TIER_KEY = "_assistant_service_tier"
CURRENT_ASSISTANT_MODE_KEY = "_assistant_agent_mode"
CURRENT_PENDING_QUESTIONS_KEY = "_pending_question_ids"
PENDING_QUESTION_COUNT_KEY = "pending_question_count"
LIVE_INTERACTION_SIGNALS_KEY = "_live_interaction_signals"
LIVE_SHELL_ACTIVITIES_KEY = "_live_shell_activities"
INTERACTION_HISTORY_KEY = "_interaction_history"
LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY = "_latest_meaningful_human_timestamp"
PENDING_QUESTION_RECONCILIATION_VERSION_KEY = (
    "_pending_question_reconciliation_version"
)
PENDING_QUESTION_RECONCILIATION_VERSION = 4


@dataclass(frozen=True, slots=True)
class _StagedConversationMessage:
    """Plain compatibility value for projections that need a generated ID.

    Phase 1 deliberately keeps Canvas reconciliation in the existing session
    flow.  Core inserts return only the identifiers it needs, so this adapter
    avoids recreating mapped ``ConversationMessage`` instances merely to call
    that unchanged compatibility projector.
    """

    id: int
    document_id: uuid.UUID
    line_number: int
    role: str | None
    content: str
    metadata_: dict

_ESSENTIAL_METADATA_KEYS = {
    "agent_depth",
    "agent_id",
    "agent_launch_description",
    "agent_launch_metadata_source",
    "agent_launch_metadata_version",
    "agent_nickname",
    "agent_path",
    "agent_tool_use_id",
    "agent_type",
    "subagent_model",
    "subagent_model_family",
    "subagent_reasoning_effort",
    "cascade_id",
    "codex_title_revision",
    "codex_title_revisions",
    CURRENT_ASSISTANT_MODEL_KEY,
    CURRENT_ASSISTANT_MODE_KEY,
    CURRENT_ASSISTANT_REASONING_KEY,
    CURRENT_ASSISTANT_SERVICE_TIER_KEY,
    STARTED_AT_METADATA_KEY,
    LAST_ACTIVITY_AT_METADATA_KEY,
    USAGE_SEGMENT_METADATA_KEY,
    CURRENT_PENDING_QUESTIONS_KEY,
    INTERACTION_HISTORY_KEY,
    LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY,
    LIVE_INTERACTION_SIGNALS_KEY,
    PENDING_QUESTION_RECONCILIATION_VERSION_KEY,
    "cwd",
    "briefing_kind",
    "briefing_session_id",
    "first_user_message",
    "forked_from_id",
    "is_subagent",
    "model",
    "memento_title_source",
    "parent_thread_id",
    PENDING_QUESTION_COUNT_KEY,
    "project_hash",
    "project_path",
    "root_session_id",
    "session_id",
    "source",
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    "thread_id",
    "thread_source",
    "title",
    "title_is_manual",
    "title_source",
}

_EMBEDDING_CATEGORIES = {"conversation", "memory", "learning", "plan", "identity"}
_PROTECTED_DOCUMENT_METADATA_KEYS = {
    "agent_launch_description",
    "agent_launch_metadata_source",
    "agent_launch_metadata_version",
    "agent_tool_use_id",
    "agent_type",
    "subagent_model",
    "subagent_model_family",
    "subagent_reasoning_effort",
    SUBAGENT_LIFECYCLE_STATUS_KEY,
    SUBAGENT_LIFECYCLE_SOURCE_KEY,
    SUBAGENT_LIFECYCLE_AT_KEY,
    SUBAGENT_LIFECYCLE_EVIDENCE_KEY,
    "codex_title_revision",
    "codex_title_revisions",
    "memento_title_source",
    CURRENT_ASSISTANT_MODEL_KEY,
    CURRENT_ASSISTANT_REASONING_KEY,
    CURRENT_ASSISTANT_SERVICE_TIER_KEY,
    STARTED_AT_METADATA_KEY,
    LAST_ACTIVITY_AT_METADATA_KEY,
    USAGE_SEGMENT_METADATA_KEY,
    INTERACTION_HISTORY_KEY,
    LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY,
    LIVE_INTERACTION_SIGNALS_KEY,
    PENDING_QUESTION_RECONCILIATION_VERSION_KEY,
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    "title_is_manual",
    "title_source",
}
_CLAUDE_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CLAUDE_AGENT_SIDECAR_SOURCE = "claude_subagent_sidecar"
_CLAUDE_AGENT_SIDECAR_VERSION = 1
_CLAUDE_AGENT_DESCRIPTION_MAX_CHARS = 1_024
_CLAUDE_AGENT_TOOL_USE_ID_MAX_CHARS = 256
_CLAUDE_AGENT_TYPE_MAX_CHARS = 128


def normalize_ingest_category(
    tool_id: str,
    category: str,
    relative_path: str,
) -> str:
    """Correct legacy collector classifications at the trust boundary."""
    normalized_path = (relative_path or "").replace("\\", "/").lower()
    if (
        tool_id == "claude_code"
        and category == "conversation"
        and "/subagents/" in f"/{normalized_path.lstrip('/')}"
        and normalized_path.endswith(".meta.json")
    ):
        return "state"
    return category


def _normalized_claude_relative_path(relative_path: str) -> str | None:
    """Normalize separators without allowing a sibling lookup to escape its path."""
    raw = str(relative_path or "").replace("\\", "/")
    if not raw or raw.startswith("/") or "\x00" in raw:
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) or None


def _claude_subagent_file_identity(
    relative_path: str,
    *,
    sidecar: bool,
) -> tuple[str, str] | None:
    """Return the normalized path and filename-backed Claude agent ID."""
    normalized = _normalized_claude_relative_path(relative_path)
    if normalized is None or "/subagents/" not in f"/{normalized}":
        return None
    filename = normalized.rsplit("/", 1)[-1]
    suffix = r"\.meta\.json" if sidecar else r"\.jsonl"
    match = re.fullmatch(rf"agent-([A-Za-z0-9][A-Za-z0-9_-]{{0,127}}){suffix}", filename)
    if match is None:
        return None
    return normalized, match.group(1)


def _bounded_claude_sidecar_value(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value.strip()
    if not bounded or any(ord(char) < 32 for char in bounded):
        return None
    return bounded[:limit]


def _claude_subagent_sidecar_evidence(
    relative_path: str,
    content: str | None,
) -> tuple[str, dict[str, object]] | None:
    """Validate one sidecar and return its exact sibling transcript metadata."""
    identity = _claude_subagent_file_identity(relative_path, sidecar=True)
    if identity is None or not content:
        return None
    sidecar_path, filename_agent_id = identity
    try:
        payload = orjson.loads(content)
    except (TypeError, orjson.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload_agent_id = payload.get("agentId")
    if payload_agent_id is not None and (
        not isinstance(payload_agent_id, str)
        or _CLAUDE_AGENT_ID_RE.fullmatch(payload_agent_id) is None
        or payload_agent_id != filename_agent_id
    ):
        return None
    # Current Claude sidecars identify the child only in their filename; older
    # variants also repeated ``agentId`` in the payload.  The validated exact
    # sibling path is authoritative in both formats, while a contradictory
    # payload identity remains grounds for rejection.
    agent_id = filename_agent_id

    metadata: dict[str, object] = {
        "agent_id": agent_id,
        "agent_launch_metadata_source": _CLAUDE_AGENT_SIDECAR_SOURCE,
        "agent_launch_metadata_version": _CLAUDE_AGENT_SIDECAR_VERSION,
    }
    for source_key, target_key, limit in (
        (
            "description",
            "agent_launch_description",
            _CLAUDE_AGENT_DESCRIPTION_MAX_CHARS,
        ),
        ("toolUseId", "agent_tool_use_id", _CLAUDE_AGENT_TOOL_USE_ID_MAX_CHARS),
        ("agentType", "agent_type", _CLAUDE_AGENT_TYPE_MAX_CHARS),
    ):
        value = _bounded_claude_sidecar_value(payload.get(source_key), limit)
        if value is not None:
            metadata[target_key] = value

    transcript_path = f"{sidecar_path[:-len('.meta.json')]}.jsonl"
    return transcript_path, metadata


def _claude_subagent_sidecar_path(relative_path: str) -> str | None:
    identity = _claude_subagent_file_identity(relative_path, sidecar=False)
    if identity is None:
        return None
    transcript_path, _ = identity
    return f"{transcript_path[:-len('.jsonl')]}.meta.json"


def _claude_subagent_pair_transcript_path(
    relative_path: str,
    category: str,
) -> str | None:
    if category == "conversation":
        identity = _claude_subagent_file_identity(relative_path, sidecar=False)
        return identity[0] if identity is not None else None
    if category == "state":
        identity = _claude_subagent_file_identity(relative_path, sidecar=True)
        if identity is not None:
            return f"{identity[0][:-len('.meta.json')]}.jsonl"
    return None


def _conversation_search_index_needs_refresh(
    *,
    is_new_document: bool,
    mode: str,
    new_search_text: str,
    previous_title: str | None,
    current_title: str | None,
) -> bool:
    """Return whether this ingest changed indexed conversation text."""
    return (
        is_new_document
        or mode != "delta"
        or bool(new_search_text)
        or previous_title != current_title
    )


def _ingest_cache_scope(
    *,
    category: str,
    mode: str,
    activity_advanced: bool,
    title_changed: bool,
    lifecycle_changed: bool,
) -> tuple[bool, bool]:
    """Return daily/project-conversation invalidations for visible changes."""
    is_conversation = category == "conversation"
    daily_changed = is_conversation and (activity_advanced or mode == "full")
    project_changed = is_conversation and (
        activity_advanced
        or mode == "full"
        or title_changed
        or lifecycle_changed
    )
    return daily_changed, project_changed


async def _record_tool_sync(
    db: AsyncSession,
    tool: Tool,
    synced_at: datetime,
    *,
    is_new_document: bool,
) -> None:
    """Update hot-path tool stats without recounting every document."""
    tool.last_sync_at = synced_at
    if is_new_document:
        await db.execute(
            update(Tool)
            .where(Tool.id == tool.id)
            .values(total_files=func.coalesce(Tool.total_files, 0) + 1)
        )


class DeltaBaseMismatch(RuntimeError):
    """A guarded append does not extend the server's committed revision."""

    def __init__(
        self,
        *,
        expected_hash: str | None,
        expected_offset: int,
    ) -> None:
        super().__init__("delta base does not match committed source revision")
        self.expected_hash = expected_hash
        self.expected_offset = expected_offset


class CursorProjectionOrderMismatch(DeltaBaseMismatch):
    """A sparse ordering hint cannot be applied to the committed row base."""

    def __init__(self, reason: str) -> None:
        super().__init__(expected_hash=None, expected_offset=0)
        self.args = (reason,)


def raw_realtime_writer_enabled(
    *,
    owner_id: str | None,
    device_id: str | None,
    tool_id: str,
    category: str,
) -> bool:
    """Return whether an authenticated source is in the Phase 2 canary.

    Empty selectors mean disabled.  Each selector is independently useful so
    rollout can begin with one owner, a single collector, or one tool family.
    Caller-supplied metadata never participates in this decision.
    """
    if category != "conversation":
        return False

    def selected(value: str, candidate: str | None) -> bool:
        values = {item.strip() for item in value.split(",") if item.strip()}
        return bool(candidate and candidate in values)

    return (
        selected(settings.realtime_ingest_raw_writer_owners, owner_id)
        or selected(settings.realtime_ingest_raw_writer_devices, device_id)
        or selected(settings.realtime_ingest_raw_writer_tools, tool_id)
    )


def _committed_delta_base(
    doc: Document | None,
    sync_row: SyncState | None,
) -> tuple[str | None, int]:
    """Return the source revision that is actually safe to extend.

    ``sync_state`` is a delivery cursor, while the document is the committed
    source revision.  A process crash or an older ingest bug can advance the
    cursor without committing the matching document.  Advertising that newer
    cursor forever makes every subsequent delta impossible to apply.  Prefer
    the cursor only while it describes the committed document; otherwise
    expose the document revision so a reproducible delta can resume from it or
    the collector can request an authoritative full rebase.
    """
    if doc is None or not doc.content_hash:
        return None, 0
    if sync_row is not None and sync_row.last_hash == doc.content_hash:
        return doc.content_hash, max(0, int(sync_row.last_offset or 0))
    return doc.content_hash, max(0, int(doc.file_size_bytes or 0))


async def committed_delta_base_for_source(
    db: AsyncSession,
    *,
    tool_id: str,
    relative_path: str,
    machine_id: str,
    user_id: str,
) -> tuple[str | None, int]:
    """Read the committed DELTA base advertised to a rejected spool upload."""

    statement = select(
        delivery_revision_expression(joined=True).label("content_hash"),
        delivery_file_size_expression(joined=True).label("file_size_bytes"),
    ).select_from(Document).where(
        Document.tool_id == tool_id,
        Document.relative_path == relative_path,
        Document.machine_id == machine_id,
        Document.machine_id.in_(
            select(Machine.id).where(Machine.user_id == user_id)
        ),
    )
    row = (await db.execute(outerjoin_document_delivery(statement))).one_or_none()
    if row is None or not isinstance(row.content_hash, str) or not row.content_hash:
        return None, 0
    sync_row = (
        await db.execute(
            _scoped_sync_state_select(
                tool_id,
                relative_path,
                machine_id,
                user_id,
            )
        )
    ).scalar_one_or_none()
    if sync_row is not None and sync_row.last_hash == row.content_hash:
        return row.content_hash, max(0, int(sync_row.last_offset or 0))
    return row.content_hash, max(0, int(row.file_size_bytes or 0))


def _logical_document_file_size(
    *,
    mode: str,
    payload_size: int,
    offset: int,
    existing_size: int = 0,
    replace_offset: bool = False,
) -> int:
    """Return total source size rather than a DELTA payload's tail size."""
    safe_payload = max(0, int(payload_size))
    if mode != "delta":
        return safe_payload
    if replace_offset:
        return max(0, int(offset))
    # Collector DELTA offsets are the cumulative source end position. Preserve
    # the existing total as a fallback for legacy senders with a zero offset.
    return max(safe_payload, max(0, int(offset)), max(0, int(existing_size)))


def _stored_source_is_current(
    doc: Document,
    revision_hash: str,
    *,
    incoming_s3_key: str | None = None,
) -> bool:
    """Return whether the persisted raw blob is complete for this revision."""
    if doc.category != "conversation":
        return True
    metadata = document_metadata(doc)
    return bool(
        # The raw compatibility TEXT is deferred on normal ingest reads. The
        # durable source proof is sufficient to establish that a snapshot is
        # present without hydrating a multi-megabyte attribute.
        (doc.content_s3_key or metadata.get(STORED_SOURCE_HASH_KEY))
        and (not incoming_s3_key or doc.content_s3_key == incoming_s3_key)
        and metadata.get(STORED_SOURCE_REVISION_KEY) == revision_hash
        and metadata.get(STORED_SOURCE_HASH_KEY)
        and metadata.get(STORED_SOURCE_SIZE_KEY) is not None
    )


def _set_stored_source_identity(
    doc: Document,
    content: str,
    *,
    revision_hash: str | None,
) -> None:
    """Record the exact sanitized blob and optional full source revision."""
    encoded = content.encode("utf-8")
    metadata = document_metadata(doc)
    metadata[STORED_SOURCE_HASH_KEY] = hashlib.sha256(encoded).hexdigest()
    metadata[STORED_SOURCE_SIZE_KEY] = len(encoded)
    if revision_hash:
        metadata[STORED_SOURCE_REVISION_KEY] = revision_hash
    else:
        metadata.pop(STORED_SOURCE_REVISION_KEY, None)
    store_document_metadata(doc, metadata)


def _set_stored_source_proof(
    doc: Document,
    *,
    source_hash: str,
    source_size: int,
    revision_hash: str | None,
) -> None:
    """Record a preverified sanitized blob without rebuilding it in memory."""
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("stored source hash must be a SHA-256 hex digest")
    if source_size < 0:
        raise ValueError("stored source size must be non-negative")
    metadata = document_metadata(doc)
    metadata[STORED_SOURCE_HASH_KEY] = source_hash
    metadata[STORED_SOURCE_SIZE_KEY] = int(source_size)
    if revision_hash:
        metadata[STORED_SOURCE_REVISION_KEY] = revision_hash
    else:
        metadata.pop(STORED_SOURCE_REVISION_KEY, None)
    store_document_metadata(doc, metadata)


def _merge_delta_metadata(existing: dict, incoming: dict) -> dict:
    """Accumulate parser statistics while preserving first-source metadata."""
    incoming = _preserve_interaction_provenance(existing, incoming)
    merged = {**existing, **incoming}
    existing_lines = existing.get("total_lines")
    incoming_lines = incoming.get("total_lines")
    if isinstance(existing_lines, int) and isinstance(incoming_lines, int):
        merged["total_lines"] = existing_lines + incoming_lines

    existing_types = existing.get("message_types")
    incoming_types = incoming.get("message_types")
    if isinstance(existing_types, dict) and isinstance(incoming_types, dict):
        combined: dict[str, int] = {}
        for source in (existing_types, incoming_types):
            for key, value in source.items():
                if isinstance(value, int):
                    combined[str(key)] = combined.get(str(key), 0) + value
        merged["message_types"] = combined

    if existing.get("first_timestamp"):
        merged["first_timestamp"] = existing["first_timestamp"]
    return merged


def _interaction_entry_id(key: object, entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    interaction = entry.get("interaction")
    if isinstance(interaction, dict) and interaction.get("id"):
        return str(interaction["id"])
    return str(key or "")


def _interaction_entries_by_id(value: object) -> dict[str, dict]:
    if isinstance(value, dict):
        candidates = value.items()
    elif isinstance(value, list):
        candidates = enumerate(value)
    else:
        return {}
    return {
        interaction_id: entry
        for key, entry in candidates
        if (interaction_id := _interaction_entry_id(key, entry))
        and isinstance(entry, dict)
    }


def _preserve_interaction_entry_provenance(
    prior: object,
    incoming: object,
) -> object:
    if not isinstance(prior, dict) or not isinstance(incoming, dict):
        return incoming
    merged = dict(incoming)
    prior_interaction = prior.get("interaction")
    incoming_interaction = incoming.get("interaction")
    prior_timestamp = str(prior.get("timestamp") or "")
    incoming_timestamp = str(incoming.get("timestamp") or "")
    same_event = (
        bool(prior_timestamp)
        and prior_timestamp == incoming_timestamp
        and isinstance(prior_interaction, dict)
        and isinstance(incoming_interaction, dict)
        and prior_interaction.get("interaction_type") == "permission_request"
        and incoming_interaction.get("interaction_type") == "permission_request"
        and bool(prior_interaction.get("requested_tool"))
        and prior_interaction.get("requested_tool")
        == incoming_interaction.get("requested_tool")
    )
    if (
        same_event
        and "interaction_origin" not in merged
        and "interaction_origin" in prior
    ):
        next_interaction = dict(incoming_interaction)
        if (
            not isinstance(next_interaction.get("tool_input"), dict)
            and isinstance(prior_interaction.get("tool_input"), dict)
        ):
            # Exact legacy backfill restores only bounded JSON mappings. Keep
            # that fingerprint input with the origin when the old collector
            # re-emits the exact same hook event without it.
            next_interaction["tool_input"] = orjson.loads(
                json.dumps(prior_interaction["tool_input"])
            )
        merged["interaction"] = next_interaction
        merged["interaction_origin"] = prior["interaction_origin"]
    if (
        same_event
        and "interaction_origin" in merged
        and "interaction_origin_backfill" not in merged
        and "interaction_origin_backfill" in prior
        and merged["interaction_origin"] == prior.get("interaction_origin")
    ):
        merged["interaction_origin_backfill"] = prior[
            "interaction_origin_backfill"
        ]
    prior_response = prior.get("response")
    incoming_response = incoming.get("response")
    prior_answers = (
        prior_response.get("answers")
        if isinstance(prior_response, dict)
        else None
    )
    incoming_answers = (
        incoming_response.get("answers")
        if isinstance(incoming_response, dict)
        else None
    )
    if (
        same_event
        and prior.get("status") == "answered"
        and incoming.get("status") == "answered"
        and prior.get("response_backfill")
        == EXACT_PERMISSION_RESPONSE_BACKFILL
        and isinstance(prior_answers, list)
        and bool(prior_answers)
        and (not isinstance(incoming_answers, list) or not incoming_answers)
    ):
        # Historical exact-execution repairs are server-enriched metadata.
        # An older collector cannot reproduce them from its side record, so a
        # later authoritative replay must not erase the proven answer.  This
        # deliberately does not carry responses across a new timestamp/state
        # or over a newly recorded non-empty native response.
        merged["response"] = orjson.loads(json.dumps(prior_response))
        merged["response_backfill"] = EXACT_PERMISSION_RESPONSE_BACKFILL
    return merged


def _preserve_interaction_container_provenance(
    prior: object,
    incoming: object,
) -> object:
    """Retain server-proven origins while accepting newer interaction state."""
    prior_by_id = _interaction_entries_by_id(prior)
    if isinstance(incoming, list):
        return [
            _preserve_interaction_entry_provenance(
                prior_by_id.get(_interaction_entry_id(index, entry)),
                entry,
            )
            for index, entry in enumerate(incoming)
        ]
    if isinstance(incoming, dict):
        return {
            key: _preserve_interaction_entry_provenance(
                prior_by_id.get(_interaction_entry_id(key, entry)),
                entry,
            )
            for key, entry in incoming.items()
        }
    return incoming


def _preserve_interaction_provenance(existing: dict, incoming: dict) -> dict:
    """Keep authoritative origins across old-collector metadata refreshes.

    Collector metadata remains authoritative for pending/answered state and
    response text.  Provenance is server-enriched during a FULL replay, so an
    older collector's later DELTA must not erase it merely because the side
    record predates provenance support.  Visibility still validates every
    retained origin against the exact permission payload and lineage row.
    """
    merged = dict(incoming)
    for key in (INTERACTION_HISTORY_KEY, LIVE_INTERACTION_SIGNALS_KEY):
        if key in incoming and key in existing:
            merged[key] = _preserve_interaction_container_provenance(
                existing[key],
                incoming[key],
            )
    return merged


async def _invalidate_embeddings_for_revision(
    db: AsyncSession,
    doc: Document,
    previous_embedding_content_hash: str,
    incoming_embedding_content_hash: str,
) -> bool:
    """Reconcile vectors against the exact bounded model input identity."""
    from .embedding_service import apply_embedding_tier_policy

    persisted_hash = doc.embedding_content_hash
    doc.embedding_content_hash = incoming_embedding_content_hash
    content_changed = (
        persisted_hash or previous_embedding_content_hash
    ) != incoming_embedding_content_hash
    # Sticky tier policy may promote fast → quality even when the bounded
    # model input is unchanged. Promotion marks pending so stale fast rows
    # are not searchable while quality vectors are generated.
    promoted = apply_embedding_tier_policy(doc)
    if not content_changed and not promoted:
        return False
    if content_changed:
        # Keep the previous rows as an internal reuse cache. Search excludes this
        # document while status is pending, and the fenced embedding finalizer
        # atomically upserts changed chunks plus trims any stale tail. Deleting here
        # forced every small edit to recompute and rewrite the full HNSW footprint.
        doc.embedding_status = "pending"
        doc.embedding_attempts = 0
        doc.embedding_claim_token = None
        doc.embedding_claimed_at = None
    return True


def _bounded_message_text(value: str, limit: int) -> str:
    """Bound a text value by UTF-8 bytes while preserving useful head/tail."""
    if len(value) <= limit and len(value.encode("utf-8")) <= limit:
        return value
    marker = (
        f"\n\n[... oversized message truncated from {len(value):,} "
        "characters by Memento ...]\n\n"
    )
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= limit:
        return marker_bytes[:limit].decode("utf-8", "ignore")
    payload_limit = max(0, limit - len(marker_bytes))
    head_limit = payload_limit * 3 // 4
    tail_limit = payload_limit - head_limit
    head = value[:head_limit].encode("utf-8")[:head_limit].decode("utf-8", "ignore")
    tail_bytes = value[-tail_limit:].encode("utf-8") if tail_limit else b""
    tail = tail_bytes[-tail_limit:].decode("utf-8", "ignore") if tail_limit else ""
    return head + marker + tail


def _conversation_message_metadata(normalized) -> dict:
    """Build the bounded metadata persisted beside normalized text."""
    from .conversation_parser import (
        normalize_message_attachments,
        normalize_tool_calls,
        strip_terminal_sequences,
    )

    meta: dict = {}
    if normalized.thinking:
        meta["thinking"] = _bounded_message_text(
            strip_terminal_sequences(normalized.thinking).replace("\x00", ""),
            MAX_STORED_AUXILIARY_CHARS,
        )
    if normalized.tool_name:
        meta["tool_name"] = _bounded_message_text(
            normalized.tool_name,
            MAX_STORED_TOOL_NAME_CHARS,
        )
    if normalized.tool_input:
        meta["tool_input"] = _bounded_message_text(
            strip_terminal_sequences(normalized.tool_input).replace("\x00", ""),
            MAX_STORED_AUXILIARY_CHARS,
        )
    if normalized.tool_call_id:
        meta["tool_call_id"] = _bounded_message_text(
            str(normalized.tool_call_id),
            512,
        )
    if normalized.tool_status:
        meta["tool_status"] = _bounded_message_text(
            str(normalized.tool_status),
            80,
        )
    if normalized.is_background:
        meta["is_background"] = True
    if normalized.background_task_id:
        meta["background_task_id"] = _bounded_message_text(
            str(normalized.background_task_id),
            512,
        )
    if normalized.session_context:
        meta["session_context"] = _bounded_message_text(
            strip_terminal_sequences(normalized.session_context).replace("\x00", ""),
            MAX_STORED_AUXILIARY_CHARS,
        )
    attachments = normalize_message_attachments(normalized.attachments)
    if attachments:
        meta["attachments"] = attachments
    tool_calls = normalize_tool_calls(normalized.tool_calls)
    if tool_calls:
        meta["tool_calls"] = tool_calls
    if normalized.interaction:
        meta["interaction"] = normalized.interaction
    if normalized.interaction_response:
        meta["interaction_response"] = normalized.interaction_response
    if normalized.source_id:
        meta["source_id"] = _bounded_message_text(
            str(normalized.source_id),
            256,
        )
    if normalized.source_turn_id:
        meta["source_turn_id"] = _bounded_message_text(
            str(normalized.source_turn_id),
            256,
        )
    if normalized.model:
        meta["model"] = _bounded_message_text(
            str(normalized.model),
            MAX_STORED_IDENTITY_CHARS,
        )
    if normalized.reasoning_effort:
        meta["reasoning_effort"] = _bounded_message_text(
            str(normalized.reasoning_effort),
            MAX_STORED_IDENTITY_CHARS,
        )
    if normalized.service_tier:
        meta["service_tier"] = _bounded_message_text(
            str(normalized.service_tier),
            MAX_STORED_IDENTITY_CHARS,
        )
    if normalized.agent_mode:
        meta["agent_mode"] = _bounded_message_text(
            str(normalized.agent_mode),
            MAX_STORED_IDENTITY_CHARS,
        )
    if normalized.task_state:
        meta["task_state"] = normalized.task_state
    if normalized.agent_event:
        meta["agent_event"] = normalized.agent_event
    origin = str(getattr(normalized, "message_origin", "") or "").strip()
    if origin in {"human", "parent_agent"}:
        meta["message_origin"] = origin
    return meta


def iter_stored_conversation_messages(
    content: str,
    tool_id: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity=None,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
):
    """Yield the exact normalized representation persisted during ingest.

    Live ingestion and offline reparses must share this boundary.  Keeping the
    terminal cleanup, size limits, metadata projection, and timestamp parsing
    here prevents a historical repair from creating rows that a subsequent
    collector update would immediately rewrite differently.
    """
    from .conversation_parser import iter_conversation_messages

    yield from _iter_stored_normalized_messages(
        iter_conversation_messages(
            content,
            tool_id,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=assistant_identity,
            initial_task_state=initial_task_state,
            incremental=incremental,
        )
    )


def iter_stored_conversation_objects(
    source_objects: Iterable[object],
    tool_id: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity=None,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
):
    """Normalize a streamed decoded-object sequence at the storage boundary."""
    from .conversation_parser import iter_conversation_messages_from_objects

    yield from _iter_stored_normalized_messages(
        iter_conversation_messages_from_objects(
            source_objects,
            tool_id,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=assistant_identity,
            initial_task_state=initial_task_state,
            incremental=incremental,
        )
    )


def iter_claude_lineage_records(
    content: str,
    *,
    conversation_source: ConversationFileSource | None = None,
) -> Iterator[object]:
    """Yield raw Claude records for lineage without normalizing UI content.

    The streamed source is re-opened by its source abstraction; DELTAs contain
    only their new records, while FULL is the authoritative complete replay.
    This deliberately observes only record identity/parents, not message text.
    """
    if conversation_source is not None:
        yield from conversation_source.iter_objects()
        return
    for raw_line in content.splitlines():
        try:
            record = orjson.loads(raw_line)
        except (TypeError, orjson.JSONDecodeError):
            continue
        if isinstance(record, dict):
            yield record


def _iter_stored_normalized_messages(
    messages: Iterable[object],
) -> Iterator[tuple[object, str, dict, datetime | None]]:
    from .conversation_parser import strip_terminal_sequences

    for normalized in messages:
        if normalized.role not in ("user", "assistant", "tool", "system"):
            continue
        full_clean_content = strip_terminal_sequences(normalized.content).replace(
            "\x00", ""
        )
        if (
            not full_clean_content.strip()
            and not normalized.thinking.strip()
            and not normalized.tool_calls
            and not normalized.attachments
        ):
            continue
        clean_content = _bounded_message_text(
            full_clean_content,
            MAX_STORED_MESSAGE_CHARS,
        )
        timestamp = None
        if normalized.timestamp:
            try:
                timestamp = datetime.fromisoformat(
                    normalized.timestamp.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass
        yield (
            normalized,
            clean_content,
            _conversation_message_metadata(normalized),
            timestamp,
        )


def _assistant_identity_for_ingest(doc: Document, mode: str):
    """Seed incremental parsing from the last committed assistant identity."""
    from .conversation_parser import AssistantIdentityState

    metadata = document_metadata(doc)
    if mode != "delta":
        return AssistantIdentityState()

    def stored_value(*keys: str) -> str:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    return AssistantIdentityState(
        model=stored_value(CURRENT_ASSISTANT_MODEL_KEY, "model"),
        reasoning_effort=stored_value(
            CURRENT_ASSISTANT_REASONING_KEY,
            "reasoning_effort",
        ),
        service_tier=stored_value(
            CURRENT_ASSISTANT_SERVICE_TIER_KEY,
            "service_tier",
        ),
        agent_mode=stored_value(
            CURRENT_ASSISTANT_MODE_KEY,
            "agent_mode",
        ),
        token_usage=token_usage_from_metadata(metadata),
        started_at=stored_value(STARTED_AT_METADATA_KEY),
        last_activity_at=stored_value(LAST_ACTIVITY_AT_METADATA_KEY),
        usage_segment_id=stored_value(USAGE_SEGMENT_METADATA_KEY),
    )


def _store_assistant_identity(doc: Document, assistant_identity) -> None:
    """Persist parser state so a later delta can label its assistant rows."""
    metadata = document_metadata(doc)
    for key, value in (
        (CURRENT_ASSISTANT_MODEL_KEY, assistant_identity.model),
        (CURRENT_ASSISTANT_REASONING_KEY, assistant_identity.reasoning_effort),
        (CURRENT_ASSISTANT_SERVICE_TIER_KEY, assistant_identity.service_tier),
        (CURRENT_ASSISTANT_MODE_KEY, assistant_identity.agent_mode),
        (STARTED_AT_METADATA_KEY, assistant_identity.started_at),
        (LAST_ACTIVITY_AT_METADATA_KEY, assistant_identity.last_activity_at),
        (USAGE_SEGMENT_METADATA_KEY, assistant_identity.usage_segment_id),
    ):
        if value:
            metadata[key] = _bounded_message_text(
                value,
                MAX_STORED_IDENTITY_CHARS,
            )
        else:
            metadata.pop(key, None)
    token_usage = normalize_token_usage(assistant_identity.token_usage)
    if token_usage:
        metadata[TOKEN_USAGE_METADATA_KEY] = token_usage
    else:
        metadata.pop(TOKEN_USAGE_METADATA_KEY, None)
    store_document_metadata(doc, metadata)


def _drain_assistant_usage_rows(
    doc: Document,
    tool_id: str,
    assistant_identity,
) -> list[dict[str, object]]:
    """Drain parser observations into bounded relational event rows."""
    observations = list(assistant_identity.usage_observations)
    assistant_identity.usage_observations.clear()
    rows: list[dict[str, object]] = []
    for observation in observations:
        rows.append(
            {
                "document_id": doc.id,
                "machine_id": doc.machine_id,
                "tool_id": tool_id,
                **usage_observation_values(observation),
            }
        )
    return rows


_USAGE_COUNT_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _merge_usage_rows(
    rows: list[dict[str, object]],
    *,
    accumulate: bool,
) -> list[dict[str, object]]:
    """Collapse same-key observations before a single multi-row upsert.

    One collector delta can legitimately contain many usage snapshots for the
    same native turn (a long turn bundled by any upload stall or restart).
    PostgreSQL rejects ``ON CONFLICT DO UPDATE`` statements whose VALUES list
    affects one row twice, so duplicates must merge in-batch: summed counts
    with the newest identity when accumulating, otherwise last-writer-wins to
    mirror the replacement semantics of the upsert itself.
    """
    merged: dict[tuple[object, object], dict[str, object]] = {}
    order: list[tuple[object, object]] = []
    for row in rows:
        key = (row["document_id"], row["source_id"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
            order.append(key)
            continue
        if accumulate:
            for field in _USAGE_COUNT_FIELDS:
                existing[field] = int(existing.get(field) or 0) + int(row.get(field) or 0)
            occurred_at = row.get("occurred_at")
            if occurred_at is not None and (
                existing.get("occurred_at") is None
                or occurred_at >= existing["occurred_at"]
            ):
                existing["occurred_at"] = occurred_at
            for field in (
                "machine_id",
                "tool_id",
                "source",
                "model",
                "reasoning_effort",
                "service_tier",
                "attribution_status",
            ):
                if row.get(field) is not None:
                    existing[field] = row[field]
        else:
            existing.update(row)
    return [merged[key] for key in order]


async def _upsert_assistant_usage_rows(
    db: AsyncSession,
    rows: list[dict[str, object]],
    *,
    detect_replacements: bool = False,
    accumulate_existing: bool = False,
) -> list[dict[str, object]]:
    if not rows:
        return []
    rows = _merge_usage_rows(rows, accumulate=accumulate_existing)
    replaced_usage: list[dict[str, object]] = []
    if detect_replacements:
        document_id = rows[0]["document_id"]
        source_ids = [str(row["source_id"]) for row in rows]
        existing_rows = (
            await db.execute(
                select(ConversationUsageEvent).where(
                    ConversationUsageEvent.document_id == document_id,
                    ConversationUsageEvent.source_id.in_(source_ids),
                )
            )
        ).scalars()
        replaced_usage = [
            {
                "input_tokens": row.input_tokens,
                "uncached_input_tokens": row.uncached_input_tokens,
                "cached_input_tokens": row.cached_input_tokens,
                "cache_write_input_tokens": row.cache_write_input_tokens,
                "output_tokens": row.output_tokens,
                "reasoning_output_tokens": row.reasoning_output_tokens,
                "total_tokens": row.total_tokens,
                "source": row.source,
            }
            for row in existing_rows
        ]
    statement = pg_insert(ConversationUsageEvent).values(rows)
    excluded = statement.excluded
    count_updates = {
        field: (
            getattr(ConversationUsageEvent, field) + getattr(excluded, field)
            if accumulate_existing
            else getattr(excluded, field)
        )
        for field in _USAGE_COUNT_FIELDS
    }
    await db.execute(
        statement.on_conflict_do_update(
            constraint="uq_conversation_usage_document_source",
            set_={
                "machine_id": excluded.machine_id,
                "tool_id": excluded.tool_id,
                "source": excluded.source,
                "occurred_at": (
                    func.greatest(
                        ConversationUsageEvent.occurred_at,
                        excluded.occurred_at,
                    )
                    if accumulate_existing
                    else excluded.occurred_at
                ),
                "model": excluded.model,
                "reasoning_effort": excluded.reasoning_effort,
                "service_tier": excluded.service_tier,
                "attribution_status": excluded.attribution_status,
                **count_updates,
            },
        )
    )
    return replaced_usage


def _remove_replaced_usage(assistant_identity, replaced_usage: Iterable[object]) -> None:
    """Remove prior Claude event values after parsing replacement records.

    Claude may append an updated record with the same native message ID in a
    later collector delta. The parser has already added the replacement's
    counters, so removing the persisted prior value keeps the lifetime total
    exact while the relational event is updated idempotently.
    """
    for usage in replaced_usage:
        assistant_identity.token_usage = subtract_token_usage(
            assistant_identity.token_usage,
            usage,
        )


def _pending_question_ids_for_ingest(doc: Document, mode: str) -> set[str]:
    """Seed active interaction IDs from the previous committed delta."""
    if mode != "delta":
        return set()
    metadata = document_metadata(doc)
    # A version bump means the durable reconciliation rules changed. Rebuild
    # from the current ingest tail instead of carrying forward stale IDs, but
    # retain current live previews because they are necessarily outside it.
    if (
        metadata.get(PENDING_QUESTION_RECONCILIATION_VERSION_KEY)
        != PENDING_QUESTION_RECONCILIATION_VERSION
    ):
        return _active_live_interaction_ids(
            doc,
            metadata.get(LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY, ""),
        )
    stored = metadata.get(CURRENT_PENDING_QUESTIONS_KEY)
    if not isinstance(stored, list):
        return set()
    return {_bounded_message_text(str(value), 512) for value in stored[:64] if value}


def _latest_human_timestamp_for_ingest(doc: Document, mode: str) -> str:
    if mode != "delta":
        return ""
    metadata = document_metadata(doc)
    return _bounded_message_text(
        str(metadata.get(LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY) or ""),
        128,
    )


def _normalized_interaction_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def interaction_at_or_before_human(
    interaction_timestamp: object,
    human_timestamp: object,
) -> bool:
    interaction_at = _normalized_interaction_timestamp(interaction_timestamp)
    human_at = _normalized_interaction_timestamp(human_timestamp)
    return (
        interaction_at is not None
        and human_at is not None
        and interaction_at <= human_at
    )


def _active_live_interaction_ids(
    doc: Document,
    latest_human_timestamp: object,
) -> set[str]:
    """Return retained, non-meta live interactions that still need attention."""
    from .conversation_parser import is_meta_tool_interaction

    metadata = document_metadata(doc)
    raw_signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
    if not isinstance(raw_signals, dict):
        return set()
    return {
        interaction_id
        for raw_interaction_id, signal in raw_signals.items()
        if isinstance(signal, dict)
        and isinstance(signal.get("interaction"), dict)
        and not is_meta_tool_interaction(signal["interaction"])
        and not interaction_at_or_before_human(
            signal.get("timestamp"),
            latest_human_timestamp,
        )
        and (
            interaction_id := _bounded_message_text(
                str(raw_interaction_id),
                512,
            )
        )
    }


def _newest_interaction_timestamp(current: object, candidate: object) -> str:
    current_at = _normalized_interaction_timestamp(current)
    candidate_at = _normalized_interaction_timestamp(candidate)
    if candidate_at is None:
        return _bounded_message_text(str(current or ""), 128)
    if current_at is not None and current_at >= candidate_at:
        return _bounded_message_text(str(current or ""), 128)
    return candidate_at.isoformat()


def _update_pending_question_ids(
    pending_ids: set[str],
    normalized,
    latest_human_timestamp: object = "",
) -> str:
    from .conversation_parser import is_meta_tool_interaction

    interactions = (
        [normalized.interaction] if isinstance(normalized.interaction, dict) else []
    )
    interactions.extend(
        interaction
        for call in normalized.tool_calls
        if isinstance(call, dict)
        and isinstance((interaction := call.get("interaction")), dict)
    )
    for interaction in interactions:
        if is_meta_tool_interaction(interaction):
            continue
        interaction_id = _bounded_message_text(str(interaction.get("id") or ""), 512)
        if interaction_id:
            if interaction_at_or_before_human(
                getattr(normalized, "timestamp", ""),
                latest_human_timestamp,
            ):
                pending_ids.discard(interaction_id)
            else:
                pending_ids.add(interaction_id)
    response = normalized.interaction_response
    if isinstance(response, dict):
        interaction_id = _bounded_message_text(
            str(response.get("interaction_id") or ""),
            512,
        )
        if interaction_id:
            pending_ids.discard(interaction_id)
    if getattr(normalized, "role", "") == "user":
        from .conversation_markdown import is_meaningful_human_turn

        if is_meaningful_human_turn(
            getattr(normalized, "content", ""),
            None,
            getattr(normalized, "role", ""),
        ):
            # A new human turn supersedes any older interactive prompt even
            # when the source tool was restarted before it emitted a structured
            # tool result. This applies equally to Cursor, Claude, and Codex.
            pending_ids.clear()
            latest_human_timestamp = _newest_interaction_timestamp(
                latest_human_timestamp,
                getattr(normalized, "timestamp", ""),
            )
    return _bounded_message_text(str(latest_human_timestamp or ""), 128)


def _store_pending_question_ids(
    doc: Document,
    pending_ids: set[str],
) -> None:
    metadata = document_metadata(doc)
    should_stamp_reconciliation_version = (
        CURRENT_PENDING_QUESTIONS_KEY in metadata
        or PENDING_QUESTION_COUNT_KEY in metadata
        or bool(pending_ids)
        or (
            metadata.get(PENDING_QUESTION_RECONCILIATION_VERSION_KEY)
            == PENDING_QUESTION_RECONCILIATION_VERSION
        )
    )
    bounded = sorted(pending_ids)[:64]
    if bounded:
        metadata[CURRENT_PENDING_QUESTIONS_KEY] = bounded
        metadata[PENDING_QUESTION_COUNT_KEY] = len(bounded)
    else:
        metadata.pop(CURRENT_PENDING_QUESTIONS_KEY, None)
        metadata.pop(PENDING_QUESTION_COUNT_KEY, None)
    if should_stamp_reconciliation_version:
        metadata[PENDING_QUESTION_RECONCILIATION_VERSION_KEY] = (
            PENDING_QUESTION_RECONCILIATION_VERSION
        )
    store_document_metadata(doc, metadata)


def _store_latest_human_timestamp(doc: Document, timestamp: object) -> None:
    metadata = document_metadata(doc)
    bounded = _bounded_message_text(str(timestamp or ""), 128)
    if bounded:
        metadata[LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY] = bounded
    else:
        metadata.pop(LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY, None)
    store_document_metadata(doc, metadata)


def _normalized_interaction_ids(normalized) -> set[str]:
    from .conversation_parser import is_meta_tool_interaction

    ids: set[str] = set()
    interactions = (
        [normalized.interaction] if isinstance(normalized.interaction, dict) else []
    )
    interactions.extend(
        interaction
        for call in normalized.tool_calls
        if isinstance(call, dict)
        and isinstance((interaction := call.get("interaction")), dict)
    )
    for interaction in interactions:
        if is_meta_tool_interaction(interaction):
            continue
        interaction_id = _bounded_message_text(
            str(interaction.get("id") or ""),
            512,
        )
        if interaction_id:
            ids.add(interaction_id)
    response = normalized.interaction_response
    if isinstance(response, dict):
        interaction_id = _bounded_message_text(
            str(response.get("interaction_id") or ""),
            512,
        )
        if interaction_id:
            ids.add(interaction_id)
    return ids


def _reconcile_live_interaction_signals(
    doc: Document,
    canonical_ids: set[str],
    *,
    clear_all: bool,
) -> None:
    """Retire previews once their canonical transcript rows have arrived."""
    from .conversation_parser import is_meta_tool_interaction

    metadata = document_metadata(doc)
    raw_signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
    if not isinstance(raw_signals, dict):
        return
    signals = {
        str(key): value
        for key, value in raw_signals.items()
        if isinstance(value, dict)
        and not is_meta_tool_interaction(value.get("interaction"))
    }
    if clear_all:
        signals.clear()
    else:
        for interaction_id in canonical_ids:
            signals.pop(interaction_id, None)
    if signals:
        metadata[LIVE_INTERACTION_SIGNALS_KEY] = signals
    else:
        metadata.pop(LIVE_INTERACTION_SIGNALS_KEY, None)
    store_document_metadata(doc, metadata)


def _normalized_terminal_tool_call_ids(normalized) -> set[str]:
    tool_call_id = _bounded_message_text(
        str(getattr(normalized, "tool_call_id", "") or ""),
        512,
    )
    if not tool_call_id:
        return set()
    raw_type = str(getattr(normalized, "raw_type", "") or "").casefold()
    tool_status = str(getattr(normalized, "tool_status", "") or "").casefold()
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
        return {tool_call_id}
    return set()


def _reconcile_live_shell_activities(
    doc: Document,
    terminal_tool_call_ids: set[str],
) -> None:
    """Retire transient shell cards once canonical terminal rows arrive."""
    metadata = document_metadata(doc)
    raw_activities = metadata.get(LIVE_SHELL_ACTIVITIES_KEY)
    if not isinstance(raw_activities, dict):
        return
    activities = {
        str(key): value
        for key, value in raw_activities.items()
        if isinstance(value, dict)
    }
    for tool_call_id in terminal_tool_call_ids:
        activities.pop(tool_call_id, None)
    if activities:
        metadata[LIVE_SHELL_ACTIVITIES_KEY] = activities
    else:
        metadata.pop(LIVE_SHELL_ACTIVITIES_KEY, None)
    store_document_metadata(doc, metadata)


def _pending_question_interactions(
    recent_rows: list[ConversationMessage],
) -> list[dict[str, object]]:
    """Recover delta-boundary questions without reviving stale Cursor prompts."""
    from .conversation_markdown import is_meaningful_human_turn
    from .conversation_parser import (
        CURSOR_QUESTION_RESPONSE_WINDOW,
        build_cursor_interaction_response,
        is_meta_tool_interaction,
    )

    if not recent_rows:
        return []

    newest_line = max(int(row.line_number or 0) for row in recent_rows)
    pending: dict[str, dict[str, object]] = {}
    latest_human_timestamp = ""
    for recent in sorted(recent_rows, key=lambda row: int(row.line_number or 0)):
        metadata = recent.metadata_ if isinstance(recent.metadata_, dict) else {}
        direct = metadata.get("interaction")
        interactions = [direct] if isinstance(direct, dict) else []
        calls = metadata.get("tool_calls")
        if isinstance(calls, list):
            interactions.extend(
                interaction
                for call in calls
                if isinstance(call, dict)
                and isinstance((interaction := call.get("interaction")), dict)
            )
        interactions = [
            interaction
            for interaction in interactions
            if not is_meta_tool_interaction(interaction)
        ]
        for interaction in interactions:
            interaction_id = str(interaction.get("id") or "")
            if not interaction_id:
                continue
            if interaction_at_or_before_human(
                getattr(recent, "timestamp", ""),
                latest_human_timestamp,
            ):
                continue
            if (
                interaction.get("source") == "cursor"
                and newest_line - int(recent.line_number or 0)
                > CURSOR_QUESTION_RESPONSE_WINDOW
            ):
                continue
            pending[interaction_id] = interaction
        response = metadata.get("interaction_response")
        if isinstance(response, dict):
            interaction_id = str(response.get("interaction_id") or "")
            if interaction_id:
                pending.pop(interaction_id, None)
        else:
            for interaction in interactions:
                inferred = build_cursor_interaction_response(
                    interaction,
                    getattr(recent, "content", ""),
                )
                if inferred is not None:
                    pending.pop(str(interaction.get("id") or ""), None)
        if is_meaningful_human_turn(
            getattr(recent, "content", ""),
            metadata,
            getattr(recent, "role", ""),
        ):
            pending.clear()
            latest_human_timestamp = _newest_interaction_timestamp(
                latest_human_timestamp,
                getattr(recent, "timestamp", ""),
            )
    return list(pending.values())


def _advance_stored_pending_questions(
    pending: dict[str, dict[str, object]],
    stored,
    latest_human_timestamp: object = "",
) -> tuple[set[str], str]:
    """Apply one canonical DB row to persisted pending-question state."""
    from .conversation_markdown import is_meaningful_human_turn
    from .conversation_parser import (
        build_cursor_interaction_response,
        is_meta_tool_interaction,
    )

    metadata = stored.metadata_ if isinstance(stored.metadata_, dict) else {}
    direct = metadata.get("interaction")
    interactions = [direct] if isinstance(direct, dict) else []
    calls = metadata.get("tool_calls")
    if isinstance(calls, list):
        interactions.extend(
            interaction
            for call in calls
            if isinstance(call, dict)
            and isinstance((interaction := call.get("interaction")), dict)
        )
    interactions = [
        interaction
        for interaction in interactions
        if not is_meta_tool_interaction(interaction)
    ]

    seen: set[str] = set()
    for interaction in interactions:
        interaction_id = _bounded_message_text(
            str(interaction.get("id") or ""),
            512,
        )
        if interaction_id:
            seen.add(interaction_id)
            if not interaction_at_or_before_human(
                getattr(stored, "timestamp", ""),
                latest_human_timestamp,
            ):
                pending[interaction_id] = interaction

    response = metadata.get("interaction_response")
    if isinstance(response, dict):
        interaction_id = _bounded_message_text(
            str(response.get("interaction_id") or ""),
            512,
        )
        if interaction_id:
            seen.add(interaction_id)
            pending.pop(interaction_id, None)
    else:
        for interaction in interactions:
            inferred = build_cursor_interaction_response(
                interaction,
                getattr(stored, "content", ""),
            )
            if inferred is None:
                continue
            interaction_id = _bounded_message_text(
                str(interaction.get("id") or ""),
                512,
            )
            if interaction_id:
                seen.add(interaction_id)
                pending.pop(interaction_id, None)
    if is_meaningful_human_turn(
        getattr(stored, "content", ""),
        metadata,
        getattr(stored, "role", ""),
    ):
        pending.clear()
        latest_human_timestamp = _newest_interaction_timestamp(
            latest_human_timestamp,
            getattr(stored, "timestamp", ""),
        )
    return seen, _bounded_message_text(str(latest_human_timestamp or ""), 128)


async def reconcile_pending_question_metadata(db: AsyncSession) -> int:
    """One-time repair for badges persisted before human-turn reconciliation."""
    from .conversation_parser import is_meta_tool_interaction
    from .dashboard_projection import refresh_dashboard_document_projection

    statement = select(Document).where(
        Document.category == "conversation",
        delivery_metadata_expression(joined=True).op("?")(
            CURRENT_PENDING_QUESTIONS_KEY
        ),
    )
    documents = (
        (
            await db.execute(outerjoin_document_delivery(statement))
        )
        .scalars()
        .all()
    )
    updated = 0
    for document in documents:
        original_metadata = document_metadata(document)
        if (
            original_metadata.get(PENDING_QUESTION_RECONCILIATION_VERSION_KEY)
            == PENDING_QUESTION_RECONCILIATION_VERSION
        ):
            continue

        pending: dict[str, dict[str, object]] = {}
        seen_ids: set[str] = set()
        latest_human_timestamp = ""
        rows = await db.stream(
            select(
                ConversationMessage.line_number,
                ConversationMessage.role,
                ConversationMessage.content,
                ConversationMessage.metadata_.label("metadata_"),
                ConversationMessage.timestamp,
            )
            .where(ConversationMessage.document_id == document.id)
            .order_by(ConversationMessage.line_number)
            .execution_options(yield_per=500)
        )
        try:
            async for row in rows:
                row_seen_ids, latest_human_timestamp = (
                    _advance_stored_pending_questions(
                        pending,
                        row,
                        latest_human_timestamp,
                    )
                )
                seen_ids.update(row_seen_ids)
        finally:
            await rows.close()

        metadata = dict(original_metadata)
        raw_signals = metadata.get(LIVE_INTERACTION_SIGNALS_KEY)
        signals = (
            {
                str(interaction_id): signal
                for interaction_id, signal in raw_signals.items()
                if isinstance(signal, dict)
                and not is_meta_tool_interaction(signal.get("interaction"))
            }
            if isinstance(raw_signals, dict)
            else {}
        )
        for interaction_id in seen_ids:
            signals.pop(interaction_id, None)
        for interaction_id, signal in list(signals.items()):
            if interaction_at_or_before_human(
                signal.get("timestamp"),
                latest_human_timestamp,
            ):
                signals.pop(interaction_id, None)

        active_ids = sorted(set(pending) | set(signals))[:64]
        if signals:
            metadata[LIVE_INTERACTION_SIGNALS_KEY] = signals
        else:
            metadata.pop(LIVE_INTERACTION_SIGNALS_KEY, None)
        if active_ids:
            metadata[CURRENT_PENDING_QUESTIONS_KEY] = active_ids
            metadata[PENDING_QUESTION_COUNT_KEY] = len(active_ids)
        else:
            metadata.pop(CURRENT_PENDING_QUESTIONS_KEY, None)
            metadata.pop(PENDING_QUESTION_COUNT_KEY, None)
        if latest_human_timestamp:
            metadata[LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY] = latest_human_timestamp
        else:
            metadata.pop(LATEST_MEANINGFUL_HUMAN_TIMESTAMP_KEY, None)
        metadata[PENDING_QUESTION_RECONCILIATION_VERSION_KEY] = (
            PENDING_QUESTION_RECONCILIATION_VERSION
        )
        if metadata != original_metadata:
            store_document_metadata(document, metadata)
            await db.flush()
            await refresh_dashboard_document_projection(db, document)
            updated += 1

    await db.commit()
    return updated


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _prepare_document_metadata(
    metadata: dict,
    *,
    tool_id: str | None = None,
) -> tuple[dict, list[dict], str]:
    """Separate transient prompt history and bound JSON stored on Document."""
    candidate = dict(metadata or {})
    raw_history = candidate.pop("user_history", [])
    first_user_message = str(candidate.pop("first_user_message", "") or "")
    normalizer = None
    if tool_id == "codex":
        from .conversation_parser import normalize_codex_user_payload

        normalizer = normalize_codex_user_payload
        first_role, first_user_message = normalizer(first_user_message)
        if first_role != "user":
            first_user_message = ""
        raw_title = candidate.get("title")
        if isinstance(raw_title, str):
            title_role, normalized_title = normalizer(raw_title)
            if title_role == "user" and normalized_title:
                candidate["title"] = normalized_title
            else:
                candidate.pop("title", None)
    first_user_message = _bounded_message_text(
        first_user_message,
        MAX_STORED_MESSAGE_CHARS,
    )
    from .conversation_hierarchy import persist_conversation_briefing_metadata

    persist_conversation_briefing_metadata(candidate, first_user_message)

    history: list[dict] = []
    history_bytes = 0
    if isinstance(raw_history, list):
        for entry in raw_history[:MAX_USER_HISTORY_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text", "") or "")
            if normalizer is not None:
                history_role, text = normalizer(text)
                if history_role != "user":
                    continue
            text = _bounded_message_text(text, MAX_STORED_MESSAGE_CHARS)
            entry_size = len(text.encode("utf-8")) + 64
            if history_bytes + entry_size > MAX_USER_HISTORY_BYTES:
                break
            history.append({"text": text, "ts": entry.get("ts", 0)})
            history_bytes += entry_size

    for key, value in list(candidate.items()):
        if isinstance(value, str) and len(value) > MAX_METADATA_STRING_CHARS:
            candidate[key] = _bounded_message_text(value, MAX_METADATA_STRING_CHARS)

    if _json_size(candidate) > MAX_DOCUMENT_METADATA_BYTES:
        retained = {
            key: value
            for key, value in candidate.items()
            if key in _ESSENTIAL_METADATA_KEYS
        }
        retained["_metadata_truncated"] = True
        candidate = retained

    # Essential values are bounded above, but a pathological nested value can
    # still exceed the total budget. Drop the largest non-marker fields until
    # the serialized document metadata is safe for a single JSONB parameter.
    while _json_size(candidate) > MAX_DOCUMENT_METADATA_BYTES:
        removable = [key for key in candidate if key != "_metadata_truncated"]
        if not removable:
            break
        largest = max(removable, key=lambda key: _json_size(candidate[key]))
        candidate.pop(largest, None)
        candidate["_metadata_truncated"] = True

    return candidate, history, first_user_message


def _is_externalized_delta_update(
    doc: Document,
    *,
    mode: str,
    persist_content: bool,
) -> bool:
    """Compatibility predicate for callers that retain a FULL object on DELTA."""
    return mode == "delta" and bool(doc.content_s3_key) and persist_content


def _history_line_number(index: int) -> int:
    """Keep injected history in a disjoint, bounded negative key range."""
    if not 0 <= index < MAX_USER_HISTORY_ENTRIES:
        raise ValueError("history index is outside the bounded range")
    return -MAX_USER_HISTORY_ENTRIES + index


async def _open_conversation_line_range(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    anchor: int,
    count: int,
    current_max: int | None = None,
    synchronize_session: bool = False,
) -> int:
    """Open a collision-free positive line range and return the new maximum."""
    if current_max is None:
        current_max = (
            await db.execute(
                select(func.max(ConversationMessage.line_number)).where(
                    ConversationMessage.document_id == document_id,
                    ConversationMessage.line_number >= 1,
                )
            )
        ).scalar() or 0
    if anchor <= current_max:
        await db.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.line_number >= anchor,
            )
            .values(line_number=-ConversationMessage.line_number)
            .execution_options(
                synchronize_session="fetch" if synchronize_session else False
            )
        )
        await db.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.line_number >= -current_max,
                ConversationMessage.line_number <= -anchor,
            )
            .values(line_number=-ConversationMessage.line_number + count)
            .execution_options(
                synchronize_session="fetch" if synchronize_session else False
            )
        )
    return current_max + count


async def _reconcile_recovered_history_rows(
    db: AsyncSession,
    document_id: uuid.UUID,
) -> tuple[int, int]:
    """Remove recovered/source duplicates and place true gaps chronologically.

    Negative line numbers were originally chosen only to avoid uniqueness
    collisions. Every reader orders by line number and line-jump APIs reject
    negatives, so they are not a valid presentation order. Surviving history
    rows are inserted before the next timestamped source event while source
    order remains otherwise unchanged.
    """
    history_rows = (
        (
            await db.execute(
                select(ConversationMessage)
                # History reconciliation mutates only this narrow row shape;
                # avoid hydrating its document FK and creation timestamp.
                .options(load_only(
                    ConversationMessage.id,
                    ConversationMessage.line_number,
                    ConversationMessage.content,
                    ConversationMessage.metadata_,
                    ConversationMessage.timestamp,
                ))
                .where(
                    ConversationMessage.document_id == document_id,
                    ConversationMessage.message_type == "history_user_message",
                )
                .order_by(
                    ConversationMessage.timestamp,
                    ConversationMessage.line_number,
                    ConversationMessage.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not history_rows:
        return 0, 0

    source_user_rows = (
        await db.execute(
            select(
                ConversationMessage.id,
                ConversationMessage.content,
                ConversationMessage.timestamp,
                ConversationMessage.line_number,
            ).where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.role == "user",
                ConversationMessage.message_type.is_distinct_from(
                    "history_user_message"
                ),
            )
        )
    ).all()
    matched, missing = partition_recovered_occurrences(
        [
            UserOccurrence(
                key=row.id,
                content=row.content,
                timestamp=row.timestamp,
                line_number=row.line_number,
            )
            for row in source_user_rows
        ],
        [
            UserOccurrence(
                key=row.id,
                content=row.content,
                timestamp=row.timestamp,
                line_number=row.line_number,
            )
            for row in history_rows
        ],
    )
    history_by_id = {row.id: row for row in history_rows}
    for occurrence in matched:
        await db.delete(history_by_id[occurrence.key])
    if matched:
        await db.flush()

    pending = [
        history_by_id[occurrence.key]
        for occurrence in missing
        if history_by_id[occurrence.key].line_number < 1
    ]
    if not pending:
        return len(matched), 0

    source_timeline_rows = (
        await db.execute(
            select(
                ConversationMessage.id,
                ConversationMessage.content,
                ConversationMessage.timestamp,
                ConversationMessage.line_number,
            )
            .where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.line_number >= 1,
                ConversationMessage.message_type.is_distinct_from(
                    "history_user_message"
                ),
            )
            .order_by(ConversationMessage.line_number)
        )
    ).all()
    timeline = [
        UserOccurrence(
            key=row.id,
            content=row.content,
            timestamp=row.timestamp,
            line_number=row.line_number,
        )
        for row in source_timeline_rows
    ]
    pending_occurrences = [
        UserOccurrence(
            key=row.id,
            content=row.content,
            timestamp=row.timestamp,
            line_number=row.line_number,
        )
        for row in pending
    ]
    anchors = recovered_occurrence_anchors(timeline, pending_occurrences)
    max_line = (
        await db.execute(
            select(func.max(ConversationMessage.line_number)).where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.line_number >= 1,
            )
        )
    ).scalar() or 0

    # Move unplaced history rows below the range produced by temporarily
    # negating positive rows. This makes each range shift collision-free under
    # the document/line unique index.
    temporary_start = -(max_line + len(pending) + 1)
    for index, row in enumerate(pending):
        row.line_number = temporary_start + index
    await db.flush()

    from collections import defaultdict

    groups: dict[int, list[ConversationMessage]] = defaultdict(list)
    for row in pending:
        groups[anchors[row.id]].append(row)
    current_max = max_line
    for anchor in sorted(groups, reverse=True):
        rows = sorted(
            groups[anchor],
            key=lambda row: (
                row.timestamp or datetime.max.replace(tzinfo=timezone.utc),
                str((row.metadata_ or {}).get("source_id") or ""),
                row.id,
            ),
        )
        count = len(rows)
        current_max = await _open_conversation_line_range(
            db,
            document_id,
            anchor=anchor,
            count=count,
            current_max=current_max,
        )
        for index, row in enumerate(rows):
            row.line_number = anchor + index
        await db.flush()
    return len(matched), len(pending)


# Re-sanitize patterns (defense-in-depth)
_RESANITIZE_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[API_KEY_REDACTED]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "[GITHUB_TOKEN_REDACTED]"),
    (re.compile(r"bot\d+:[A-Za-z0-9_-]{35}"), "[TELEGRAM_BOT_TOKEN_REDACTED]"),
    (
        re.compile(
            r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
            re.MULTILINE,
        ),
        "[PRIVATE_KEY_REDACTED]",
    ),
]

_GENERATED_CONVERSATION_TITLE_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?:agent|session|rollout|conversation|chat)[-_][a-z0-9_-]{8,}"
    r")$",
    re.IGNORECASE,
)
_CLAUDE_LOCAL_COMMAND_PREFIXES = (
    "<command-name",
    "<command-message",
    "<command-args",
    "<local-command-caveat",
    "<local-command-stdout",
    "<local-command-stderr",
)


def _resanitize(text: str) -> tuple[str, bool]:
    """Server-side re-sanitization. Returns (cleaned_text, had_sensitive)."""
    found = False
    for pattern, replacement in _RESANITIZE_PATTERNS:
        text, n = pattern.subn(replacement, text)
        if n > 0:
            found = True
    return text, found


def _has_generated_conversation_title(title: str | None) -> bool:
    """Return whether a source title is an opaque machine-generated identifier."""
    candidate = (title or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    candidate = re.sub(r"\.(?:jsonl?|md|txt)$", "", candidate, flags=re.IGNORECASE)
    return bool(_GENERATED_CONVERSATION_TITLE_RE.fullmatch(candidate))


def _conversation_title_needs_derivation(
    title: str | None,
    tool_id: str | None = None,
) -> bool:
    """Return whether a source title is opaque or injected Codex context."""
    if _has_generated_conversation_title(title):
        return True
    candidate = (title or "").strip()
    if not candidate:
        return True
    if tool_id == "codex":
        from .conversation_parser import normalize_codex_user_payload

        role, normalized = normalize_codex_user_payload(candidate)
        return role != "user" or normalized != candidate
    if tool_id == "cursor":
        from .conversation_parser import (
            has_cursor_session_context_prefix,
            normalize_cursor_additional_directives,
            split_cursor_user_payload,
        )

        normalized, _timestamp, context = split_cursor_user_payload(candidate)
        return (
            bool(context)
            or normalized != candidate
            or has_cursor_session_context_prefix(candidate)
            or normalize_cursor_additional_directives(candidate) is not None
        )
    return False


def _friendly_conversation_title(
    content: str,
    max_length: int = 96,
    *,
    tool_id: str | None = None,
) -> str | None:
    """Build a compact thread name from the first meaningful human prompt."""
    text = (content or "").strip()
    if tool_id == "codex":
        from .conversation_parser import normalize_codex_user_payload

        role, text = normalize_codex_user_payload(text)
        if role != "user":
            return None
    elif tool_id == "cursor":
        from .conversation_parser import (
            normalize_cursor_additional_directives,
            split_cursor_user_payload,
        )

        text, _timestamp, _context = split_cursor_user_payload(text)
        directives = normalize_cursor_additional_directives(text)
        if directives is not None:
            text = directives
    if not text or text.lower().startswith(_CLAUDE_LOCAL_COMMAND_PREFIXES):
        return None

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[#>*`\-\s]+", "", text).strip()
    if not text:
        return None
    if len(text) <= max_length:
        return text

    shortened = text[: max_length - 1].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(".,;:-") + "…"


def _friendly_codex_agent_title(
    metadata: dict | None,
    max_length: int = 96,
) -> str | None:
    """Build a readable task-oriented title from subagent metadata."""
    values = metadata or {}
    agent_path = str(values.get("agent_path") or "").strip()
    if agent_path:
        label = agent_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    else:
        label = str(values.get("agent_nickname") or "").strip()
    readable = re.sub(r"[_-]+", " ", label).strip()
    return readable[:max_length] or None


def _select_updated_document_title(
    existing_title: str | None,
    incoming_title: str,
    *,
    category: str,
    tool_id: str,
    metadata: object = None,
    incoming_title_is_explicit: bool = False,
) -> str:
    """Keep legitimate source titles across later transcript deltas."""
    stored_metadata = metadata if isinstance(metadata, dict) else {}
    if (
        tool_id == "claude_code"
        and category == "conversation"
        and stored_metadata.get("memento_title_source") == "claude_ai_title"
        and existing_title
    ):
        return incoming_title if incoming_title_is_explicit else existing_title
    if (
        tool_id == "codex"
        and category == "conversation"
        and existing_title
        and not _conversation_title_needs_derivation(existing_title, tool_id)
    ):
        return existing_title
    return incoming_title


async def _apply_friendly_conversation_title(
    db: AsyncSession,
    doc: Document,
) -> str | None:
    """Replace opaque transcript identifiers with the first real user prompt."""
    metadata = document_metadata(doc)
    title_source = str(metadata.get("memento_title_source") or "").strip().lower()
    legacy_title_source = str(metadata.get("title_source") or "").strip().lower()
    try:
        title_revision = int(metadata.get("codex_title_revision") or 0)
    except (TypeError, ValueError):
        title_revision = 0
    title_revisions = metadata.get("codex_title_revisions")
    has_source_revision = title_revision > 0 or (
        isinstance(title_revisions, dict)
        and any(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in title_revisions.values()
        )
    )
    if (
        doc.tool_id == "codex"
        and (
            (title_source == "codex_explicit_rename" and has_source_revision)
            or title_source in {"manual", "memento_manual", "memento_user"}
            or legacy_title_source in {"manual", "user", "memento_manual"}
            or metadata.get("title_is_manual") is True
        )
        and not _conversation_title_needs_derivation(doc.title, doc.tool_id)
    ):
        # The metadata-only rename endpoint is the sole writer of this marker.
        # Preserve it across later FULL transcript ingests, including subagents
        # whose agent_path would otherwise overwrite the explicit source title.
        return doc.title
    if (
        doc.tool_id == "codex"
        and str(metadata.get("thread_source") or "").strip().lower() == "subagent"
    ):
        # A subagent starts with a cloned copy of its root transcript. Its first
        # user row therefore describes the parent task, not this fork. Prefer
        # the task-oriented agent path (or nickname fallback) unconditionally.
        agent_title = _friendly_codex_agent_title(metadata)
        if agent_title:
            doc.title = agent_title
            return agent_title
    if not _conversation_title_needs_derivation(doc.title, doc.tool_id):
        return doc.title
    if doc.tool_id == "cursor":
        from .conversation_parser import normalize_cursor_additional_directives

        if normalize_cursor_additional_directives(doc.title or "") is not None:
            friendly = _friendly_conversation_title(
                doc.title or "",
                tool_id="cursor",
            )
            if friendly:
                doc.title = friendly
                return friendly

    result = await db.execute(
        select(ConversationMessage.content)
        .where(
            ConversationMessage.document_id == doc.id,
            ConversationMessage.role == "user",
        )
        .order_by(ConversationMessage.line_number.asc())
        .limit(25)
    )
    for content in result.scalars():
        friendly = _friendly_conversation_title(
            content or "",
            tool_id=doc.tool_id,
        )
        if friendly:
            doc.title = friendly
            return friendly
    if doc.tool_id == "codex":
        agent_title = _friendly_codex_agent_title(document_metadata(doc))
        if agent_title:
            doc.title = agent_title
            return agent_title
    return doc.title


_WORKSPACE_PATTERNS = [
    # d:/dev/2026/0123/project_name/... (with or without file:/// or e:/// prefix)
    re.compile(r"([a-zA-Z]:/dev/\d{4}/\d+/[^/\s\)\]\"*?<>|`]+)"),
    # d:/dev/MMDD/project_name/...
    re.compile(r"([a-zA-Z]:/dev/\d+/[^/\s\)\]\"*?<>|`]+)"),
    # C:/Users/xxx/Desktop/project_name/...
    re.compile(r"([a-zA-Z]:/Users/[^/]+/Desktop/[^/\s\)\]\"*?<>|`]+)"),
    # /Users/xxx/Desktop/dev/lang/project/...
    re.compile(r"(/Users/[^/]+/Desktop/dev/[^/]+/[^/\s\)\]\"*?<>|`]+)"),
    # F:/dev/project/...
    re.compile(r"([a-zA-Z]:/dev/[^/\s\)\]\"*?<>|`]+)"),
]


def _extract_workspace_from_content(content: str) -> tuple[str | None, str | None]:
    """Extract (project_name, full_path) from brain file content."""
    from collections import Counter

    roots: Counter[str] = Counter()
    for pattern in _WORKSPACE_PATTERNS:
        for match in pattern.finditer(content):
            root = match.group(1).replace("\\", "/")
            if "/antigravity/" in root or "/.gemini/" in root:
                continue
            roots[root] += 1

    if not roots:
        return None, None

    best_root = roots.most_common(1)[0][0]
    parts = best_root.rstrip("/").split("/")
    project_name = parts[-1] if parts else None
    return project_name, best_root


async def ensure_tool(db: AsyncSession, tool_id: str) -> Tool:
    """Ensure a tool record exists, create if needed."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = result.scalar_one_or_none()
    if tool is None:
        tool = Tool(
            id=tool_id,
            display_name=tool_display_name(tool_id),
        )
        db.add(tool)
        await db.flush()
    return tool


def _prettify_project_name(raw: str) -> str:
    """Convert path-encoded project hash to a human-readable project name.

    Examples:
      '-Users-haixingdong-Desktop-dev-python-quant-future' → 'quant-future'
      'Users-haixingdong-Desktop-dev-ft-userdata' → 'ft-userdata'
      'D--dev-2026-0104-yicaigou-bulk-import' → 'bulk-import'
      'd--dev-1106-chembook' → 'chembook'
    """
    name = raw.strip("-")

    # Known path prefix patterns to strip (greedy match)
    # Pattern: optional drive + common dirs + optional date folders
    prefix_re = re.compile(
        r"^(?:[A-Za-z]--?)?"  # optional drive letter: D-- or C-
        r"(?:Users-[^-]+-(?:Desktop-?|Documents-?)?)?"  # Users-xxx-Desktop- or Users-xxx-
        r"(?:dev-?)?"  # dev-
        r"(?:python-?)?"  # python-
        r"(?:\d{4}-\d{2,4}-?)?"  # 2026-0104- (year-monthday)
        r"(?:\d{2,4}-?)?",  # or just MMDD-
        re.IGNORECASE,
    )
    cleaned = prefix_re.sub("", name).strip("-")
    return cleaned if cleaned else raw


def _hash_to_path(project_hash: str) -> str:
    """Convert path-encoded project hash back to a readable filesystem path.

    'Users-haixingdong-Desktop-dev-python-quant-future' → '/Users/haixingdong/Desktop/dev/python/quant-future'
    'D--dev-2026-0104-yicaigou' → 'D:/dev/2026/0104/yicaigou'
    """
    raw = project_hash.strip("-")
    # Windows drive: 'D--dev-...' → 'D:/dev/...'
    m = re.match(r"^([A-Za-z])--(.+)$", raw)
    if m:
        return f"{m.group(1)}:/{m.group(2).replace('-', '/')}"
    # Unix: 'Users-xxx-Desktop-dev-...' → '/Users/xxx/Desktop/dev/...'
    if raw.startswith("Users-"):
        return "/" + raw.replace("-", "/")
    return project_hash


def _clean_source_path(path: str | None) -> str | None:
    if not path:
        return path
    # Strip file:/// URI prefix
    if path.startswith("file:///"):
        path = path[8:] if len(path) > 9 and path[9:10] == ":" else path[7:]
    # URL decode
    from urllib.parse import unquote

    path = unquote(path)
    # Strip \\?\
    path = re.sub(r"^\\\\?\?\\", "", path)
    return path


async def ensure_project(
    db: AsyncSession,
    tool_id: str,
    project_hash: str,
    source_path: str | None = None,
) -> Project:
    """Ensure a project record exists for a given hash/path."""
    source_path = _clean_source_path(source_path)
    slug = f"{tool_id}/{project_hash}"
    result = await db.execute(select(Project).where(Project.slug == slug))
    project = result.scalar_one_or_none()
    if project is None:
        project = Project(
            slug=slug,
            title=project_hash,
            tool_id=tool_id,
            source_path=source_path or project_hash,
        )
        db.add(project)
        await db.flush()
    elif source_path and (
        not project.source_path
        or project.source_path == project.title
        or len(project.source_path) < 10
    ):
        # Update incomplete source_path with better data
        project.source_path = source_path
    return project


def _scoped_document_select(
    tool_id: str,
    relative_path: str,
    machine_id: str | None,
    user_id: str | None,
):
    """Select one source document without crossing device/user boundaries."""
    statement = select(Document).where(
        Document.tool_id == tool_id,
        Document.relative_path == relative_path,
        Document.machine_id == machine_id,
    )
    if user_id is not None:
        statement = statement.where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            )
        )
    return statement


def _ingest_document_load_only():
    """Load ingest state without pulling raw source bodies on every DELTA."""
    return load_only(
        Document.id,
        Document.tool_id,
        Document.project_id,
        Document.machine_id,
        Document.relative_path,
        Document.category,
        Document.content_type,
        Document.title,
        Document.content_s3_key,
        Document.content_object_sha256,
        Document.content_object_size_bytes,
        Document.content_object_verified_at,
        Document.content_hash,
        Document.file_size_bytes,
        Document.metadata_,
        Document.needs_review,
        Document.embedding_status,
        Document.embedding_attempts,
        Document.embedding_claim_token,
        Document.embedding_claimed_at,
        Document.embedding_content_hash,
        Document.embedding_tier,
        Document.source_modified_at,
        Document.activity_at,
        Document.synced_at,
        Document.visibility,
    )


async def _reconcile_claude_subagent_launch_metadata(
    db: AsyncSession,
    document: Document,
    *,
    machine_id: str | None,
    user_id: str | None,
    pair_locked: bool = False,
) -> Document | None:
    """Enrich an exact Claude transcript/sidecar sibling pair once."""
    if document.tool_id != "claude_code":
        return None

    normalized_path = _normalized_claude_relative_path(document.relative_path)
    if normalized_path is None:
        return None

    sidecar_document: Document | None
    transcript_document: Document | None
    if document.category == "state":
        evidence = _claude_subagent_sidecar_evidence(
            normalized_path,
            await document_content(db, document),
        )
        if evidence is None:
            return None
        transcript_path, launch_metadata = evidence
        sidecar_document = document
        transcript_document = None
    elif document.category == "conversation":
        sidecar_path = _claude_subagent_sidecar_path(normalized_path)
        if sidecar_path is None:
            return None
        transcript_path = normalized_path
        sidecar_document = None
        transcript_document = document
        launch_metadata = {}
    else:
        return None

    # Sidecar and transcript uploads have different source locks. A shared
    # transaction lock closes the concurrent-arrival gap while retaining exact
    # path/device/user scoping.
    if not pair_locked:
        await _lock_ingest_source(
            db,
            machine_id=machine_id,
            user_id=user_id,
            tool_id="claude_code",
            relative_path=f"sidecar-pair:{transcript_path}",
        )

    if sidecar_document is None:
        sidecar_document = (
            await db.execute(
                _scoped_document_select(
                    "claude_code",
                    sidecar_path,
                    machine_id,
                    user_id,
                ).with_for_update(of=Document)
            )
        ).scalar_one_or_none()
        if sidecar_document is None or sidecar_document.category != "state":
            return None
        evidence = _claude_subagent_sidecar_evidence(
            sidecar_document.relative_path,
            await document_content(db, sidecar_document),
        )
        if evidence is None:
            return None
        evidence_transcript_path, launch_metadata = evidence
        if evidence_transcript_path != transcript_path:
            return None

    if transcript_document is None:
        transcript_document = (
            await db.execute(
                _scoped_document_select(
                    "claude_code",
                    transcript_path,
                    machine_id,
                    user_id,
                ).with_for_update(of=Document)
            )
        ).scalar_one_or_none()
    if (
        transcript_document is None
        or transcript_document.category != "conversation"
        or _normalized_claude_relative_path(transcript_document.relative_path)
        != transcript_path
    ):
        return None

    agent_id = str(launch_metadata["agent_id"])
    session_id = document_metadata(transcript_document).get("session_id")
    if session_id and str(session_id) != f"agent-{agent_id}":
        return None

    existing_metadata = document_metadata(transcript_document)
    if all(
        existing_metadata.get(key) == value
        for key, value in launch_metadata.items()
    ):
        return None
    merged_metadata, _, _ = _prepare_document_metadata(
        {**existing_metadata, **launch_metadata},
        tool_id="claude_code",
    )
    delivery_state = await ensure_document_delivery_state(db, transcript_document)
    attach_document_delivery(
        transcript_document,
        delivery_state,
        runtime_only=True,
    )
    store_document_metadata(transcript_document, merged_metadata)
    return transcript_document


def _stage_ingest_read_cache_invalidations(
    db: AsyncSession,
    user_id: str | None,
    project_id: object | None,
    *,
    daily: bool,
    project: bool,
) -> None:
    if not user_id:
        return
    from .cache import (
        daily_cache_namespace,
        project_conversations_cache_namespace,
        stage_cache_invalidation,
    )

    namespaces: list[str] = []
    if daily:
        namespaces.append(daily_cache_namespace(user_id))
    if project and project_id:
        namespaces.append(
            project_conversations_cache_namespace(user_id, project_id)
        )
    stage_cache_invalidation(db, *namespaces)


def _reconcile_subagent_document_lifecycle(
    document: Document,
    content: str | None,
    *,
    source_timestamp: object = None,
    source_objects: Iterable[object] | None = None,
) -> bool:
    """Persist source-backed child state without treating silence as completion."""
    if document.category != "conversation":
        return False
    from .conversation_hierarchy import is_conversation_subagent

    if not is_conversation_subagent(
        document.tool_id,
        document.relative_path,
        document_metadata(document),
    ):
        return False
    evidence = (
        child_lifecycle_evidence_from_objects(
            document.tool_id,
            document_metadata(document),
            source_objects,
            source_timestamp=source_timestamp or document.source_modified_at,
        )
        if source_objects is not None
        else child_lifecycle_evidence(
            document.tool_id,
            document_metadata(document),
            content,
            source_timestamp=source_timestamp or document.source_modified_at,
        )
    )
    metadata, changed = reconcile_child_lifecycle_metadata(
        document_metadata(document),
        evidence,
    )
    if changed:
        store_document_metadata(document, metadata)
    return changed


def _interaction_event_signature(metadata: object) -> str:
    values = metadata if isinstance(metadata, dict) else {}
    scoped = {
        key: values.get(key)
        for key in (
            CURRENT_PENDING_QUESTIONS_KEY,
            INTERACTION_HISTORY_KEY,
            LIVE_INTERACTION_SIGNALS_KEY,
            LIVE_SHELL_ACTIVITIES_KEY,
            PENDING_QUESTION_COUNT_KEY,
        )
        if key in values
    }
    return json.dumps(scoped, ensure_ascii=False, sort_keys=True, default=str)


def _conversation_event_changes(
    *,
    mode: str,
    search_text: str,
    title_changed: bool,
    interactions_changed: bool,
    dashboard_changed: bool,
) -> list[str]:
    changes = {
        "conversation.messages",
        "conversation.metadata",
        "project",
    }
    if dashboard_changed:
        changes.add("dashboard")
    if mode == "full" or "[user]" in search_text:
        changes.add("conversation.prompts")
    if mode == "full" or search_text or title_changed:
        changes.add("conversation.search")
    if interactions_changed:
        changes.add("conversation.pending_interactions")
    return sorted(changes)


# The dashboard's message total is useful during a long-running conversation,
# but sending a global invalidation for every normalized row recreates the SSE
# refetch stampede. Crossing this stable bucket is stateless across workers.
_DASHBOARD_MESSAGE_COUNT_EVENT_BUCKET = 20


def _dashboard_projection_requires_event(
    projection: DashboardDocumentProjection,
    *,
    is_new_document: bool = False,
) -> bool:
    """Return whether this projection change warrants a dashboard refetch."""
    state = inspect(projection)
    if is_new_document or state.pending:
        return True
    # These are immediately visible on the dashboard, unlike delivery-only
    # fields such as synced_at and hierarchy bookkeeping.
    if any(
        state.attrs[field].history.has_changes()
        for field in (
            "title",
            "category",
            "activity_at",
            "is_archived",
            "pending_question_count",
        )
    ):
        return True
    message_count = state.attrs.message_count.history
    if not message_count.deleted:
        return False
    previous_count = int(message_count.deleted[0] or 0)
    return (
        previous_count // _DASHBOARD_MESSAGE_COUNT_EVENT_BUCKET
        != int(projection.message_count or 0) // _DASHBOARD_MESSAGE_COUNT_EVENT_BUCKET
    )


def _publish_file_synced_event(
    db: AsyncSession,
    document: Document,
    user_id: str | None,
    *,
    changes: Iterable[str] | None = None,
) -> None:
    from ..db.session import queue_realtime_event

    event_changes = changes
    if event_changes is None:
        event_changes = (
            {
                "conversation.messages",
                "conversation.metadata",
                "conversation.pending_interactions",
                "conversation.prompts",
                "conversation.search",
                "dashboard",
                "project",
            }
            if document.category == "conversation"
            else {"dashboard", "project"}
        )
    scoped_changes = set(event_changes)
    if document.project_id is None:
        scoped_changes.discard("project")
    queue_realtime_event(
        db,
        "file_synced",
        {
            "document_id": str(document.id),
            "tool_id": document.tool_id,
            "category": document.category,
            "relative_path": document.relative_path,
            "title": document.title,
            "project_id": (
                str(document.project_id)
                if document.project_id is not None
                else None
            ),
            "changes": sorted(scoped_changes),
        },
        user_id=user_id,
    )


async def _reconcile_idempotent_claude_ingest(
    db: AsyncSession,
    document: Document,
    *,
    machine_id: str | None,
    user_id: str | None,
    pair_locked: bool = False,
) -> None:
    enriched_child = await _reconcile_claude_subagent_launch_metadata(
        db,
        document,
        machine_id=machine_id,
        user_id=user_id,
        pair_locked=pair_locked,
    )
    lifecycle_document = enriched_child or document
    lifecycle_changed = _reconcile_subagent_document_lifecycle(
        lifecycle_document,
        # Go through the verified pointer accessor; raw source has no ORM body
        # after the contract migration.
        await document_content(db, lifecycle_document),
    )
    if enriched_child is None and not lifecycle_changed:
        return
    await db.flush()
    from .dashboard_projection import refresh_dashboard_document_projection

    projection, _ = await refresh_dashboard_document_projection(db, lifecycle_document)
    _stage_ingest_read_cache_invalidations(
        db,
        user_id,
        lifecycle_document.project_id,
        daily=False,
        project=True,
    )
    event_changes = {"conversation.metadata", "project"}
    if _dashboard_projection_requires_event(projection):
        event_changes.add("dashboard")
    _publish_file_synced_event(
        db,
        lifecycle_document,
        user_id,
        changes=event_changes,
    )


def _scoped_sync_state_select(
    tool_id: str,
    relative_path: str,
    machine_id: str | None,
    user_id: str | None,
):
    """Select sync state using the same ownership key as its document."""
    statement = select(SyncState).where(
        SyncState.tool_id == tool_id,
        SyncState.relative_path == relative_path,
        SyncState.machine_id == machine_id,
    )
    if user_id is not None:
        statement = statement.where(
            SyncState.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            )
        )
    return statement


def _scoped_conversation_identity_select(
    tool_id: str,
    session_id: str,
    machine_id: str | None,
    user_id: str | None,
):
    """Select all same-device aliases for one verified session UUID."""
    effective_metadata = delivery_metadata_expression()
    statement = select(Document).where(
        Document.tool_id == tool_id,
        Document.category == "conversation",
        Document.machine_id == machine_id,
        effective_metadata["session_id"].astext == session_id,
    )
    if tool_id == "codex":
        statement = statement.where(
            effective_metadata["thread_id"].astext == session_id,
        )
    if user_id is not None:
        statement = statement.where(
            Document.machine_id.in_(
                select(Machine.id).where(Machine.user_id == user_id)
            )
        )
    return statement


def _scoped_cursor_identity_select(
    session_id: str,
    machine_id: str | None,
    user_id: str | None,
):
    """Backward-compatible Cursor-specific identity selector."""
    return _scoped_conversation_identity_select(
        "cursor",
        session_id,
        machine_id,
        user_id,
    )


def _source_lock_id(
    machine_id: str | None,
    user_id: str | None,
    tool_id: str,
    relative_path: str,
    source_identity: str | None = None,
) -> int:
    """Return a stable signed 64-bit advisory-lock key for one source."""
    owner = (
        f"machine:{machine_id}"
        if machine_id is not None
        else f"user:{user_id or 'legacy'}"
    )
    source_key = (
        f"identity:{source_identity}"
        if source_identity is not None
        else f"path:{relative_path}"
    )
    identity = json.dumps(
        [owner, tool_id, source_key],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(b"memento:ingest-source:v1\0" + identity).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def _lock_ingest_source(
    db: AsyncSession,
    *,
    machine_id: str | None,
    user_id: str | None,
    tool_id: str,
    relative_path: str,
    source_identity: str | None = None,
) -> None:
    """Serialize all direct and spooled writers until their transaction ends."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS bigint))"),
        {
            "lock_id": _source_lock_id(
                machine_id,
                user_id,
                tool_id,
                relative_path,
                source_identity,
            )
        },
    )


async def ingest_file(
    db: AsyncSession,
    tool_id: str,
    category: str,
    content_type: str,
    relative_path: str,
    content: str,
    content_hash: str,
    file_size: int,
    mode: str,
    offset: int,
    metadata: dict,
    timestamp: float | None = None,
    machine_id: str | None = None,
    user_id: str | None = None,
    schedule_post_ingest: bool = True,
    persist_content: bool = True,
    content_s3_key: str | None = None,
    content_already_sanitized: bool = False,
    content_had_sensitive: bool = False,
    conversation_source: ConversationFileSource | None = None,
    base_hash: str | None = None,
    base_offset: int | None = None,
    authoritative_rebase: bool = False,
    use_core_delta_message_staging: bool = True,
    writer: str | None = None,
) -> Document | object:
    """Process and store an ingested file."""
    if writer == "raw" and conversation_source is not None:
        # The stream-backed FULL path retains its bounded parser/source handle;
        # Phase 2's inline raw reducer must not accidentally treat it as an
        # empty body.  This is an unhandled shape, so select the old writer
        # before a raw transaction is opened.
        writer = "legacy"
    if writer == "raw":
        # The raw implementation owns one asyncpg transaction and has no
        # mapped instances/session work in that transaction.  Unsupported
        # reducers and every pre-commit raw failure are deliberately retried
        # through this unchanged writer before this call returns.
        from .realtime_raw_writer import (
            RawWriterFailure,
            RawWriterUnsupported,
            ingest_conversation_raw,
        )

        try:
            raw_document, raw_event = await ingest_conversation_raw(
                tool_id=tool_id,
                category=category,
                content_type=content_type,
                relative_path=relative_path,
                content=content,
                content_hash=content_hash,
                file_size=file_size,
                mode=mode,
                offset=offset,
                metadata=metadata,
                timestamp=timestamp,
                machine_id=machine_id,
                user_id=user_id,
                base_hash=base_hash,
                base_offset=base_offset,
                authoritative_rebase=authoritative_rebase,
                database_url=db.get_bind().url.render_as_string(
                    hide_password=False
                ),
                content_already_sanitized=content_already_sanitized,
                content_had_sensitive=content_had_sensitive,
            )
        except (RawWriterUnsupported, RawWriterFailure) as raw_error:
            # The asyncpg transaction is rolled back before either exception
            # can escape.  Never run this fallback after a raw commit.
            # Canary observability: fallbacks MUST be visible in logs or the
            # rollout cannot be monitored.
            logging.getLogger("realtime_ingest").warning(
                "raw writer fallback to legacy for %s/%s (%s): %s",
                tool_id,
                relative_path,
                type(raw_error).__name__,
                str(raw_error)[:200],
            )
            writer = "legacy"
        else:
            # The raw writer commits through its own asyncpg transaction.
            # Invalidate only tables it can have changed: expiring unrelated
            # caller-owned Machine/User instances makes ordinary scalar access
            # attempt async IO outside greenlet context.
            raw_mutated_tables = {
                "tools",
                "documents",
                "document_delivery_state",
                "sync_state",
                "conversation_messages",
                "conversation_usage_events",
                "conversation_read_models",
                "conversation_prompt_projections",
                "conversation_task_states",
                "dashboard_document_projections",
                "projects",
            }
            for mapped_instance in tuple(db.identity_map.values()):
                if getattr(mapped_instance, "__tablename__", None) in raw_mutated_tables:
                    db.expire(mapped_instance)
            if raw_event is not None:
                from ..db.session import queue_realtime_event

                if raw_event.get("claw_delegate_metadata"):
                    queue_realtime_event(
                        db,
                        "file_synced",
                        {
                            "document_id": str(raw_document.id),
                            "changes": [
                                "conversation.metadata",
                                "dashboard",
                                "project",
                            ],
                        },
                        user_id=raw_event["user_id"],
                    )
                cache_scope = raw_event.get("cache") or {}
                _stage_ingest_read_cache_invalidations(
                    db,
                    raw_event["user_id"],
                    cache_scope.get("project_id"),
                    daily=bool(cache_scope.get("daily")),
                    project=bool(cache_scope.get("project")),
                )
                queue_realtime_event(
                    db,
                    raw_event["event_type"],
                    raw_event["data"],
                    user_id=raw_event["user_id"],
                )
            if (
                schedule_post_ingest
                and raw_document._memento_ingest_disposition == "committed"
            ):
                await _schedule_post_ingest_work(
                    document_id=raw_document.id,
                    tool_id=tool_id,
                    category=category,
                    file_size_bytes=raw_document.file_size_bytes,
                    revision=content_hash,
                )
            return raw_document
    content_store_enabled = settings.document_content_minio_enabled
    metadata = dict(metadata or {})
    category = normalize_ingest_category(tool_id, category, relative_path)
    cursor_projection_delta = (
        mode == "delta"
        and tool_id == "cursor"
        and metadata.get("source") == "cursor_state_v1"
    )
    cursor_projection_order = metadata.pop(CURSOR_PROJECTION_ORDER_KEY, None)
    if cursor_projection_order is not None and not cursor_projection_delta:
        raise ValueError(
            "Cursor projection ordering hints require a Cursor state delta"
        )
    if conversation_source is not None:
        if category != "conversation" or content_type != "jsonl":
            raise ValueError(
                "streamed sources are supported only for conversation JSONL"
            )
        if content:
            raise ValueError("streamed conversation ingest must not include inline content")
        if not content_already_sanitized:
            raise ValueError("streamed conversation source must already be sanitized")
        # With the contract column gone, flag-off is deliberately only an
        # emergency write bypass. The source is still parsed, but no raw body
        # is committed; recovery is reprocessing it from the client source.
        file_size = conversation_source.size
    content_preview = (
        conversation_source.prefix if conversation_source is not None else content
    )
    received_at = datetime.now(timezone.utc)
    source_modified_at = bounded_source_timestamp(timestamp, received_at) or received_at
    stable_source_identity = conversation_session_id(tool_id, category, metadata)
    await _lock_ingest_source(
        db,
        machine_id=machine_id,
        user_id=user_id,
        tool_id=tool_id,
        relative_path=relative_path,
        source_identity=stable_source_identity,
    )
    claude_pair_transcript_path = (
        _claude_subagent_pair_transcript_path(relative_path, category)
        if tool_id == "claude_code"
        else None
    )
    claude_pair_locked = claude_pair_transcript_path is not None
    if claude_pair_transcript_path is not None:
        await _lock_ingest_source(
            db,
            machine_id=machine_id,
            user_id=user_id,
            tool_id="claude_code",
            relative_path=f"sidecar-pair:{claude_pair_transcript_path}",
        )
    # Fast-path dedup: if this exact (tool_id, relative_path, content_hash,
    # offset) was already ingested, skip everything. Common in multi-collector
    # setups where pip + Tauri sidecar both watch the same .jsonl and resend
    # the same chunk within milliseconds. Without this, the second request:
    #   - holds a get_db() connection for several seconds
    #   - races UPDATE on the same Document row
    #   - fires a redundant post-ingest task that re-embeds 50 chunks
    # all to write the same bytes back to the same row.
    sync_row = (
        await db.execute(
            _scoped_sync_state_select(
                tool_id,
                relative_path,
                machine_id,
                user_id,
            )
        )
    ).scalar_one_or_none()
    path_doc = (
        await db.execute(
            _scoped_document_select(
                tool_id,
                relative_path,
                machine_id,
                user_id,
            )
            # The normal conversation DELTA never reads raw source; load only
            # the immutable pointer and mutable ingest state.
            .options(_ingest_document_load_only())
            .with_for_update(of=Document)
        )
    ).scalar_one_or_none()
    doc = path_doc
    if stable_source_identity is not None:
        identity_documents = (
            (
                await db.execute(
                    _scoped_conversation_identity_select(
                        tool_id,
                        stable_source_identity,
                        machine_id,
                        user_id,
                    )
                    # Same canonical-document path: retain mutable ingest
                    # fields but leave the raw source body deferred.
                    .options(_ingest_document_load_only())
                    .with_for_update(of=Document)
                )
            )
            .scalars()
            .all()
        )
        if identity_documents:
            doc = select_canonical_conversation_document(
                identity_documents,
                tool_id=tool_id,
                session_id=stable_source_identity,
            )
    delivery_state = None
    if doc is not None and category == "conversation":
        delivery_state = await ensure_document_delivery_state(db, doc)
        attach_document_delivery(
            doc,
            delivery_state,
            runtime_only=mode == "delta",
        )
    current_revision = (
        delivery_state.revision_hash
        if delivery_state is not None
        else (doc.content_hash if doc is not None else None)
    )
    current_source_modified_at = (
        delivery_state.source_modified_at
        if delivery_state is not None
        else (doc.source_modified_at if doc is not None else None)
    )
    current_file_size = (
        delivery_state.file_size_bytes
        if delivery_state is not None
        else (doc.file_size_bytes if doc is not None else 0)
    )
    identity_path_conflict = (
        path_doc is not None and doc is not None and path_doc.id != doc.id
    )
    identity_relocation = (
        stable_source_identity is not None
        and doc is not None
        and not identity_path_conflict
        and should_relocate_conversation_document(
            tool_id=tool_id,
            session_id=stable_source_identity,
            current_path=doc.relative_path,
            incoming_path=relative_path,
            current_modified_at=current_source_modified_at,
            incoming_modified_at=source_modified_at,
        )
    )
    # Repair timestamps accepted before source-clock bounding was introduced.
    # Leaving a future value in place would make later valid FULL snapshots
    # look stale indefinitely, even though new incoming times are bounded.
    if doc is not None and current_source_modified_at is not None:
        observed_at = (
            delivery_state.synced_at
            if delivery_state is not None
            else doc.synced_at
        ) or received_at
        current_source_modified_at = bounded_source_timestamp(
            current_source_modified_at,
            observed_at,
        )
        update_document_source_modified(doc, current_source_modified_at)
    is_new_document = doc is None
    previous_title = doc.title if doc is not None else None
    previous_interaction_signature = _interaction_event_signature(
        document_metadata(doc) if doc is not None else {}
    )
    previous_embedding_content_hash: str | None = None
    logical_file_size = _logical_document_file_size(
        mode=mode,
        payload_size=file_size,
        offset=offset,
        existing_size=current_file_size,
        replace_offset=cursor_projection_delta,
    )
    same_hash_before_write = doc is not None and current_revision == content_hash
    if (
        sync_row is not None
        and doc is not None
        and current_revision == content_hash
        and sync_row.last_hash == content_hash
        and sync_row.last_offset == offset
        and not identity_relocation
    ):
        # Touch last_synced_at so dashboards know we still see this file,
        # but skip all the actual ingestion work + the post-ingest task.
        sync_row.last_synced_at = received_at
        pointer_is_current = _stored_source_is_current(
            doc,
            content_hash,
            incoming_s3_key=content_s3_key,
        )
        if pointer_is_current or cursor_projection_delta:
            latest_source_modified_at = max(
                filter(None, (current_source_modified_at, source_modified_at))
            )
            update_document_source_modified(doc, latest_source_modified_at)
            await _reconcile_idempotent_claude_ingest(
                db,
                doc,
                machine_id=machine_id,
                user_id=user_id,
                pair_locked=claude_pair_locked,
            )
            setattr(doc, "_memento_ingest_disposition", "idempotent")
            return doc

    if (
        mode == "delta"
        and not cursor_projection_delta
        and doc is not None
        and sync_row is not None
        and sync_row.last_hash == current_revision
        and int(sync_row.last_offset or 0) >= offset
    ):
        # A delayed/replayed append can remain in the durable spool after a
        # newer contiguous batch commits. It is already represented by the
        # authoritative committed offset, so treating its older base as a
        # mismatch creates a retry/quarantine storm instead of making progress.
        sync_row.last_synced_at = received_at
        setattr(doc, "_memento_ingest_disposition", "stale_delta")
        return doc

    if mode == "delta" and base_hash is not None:
        expected_hash, expected_offset = _committed_delta_base(doc, sync_row)
        if (
            expected_hash != base_hash
            or base_offset is None
            or expected_offset != int(base_offset)
        ):
            raise DeltaBaseMismatch(
                expected_hash=expected_hash,
                expected_offset=expected_offset,
            )

    if (
        mode == "full"
        and doc is not None
        and current_revision == content_hash
        and not identity_relocation
    ):
        pointer_is_current = _stored_source_is_current(
            doc,
            content_hash,
            incoming_s3_key=content_s3_key,
        )
        if pointer_is_current:
            if (
                current_source_modified_at is None
                or source_modified_at > current_source_modified_at
            ):
                update_document_source_modified(doc, source_modified_at)
            await _update_sync_state(
                db,
                tool_id,
                relative_path,
                content_hash,
                offset,
                machine_id,
                user_id,
                mode=mode,
                monotonic_offset=True,
            )
            await _reconcile_idempotent_claude_ingest(
                db,
                doc,
                machine_id=machine_id,
                user_id=user_id,
                pair_locked=claude_pair_locked,
            )
            setattr(doc, "_memento_ingest_disposition", "idempotent")
            return doc

    if (
        mode == "full"
        and doc is not None
        and current_revision != content_hash
        and not authoritative_rebase
    ):
        existing_offset = 0
        if sync_row is not None and sync_row.last_hash == current_revision:
            existing_offset = int(sync_row.last_offset or 0)
        if committed_full_supersedes(
            existing_hash=current_revision,
            existing_timestamp=current_source_modified_at,
            existing_offset=existing_offset,
            existing_size=current_file_size,
            incoming_hash=content_hash,
            incoming_timestamp=source_modified_at,
            incoming_offset=offset,
            incoming_size=file_size,
        ):
            setattr(doc, "_memento_ingest_disposition", "superseded")
            return doc

    # Re-sanitize
    content = content.replace("\x00", "")  # PostgreSQL TEXT rejects null bytes
    if conversation_source is not None:
        had_sensitive = content_had_sensitive
    elif content_already_sanitized:
        had_sensitive = content_had_sensitive
    else:
        content, had_sensitive = _resanitize(content)

    # Collector metadata is advisory and older clients omitted Codex thread
    # identity entirely.  The first session_meta object is authoritative and
    # cheap to parse even for an externalized multi-hundred-megabyte FULL.
    if tool_id == "codex" and category == "conversation" and content_preview:
        from .conversation_parser import extract_codex_session_metadata

        metadata.update(extract_codex_session_metadata(content_preview))

    if tool_id in {"cursor", "claude_code"} and category == "conversation":
        from .conversation_hierarchy import path_linked_subagent_identity

        for key, value in path_linked_subagent_identity(relative_path).items():
            metadata.setdefault(key, value)

    # Ensure tool exists
    tool = await ensure_tool(db, tool_id)

    # Extract project if present in metadata
    project_id = None
    project_hash = metadata.get("project_hash")

    # Server-side project extraction fallback
    # Trigger if: no hash, UUID-like, contains --, or looks like a path-encoded hash (Users-xxx or drive--)
    _looks_like_hash = bool(
        project_hash
        and (
            re.match(r"^[0-9a-f]{8}-", project_hash)
            or "--" in project_hash
            or re.match(r"^-?Users-", project_hash)
            or re.match(r"^[A-Za-z]--", project_hash)
            or len(project_hash) > 30
        )
    )
    _needs_extract = not project_hash or _looks_like_hash
    project_path: str | None = metadata.get("project_path")

    if _needs_extract and content_preview and category == "conversation":
        # Universal: extract cwd from first occurrence in content (Claude Code, Codex, Cursor all have it)
        cwd_match = re.search(r'"cwd"\s*:\s*"([^"]+)"', content_preview[:10000])
        if cwd_match:
            raw_cwd = cwd_match.group(1)
            raw_cwd = re.sub(r"^\\\\?\?\\", "", raw_cwd)
            cwd = raw_cwd.replace("\\", "/").rstrip("/")
            project_path = project_path or raw_cwd
            project_hash = cwd.split("/")[-1]
        elif _looks_like_hash and project_hash:
            # No cwd found but hash looks like encoded path — prettify it
            project_hash = _prettify_project_name(project_hash)

    if (
        _needs_extract
        and content_preview
        and tool_id == "antigravity"
        and "brain" in relative_path
    ):
        # Antigravity: extract workspace from file:// URIs in brain content
        extracted_name, extracted_path = _extract_workspace_from_content(
            content_preview
        )
        if extracted_name:
            project_hash = extracted_name
            if extracted_path and not project_path:
                project_path = extracted_path

    if project_hash:
        # Sanitize: strip control characters and null bytes
        project_hash = re.sub(r"[\x00-\x1f].*", "", project_hash).strip()
    if project_hash:
        if not project_path:
            project_path = metadata.get("project_path")
        project = await ensure_project(
            db, tool_id, project_hash, source_path=project_path
        )
        project_id = project.id

    # Fallback: match project via session_id from existing documents
    if not project_id:
        session_id = metadata.get("session_id") or metadata.get("cascade_id")
        if session_id:
            effective_metadata = delivery_metadata_expression()
            project_statement = select(Document.project_id).where(
                Document.tool_id == tool_id,
                effective_metadata["session_id"].astext == session_id,
                Document.project_id.isnot(None),
                Document.machine_id == machine_id,
            )
            if user_id is not None:
                project_statement = project_statement.where(
                    Document.machine_id.in_(
                        select(Machine.id).where(Machine.user_id == user_id)
                    )
                )
            existing = await db.execute(project_statement.limit(1))
            row = existing.scalar_one_or_none()
            if row:
                project_id = row

    # These provenance fields are server-owned. A normal file upload must not
    # be able to impersonate the metadata-only rename endpoint or a future
    # Memento-side manual title. Existing protected values are merged below.
    collector_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in _PROTECTED_DOCUMENT_METADATA_KEYS
    }
    stored_metadata, user_history, first_user_message = _prepare_document_metadata(
        collector_metadata,
        tool_id=tool_id,
    )
    now = received_at
    incoming_title_is_explicit = (
        stored_metadata.pop("source_title_kind", None) == "claude_ai_title"
    )
    if incoming_title_is_explicit:
        stored_metadata["memento_title_source"] = "claude_ai_title"
    title = stored_metadata.pop("title", None) or relative_path.split("/")[-1]
    previous_stored_revision = (
        document_metadata(doc).get(STORED_SOURCE_REVISION_KEY)
        if doc is not None
        else None
    )
    # This is the exact transient source value. For a streamed FULL the
    # finalizer reads the sanitized file directly; conversation DELTAs retain
    # their existing durable FULL pointer.
    stored_blob_content = content
    stored_revision_hash = content_hash if mode == "full" else None
    preserve_stored_source_identity = False

    if doc is not None and category in _EMBEDDING_CATEGORIES:
        previous_embedding_content_hash = doc.embedding_content_hash
        if previous_embedding_content_hash is None:
            # Backward compatibility for rows created before the persisted
            # input identity existed: derive it while the old content and
            # messages are still current, before this ingest replaces them.
            from .embedding_service import document_embedding_input

            _, previous_embedding_content_hash = await document_embedding_input(
                db,
                doc,
            )
            doc.embedding_content_hash = previous_embedding_content_hash

    if doc is None:
        # Raw source is persisted only through a verified immutable pointer;
        # conversations are still fully parsed into ConversationMessage rows.
        from .embedding_service import desired_embedding_tier

        doc = Document(
            # Allocate before the immutable PUT so the final key is stable
            # throughout the still-uncommitted ingest transaction.
            id=uuid.uuid4(),
            tool_id=tool_id,
            project_id=project_id,
            machine_id=machine_id,
            relative_path=relative_path,
            category=category,
            content_type=content_type,
            title=title,
            content_hash=content_hash,
            file_size_bytes=logical_file_size,
            metadata_=stored_metadata,
            needs_review=had_sensitive,
            synced_at=now,
            source_modified_at=source_modified_at,
            # Initial tier only; quality is sticky thereafter (never auto-demoted).
            embedding_tier=desired_embedding_tier(category),
        )
        db.add(doc)
    else:
        # Update existing document
        doc.category = category
        doc.content_type = content_type
        if identity_relocation:
            doc.relative_path = relative_path
        if category == "conversation" and mode == "delta":
            # Raw conversation blobs are immutable snapshots. Keep the last
            # FULL pointer and append only normalized messages until another
            # FULL source snapshot arrives.
            preserve_stored_source_identity = True
        elif (
            mode == "delta" and conversation_source is None
        ):
            # Non-conversation DELTAs advance raw source. Fetch the existing
            # immutable object, construct the exact old+newline+delta value,
            # and let the finalizer publish a new immutable object. Never
            # mirror that value into PostgreSQL.
            previous_content = await document_content(db, doc)
            stored_blob_content = (
                previous_content + "\n" + content
                if previous_content
                else content
            )
            if previous_stored_revision == base_hash:
                stored_revision_hash = content_hash
            else:
                stored_revision_hash = None
        elif not content_store_enabled:
            # This is intentionally not a read rollback: after the cutover no
            # inline body exists. Clearing a stale pointer makes the emergency
            # S3-bypass's loss of raw persistence explicit and recoverable by
            # client-source reprocessing rather than serving old bytes.
            doc.content_s3_key = None
            doc.content_object_sha256 = None
            doc.content_object_size_bytes = None
            doc.content_object_verified_at = None
        latest_source_modified_at = max(
            filter(None, (current_source_modified_at, source_modified_at))
        )
        if delivery_state is not None:
            update_document_delivery(
                doc,
                delivery_state,
                revision_hash=content_hash,
                file_size_bytes=logical_file_size,
                source_modified_at=latest_source_modified_at,
                synced_at=now,
            )
        else:
            doc.content_hash = content_hash
            doc.file_size_bytes = logical_file_size
            doc.synced_at = now
            doc.source_modified_at = latest_source_modified_at
        existing_metadata = document_metadata(doc)
        existing_metadata.pop("user_history", None)
        existing_metadata.pop("first_user_message", None)
        metadata_update = (
            _merge_delta_metadata(existing_metadata, stored_metadata)
            if mode == "delta"
            else {
                **existing_metadata,
                **_preserve_interaction_provenance(
                    existing_metadata,
                    stored_metadata,
                ),
            }
        )
        if incoming_title_is_explicit:
            metadata_update["memento_title_source"] = "claude_ai_title"
        merged_metadata, _, _ = _prepare_document_metadata(
            metadata_update,
            tool_id=tool_id,
        )
        store_document_metadata(doc, merged_metadata)
        doc.needs_review = doc.needs_review or had_sensitive
        if machine_id and not doc.machine_id:
            doc.machine_id = machine_id
        doc.title = _select_updated_document_title(
            doc.title,
            title,
            category=category,
            tool_id=tool_id,
            metadata=document_metadata(doc),
            incoming_title_is_explicit=incoming_title_is_explicit,
        )
        # Backfill project_id when newly resolved (was NULL, or changed).
        # Don't overwrite an existing link with NULL — keep last good value.
        if project_id and doc.project_id != project_id:
            doc.project_id = project_id
        if delivery_state is not None:
            delivery_state.project_id = doc.project_id

        # Conversation DELTAs are transport fragments, not independently
        # restorable document versions (content_delta is not populated). Keep
        # checkpoints for FULL snapshots while avoiding one extra row/write for
        # every filesystem append.
        if category != "conversation" or mode == "full":
            version = DocumentVersion(
                document_id=doc.id,
                content_hash=content_hash,
                file_size_bytes=file_size,
            )
            db.add(version)

    if category == "conversation" and not preserve_stored_source_identity:
        if conversation_source is not None:
            _set_stored_source_proof(
                doc,
                source_hash=conversation_source.sha256,
                source_size=conversation_source.size,
                revision_hash=stored_revision_hash,
            )
        else:
            _set_stored_source_identity(
                doc,
                stored_blob_content,
                revision_hash=stored_revision_hash,
            )

    from sqlalchemy import func as _func
    from sqlalchemy import update as _update

    await db.flush()
    if category == "conversation" and delivery_state is None:
        delivery_state = await ensure_document_delivery_state(db, doc)
        attach_document_delivery(doc, delivery_state, runtime_only=False)
    if category == "conversation" and user_id:
        # Content and lightweight metadata travel independently. Reconcile any
        # signal accepted before this document existed while the canonical
        # path/session identity and source lock are both authoritative.
        from .conversation_metadata_inbox import (
            apply_deferred_conversation_metadata,
        )

        await apply_deferred_conversation_metadata(
            db,
            document=doc,
            user_id=uuid.UUID(str(user_id)),
        )
    enriched_claude_child = await _reconcile_claude_subagent_launch_metadata(
        db,
        doc,
        machine_id=machine_id,
        user_id=user_id,
        pair_locked=claude_pair_locked,
    )
    lifecycle_changed = _reconcile_subagent_document_lifecycle(
        doc,
        content if conversation_source is None else None,
        source_timestamp=source_modified_at,
        source_objects=(
            conversation_source.iter_objects()
            if conversation_source is not None
            else None
        ),
    )
    enriched_lifecycle_changed = False
    if enriched_claude_child is not None and enriched_claude_child.id != doc.id:
        enriched_lifecycle_changed = _reconcile_subagent_document_lifecycle(
            enriched_claude_child,
            await document_content(db, enriched_claude_child),
        )
    if (
        enriched_claude_child is not None
        or lifecycle_changed
        or enriched_lifecycle_changed
    ):
        await db.flush()

    # Existing-document appends dominate live sync. Updating the timestamp is
    # sufficient for those; only a new document changes the count, and that
    # increment is atomic across concurrent collectors.
    await _record_tool_sync(
        db,
        tool,
        now,
        is_new_document=is_new_document,
    )

    # Extract conversation messages into conversation_messages table
    # For DELTA mode, only parse new content; for FULL mode, re-parse all
    conversation_search_text = ""
    new_conversation_search_text = ""
    refresh_content_tsv = category != "conversation"
    activity_advanced = False
    if category == "conversation" and (
        content_type == "jsonl" or (content_type == "json" and tool_id == "hermes")
    ):
        new_conversation_search_text = await _extract_messages(
            db,
            doc,
            content,
            mode,
            user_history=user_history,
            first_user_message=first_user_message,
            conversation_source=conversation_source,
            cursor_projection_order=cursor_projection_order,
            # A targeted collector repair intentionally replays the complete
            # authoritative transcript even when its source hash is unchanged.
            # Rebuild the bounded read projection from those normalized rows as
            # part of the same transaction; retaining the incremental model can
            # otherwise preserve stale lifecycle/task state corrected above.
            force_projection_rebuild=authoritative_rebase,
            use_core_delta_message_staging=use_core_delta_message_staging,
        )
        from .conversation_activity import refresh_document_activity_at

        previous_activity = (
            delivery_state.activity_at
            if delivery_state is not None
            else doc.activity_at
        )
        current_activity = await refresh_document_activity_at(db, doc)
        activity_advanced = current_activity is not None and (
            previous_activity is None or current_activity > previous_activity
        )
        title = await _apply_friendly_conversation_title(db, doc) or title
        refresh_content_tsv = _conversation_search_index_needs_refresh(
            is_new_document=is_new_document,
            mode=mode,
            new_search_text=new_conversation_search_text,
            previous_title=previous_title,
            current_title=doc.title,
        )
        if refresh_content_tsv and not settings.realtime_ingest_deferred_projections:
            # Build FTS from bounded normalized rows, never from a multi-
            # hundred-megabyte transcript. Tool-only DELTAs leave the indexed
            # user/assistant text unchanged and skip both this read and write.
            # Phase 4 deferred mode leaves this to the revision-fenced projector.
            latest_search_rows = (
                (
                    await db.execute(
                        select(_func.left(ConversationMessage.content, 2_048))
                        .where(
                            ConversationMessage.document_id == doc.id,
                            ConversationMessage.role.in_(("user", "assistant")),
                        )
                        .order_by(ConversationMessage.line_number.desc())
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
            conversation_search_text = _bounded_message_text(
                "\n".join(row for row in reversed(latest_search_rows) if row),
                MAX_SEARCH_TEXT_CHARS,
            )

        # Claw lifecycle metadata and native transcripts arrive independently.
        # Retry the normalized join on every advancing conversation revision so
        # either arrival order converges without reparsing unrelated documents.
        from .orchestration_events import reconcile_orchestration_for_document

        await reconcile_orchestration_for_document(db, doc)

    # Every accepted FULL and every non-conversation DELTA publishes one
    # immutable exact-byte object only after parser staging succeeded. The
    # PUT/reuse + streamed-GET proof finishes before these pointer fields can
    # commit with messages, sync state, and delivery state. Conversation
    # DELTAs intentionally retain the last durable FULL pointer.
    should_finalize_content = content_store_enabled and (
        mode == "full" or category != "conversation"
    )
    if should_finalize_content:
        if conversation_source is not None:
            pointer = await finalize_document_content(
                document_id=doc.id,
                payload_path=conversation_source.path,
                db=db,
            )
        else:
            pointer = await finalize_document_content(
                document_id=doc.id,
                content=stored_blob_content,
                db=db,
            )
        doc.content_s3_key = pointer.key
        doc.content_object_sha256 = pointer.sha256
        doc.content_object_size_bytes = pointer.size_bytes
        doc.content_object_verified_at = pointer.verified_at

    if category in _EMBEDDING_CATEGORIES:
        from .embedding_service import document_embedding_input

        # A conversation DELTA changes normalized rows, not its retained raw
        # snapshot. Use those rows directly so the deferred snapshot body is
        # never fetched merely to derive the next embedding input hash.
        _, incoming_embedding_content_hash = await document_embedding_input(
            db,
            doc,
            prefer_conversation_messages=(
                category == "conversation" and mode == "delta"
            ),
        )
        if is_new_document:
            doc.embedding_content_hash = incoming_embedding_content_hash
        else:
            # Existing rows always have a baseline: either the persisted hash
            # or the lazily derived pre-update value above.
            assert previous_embedding_content_hash is not None
            await _invalidate_embeddings_for_revision(
                db,
                doc,
                previous_embedding_content_hash,
                incoming_embedding_content_hash,
            )

    # Refresh the content_tsv full-text index after conversation extraction so
    # an opaque source filename can be replaced by its human-readable prompt.
    # The tokenized value is bound as a parameter, not compiled into SQL.
    from .tokenize import tokenize_for_index as _tok

    if category == "conversation":
        searchable_content = conversation_search_text
    else:
        searchable_content = stored_blob_content[:MAX_SEARCH_TEXT_CHARS]
    if refresh_content_tsv and not (
        category == "conversation" and settings.realtime_ingest_deferred_projections
    ):
        tsv_input = _tok(f"{doc.title or ''} {searchable_content}")
        await db.execute(
            _update(Document)
            .where(Document.id == doc.id)
            .values(content_tsv=_func.to_tsvector("simple", tsv_input))
        )

    # Dashboard reads only this narrow replacement row. It is refreshed in the
    # same transaction after normalized messages, activity, title, hierarchy,
    # visibility, and category have reached their final current values.
    from .dashboard_projection import refresh_dashboard_document_projection

    dashboard_projection, _ = await refresh_dashboard_document_projection(db, doc)
    dashboard_changed = _dashboard_projection_requires_event(
        dashboard_projection,
        is_new_document=is_new_document,
    )
    enriched_dashboard_changed = False
    if (
        enriched_claude_child is not None
        and enriched_claude_child.id != doc.id
    ):
        enriched_dashboard_projection, _ = await refresh_dashboard_document_projection(
            db,
            enriched_claude_child,
        )
        enriched_dashboard_changed = _dashboard_projection_requires_event(
            enriched_dashboard_projection,
        )

    # Update sync state
    await _update_sync_state(
        db,
        tool_id,
        relative_path,
        content_hash,
        offset,
        machine_id,
        user_id,
        mode=mode,
        monotonic_offset=same_hash_before_write,
        replace_offset=cursor_projection_delta,
    )

    # Queue only namespaces whose response data changed. The session publishes
    # these generation bumps after commit; rollback discards them. Tool-only
    # DELTAs therefore perform no Redis work.
    daily_changed, project_changed = _ingest_cache_scope(
        category=category,
        mode=mode,
        activity_advanced=activity_advanced,
        title_changed=previous_title != doc.title,
        lifecycle_changed=lifecycle_changed or enriched_lifecycle_changed,
    )
    project_activity_changed = category != "conversation" or project_changed
    # Reorder project activity only when a visible resource changed. Tool-only
    # transport DELTAs must not make a dormant project look newly active.
    if project_activity_changed and doc.project_id:
        await db.execute(
            _update(Project)
            .where(Project.id == doc.project_id)
            .values(updated_at=_func.now())
        )
    _stage_ingest_read_cache_invalidations(
        db,
        user_id,
        doc.project_id,
        daily=daily_changed,
        project=project_changed,
    )
    if (
        project_changed
        and enriched_claude_child is not None
        and enriched_claude_child.project_id != doc.project_id
    ):
        _stage_ingest_read_cache_invalidations(
            db,
            user_id,
            enriched_claude_child.project_id,
            daily=False,
            project=True,
        )

    # Trigger AI summary generation (async via Celery)
    if (
        category in ("memory", "identity", "plan", "note", "learning")
        and len(content) > 50
    ):
        try:
            from ..tasks.summary_tasks import generate_document_summary_task

            generate_document_summary_task.delay(str(doc.id))
        except Exception:
            pass  # Celery may not be running in dev

    # Publish the ordinary file event. If a sidecar enriched a previously
    # ingested transcript, publish that child too so parent companion refresh
    # logic sees a conversation-path event. A transcript that enriches itself
    # already has the ordinary event and must not receive a duplicate.
    if category == "conversation":
        event_changes = _conversation_event_changes(
            mode=mode,
            search_text=new_conversation_search_text,
            title_changed=doc.title != previous_title,
            interactions_changed=(
                bool(getattr(doc, "_memento_interactions_changed", False))
                or _interaction_event_signature(document_metadata(doc))
                != previous_interaction_signature
            ),
            dashboard_changed=dashboard_changed,
        )
    else:
        event_changes = ["project"]
        if dashboard_changed:
            event_changes.append("dashboard")
    _publish_file_synced_event(
        db,
        doc,
        user_id,
        changes=event_changes,
    )
    if (
        enriched_claude_child is not None
        and enriched_claude_child.id != doc.id
    ):
        enriched_event_changes = {"conversation.metadata", "project"}
        if enriched_dashboard_changed:
            enriched_event_changes.add("dashboard")
        _publish_file_synced_event(
            db,
            enriched_claude_child,
            user_id,
            changes=enriched_event_changes,
        )

    if schedule_post_ingest:
        await _schedule_post_ingest_work(
            document_id=doc.id,
            tool_id=str(doc.tool_id),
            category=category,
            file_size_bytes=int(doc.file_size_bytes),
            revision=str(doc.content_hash),
        )

    if (
        category == "conversation"
        and settings.realtime_ingest_deferred_projections
    ):
        from .realtime_ingest_projector import enqueue_projection_candidates

        fence = (
            delivery_state.revision_hash
            if delivery_state is not None and delivery_state.revision_hash
            else doc.content_hash
        )
        await enqueue_projection_candidates(
            db,
            document_id=doc.id,
            revision_hash=str(fence),
            canvas=bool(getattr(doc, "_memento_canvas_projection_candidate", False)),
            search=bool(refresh_content_tsv),
        )

    return doc


async def _schedule_post_ingest_work(
    *,
    document_id,
    tool_id: str,
    category: str,
    file_size_bytes: int,
    revision: str,
) -> None:
    """Preserve the existing best-effort post-ingest scheduling contract."""
    try:
        # Configured direct/multipart conversations obey the same durable
        # quiet window as chunked spool ingestion. Deployments without Celery
        # can retain the lightweight in-process development path.
        from ..tasks.post_ingest import (
            initial_post_ingest_countdown,
            schedule_coalesced_post_ingest,
        )

        countdown = initial_post_ingest_countdown(category, int(file_size_bytes))
        if countdown is not None:
            await schedule_coalesced_post_ingest(
                document_id,
                tool_id,
                category,
                revision,
                countdown=countdown,
            )
        else:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                _run_post_ingest(document_id, tool_id, category)
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
    except Exception:
        pass


async def _run_post_ingest(doc_id, tool_id: str, category: str) -> None:
    """Post-ingest: generate embeddings and extract knowledge (best-effort, own session)."""
    # Only process conversations and memory — skip configs, extensions, etc.
    if category not in ("conversation", "memory", "learning", "plan", "identity"):
        return

    sem = _get_post_ingest_semaphore()
    async with sem:
        await _run_post_ingest_inner(doc_id, tool_id, category)


async def _run_post_ingest_inner(
    doc_id,
    tool_id: str,
    category: str,
    expected_revision: str | None = None,
) -> None:
    import logging

    logger = logging.getLogger("post_ingest")
    logger.info(
        "Post-ingest starting for %s/%s (category=%s)", tool_id, doc_id, category
    )
    try:
        from ..db.session import post_ingest_session_factory

        async with post_ingest_session_factory() as db:
            doc = (
                await db.execute(select(Document).where(Document.id == doc_id))
            ).scalar_one_or_none()
            if not doc:
                logger.info("Post-ingest: doc %s not found", doc_id)
                return
            # A queued task names the exact revision that created it. The
            # Celery preflight checks this too, but ingestion can commit a new
            # revision between that check and this independent session. Do not
            # let the old delivery bypass the new revision's quiet window.
            # generate_document_embeddings has its own atomic claim/final-write
            # fence for the smaller race after this reload.
            if expected_revision and doc.content_hash != expected_revision:
                logger.info(
                    "Post-ingest: revision %s superseded for %s",
                    expected_revision,
                    doc_id,
                )
                return
            # Embedding and graph helpers own short transactions and may
            # commit or roll back internally. A rollback expires ORM state
            # even though this session uses expire_on_commit=False, so keep
            # log labels as plain scalars and reload the document before the
            # next helper instead of triggering implicit async IO from an
            # expired attribute.
            relative_path = doc.relative_path

            # Embedding (skip if API not available)
            try:
                from .embedding_service import generate_document_embeddings

                count = await generate_document_embeddings(db, doc)
                if count > 0:
                    await db.commit()
            except Exception as e:
                logger.info("Embedding skipped for %s: %s", relative_path, e)
                await db.rollback()

            # generate_document_embeddings may legitimately roll back and
            # return zero when its exact revision claim is lost. That expires
            # ``doc`` without entering the exception branch above.
            doc = await db.get(Document, doc_id, populate_existing=True)
            if not doc:
                logger.info("Post-ingest: doc %s disappeared after embedding", doc_id)
                return
            if expected_revision and doc.content_hash != expected_revision:
                logger.info(
                    "Post-ingest: revision %s superseded after embedding for %s",
                    expected_revision,
                    doc_id,
                )
                return

            # Knowledge graph extraction. Do not even open the graph path when
            # the deployment has no provider credential; this is a normal
            # self-hosted configuration, not a failed extraction to retry.
            try:
                from .graph_service import (
                    extract_knowledge_from_document,
                    knowledge_provider_configured,
                )

                if not knowledge_provider_configured():
                    logger.debug(
                        "Graph extraction disabled for %s: no provider configured",
                        relative_path,
                    )
                    return

                count = await extract_knowledge_from_document(db, doc)
                await db.commit()
                if count > 0:
                    logger.info(
                        "Extracted %d knowledge items from %s", count, relative_path
                    )
                else:
                    logger.info("No knowledge extracted from %s", relative_path)
            except Exception as e:
                import traceback

                logger.info(
                    "Graph extraction failed for %s: %s\n%s",
                    relative_path,
                    e,
                    traceback.format_exc(),
                )
                await db.rollback()
    except Exception as e:
        logger.info("Post-ingest error for %s/%s: %s", tool_id, doc_id, e)


def _stored_message_matches(
    existing: ConversationMessage,
    *,
    message_type: str,
    role: str,
    content: str,
    metadata: dict,
    timestamp: datetime | None,
) -> bool:
    return (
        existing.message_type == message_type
        and existing.role == role
        and existing.content == content
        and (existing.metadata_ or {}) == metadata
        and existing.timestamp == timestamp
    )


def _stored_message_source_id(message: ConversationMessage) -> str:
    metadata = message.metadata_ if isinstance(message.metadata_, dict) else {}
    return str(metadata.get("source_id") or "")


def _cursor_projection_hint_source_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
    ):
        raise CursorProjectionOrderMismatch(
            f"Cursor projection {field} must be a bounded source ID"
        )
    return value


async def _apply_cursor_projection_order(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    raw_hint: object,
    incoming_source_ids: list[str],
    current_max: int,
) -> tuple[dict[str, int], set[int]]:
    """Validate anchor hints, open bounded gaps, and return final insert lines."""
    if not isinstance(raw_hint, dict) or set(raw_hint) != {
        "version",
        "base_count",
        "groups",
    }:
        raise CursorProjectionOrderMismatch(
            "Malformed Cursor projection ordering hint"
        )
    version = raw_hint.get("version")
    base_count = raw_hint.get("base_count")
    raw_groups = raw_hint.get("groups")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != 1
        or not isinstance(base_count, int)
        or isinstance(base_count, bool)
        # ``base_count`` is in the collector's projected-source domain while
        # ``current_max`` is in the normalized read-model domain. Cursor can
        # project records that normalize to no stored content, so equality is
        # not a valid invariant. Each projected record produces at most one
        # stored row, so a smaller source count is still impossible.
        # The committed base-hash guard and the exact adjacent source-ID
        # anchors below provide the stale/race protection for this insertion.
        or base_count < current_max
        or base_count > MAX_CURSOR_PROJECTION_BASELINE_RECORDS
        or current_max < 1
        or current_max > 2_147_483_647 - MAX_CURSOR_PROJECTION_INSERTED_RECORDS
        or not isinstance(raw_groups, list)
        or not raw_groups
        or len(raw_groups) > MAX_CURSOR_PROJECTION_INSERTION_GROUPS
    ):
        raise CursorProjectionOrderMismatch(
            "Invalid Cursor projection ordering bounds"
        )

    groups: list[dict[str, object]] = []
    listed_source_ids: list[str] = []
    anchor_source_ids: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict) or set(raw_group) != {
            "after_source_id",
            "before_source_id",
            "source_ids",
        }:
            raise CursorProjectionOrderMismatch(
                "Malformed Cursor projection insertion group"
            )
        after_source_id = _cursor_projection_hint_source_id(
            raw_group.get("after_source_id"),
            "after anchor",
        )
        raw_before = raw_group.get("before_source_id")
        before_source_id = (
            None
            if raw_before is None
            else _cursor_projection_hint_source_id(raw_before, "before anchor")
        )
        raw_source_ids = raw_group.get("source_ids")
        if not isinstance(raw_source_ids, list) or not raw_source_ids:
            raise CursorProjectionOrderMismatch(
                "Cursor projection insertion group is empty"
            )
        source_ids = [
            _cursor_projection_hint_source_id(value, "inserted identity")
            for value in raw_source_ids
        ]
        if len(set(source_ids)) != len(source_ids):
            raise CursorProjectionOrderMismatch(
                "Cursor projection insertion identities are duplicated"
            )
        listed_source_ids.extend(source_ids)
        anchor_source_ids.add(after_source_id)
        if before_source_id is not None:
            anchor_source_ids.add(before_source_id)
        groups.append({
            "after_source_id": after_source_id,
            "before_source_id": before_source_id,
            "source_ids": source_ids,
        })

    if (
        len(listed_source_ids) > MAX_CURSOR_PROJECTION_INSERTED_RECORDS
        or len(set(listed_source_ids)) != len(listed_source_ids)
    ):
        raise CursorProjectionOrderMismatch(
            "Cursor projection inserted identities exceed bounds"
        )

    lookup_source_ids = sorted(set(incoming_source_ids) | anchor_source_ids)
    located = (
        await db.execute(
            select(
                ConversationMessage.metadata_["source_id"].astext.label("source_id"),
                ConversationMessage.line_number,
            ).where(
                ConversationMessage.document_id == document_id,
                ConversationMessage.metadata_["source_id"].astext.in_(
                    lookup_source_ids
                ),
            )
        )
    ).all()
    source_lines: dict[str, int] = {}
    for source_id, line_number in located:
        source_id = str(source_id or "")
        if source_id in source_lines:
            raise CursorProjectionOrderMismatch(
                "Cursor projection source anchor is ambiguous"
            )
        source_lines[source_id] = int(line_number)

    missing_incoming = [
        source_id
        for source_id in incoming_source_ids
        if source_id not in source_lines
    ]
    if listed_source_ids != missing_incoming:
        raise CursorProjectionOrderMismatch(
            "Cursor projection ordering hint does not cover new identities"
        )

    internal_groups: list[tuple[int, list[str]]] = []
    append_group: list[str] | None = None
    previous_anchor = 0
    for group in groups:
        after_source_id = str(group["after_source_id"])
        before_source_id = group["before_source_id"]
        source_ids = list(group["source_ids"])
        after_line = source_lines.get(after_source_id)
        if after_line is None or after_line < 1:
            raise CursorProjectionOrderMismatch(
                "Cursor projection after anchor is missing"
            )
        if before_source_id is None:
            if append_group is not None or after_line != current_max:
                raise CursorProjectionOrderMismatch(
                    "Cursor projection append anchor is ambiguous"
                )
            anchor = current_max + 1
            append_group = source_ids
        else:
            before_line = source_lines.get(str(before_source_id))
            if before_line is None or before_line != after_line + 1:
                raise CursorProjectionOrderMismatch(
                    "Cursor projection anchors are not adjacent"
                )
            anchor = before_line
            if (
                current_max - anchor + 1
                > MAX_CURSOR_PROJECTION_INSERTION_TAIL_RECORDS
            ):
                raise CursorProjectionOrderMismatch(
                    "Cursor projection insertion is too far from tail"
                )
            internal_groups.append((anchor, source_ids))
        if anchor <= previous_anchor:
            raise CursorProjectionOrderMismatch(
                "Cursor projection insertion groups are unordered"
            )
        previous_anchor = anchor

    if append_group is not None and groups[-1]["before_source_id"] is not None:
        raise CursorProjectionOrderMismatch(
            "Cursor projection append group must be last"
        )

    dirty_lines: set[int] = set()
    if internal_groups:
        first_anchor = internal_groups[0][0]
        suffix_lines = [
            int(value)
            for value in (
                await db.execute(
                    select(ConversationMessage.line_number)
                    .where(
                        ConversationMessage.document_id == document_id,
                        ConversationMessage.line_number >= first_anchor,
                    )
                    .order_by(ConversationMessage.line_number)
                    .limit(MAX_CURSOR_PROJECTION_INSERTION_TAIL_RECORDS + 1)
                )
            ).scalars()
        ]
        if len(suffix_lines) > MAX_CURSOR_PROJECTION_INSERTION_TAIL_RECORDS:
            raise CursorProjectionOrderMismatch(
                "Cursor projection suffix exceeds the bounded tail"
            )
        for original_line in suffix_lines:
            dirty_lines.add(
                original_line
                + sum(
                    len(source_ids)
                    for anchor, source_ids in internal_groups
                    if anchor <= original_line
                )
            )

    current_shifted_max = current_max
    for anchor, source_ids in reversed(internal_groups):
        current_shifted_max = await _open_conversation_line_range(
            db,
            document_id,
            anchor=anchor,
            count=len(source_ids),
            current_max=current_shifted_max,
            synchronize_session=True,
        )

    insert_lines: dict[str, int] = {}
    inserted_before = 0
    for anchor, source_ids in internal_groups:
        final_anchor = anchor + inserted_before
        for index, source_id in enumerate(source_ids):
            insert_lines[source_id] = final_anchor + index
        inserted_before += len(source_ids)
    if append_group is not None:
        append_anchor = current_max + inserted_before + 1
        for index, source_id in enumerate(append_group):
            insert_lines[source_id] = append_anchor + index
    dirty_lines.update(insert_lines.values())
    return insert_lines, dirty_lines


def _conversation_message_insert_values(
    *,
    document_id: uuid.UUID,
    line_number: int,
    message_type: str | None,
    role: str | None,
    content: str,
    metadata: dict,
    timestamp: datetime | None,
) -> dict[str, object]:
    """Return the plain row shape used by DELTA Core staging.

    Keep the column spelling here (``metadata`` rather than the ORM attribute
    ``metadata_``) so the values may be passed directly to the mapped table's
    SQLAlchemy Core insert.
    """
    return {
        "document_id": document_id,
        "line_number": line_number,
        "message_type": message_type,
        "role": role,
        "content": content,
        "metadata": metadata,
        "timestamp": timestamp,
    }


def _conversation_message_from_values(
    values: dict[str, object],
) -> ConversationMessage:
    """Build the retained legacy staging object for the Phase 0 comparator."""
    return ConversationMessage(
        document_id=values["document_id"],
        line_number=values["line_number"],
        message_type=values["message_type"],
        role=values["role"],
        content=values["content"],
        metadata_=values["metadata"],
        timestamp=values["timestamp"],
    )


async def _stage_new_conversation_messages(
    db: AsyncSession,
    document: Document,
    values: list[dict[str, object]],
    *,
    use_core: bool,
) -> None:
    """Persist one semantic batch without changing its projection sequence.

    DELTAs take the Core branch by default.  The legacy branch exists only for
    Phase 0's current-path golden gate, making the comparison exercise the
    same surrounding transaction, tail mutations, projections, and SSE
    staging as the shipped path.
    """
    if not values:
        return

    from .canvas_artifact_store import (
        canvas_message_can_have_reference,
        project_message_canvases,
    )
    from .realtime_ingest_projector import deferred_projections_enabled

    canvas_values = [
        value
        for value in values
        if canvas_message_can_have_reference(
            value["role"],
            value["metadata"],
        )
        and ".canvas.tsx" in str(value["content"]).casefold()
    ]
    if canvas_values:
        setattr(document, "_memento_canvas_projection_candidate", True)
    defer_canvas = deferred_projections_enabled()

    if not use_core:
        legacy_batch = [_conversation_message_from_values(value) for value in values]
        db.add_all(legacy_batch)
        await db.flush()
        if not defer_canvas:
            await project_message_canvases(db, document, legacy_batch)
        return

    # The unchanged Canvas compatibility projector only needs generated IDs
    # for rows that can actually name a Canvas.  Avoid a RETURNING payload,
    # adapter allocation, and no-op reconciliation query for the normal batch
    # that cannot affect this projection.  Phase 4 deferred mode also skips
    # RETURNING: the projector re-reads current rows by document revision.
    message_table = ConversationMessage.__table__
    if defer_canvas or not canvas_values:
        await db.execute(insert(message_table), values)
        return

    canvas_line_numbers = {
        int(value["line_number"])
        for value in canvas_values
    }
    ordinary_values = [
        value
        for value in values
        if int(value["line_number"]) not in canvas_line_numbers
    ]
    if ordinary_values:
        await db.execute(insert(message_table), ordinary_values)
    result = await db.execute(
        insert(message_table).returning(
            message_table.c.id,
            message_table.c.line_number,
        ),
        canvas_values,
    )
    inserted_ids = {
        int(row.line_number): int(row.id)
        for row in result
    }
    projection_batch = [
        _StagedConversationMessage(
            id=inserted_ids[int(value["line_number"])],
            document_id=value["document_id"],
            line_number=int(value["line_number"]),
            role=value["role"],
            content=value["content"],
            metadata_=value["metadata"],
        )
        for value in canvas_values
    ]
    await project_message_canvases(db, document, projection_batch)


async def _extract_messages(
    db: AsyncSession,
    doc: Document,
    content: str,
    mode: str,
    *,
    user_history: list[dict] | None = None,
    first_user_message: str = "",
    conversation_source: ConversationFileSource | None = None,
    cursor_projection_order: object | None = None,
    force_projection_rebuild: bool = False,
    use_core_delta_message_staging: bool = True,
) -> str:
    """Store bounded normalized messages and return bounded FTS source text."""
    from .conversation_parser import (
        codex_assistant_transport_priority,
        is_codex_assistant_mirror_pair,
        is_codex_user_mirror_pair,
        pop_matching_claude_queue_user,
    )
    from .message_search import (
        MAX_LEXICON_TERMS_PER_INGEST,
        extract_search_terms,
        upsert_search_terms,
    )
    from .canvas_artifact_store import (
        project_message_canvases,
        reconcile_message_canvases,
    )
    from .realtime_ingest_projector import (
        deferred_projections_enabled,
        message_is_canvas_projection_candidate,
    )

    search_parts: list[str] = []
    search_bytes = 0
    search_terms: set[str] = set()

    def add_search_text(role: str, value: str) -> None:
        nonlocal search_bytes
        if role not in ("user", "assistant"):
            return
        if len(search_terms) < MAX_LEXICON_TERMS_PER_INGEST:
            remaining_terms = MAX_LEXICON_TERMS_PER_INGEST - len(search_terms)
            search_terms.update(list(extract_search_terms(value))[:remaining_terms])
        if search_bytes >= MAX_SEARCH_TEXT_CHARS:
            return
        remaining = MAX_SEARCH_TEXT_CHARS - search_bytes
        fragment = _bounded_message_text(f"[{role}] {value}\n", min(2_048, remaining))
        encoded_size = len(fragment.encode("utf-8"))
        search_parts.append(fragment)
        search_bytes += encoded_size

    # Hermes stores a whole session as a single top-level JSON, not JSONL.
    # Always full-replace (file is rewritten on each turn).
    if doc.tool_id == "hermes":
        from .conversation_parser import parse_conversation
        from .conversation_read_model import refresh_conversation_read_model

        await db.execute(
            delete(ConversationMessage).where(ConversationMessage.document_id == doc.id)
        )
        msgs = parse_conversation(content, "hermes")
        batch: list[ConversationMessage] = []
        batch_bytes = 0
        for i, m in enumerate(msgs, start=1):
            ts = None
            if m.timestamp:
                try:
                    ts = datetime.fromisoformat(m.timestamp.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
            clean_content = _bounded_message_text(
                (m.content or "").replace("\x00", ""),
                MAX_STORED_MESSAGE_CHARS,
            )
            meta = _conversation_message_metadata(m)
            batch.append(
                ConversationMessage(
                    document_id=doc.id,
                    line_number=i,
                    message_type=_bounded_message_text(m.role, 50),
                    role=m.role,
                    content=clean_content,
                    metadata_=meta,
                    timestamp=ts,
                )
            )
            add_search_text(m.role, clean_content)
            batch_bytes += (
                len(clean_content.encode("utf-8"))
                + sum(len(str(value).encode("utf-8")) for value in meta.values())
                + 256
            )
            if len(batch) >= 100 or batch_bytes >= MAX_MESSAGE_BATCH_CHARS:
                db.add_all(batch)
                await db.flush()
                if any(
                    message_is_canvas_projection_candidate(
                        item.role, item.metadata_, item.content
                    )
                    for item in batch
                ):
                    setattr(doc, "_memento_canvas_projection_candidate", True)
                if not deferred_projections_enabled():
                    await project_message_canvases(db, doc, batch)
                batch = []
                batch_bytes = 0
        if batch:
            db.add_all(batch)
            await db.flush()
            if any(
                message_is_canvas_projection_candidate(
                    item.role, item.metadata_, item.content
                )
                for item in batch
            ):
                setattr(doc, "_memento_canvas_projection_candidate", True)
            if not deferred_projections_enabled():
                await project_message_canvases(db, doc, batch)
        await refresh_conversation_read_model(
            db,
            doc,
            mode="full",
            force_full=True,
        )
        if not deferred_projections_enabled():
            await upsert_search_terms(db, search_terms)
        return "".join(search_parts)

    tool_id = doc.tool_id
    lineage_changed = False
    if tool_id == "claude_code":
        # Persist the raw parent UUID tree before rendering projections. This
        # path sees UUID-bearing progress/file-history records even when the
        # semantic parser intentionally emits no corresponding UI row.
        from .claude_lineage import (
            backfill_legacy_interaction_origins,
            refresh_claude_lineage,
        )
        from .conversation_hierarchy import is_conversation_subagent

        effective_metadata = document_metadata(doc)
        lineage_changed = await refresh_claude_lineage(
            db,
            doc,
            iter_claude_lineage_records(
                content,
                conversation_source=conversation_source,
            ),
            mode=mode,
            document_is_subagent=is_conversation_subagent(
                doc.tool_id,
                doc.relative_path,
                effective_metadata,
            ),
        )
        if mode == "full" and backfill_legacy_interaction_origins(
            effective_metadata,
            iter_claude_lineage_records(
                content,
                conversation_source=conversation_source,
            ),
            document_is_subagent=is_conversation_subagent(
                doc.tool_id,
                doc.relative_path,
                effective_metadata,
            ),
            session_id=str(effective_metadata.get("session_id") or ""),
        ):
            # The history list remains bounded by its existing owner. Only an
            # exact, unique origin annotation changed, so no message reparse
            # or document-level UUID chain is introduced.
            delivery_state = await ensure_document_delivery_state(db, doc)
            attach_document_delivery(doc, delivery_state, runtime_only=True)
            store_document_metadata(doc, effective_metadata)
    cursor_projection_delta = (
        mode == "delta"
        and tool_id == "cursor"
        and document_metadata(doc).get("source") == "cursor_state_v1"
    )
    preserve_full_rebase = (
        mode != "delta"
        and tool_id in {"claude_code", "cursor"}
        and not user_history
    )
    full_existing_max = 0
    full_existing_rows: dict[int, ConversationMessage] = {}
    full_loaded_through = 0
    full_prefix_intact = preserve_full_rebase
    projection_requires_rebuild = False
    dirty_projection_lines: set[int] = set()

    # DELTA appends after the committed tail. A FULL/rebase keeps the exact
    # normalized prefix and only replaces the first changed suffix.
    if mode == "delta":
        result = await db.execute(
            select(ConversationMessage.line_number)
            .where(ConversationMessage.document_id == doc.id)
            .order_by(ConversationMessage.line_number.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        start_line = (row or 0) + 1
    elif preserve_full_rebase:
        full_existing_max = int(
            (
                await db.execute(
                    select(func.max(ConversationMessage.line_number)).where(
                        ConversationMessage.document_id == doc.id
                    )
                )
            ).scalar_one_or_none()
            or 0
        )
        start_line = 1
    else:
        await db.execute(
            delete(ConversationMessage).where(ConversationMessage.document_id == doc.id)
        )
        start_line = 1

    if mode != "delta":
        await db.execute(
            delete(ConversationUsageEvent).where(
                ConversationUsageEvent.document_id == doc.id
            )
        )

    assistant_identity = _assistant_identity_for_ingest(doc, mode)
    from .conversation_tasks import canonical_task_state
    from ..db.models import ConversationTaskState

    existing_task_projection = (
        await db.get(ConversationTaskState, doc.id)
        if mode == "delta"
        else None
    )
    initial_task_state = canonical_task_state(
        existing_task_projection.state
        if existing_task_projection is not None
        else None
    )
    pending_question_ids = _pending_question_ids_for_ingest(doc, mode)
    latest_human_timestamp = _latest_human_timestamp_for_ingest(doc, mode)
    canonical_interaction_ids: set[str] = set()
    interactions_changed = mode == "full" or lineage_changed
    terminal_tool_call_ids: set[str] = set()
    clear_live_interaction_signals = False
    line_num = start_line
    # New DELTA rows are deliberately kept as dictionaries until Core inserts
    # them.  Existing rows participating in tail/queue/source reconciliation
    # remain ORM instances in this phase.
    use_core_batch_staging = mode == "delta" and use_core_delta_message_staging
    batch: list[dict[str, object]] = []
    batch_bytes = 0
    usage_event_rows: list[dict[str, object]] = []
    delta_tail = None
    queued_claude_users: dict[str, list[ConversationMessage]] = {}
    recent_lifecycle_rows: dict[
        tuple[str, str, str],
        ConversationMessage,
    ] = {}
    initial_question_interactions: list[dict[str, object]] = []
    if mode == "delta" and start_line > 1:
        recent_rows = (
            (
                await db.execute(
                    select(ConversationMessage)
                    # Delta-tail reconciliation needs these fields to compare
                    # or update rows, not the full ORM row payload.
                    .options(load_only(
                        ConversationMessage.id,
                        ConversationMessage.line_number,
                        ConversationMessage.message_type,
                        ConversationMessage.role,
                        ConversationMessage.content,
                        ConversationMessage.metadata_,
                        ConversationMessage.timestamp,
                    ))
                    .where(ConversationMessage.document_id == doc.id)
                    .order_by(ConversationMessage.line_number.desc())
                    .limit(32)
                )
            )
            .scalars()
            .all()
        )
        delta_tail = recent_rows[0] if recent_rows else None
        for recent in reversed(recent_rows):
            recent_metadata = (
                recent.metadata_ if isinstance(recent.metadata_, dict) else {}
            )
            recent_event = recent_metadata.get("agent_event")
            event_key = lifecycle_event_identity(
                recent_event if isinstance(recent_event, dict) else None
            )
            if event_key is not None:
                recent_lifecycle_rows[event_key] = recent
        initial_question_interactions = _pending_question_interactions(recent_rows)
        for interaction in initial_question_interactions:
            interaction_id = _bounded_message_text(
                str(interaction.get("id") or ""),
                512,
            )
            if interaction_id:
                pending_question_ids.add(interaction_id)
        if tool_id == "claude_code":
            queue_rows = (
                (
                    await db.execute(
                        select(ConversationMessage)
                        # Queue matching reads content/metadata AND timestamp
                        # (pop_matching_claude_queue_user compares candidate
                        # timestamps); a deferred timestamp lazy-loads outside
                        # the greenlet and 500s the delta (MissingGreenlet,
                        # seen live ~8x/30min on claude_code deltas).
                        .options(load_only(
                            ConversationMessage.id,
                            ConversationMessage.line_number,
                            ConversationMessage.content,
                            ConversationMessage.metadata_,
                            ConversationMessage.timestamp,
                        ))
                        .where(
                            ConversationMessage.document_id == doc.id,
                            ConversationMessage.message_type.in_(
                                (
                                    "queued_user_message",
                                    "queued_scheduled_automation",
                                )
                            ),
                        )
                        .order_by(ConversationMessage.line_number)
                    )
                )
                .scalars()
                .all()
            )
            for queue_row in queue_rows:
                metadata = (
                    queue_row.metadata_ if isinstance(queue_row.metadata_, dict) else {}
                )
                if metadata.get("canonical_source_id"):
                    continue
                queued_claude_users.setdefault(
                    (queue_row.content or "").strip(),
                    [],
                ).append(queue_row)

    # The shared iterator is the single source of truth for semantic identity,
    # pagination, counting, and ingestion.  In particular, it preserves valid
    # repeated prompts and collapses only Codex's observed cross-transport pair.
    stored_messages = (
        iter_stored_conversation_objects(
            conversation_source.iter_objects(),
            tool_id,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=assistant_identity,
            initial_task_state=initial_task_state,
            incremental=mode == "delta",
        )
        if conversation_source is not None
        else iter_stored_conversation_messages(
            content,
            tool_id,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=assistant_identity,
            initial_task_state=initial_task_state,
            incremental=mode == "delta",
        )
    )
    cursor_projection_rows: dict[str, ConversationMessage] = {}
    cursor_projection_insert_lines: dict[str, int] = {}
    canvas_reconcile_rows: list[ConversationMessage] = []
    if cursor_projection_delta:
        # Projection payloads contain only changed/new source records, so parse
        # the bounded delta once and fetch just those stable identities. The
        # document/source-id expression index keeps this independent of the
        # transcript's total row count.
        incoming_projection = list(stored_messages)
        incoming_source_ids = [
            str(meta.get("source_id") or "")
            for _normalized, _content, meta, _timestamp in incoming_projection
        ]
        if (
            not incoming_source_ids
            or any(not source_id for source_id in incoming_source_ids)
            or len(set(incoming_source_ids)) != len(incoming_source_ids)
        ):
            raise CursorProjectionOrderMismatch(
                "Cursor projection delta requires unique stable source identities"
            )
        if cursor_projection_order is not None:
            (
                cursor_projection_insert_lines,
                shifted_projection_lines,
            ) = await _apply_cursor_projection_order(
                db,
                doc.id,
                raw_hint=cursor_projection_order,
                incoming_source_ids=incoming_source_ids,
                current_max=start_line - 1,
            )
            dirty_projection_lines.update(shifted_projection_lines)
        existing_projection_rows = (
            (
                await db.execute(
                    select(ConversationMessage)
                    # Cursor updates reconcile a bounded identity set. Keep
                    # only comparison/canvas fields out of full-row hydration.
                    # document_id stays loaded so any document-scoped ORM
                    # DELETE's evaluate-sync cannot expire these rows.
                    .options(load_only(
                        ConversationMessage.id,
                        ConversationMessage.document_id,
                        ConversationMessage.line_number,
                        ConversationMessage.message_type,
                        ConversationMessage.role,
                        ConversationMessage.content,
                        ConversationMessage.metadata_,
                        ConversationMessage.timestamp,
                    ))
                    .where(
                        ConversationMessage.document_id == doc.id,
                        ConversationMessage.metadata_["source_id"].astext.in_(
                            incoming_source_ids
                        ),
                    )
                    .order_by(ConversationMessage.line_number)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for existing_projection_row in existing_projection_rows:
            source_id = _stored_message_source_id(existing_projection_row)
            if source_id:
                cursor_projection_rows.setdefault(
                    source_id,
                    existing_projection_row,
                )
        stored_messages = iter(incoming_projection)

    for normalized, clean_content, meta, ts in stored_messages:
        usage_event_rows.extend(
            _drain_assistant_usage_rows(doc, tool_id, assistant_identity)
        )
        if len(usage_event_rows) >= 250:
            replaced_usage = await _upsert_assistant_usage_rows(
                db,
                usage_event_rows,
                detect_replacements=(
                    mode == "delta" and tool_id == "claude_code"
                ),
                accumulate_existing=(tool_id == "codex"),
            )
            _remove_replaced_usage(assistant_identity, replaced_usage)
            usage_event_rows = []
        lifecycle_key = lifecycle_event_identity(normalized.agent_event)
        prior_lifecycle_row = (
            recent_lifecycle_rows.get(lifecycle_key)
            if lifecycle_key is not None
            else None
        )
        if prior_lifecycle_row is not None and normalized.agent_event is not None:
            prior_metadata = (
                dict(prior_lifecycle_row.metadata_)
                if isinstance(prior_lifecycle_row.metadata_, dict)
                else {}
            )
            prior_event = prior_metadata.get("agent_event")
            if isinstance(prior_event, dict):
                merged_event = merge_duplicate_lifecycle_events(
                    prior_event,
                    normalized.agent_event,
                )
                prior_metadata["agent_event"] = merged_event
                prior_lifecycle_row.metadata_ = prior_metadata
                prior_lifecycle_row.content = (
                    f"{merged_event.get('label') or 'Subagent'} "
                    f"{merged_event.get('kind') or 'updated'}"
                )
                if delta_tail is prior_lifecycle_row:
                    delta_tail = None
                dirty_projection_lines.add(prior_lifecycle_row.line_number)
                continue

        pending_before = bool(pending_question_ids)
        latest_human_timestamp = _update_pending_question_ids(
            pending_question_ids,
            normalized,
            latest_human_timestamp,
        )
        normalized_interaction_ids = _normalized_interaction_ids(normalized)
        canonical_interaction_ids.update(normalized_interaction_ids)
        interactions_changed = interactions_changed or bool(
            normalized_interaction_ids
        )
        terminal_tool_call_ids.update(
            _normalized_terminal_tool_call_ids(normalized)
        )
        if (
            pending_before
            and not pending_question_ids
            and normalized.role == "user"
            and not isinstance(normalized.interaction_response, dict)
        ):
            clear_live_interaction_signals = True

        incoming_source_id = str(meta.get("source_id") or "")
        existing_cursor_projection = (
            cursor_projection_rows.get(incoming_source_id)
            if cursor_projection_delta
            else None
        )
        if existing_cursor_projection is not None:
            message_type = _bounded_message_text(
                normalized.raw_type or normalized.role,
                50,
            )
            if not _stored_message_matches(
                existing_cursor_projection,
                message_type=message_type,
                role=normalized.role,
                content=clean_content,
                metadata=meta,
                timestamp=ts,
            ):
                existing_cursor_projection.message_type = message_type
                existing_cursor_projection.role = normalized.role
                existing_cursor_projection.content = clean_content
                existing_cursor_projection.metadata_ = meta
                existing_cursor_projection.timestamp = ts
                dirty_projection_lines.add(
                    existing_cursor_projection.line_number
                )
                canvas_reconcile_rows.append(existing_cursor_projection)
            add_search_text(normalized.role, clean_content)
            if delta_tail is existing_cursor_projection:
                delta_tail = None
            continue

        # Claude persists steers and scheduled instructions as queue enqueues,
        # then may write their canonical row in a later collector delta.
        # Retain the submission-time row and mark it as reconciled instead of
        # appending a duplicate at completion time.
        if (
            mode == "delta"
            and tool_id == "claude_code"
            and (
                (
                    normalized.role == "user"
                    and normalized.raw_type == "user"
                )
                or (
                    normalized.role == "system"
                    and normalized.raw_type == "scheduled_automation"
                )
            )
        ):
            queued_row = pop_matching_claude_queue_user(
                queued_claude_users,
                clean_content,
                ts,
            )
            if queued_row is not None:
                queued_metadata = (
                    dict(queued_row.metadata_)
                    if isinstance(queued_row.metadata_, dict)
                    else {}
                )
                identity_kind = (
                    "scheduled"
                    if normalized.raw_type == "scheduled_automation"
                    else "user"
                )
                canonical_identity = normalized.source_id or (
                    f"claude-{identity_kind}:"
                    + hashlib.sha256(
                        "\x1f".join(
                            (
                                normalized.timestamp or "",
                                clean_content,
                            )
                        ).encode("utf-8")
                    ).hexdigest()
                )
                queued_metadata["canonical_source_id"] = _bounded_message_text(
                    canonical_identity, 256
                )
                queued_row.metadata_ = queued_metadata
                continue

        # A filesystem event can split Codex's adjacent response/event
        # transport pair across two DELTA uploads.  If the previous DB row
        # is the pending response copy, promote it to the canonical event
        # in place instead of inserting a duplicate.  The explicit
        # source_paired flag prevents a new in-payload pair from being
        # compared with an older tail row.
        if (
            mode == "delta"
            and tool_id == "codex"
            and normalized.role == "user"
            and normalized.raw_type == "user_message"
            and not normalized.source_paired
            and delta_tail is not None
            and delta_tail.line_number == line_num - 1
            and delta_tail.role == "user"
            and is_codex_user_mirror_pair(
                delta_tail.message_type,
                delta_tail.content,
                delta_tail.timestamp,
                normalized.raw_type,
                clean_content,
                ts,
            )
        ):
            delta_tail.message_type = "user_message"
            delta_tail.content = clean_content
            delta_tail.metadata_ = meta
            delta_tail.timestamp = ts
            add_search_text(normalized.role, clean_content)
            dirty_projection_lines.add(delta_tail.line_number)
            delta_tail = None
            continue
        if (
            mode == "delta"
            and tool_id == "codex"
            and normalized.role == "assistant"
            and not normalized.source_paired
            and delta_tail is not None
            and delta_tail.line_number == line_num - 1
            and delta_tail.role == "assistant"
            and is_codex_assistant_mirror_pair(
                delta_tail.message_type,
                delta_tail.content,
                delta_tail.timestamp,
                normalized.raw_type,
                clean_content,
                ts,
            )
        ):
            if codex_assistant_transport_priority(
                normalized.raw_type,
            ) > codex_assistant_transport_priority(delta_tail.message_type):
                delta_tail.message_type = normalized.raw_type
                delta_tail.content = clean_content
                delta_tail.metadata_ = meta
                delta_tail.timestamp = ts
            add_search_text(normalized.role, clean_content)
            dirty_projection_lines.add(delta_tail.line_number)
            delta_tail = None
            continue
        message_type = _bounded_message_text(
            normalized.raw_type or normalized.role,
            50,
        )
        if full_prefix_intact:
            if line_num > full_loaded_through:
                block_end = min(full_existing_max, line_num + 255)
                if block_end >= line_num:
                    existing_block = (
                        (
                            await db.execute(
                                select(ConversationMessage)
                                # Full/rebase suffix comparison updates rows in
                                # place, so load only the fields it compares.
                                # document_id must stay loaded: the later ORM
                                # DELETE filters on it, and evaluate-sync fully
                                # expires any loaded row whose referenced
                                # column is deferred (MissingGreenlet at the
                                # canvas reconcile that reads those rows).
                                .options(load_only(
                                    ConversationMessage.id,
                                    ConversationMessage.document_id,
                                    ConversationMessage.line_number,
                                    ConversationMessage.message_type,
                                    ConversationMessage.role,
                                    ConversationMessage.content,
                                    ConversationMessage.metadata_,
                                    ConversationMessage.timestamp,
                                ))
                                .where(
                                    ConversationMessage.document_id == doc.id,
                                    ConversationMessage.line_number >= line_num,
                                    ConversationMessage.line_number <= block_end,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    full_existing_rows.update(
                        {row.line_number: row for row in existing_block}
                    )
                full_loaded_through = max(full_loaded_through, block_end)
            existing_message = full_existing_rows.pop(line_num, None)
            if existing_message is not None and _stored_message_matches(
                existing_message,
                message_type=message_type,
                role=normalized.role,
                content=clean_content,
                metadata=meta,
                timestamp=ts,
            ):
                add_search_text(normalized.role, clean_content)
                line_num += 1
                continue
            existing_source_id = (
                _stored_message_source_id(existing_message)
                if existing_message is not None
                else ""
            )
            incoming_source_id = str(meta.get("source_id") or "")
            if (
                existing_message is not None
                and incoming_source_id
                and incoming_source_id == existing_source_id
            ):
                # Mutable projections (notably Cursor's current-task row) keep
                # one stable source identity. Update that row without forcing
                # the immutable transcript suffix through every GIN index.
                existing_message.message_type = message_type
                existing_message.role = normalized.role
                existing_message.content = clean_content
                existing_message.metadata_ = meta
                existing_message.timestamp = ts
                add_search_text(normalized.role, clean_content)
                dirty_projection_lines.add(existing_message.line_number)
                canvas_reconcile_rows.append(existing_message)
                line_num += 1
                continue
            if line_num <= full_existing_max:
                projection_requires_rebuild = True
            await db.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.document_id == doc.id,
                    ConversationMessage.line_number >= line_num,
                )
            )
            await db.flush()
            full_prefix_intact = False
            full_existing_rows.clear()

        target_line = (
            cursor_projection_insert_lines.get(incoming_source_id, line_num)
            if cursor_projection_delta
            else line_num
        )
        if (
            cursor_projection_delta
            and cursor_projection_order is not None
            and incoming_source_id not in cursor_projection_insert_lines
        ):
            raise CursorProjectionOrderMismatch(
                "Cursor projection ordering hint omitted a new source identity"
            )
        batch.append(
            _conversation_message_insert_values(
                document_id=doc.id,
                line_number=target_line,
                message_type=message_type,
                role=normalized.role,
                content=clean_content,
                metadata=meta,
                timestamp=ts,
            )
        )
        add_search_text(normalized.role, clean_content)
        batch_bytes += (
            len(clean_content.encode("utf-8"))
            + sum(len(str(value).encode("utf-8")) for value in meta.values())
            + 256
        )
        line_num += 1

        # Flush in batches to avoid memory issues with large files
        if len(batch) >= 100 or batch_bytes >= MAX_MESSAGE_BATCH_CHARS:
            await _stage_new_conversation_messages(
                db,
                doc,
                batch,
                use_core=use_core_batch_staging,
            )
            batch = []
            batch_bytes = 0

    if full_prefix_intact and line_num <= full_existing_max:
        projection_requires_rebuild = True
        await db.execute(
            delete(ConversationMessage).where(
                ConversationMessage.document_id == doc.id,
                ConversationMessage.line_number >= line_num,
            )
        )
        await db.flush()

    _reconcile_live_interaction_signals(
        doc,
        canonical_interaction_ids,
        clear_all=clear_live_interaction_signals,
    )
    _reconcile_live_shell_activities(doc, terminal_tool_call_ids)
    _store_pending_question_ids(doc, pending_question_ids)
    _store_latest_human_timestamp(doc, latest_human_timestamp)

    usage_event_rows.extend(
        _drain_assistant_usage_rows(doc, tool_id, assistant_identity)
    )
    replaced_usage = await _upsert_assistant_usage_rows(
        db,
        usage_event_rows,
        detect_replacements=(mode == "delta" and tool_id == "claude_code"),
        accumulate_existing=(tool_id == "codex"),
    )
    _remove_replaced_usage(assistant_identity, replaced_usage)
    _store_assistant_identity(doc, assistant_identity)

    if batch:
        await _stage_new_conversation_messages(
            db,
            doc,
            batch,
            use_core=use_core_batch_staging,
        )
    if canvas_reconcile_rows:
        setattr(doc, "_memento_canvas_projection_candidate", True)
        if not deferred_projections_enabled():
            await reconcile_message_canvases(db, doc, canvas_reconcile_rows)

    # Codex user messages: supplement from history.jsonl and state_5.sqlite.
    # history.jsonl has ALL user inputs with timestamps; state_5.sqlite has first prompt.
    recovered_history_changed = False
    if user_history and isinstance(user_history, list):
        codex_normalizer = None
        if tool_id == "codex":
            from .conversation_parser import normalize_codex_user_payload

            codex_normalizer = normalize_codex_user_payload
        # history.jsonl is append-only within a session, so its per-session
        # ordinal is a stable source identity. Rollout events are written after
        # submission, however, so exact timestamp-second equality produces
        # duplicates. Reconcile identical content one-to-one within the bounded
        # transport-delay window; this preserves genuinely repeated prompts.
        source_users = await db.execute(
            select(
                ConversationMessage.id,
                ConversationMessage.content,
                ConversationMessage.timestamp,
                ConversationMessage.line_number,
            )
            .where(
                ConversationMessage.document_id == doc.id,
                ConversationMessage.role == "user",
                ConversationMessage.message_type.is_distinct_from(
                    "history_user_message"
                ),
            )
            .order_by(ConversationMessage.line_number)
        )
        source_occurrences = [
            UserOccurrence(
                key=row.id,
                content=row.content,
                timestamp=row.timestamp,
                line_number=row.line_number,
            )
            for row in source_users.all()
        ]
        existing_history = (
            (
                await db.execute(
                    # History dedup uses just stable source IDs and negative
                    # line slots; extract JSONB text rather than decode rows.
                    select(
                        ConversationMessage.line_number,
                        ConversationMessage.metadata_["source_id"].astext.label(
                            "source_id"
                        ),
                    ).where(
                        ConversationMessage.document_id == doc.id,
                        ConversationMessage.message_type == "history_user_message",
                    )
                )
            )
            .all()
        )
        existing_source_ids = {
            str(row.source_id)
            for row in existing_history
            if row.source_id
        }
        used_history_lines = {
            row.line_number for row in existing_history if row.line_number < 0
        }
        prepared_history: list[tuple[int, str, datetime | None, str]] = []
        for history_index, entry in enumerate(user_history):
            text = entry.get("text", "").strip()
            if codex_normalizer is not None:
                history_role, text = codex_normalizer(text)
                if history_role != "user":
                    continue
            ts_epoch = entry.get("ts", 0)
            if not text:
                continue
            ts = None
            if ts_epoch:
                try:
                    ts = datetime.fromtimestamp(float(ts_epoch), tz=timezone.utc)
                except (OSError, OverflowError, TypeError, ValueError):
                    ts = None
            clean_history = _bounded_message_text(
                text.replace("\x00", ""),
                MAX_STORED_MESSAGE_CHARS,
            )
            source_id = f"codex-history:{history_index}"
            if source_id in existing_source_ids:
                continue
            prepared_history.append((history_index, clean_history, ts, source_id))

        _, missing_history = partition_recovered_occurrences(
            source_occurrences,
            [
                UserOccurrence(
                    key=history_index,
                    content=content,
                    timestamp=ts,
                )
                for history_index, content, ts, _ in prepared_history
            ],
        )
        missing_indexes = {int(row.key) for row in missing_history}
        injected = 0
        next_free_history_index = 0
        for history_index, clean_history, ts, source_id in prepared_history:
            if history_index not in missing_indexes:
                continue
            preferred_line = _history_line_number(history_index)
            if preferred_line in used_history_lines:
                while (
                    next_free_history_index < MAX_USER_HISTORY_ENTRIES
                    and _history_line_number(next_free_history_index)
                    in used_history_lines
                ):
                    next_free_history_index += 1
                if next_free_history_index >= MAX_USER_HISTORY_ENTRIES:
                    break
                history_line = _history_line_number(next_free_history_index)
                next_free_history_index += 1
            else:
                history_line = preferred_line
            used_history_lines.add(history_line)
            db.add(
                ConversationMessage(
                    document_id=doc.id,
                    line_number=history_line,
                    message_type="history_user_message"[:50],
                    role="user",
                    content=clean_history,
                    metadata_={"source_id": source_id},
                    timestamp=ts,
                )
            )
            add_search_text("user", clean_history)
            existing_source_ids.add(source_id)
            injected += 1
        if injected:
            await db.flush()
        removed_history, placed_history = await _reconcile_recovered_history_rows(
            db,
            doc.id,
        )
        recovered_history_changed = bool(
            injected or removed_history or placed_history
        )
    elif not user_history:
        # Fallback: first_user_message from state_5.sqlite
        first_user_msg = (first_user_message or "").strip()
        if tool_id == "codex" and first_user_msg:
            from .conversation_parser import normalize_codex_user_payload

            first_role, first_user_msg = normalize_codex_user_payload(first_user_msg)
            if first_role != "user":
                first_user_msg = ""
        if first_user_msg:
            existing_user = await db.execute(
                select(ConversationMessage.id)
                .where(
                    ConversationMessage.document_id == doc.id,
                    ConversationMessage.role == "user",
                )
                .limit(1)
            )
            if existing_user.scalar_one_or_none() is None:
                clean_first_user = _bounded_message_text(
                    first_user_msg.replace("\x00", ""),
                    MAX_STORED_MESSAGE_CHARS,
                )
                first_non_system, max_line = (
                    await db.execute(
                        select(
                            func.min(ConversationMessage.line_number).filter(
                                ConversationMessage.role.is_distinct_from("system")
                            ),
                            func.max(ConversationMessage.line_number),
                        ).where(
                            ConversationMessage.document_id == doc.id,
                            ConversationMessage.line_number >= 1,
                        )
                    )
                ).one()
                max_line = max_line or 0
                anchor = first_non_system or max_line + 1
                await _open_conversation_line_range(
                    db,
                    doc.id,
                    anchor=anchor,
                    count=1,
                    current_max=max_line,
                )
                db.add(
                    ConversationMessage(
                        document_id=doc.id,
                        line_number=anchor,
                        message_type="first_user_message",
                        role="user",
                        content=clean_first_user,
                        metadata_={},
                        timestamp=doc.source_modified_at or doc.synced_at,
                    )
                )
                add_search_text("user", clean_first_user)
                await db.flush()
                recovered_history_changed = True

    # Read projections commit with normalized history. Ordinary DELTAs inspect
    # only rows beyond the prior high-water mark plus explicitly mutated rows.
    # A FULL upload whose normalized prefix stayed intact has the same safe
    # shape: it is either unchanged or append-only, so replaying all prior rows
    # would merely duplicate work.
    from .conversation_read_model import refresh_conversation_read_model

    projection_mode = (
        "delta"
        if mode == "delta"
        or (
            preserve_full_rebase
            and not projection_requires_rebuild
        )
        else "full"
    )
    await refresh_conversation_read_model(
        db,
        doc,
        mode=projection_mode,
        dirty_line_numbers=dirty_projection_lines,
        force_full=(force_projection_rebuild or recovered_history_changed),
    )
    if not deferred_projections_enabled():
        await upsert_search_terms(db, search_terms)
    setattr(doc, "_memento_interactions_changed", interactions_changed)
    return "".join(search_parts)


async def _update_sync_state(
    db: AsyncSession,
    tool_id: str,
    relative_path: str,
    content_hash: str,
    offset: int,
    machine_id: str | None,
    user_id: str | None = None,
    *,
    mode: str = "full",
    monotonic_offset: bool = False,
    replace_offset: bool = False,
) -> None:
    """Update server-side sync state."""
    result = await db.execute(
        _scoped_sync_state_select(
            tool_id,
            relative_path,
            machine_id,
            user_id,
        )
    )
    state = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if state is None:
        state = SyncState(
            machine_id=machine_id,
            tool_id=tool_id,
            relative_path=relative_path,
            last_hash=content_hash,
            last_offset=offset,
            last_synced_at=now,
        )
        db.add(state)
    else:
        state.last_hash = content_hash
        state.last_offset = (
            offset
            if replace_offset
            else (
                max(int(state.last_offset or 0), offset)
                if mode == "delta" or monotonic_offset
                else offset
            )
        )
        state.last_synced_at = now
