"""Authoritative Claude transcript lineage and permission-history visibility.

Claude session JSONL is a tree, not an append-only list. A resumed session can
add a new child to an older parent while document-level hook metadata still
contains permissions from the abandoned suffix. This module keeps the bounded
hook history separate from the unbounded tree and is the sole place where an
inline history entry is allowed to use that tree as identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ClaudeConversationLineageRecord, Document

INTERACTION_ORIGIN_KEY = "interaction_origin"
INTERACTION_ORIGIN_VERSION = 1
EXACT_PERMISSION_RESPONSE_BACKFILL = "exact_executed_tool_result_v1"
_ORIGIN_KINDS = {"claude_record", "claude_subagent_record", "hook_only"}
_UUID_VALUE_LIMIT = 512
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_LINEAGE_BATCH_SIZE = 1_000
_MAX_PERMISSION_TOOL_INPUT_BYTES = 64 * 1024


def _bounded(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_transcript_path(value: object) -> str:
    """Keep only a collector-relative identity; never persist host paths."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().replace("\\", "/")
    if (
        not candidate
        or candidate.startswith("/")
        or "\x00" in candidate
        or any(part in {"", ".", ".."} for part in candidate.split("/"))
    ):
        return ""
    return candidate[:2_048]


def normalize_interaction_origin(value: object) -> dict[str, object] | None:
    """Validate the collector's compact v1 permission provenance contract.

    Invalid/unknown values deliberately become legacy-unknown rather than a
    reason to suppress a prompt. The server never derives identity from a
    timestamp, line number, tool name, or partial text.
    """
    if (
        not isinstance(value, dict)
        or value.get("version") != INTERACTION_ORIGIN_VERSION
    ):
        return None
    kind = _bounded(value.get("kind"), 64)
    if kind not in _ORIGIN_KINDS:
        return None
    fingerprint = _bounded(value.get("fingerprint"), 128).casefold()
    if fingerprint and _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        return None
    origin: dict[str, object] = {
        "version": INTERACTION_ORIGIN_VERSION,
        "kind": kind,
        "record_uuid": _bounded(value.get("record_uuid"), _UUID_VALUE_LIMIT),
        "parent_uuid": _bounded(value.get("parent_uuid"), _UUID_VALUE_LIMIT),
        "tool_use_id": _bounded(value.get("tool_use_id"), _UUID_VALUE_LIMIT),
        "fingerprint": fingerprint,
        "agent_id": _bounded(value.get("agent_id"), _UUID_VALUE_LIMIT),
        "is_sidechain": value.get("is_sidechain") is True,
    }
    transcript_path = _safe_transcript_path(value.get("transcript_path"))
    if transcript_path:
        origin["transcript_path"] = transcript_path
    # A hook-only source intentionally carries no tree identity. Do not turn
    # malformed hook values into an apparent authoritative record.
    if kind == "hook_only":
        origin["record_uuid"] = ""
        origin["parent_uuid"] = ""
        origin["tool_use_id"] = ""
        origin["agent_id"] = ""
        origin["is_sidechain"] = False
    return origin


def canonical_permission_fingerprint(
    tool_name: object,
    tool_input: object,
) -> str | None:
    """Return the collector-compatible, exact v1 permission fingerprint."""
    try:
        canonical = json.dumps(
            {"tool_name": str(tool_name or ""), "tool_input": tool_input},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    payload = ("memento.claude.permission-origin.v1\x00" + canonical).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def legacy_permission_interaction_id(
    session_id: object,
    tool_name: object,
    tool_input: object,
) -> str | None:
    """Reproduce the collector's pre-provenance synthetic permission ID.

    This is an exact historical compatibility identity, not a fuzzy matcher.
    Duplicate invocations still remain ambiguous and are never backfilled.
    """
    session = str(session_id or "").strip()
    tool = str(tool_name or "").strip()
    if not session or not tool or not isinstance(tool_input, dict):
        return None
    serialized = json.dumps(
        ["permission", session, tool, tool_input],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
    return f"memento-permission-{digest}"


def _bounded_exact_tool_input(value: object) -> dict | None:
    """Canonicalize the literal permission input within the v1 size bound."""
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_PERMISSION_TOOL_INPUT_BYTES:
        return None
    return json.loads(encoded)


def _record_uuid(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    return _bounded(
        record.get("uuid") or record.get("record_uuid"),
        _UUID_VALUE_LIMIT,
    )


def _record_parent_uuid(record: dict) -> str:
    return _bounded(
        record.get("parentUuid") or record.get("parent_uuid"),
        _UUID_VALUE_LIMIT,
    )


def _record_agent_id(record: dict) -> str:
    return _bounded(
        record.get("agentId") or record.get("agent_id"),
        _UUID_VALUE_LIMIT,
    )


def _record_is_subagent(
    record: dict,
    *,
    document_is_subagent: bool = False,
) -> bool:
    """Classify Claude branch scope from the same raw identity contract."""
    return (
        document_is_subagent
        or record.get("isSidechain") is True
        or record.get("is_sidechain") is True
        or bool(_record_agent_id(record))
    )


def _record_is_eligible(record: dict) -> bool:
    """Only main user/assistant tree records may choose the active leaf.

    Progress and file-history records can carry UUIDs and parents, so they are
    retained for audit/reconstruction but must never displace a conversation
    leaf. Restricting eligibility to actual turn records also safely excludes
    future housekeeping record types by default.
    """
    return str(record.get("type") or "").casefold() in {"user", "assistant"}


@dataclass(frozen=True, slots=True)
class LineageState:
    active: bool
    is_sidechain: bool
    is_subagent: bool
    agent_id: str


@dataclass(frozen=True, slots=True)
class _RawPermissionCandidate:
    origin: dict[str, object]
    tool_name: str
    tool_input: dict


async def _upsert_lineage_batch(
    db: AsyncSession,
    document_id: object,
    values: list[tuple[int, dict]],
    *,
    mode: str,
    document_is_subagent: bool,
) -> None:
    record_ids = [
        record_uuid
        for _order, record in values
        if (record_uuid := _record_uuid(record))
    ]
    if not record_ids:
        return
    existing_rows = (
        (
            await db.execute(
                select(ClaudeConversationLineageRecord).where(
                    ClaudeConversationLineageRecord.document_id == document_id,
                    ClaudeConversationLineageRecord.record_uuid.in_(record_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    existing = {row.record_uuid: row for row in existing_rows}
    for source_order, record in values:
        record_uuid = _record_uuid(record)
        if not record_uuid:
            continue
        is_sidechain = (
            record.get("isSidechain") is True or record.get("is_sidechain") is True
        )
        is_subagent = _record_is_subagent(
            record,
            document_is_subagent=document_is_subagent,
        )
        row = existing.get(record_uuid)
        if row is None:
            row = ClaudeConversationLineageRecord(
                document_id=document_id,
                record_uuid=record_uuid,
                parent_uuid=_record_parent_uuid(record) or None,
                source_order=source_order,
                is_sidechain=is_sidechain,
                is_subagent=is_subagent,
                agent_id=_record_agent_id(record) or None,
                is_eligible=_record_is_eligible(record),
                active=False,
            )
            db.add(row)
            # Claude can repeat a record UUID inside one physical JSONL
            # snapshot. Make the pending ORM row visible to later values in
            # this batch so they enrich/update it instead of scheduling a
            # second INSERT for the same primary key. FULL replay below keeps
            # the last physical occurrence's authoritative source order.
            existing[record_uuid] = row
            continue
        # A replayed DELTA can enrich parent/agent data. A record's original
        # source order remains stable; a FULL replay is authoritative ordering.
        row.parent_uuid = _record_parent_uuid(record) or None
        row.is_sidechain = is_sidechain
        row.is_subagent = is_subagent
        row.agent_id = _record_agent_id(record) or None
        row.is_eligible = _record_is_eligible(record)
        if mode == "full":
            row.source_order = source_order


async def _recompute_active_lineage(
    db: AsyncSession,
    document_id: object,
    *,
    include_sidechain: bool,
) -> bool:
    rows = (
        (
            await db.execute(
                select(ClaudeConversationLineageRecord)
                .where(ClaudeConversationLineageRecord.document_id == document_id)
                .order_by(
                    ClaudeConversationLineageRecord.source_order,
                    ClaudeConversationLineageRecord.record_uuid,
                )
            )
        )
        .scalars()
        .all()
    )
    active_ids = active_lineage_record_ids(
        rows,
        include_sidechain=include_sidechain,
    )
    changed = False
    for row in rows:
        next_active = row.record_uuid in active_ids
        changed = changed or row.active != next_active
        row.active = next_active
    return changed


def active_lineage_record_ids(
    rows: Iterable[object],
    *,
    include_sidechain: bool = False,
) -> set[str]:
    """Return the active ancestry for already-loaded lineage rows.

    Kept pure so the branch rule is testable independently of database IO.
    """
    ordered_rows = sorted(
        rows,
        key=lambda row: (int(row.source_order or 0), row.record_uuid),
    )
    by_uuid = {row.record_uuid: row for row in ordered_rows}
    non_sidechain_children: set[str] = {
        row.parent_uuid
        for row in ordered_rows
        if row.is_eligible
        and (include_sidechain or not getattr(row, "is_subagent", row.is_sidechain))
        and row.parent_uuid in by_uuid
    }
    leaves = [
        row
        for row in ordered_rows
        if row.is_eligible
        and (include_sidechain or not getattr(row, "is_subagent", row.is_sidechain))
        and row.record_uuid not in non_sidechain_children
    ]
    # Deterministic newest leaf. When a DELTA adds D -> A after A -> B -> C,
    # D wins and B/C become inactive without reparsing their message content.
    terminal = max(
        leaves,
        key=lambda row: (int(row.source_order or 0), row.record_uuid),
        default=None,
    )
    active_ids: set[str] = set()
    current = terminal
    while current is not None and current.record_uuid not in active_ids:
        active_ids.add(current.record_uuid)
        current = by_uuid.get(str(current.parent_uuid or ""))
    return active_ids


def delta_continuation_chain(
    source_records: Iterable[object],
    *,
    current_terminal_uuid: str,
    include_sidechain: bool,
    document_is_subagent: bool = False,
) -> set[str] | None:
    """Return a proven append suffix, or ``None`` for rewind/ambiguity.

    The check is identity-only. A DELTA is fast only when its one terminal
    eligible leaf walks through newly received parent UUIDs to the current
    active terminal. Any different older parent, multiple leaves, or cycle
    falls back to the authoritative table recomputation.
    """
    records = {
        _record_uuid(record): record
        for record in source_records
        if isinstance(record, dict) and _record_uuid(record)
    }
    eligible_children = {
        _record_parent_uuid(record)
        for record in records.values()
        if _record_is_eligible(record)
        and (
            include_sidechain
            or not _record_is_subagent(
                record,
                document_is_subagent=document_is_subagent,
            )
        )
        and _record_parent_uuid(record) in records
    }
    leaves = [
        record_uuid
        for record_uuid, record in records.items()
        if _record_is_eligible(record)
        and (
            include_sidechain
            or not _record_is_subagent(
                record,
                document_is_subagent=document_is_subagent,
            )
        )
        and record_uuid not in eligible_children
    ]
    if len(leaves) != 1 or not current_terminal_uuid:
        return None
    chain: set[str] = set()
    current_uuid = leaves[0]
    while current_uuid in records and current_uuid not in chain:
        chain.add(current_uuid)
        current_uuid = _record_parent_uuid(records[current_uuid])
    if current_uuid != current_terminal_uuid:
        return None
    return chain


def delta_has_eligible_record(
    source_records: Iterable[object],
    *,
    include_sidechain: bool,
    document_is_subagent: bool = False,
) -> bool:
    """Whether a DELTA can possibly replace the terminal eligible leaf."""
    return any(
        isinstance(record, dict)
        and _record_is_eligible(record)
        and (
            include_sidechain
            or not _record_is_subagent(
                record,
                document_is_subagent=document_is_subagent,
            )
        )
        for record in source_records
    )


async def refresh_claude_lineage(
    db: AsyncSession,
    document: Document,
    source_records: Iterable[object],
    *,
    mode: str,
    document_is_subagent: bool | None = None,
) -> bool:
    """Persist raw Claude UUID edges and recompute one document's active path.

    ``source_records`` may be streamed. FULL first removes the old complete
    tree, then consumes every source record in batches. DELTA consumes only
    its new records and uses the durable tree to resolve a branch rewind, so
    normal appends never load or reparse prior JSONL message content.
    """
    if document.tool_id != "claude_code" or document.category != "conversation":
        return False
    if mode == "full":
        await db.execute(
            delete(ClaudeConversationLineageRecord).where(
                ClaudeConversationLineageRecord.document_id == document.id
            )
        )
        base_order = 0
    else:
        base_order = int(
            (
                await db.scalar(
                    select(
                        func.max(ClaudeConversationLineageRecord.source_order)
                    ).where(ClaudeConversationLineageRecord.document_id == document.id)
                )
            )
            or 0
        )
    if document_is_subagent is None:
        from .conversation_hierarchy import is_conversation_subagent

        document_is_subagent = is_conversation_subagent(
            document.tool_id,
            document.relative_path,
            document.metadata_ if isinstance(document.metadata_, dict) else {},
        )
    include_sidechain = bool(document_is_subagent)
    current_terminal_uuid = ""
    delta_records: list[object] | None = None
    if mode == "delta":
        # DELTAs are already bounded to their appended records. Retaining raw
        # UUID/parent mappings lets us prove the common append path without a
        # full lineage-table read or rewrite.
        delta_records = [
            record for record in source_records if isinstance(record, dict)
        ]
        terminal_statement = select(ClaudeConversationLineageRecord.record_uuid).where(
            ClaudeConversationLineageRecord.document_id == document.id,
            ClaudeConversationLineageRecord.active.is_(True),
            ClaudeConversationLineageRecord.is_eligible.is_(True),
        )
        if not include_sidechain:
            terminal_statement = terminal_statement.where(
                ClaudeConversationLineageRecord.is_subagent.is_(False)
            )
        current_terminal_uuid = str(
            (
                await db.execute(
                    terminal_statement.order_by(
                        ClaudeConversationLineageRecord.source_order.desc(),
                        ClaudeConversationLineageRecord.record_uuid.desc(),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            or ""
        )
        source_records = delta_records

    batch: list[tuple[int, dict]] = []
    for offset, raw_record in enumerate(source_records, start=1):
        if isinstance(raw_record, dict) and _record_uuid(raw_record):
            batch.append((base_order + offset, raw_record))
        if len(batch) >= _LINEAGE_BATCH_SIZE:
            await _upsert_lineage_batch(
                db,
                document.id,
                batch,
                mode=mode,
                document_is_subagent=bool(document_is_subagent),
            )
            await db.flush()
            batch.clear()
    if batch:
        await _upsert_lineage_batch(
            db,
            document.id,
            batch,
            mode=mode,
            document_is_subagent=bool(document_is_subagent),
        )
        await db.flush()
    # Audit-only DELTAs (progress/file-history/sidechain on a parent document)
    # do not choose a terminal eligible leaf. Their rows were upserted above;
    # the already-active branch remains authoritative without a table scan.
    if mode == "delta" and not delta_has_eligible_record(
        delta_records or (),
        include_sidechain=include_sidechain,
        document_is_subagent=bool(document_is_subagent),
    ):
        return False
    fast_suffix = (
        delta_continuation_chain(
            delta_records or (),
            current_terminal_uuid=current_terminal_uuid,
            include_sidechain=include_sidechain,
            document_is_subagent=bool(document_is_subagent),
        )
        if mode == "delta"
        else None
    )
    if fast_suffix is not None:
        if fast_suffix:
            await db.execute(
                update(ClaudeConversationLineageRecord)
                .where(
                    ClaudeConversationLineageRecord.document_id == document.id,
                    ClaudeConversationLineageRecord.record_uuid.in_(fast_suffix),
                )
                .values(active=True)
            )
        return bool(fast_suffix)
    return await _recompute_active_lineage(
        db,
        document.id,
        include_sidechain=include_sidechain,
    )


def _raw_claude_tool_use_candidates(
    source_records: Iterable[object],
    *,
    document_is_subagent: bool = False,
    session_id: str = "",
) -> dict[str, list[_RawPermissionCandidate]]:
    """Index exact raw tool-use origins by digest and legacy interaction ID."""
    candidates: dict[str, list[_RawPermissionCandidate]] = {}
    for record in source_records:
        if not isinstance(record, dict):
            continue
        if str(record.get("type") or "").casefold() != "assistant":
            continue
        record_uuid = _record_uuid(record)
        if not record_uuid:
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or str(
                part.get("type") or ""
            ).casefold() not in {"tool_use", "toolcall"}:
                continue
            tool_name = part.get("name")
            tool_input = part.get("input") if "input" in part else part.get("arguments")
            exact_tool_input = _bounded_exact_tool_input(tool_input)
            if exact_tool_input is None:
                continue
            exact_tool_name = str(tool_name or "").strip()
            fingerprint = canonical_permission_fingerprint(
                exact_tool_name,
                exact_tool_input,
            )
            if not fingerprint or not exact_tool_name:
                continue
            tool_use_id = _bounded(
                part.get("id") or part.get("tool_use_id"),
                _UUID_VALUE_LIMIT,
            )
            candidate = _RawPermissionCandidate(
                origin={
                    "version": INTERACTION_ORIGIN_VERSION,
                    "kind": (
                        "claude_subagent_record"
                        if _record_is_subagent(
                            record,
                            document_is_subagent=document_is_subagent,
                        )
                        else "claude_record"
                    ),
                    "record_uuid": record_uuid,
                    "parent_uuid": _record_parent_uuid(record),
                    "tool_use_id": tool_use_id,
                    "fingerprint": fingerprint,
                    "agent_id": _record_agent_id(record),
                    "is_sidechain": record.get("isSidechain") is True,
                },
                tool_name=exact_tool_name,
                tool_input=exact_tool_input,
            )
            candidates.setdefault(f"fingerprint:{fingerprint}", []).append(candidate)
            exact_ids = {
                tool_use_id,
                legacy_permission_interaction_id(
                    session_id,
                    exact_tool_name,
                    exact_tool_input,
                )
                or "",
            }
            for exact_id in exact_ids - {""}:
                candidates.setdefault(f"interaction:{exact_id}", []).append(candidate)
    return candidates


def backfill_legacy_interaction_origins(
    metadata: dict,
    source_records: Iterable[object],
    *,
    document_is_subagent: bool = False,
    session_id: str = "",
) -> bool:
    """Attach only exact-and-unique raw origins during an authoritative FULL.

    Legacy history is never guessed: an exact fingerprint or collector
    interaction identity needs exactly one raw tool use in this complete
    transcript. The retained history entry remains auditable and records how
    its authoritative origin was obtained.
    """
    raw_history = metadata.get("_interaction_history")
    if isinstance(raw_history, list):
        history_items = enumerate(raw_history)
        next_history: list[object] | dict[object, object] = list(raw_history)
    elif isinstance(raw_history, dict):
        history_items = raw_history.items()
        next_history = dict(raw_history)
    else:
        return False
    candidates = _raw_claude_tool_use_candidates(
        source_records,
        document_is_subagent=document_is_subagent,
        session_id=session_id,
    )
    changed = False
    for history_key, raw_entry in history_items:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        if normalize_interaction_origin(entry.get(INTERACTION_ORIGIN_KEY)) is not None:
            continue
        interaction = entry.get("interaction")
        if not isinstance(interaction, dict):
            continue
        if interaction.get("interaction_type") != "permission_request":
            continue
        tool_name = interaction.get("requested_tool")
        tool_input = interaction.get("tool_input")
        matches: list[_RawPermissionCandidate] = []
        if isinstance(tool_name, str) and tool_name and isinstance(tool_input, dict):
            fingerprint = canonical_permission_fingerprint(tool_name, tool_input)
            matches = candidates.get(f"fingerprint:{fingerprint or ''}", [])
        if len(matches) != 1:
            interaction_id = _bounded(interaction.get("id"), _UUID_VALUE_LIMIT)
            matches = candidates.get(f"interaction:{interaction_id}", [])
        if len(matches) == 1:
            match = matches[0]
            if str(tool_name or "").strip() != match.tool_name:
                continue
            next_interaction = dict(interaction)
            next_interaction["tool_input"] = match.tool_input
            entry["interaction"] = next_interaction
            entry[INTERACTION_ORIGIN_KEY] = match.origin
            entry["interaction_origin_backfill"] = "exact_unique_v1"
            next_history[history_key] = entry
            changed = True
    if changed:
        metadata["_interaction_history"] = next_history
    return changed


async def load_lineage_active_states(
    db: AsyncSession,
    entries: Iterable[tuple[object, dict]],
) -> dict[tuple[str, str], LineageState]:
    """Fetch only identity-bearing record states needed by one API response."""
    requested: list[tuple[uuid.UUID, str]] = []
    for document_id, entry in entries:
        origin = normalize_interaction_origin(entry.get(INTERACTION_ORIGIN_KEY))
        if (
            origin is None
            or origin.get("kind")
            not in {
                "claude_record",
                "claude_subagent_record",
            }
            or not origin.get("record_uuid")
        ):
            continue
        if isinstance(document_id, uuid.UUID):
            normalized_document_id = document_id
        else:
            try:
                normalized_document_id = uuid.UUID(str(document_id))
            except (TypeError, ValueError, AttributeError):
                # An unparseable key cannot identify a UUID database row; it
                # remains unknown lineage and therefore fails open upstream.
                continue
        requested.append((normalized_document_id, str(origin.get("record_uuid") or "")))
    if not requested:
        return {}
    document_ids = {document_id for document_id, _record_uuid in requested}
    record_ids = {record_uuid for _document_id, record_uuid in requested}
    rows = (
        await db.execute(
            select(
                ClaudeConversationLineageRecord.document_id,
                ClaudeConversationLineageRecord.record_uuid,
                ClaudeConversationLineageRecord.active,
                ClaudeConversationLineageRecord.is_sidechain,
                ClaudeConversationLineageRecord.is_subagent,
                ClaudeConversationLineageRecord.agent_id,
            ).where(
                ClaudeConversationLineageRecord.document_id.in_(document_ids),
                ClaudeConversationLineageRecord.record_uuid.in_(record_ids),
            )
        )
    ).all()
    return {
        (str(document_id), record_uuid): LineageState(
            active=bool(active),
            is_sidechain=bool(is_sidechain),
            is_subagent=bool(is_subagent),
            agent_id=str(agent_id or ""),
        )
        for (
            document_id,
            record_uuid,
            active,
            is_sidechain,
            is_subagent,
            agent_id,
        ) in rows
    }


def origin_matches_permission_interaction(
    origin: object,
    interaction: object,
) -> bool:
    """Require the v1 digest to agree with the normalized permission payload."""
    normalized_origin = normalize_interaction_origin(origin)
    if normalized_origin is None or not isinstance(interaction, dict):
        return False
    if interaction.get("interaction_type") != "permission_request":
        return False
    requested_tool = interaction.get("requested_tool")
    tool_input = interaction.get("tool_input")
    if not isinstance(requested_tool, str) or not isinstance(tool_input, dict):
        return False
    expected = canonical_permission_fingerprint(requested_tool, tool_input)
    return bool(expected and expected == normalized_origin.get("fingerprint"))


def history_entry_is_visible(
    entry: object,
    *,
    projected_through_line: object,
    lineage_state: LineageState | None,
    document_is_subagent: bool = False,
) -> bool:
    """The shared fail-safe predicate for a document-level permission card."""
    if not isinstance(entry, dict):
        return False
    try:
        anchor = max(0, int(entry.get("anchor_line_number") or 0))
        projected = max(0, int(projected_through_line or 0))
    except (TypeError, ValueError):
        return False
    # Preserve the existing projection-range defence for legacy entries.
    if anchor > projected:
        return False
    origin = normalize_interaction_origin(entry.get(INTERACTION_ORIGIN_KEY))
    if origin is None or origin.get("kind") == "hook_only":
        return True
    # A malformed or forged source mapping must never become a suppression
    # authority. The collector's v1 digest binds its classification to the
    # normalized PermissionRequest content.
    if not origin_matches_permission_interaction(origin, entry.get("interaction")):
        return True
    if lineage_state is None:
        return True
    origin_agent_id = str(origin.get("agent_id") or "")
    origin_is_sidechain = origin.get("is_sidechain") is True
    lineage_agent_id = str(getattr(lineage_state, "agent_id", "") or "")
    lineage_is_sidechain = bool(getattr(lineage_state, "is_sidechain", False))
    # A child transcript can be scoped as subagent by its document path even
    # where an individual raw record has neither `agentId` nor the literal
    # `isSidechain` bit. The raw values still have to agree below before this
    # becomes visibility authority.
    lineage_is_subagent = (
        bool(getattr(lineage_state, "is_subagent", False))
        or document_is_subagent
        or bool(lineage_agent_id)
        or lineage_is_sidechain
    )
    if (
        lineage_is_sidechain != origin_is_sidechain
        or lineage_agent_id != origin_agent_id
    ):
        return True
    # A subagent's permission is retained in history for audit, but never
    # rendered as a parent conversation's inline card.
    if origin.get("kind") == "claude_subagent_record":
        if not lineage_is_subagent:
            return True
        return lineage_state.active if document_is_subagent else False
    if lineage_is_subagent:
        return True
    # Only a durable, agreed main-thread record can suppress an inactive card.
    return lineage_state.active
