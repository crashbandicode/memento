"""Ingest service — processes incoming files from the collector."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ConversationMessage,
    Document,
    DocumentEmbedding,
    DocumentVersion,
    Machine,
    Project,
    SyncState,
    Tool,
)
from ..tool_catalog import tool_display_name
from .ingest_revision import bounded_source_timestamp, committed_full_supersedes

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
MAX_MESSAGE_BATCH_CHARS = 4 * 1024 * 1024
MAX_SEARCH_TEXT_CHARS = 200 * 1024
MAX_DOCUMENT_METADATA_BYTES = 256 * 1024
MAX_METADATA_STRING_CHARS = 16 * 1024
MAX_USER_HISTORY_ENTRIES = 2_000
MAX_USER_HISTORY_BYTES = 4 * 1024 * 1024
STORED_SOURCE_REVISION_KEY = "_stored_source_revision_hash"
STORED_SOURCE_HASH_KEY = "_stored_source_hash"
STORED_SOURCE_SIZE_KEY = "_stored_source_size"

_ESSENTIAL_METADATA_KEYS = {
    "agent_depth",
    "agent_id",
    "agent_nickname",
    "agent_path",
    "cascade_id",
    "codex_title_revision",
    "codex_title_revisions",
    "cwd",
    "first_user_message",
    "forked_from_id",
    "model",
    "memento_title_source",
    "parent_thread_id",
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
    "codex_title_revision",
    "codex_title_revisions",
    "memento_title_source",
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    "title_is_manual",
    "title_source",
}


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


def _logical_document_file_size(
    *,
    mode: str,
    payload_size: int,
    offset: int,
    existing_size: int = 0,
) -> int:
    """Return total source size rather than a DELTA payload's tail size."""
    safe_payload = max(0, int(payload_size))
    if mode != "delta":
        return safe_payload
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
    metadata = dict(doc.metadata_ or {})
    return bool(
        (doc.content is not None or doc.content_s3_key)
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
    metadata = dict(doc.metadata_ or {})
    metadata[STORED_SOURCE_HASH_KEY] = hashlib.sha256(encoded).hexdigest()
    metadata[STORED_SOURCE_SIZE_KEY] = len(encoded)
    if revision_hash:
        metadata[STORED_SOURCE_REVISION_KEY] = revision_hash
    else:
        metadata.pop(STORED_SOURCE_REVISION_KEY, None)
    doc.metadata_ = metadata


def _merge_delta_metadata(existing: dict, incoming: dict) -> dict:
    """Accumulate parser statistics while preserving first-source metadata."""
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


async def _invalidate_embeddings_for_revision(
    db: AsyncSession,
    doc: Document,
    previous_embedding_content_hash: str,
    incoming_embedding_content_hash: str,
) -> bool:
    """Reconcile vectors against the exact bounded model input identity."""
    persisted_hash = doc.embedding_content_hash
    doc.embedding_content_hash = incoming_embedding_content_hash
    if (
        persisted_hash or previous_embedding_content_hash
    ) == incoming_embedding_content_hash:
        return False
    await db.execute(
        delete(DocumentEmbedding).where(DocumentEmbedding.document_id == doc.id)
    )
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
    from .conversation_parser import normalize_tool_calls, strip_terminal_sequences

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
    if normalized.session_context:
        meta["session_context"] = _bounded_message_text(
            strip_terminal_sequences(normalized.session_context).replace(
                "\x00", ""
            ),
            MAX_STORED_AUXILIARY_CHARS,
        )
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
    return meta


def iter_stored_conversation_messages(
    content: str,
    tool_id: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
):
    """Yield the exact normalized representation persisted during ingest.

    Live ingestion and offline reparses must share this boundary.  Keeping the
    terminal cleanup, size limits, metadata projection, and timestamp parsing
    here prevents a historical repair from creating rows that a subsequent
    collector update would immediately rewrite differently.
    """
    from .conversation_parser import (
        iter_conversation_messages,
        strip_terminal_sequences,
    )

    for normalized in iter_conversation_messages(
        content,
        tool_id,
        initial_question_interactions=initial_question_interactions,
    ):
        if normalized.role not in ("user", "assistant", "tool", "system"):
            continue
        full_clean_content = strip_terminal_sequences(normalized.content).replace(
            "\x00", ""
        )
        if not full_clean_content.strip() and not normalized.tool_calls:
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


def _pending_question_interactions(
    recent_rows: list[ConversationMessage],
) -> list[dict[str, object]]:
    """Recover delta-boundary questions without reviving stale Cursor prompts."""
    from .conversation_parser import CURSOR_QUESTION_RESPONSE_WINDOW

    if not recent_rows:
        return []

    newest_line = max(int(row.line_number or 0) for row in recent_rows)
    pending: dict[str, dict[str, object]] = {}
    for recent in reversed(recent_rows):
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
        for interaction in interactions:
            interaction_id = str(interaction.get("id") or "")
            if not interaction_id:
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
            pending.pop(str(response.get("interaction_id") or ""), None)
    return list(pending.values())


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
    """Return whether an incremental tail must keep the last full S3 source."""
    return (
        mode == "delta"
        and doc.content is None
        and bool(doc.content_s3_key)
        and persist_content
    )


def _history_line_number(index: int) -> int:
    """Keep injected history in a disjoint, bounded negative key range."""
    if not 0 <= index < MAX_USER_HISTORY_ENTRIES:
        raise ValueError("history index is outside the bounded range")
    return -MAX_USER_HISTORY_ENTRIES + index


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
            split_cursor_user_payload,
        )

        normalized, _timestamp, context = split_cursor_user_payload(candidate)
        return (
            bool(context)
            or normalized != candidate
            or has_cursor_session_context_prefix(candidate)
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
        from .conversation_parser import split_cursor_user_payload

        text, _timestamp, _context = split_cursor_user_payload(text)
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
) -> str:
    """Keep a legitimate existing Codex title across collector syncs."""
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
    metadata = doc.metadata_ or {}
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
        and str(metadata.get("thread_source") or "").strip().lower()
        == "subagent"
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
        agent_title = _friendly_codex_agent_title(doc.metadata_)
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


def _source_lock_id(
    machine_id: str | None,
    user_id: str | None,
    tool_id: str,
    relative_path: str,
) -> int:
    """Return a stable signed 64-bit advisory-lock key for one source."""
    owner = (
        f"machine:{machine_id}"
        if machine_id is not None
        else f"user:{user_id or 'legacy'}"
    )
    identity = json.dumps(
        [owner, tool_id, relative_path],
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
    base_hash: str | None = None,
    base_offset: int | None = None,
) -> Document:
    """Process and store an ingested file."""
    metadata = dict(metadata or {})
    received_at = datetime.now(timezone.utc)
    source_modified_at = bounded_source_timestamp(timestamp, received_at) or received_at
    await _lock_ingest_source(
        db,
        machine_id=machine_id,
        user_id=user_id,
        tool_id=tool_id,
        relative_path=relative_path,
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
    doc = (
        await db.execute(
            _scoped_document_select(
                tool_id,
                relative_path,
                machine_id,
                user_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    # Repair timestamps accepted before source-clock bounding was introduced.
    # Leaving a future value in place would make later valid FULL snapshots
    # look stale indefinitely, even though new incoming times are bounded.
    if doc is not None and doc.source_modified_at is not None:
        observed_at = doc.synced_at or received_at
        doc.source_modified_at = bounded_source_timestamp(
            doc.source_modified_at,
            observed_at,
        )
    is_new_document = doc is None
    previous_embedding_content_hash: str | None = None
    logical_file_size = _logical_document_file_size(
        mode=mode,
        payload_size=file_size,
        offset=offset,
        existing_size=doc.file_size_bytes if doc is not None else 0,
    )
    same_hash_before_write = doc is not None and doc.content_hash == content_hash
    if (
        sync_row is not None
        and doc is not None
        and doc.content_hash == content_hash
        and sync_row.last_hash == content_hash
        and sync_row.last_offset == offset
    ):
        # Touch last_synced_at so dashboards know we still see this file,
        # but skip all the actual ingestion work + the post-ingest task.
        sync_row.last_synced_at = received_at
        pointer_is_current = _stored_source_is_current(
            doc,
            content_hash,
            incoming_s3_key=content_s3_key,
        )
        if pointer_is_current:
            doc.source_modified_at = max(
                filter(None, (doc.source_modified_at, source_modified_at))
            )
            setattr(doc, "_memento_ingest_disposition", "idempotent")
            return doc

    if mode == "delta" and base_hash is not None:
        expected_hash = sync_row.last_hash if sync_row is not None else None
        expected_offset = int(sync_row.last_offset or 0) if sync_row is not None else 0
        committed_matches_state = (
            doc is not None
            and expected_hash is not None
            and doc.content_hash == expected_hash
        )
        if (
            not committed_matches_state
            or expected_hash != base_hash
            or base_offset is None
            or expected_offset != int(base_offset)
        ):
            raise DeltaBaseMismatch(
                expected_hash=expected_hash,
                expected_offset=expected_offset,
            )

    if mode == "full" and doc is not None and doc.content_hash == content_hash:
        pointer_is_current = _stored_source_is_current(
            doc,
            content_hash,
            incoming_s3_key=content_s3_key,
        )
        if pointer_is_current:
            if (
                doc.source_modified_at is None
                or source_modified_at > doc.source_modified_at
            ):
                doc.source_modified_at = source_modified_at
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
            setattr(doc, "_memento_ingest_disposition", "idempotent")
            return doc

    if mode == "full" and doc is not None and doc.content_hash != content_hash:
        existing_offset = 0
        if sync_row is not None and sync_row.last_hash == doc.content_hash:
            existing_offset = int(sync_row.last_offset or 0)
        if committed_full_supersedes(
            existing_hash=doc.content_hash,
            existing_timestamp=doc.source_modified_at,
            existing_offset=existing_offset,
            existing_size=doc.file_size_bytes,
            incoming_hash=content_hash,
            incoming_timestamp=source_modified_at,
            incoming_offset=offset,
            incoming_size=file_size,
        ):
            setattr(doc, "_memento_ingest_disposition", "superseded")
            return doc

    if (
        mode == "delta"
        and doc is not None
        and sync_row is not None
        and sync_row.last_hash == doc.content_hash
        and int(sync_row.last_offset or 0) >= offset
    ):
        sync_row.last_synced_at = received_at
        setattr(doc, "_memento_ingest_disposition", "stale_delta")
        return doc

    # Re-sanitize
    content = content.replace("\x00", "")  # PostgreSQL TEXT rejects null bytes
    if content_already_sanitized:
        had_sensitive = content_had_sensitive
    else:
        content, had_sensitive = _resanitize(content)

    # Collector metadata is advisory and older clients omitted Codex thread
    # identity entirely.  The first session_meta object is authoritative and
    # cheap to parse even for an externalized multi-hundred-megabyte FULL.
    if tool_id == "codex" and category == "conversation" and content:
        from .conversation_parser import extract_codex_session_metadata

        metadata.update(extract_codex_session_metadata(content))

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

    if _needs_extract and content and category == "conversation":
        # Universal: extract cwd from first occurrence in content (Claude Code, Codex, Cursor all have it)
        cwd_match = re.search(r'"cwd"\s*:\s*"([^"]+)"', content[:10000])
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
        and content
        and tool_id == "antigravity"
        and "brain" in relative_path
    ):
        # Antigravity: extract workspace from file:// URIs in brain content
        extracted_name, extracted_path = _extract_workspace_from_content(content)
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
            project_statement = select(Document.project_id).where(
                Document.tool_id == tool_id,
                Document.metadata_["session_id"].astext == session_id,
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
    title = stored_metadata.pop("title", None) or relative_path.split("/")[-1]
    previous_stored_revision = (
        (doc.metadata_ or {}).get(STORED_SOURCE_REVISION_KEY)
        if doc is not None
        else None
    )
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
        # Very large conversations keep their raw source in object storage.
        # They are still fully parsed into ConversationMessage rows below.
        doc = Document(
            tool_id=tool_id,
            project_id=project_id,
            machine_id=machine_id,
            relative_path=relative_path,
            category=category,
            content_type=content_type,
            title=title,
            content=content if persist_content else None,
            content_s3_key=content_s3_key,
            content_hash=content_hash,
            file_size_bytes=logical_file_size,
            metadata_=stored_metadata,
            needs_review=had_sensitive,
            synced_at=now,
            source_modified_at=source_modified_at,
        )
        db.add(doc)
    else:
        # Update existing document
        preserve_externalized_delta = _is_externalized_delta_update(
            doc,
            mode=mode,
            persist_content=persist_content,
        )
        if not persist_content:
            doc.content = None
            doc.content_s3_key = content_s3_key
        elif preserve_externalized_delta:
            # A small incremental append must not replace a large archived
            # transcript with only the tail. ConversationMessage rows retain
            # the complete normalized history; the immutable S3 object remains
            # the last full source snapshot until the next externalized FULL.
            doc.content = None
            preserve_stored_source_identity = True
        elif mode == "delta" and doc.content:
            # For large files, replace instead of append to avoid unbounded growth
            if len(doc.content) + len(content) > 10_000_000:
                doc.content = content  # Replace with latest delta
                stored_revision_hash = None
            else:
                doc.content = doc.content + "\n" + content
                if previous_stored_revision == base_hash:
                    stored_revision_hash = content_hash
                else:
                    stored_revision_hash = None
            stored_blob_content = doc.content
        else:
            doc.content = content
        if persist_content and not preserve_externalized_delta:
            doc.content_s3_key = None
        doc.content_hash = content_hash
        doc.file_size_bytes = logical_file_size
        existing_metadata = dict(doc.metadata_ or {})
        existing_metadata.pop("user_history", None)
        existing_metadata.pop("first_user_message", None)
        metadata_update = (
            _merge_delta_metadata(existing_metadata, stored_metadata)
            if mode == "delta"
            else {**existing_metadata, **stored_metadata}
        )
        merged_metadata, _, _ = _prepare_document_metadata(
            metadata_update,
            tool_id=tool_id,
        )
        doc.metadata_ = merged_metadata
        doc.needs_review = doc.needs_review or had_sensitive
        doc.synced_at = now
        if (
            doc.source_modified_at is None
            or source_modified_at > doc.source_modified_at
        ):
            doc.source_modified_at = source_modified_at
        if machine_id and not doc.machine_id:
            doc.machine_id = machine_id
        doc.title = _select_updated_document_title(
            doc.title,
            title,
            category=category,
            tool_id=tool_id,
        )
        # Backfill project_id when newly resolved (was NULL, or changed).
        # Don't overwrite an existing link with NULL — keep last good value.
        if project_id and doc.project_id != project_id:
            doc.project_id = project_id

        # Save version history
        version = DocumentVersion(
            document_id=doc.id,
            content_hash=content_hash,
            file_size_bytes=file_size,
        )
        db.add(version)

    if category == "conversation" and not preserve_stored_source_identity:
        _set_stored_source_identity(
            doc,
            stored_blob_content,
            revision_hash=stored_revision_hash,
        )

    from sqlalchemy import func as _func, update as _update

    await db.flush()

    # Bump the parent project's updated_at so the projects list (sorted
    # by Project.updated_at desc) actually reorders when a new doc
    # lands. SQLAlchemy's `onupdate` only fires when the Project row
    # itself is touched — a child Document INSERT doesn't cascade.
    if doc.project_id:
        await db.execute(
            _update(Project)
            .where(Project.id == doc.project_id)
            .values(updated_at=_func.now())
        )

    # Bust read caches for this user's surface area: daily detail
    # (60 s TTL), daily list-of-dates, per-project conversations
    # (30 s). Without these, shared daily / shared timeline / dashboard
    # "recent activity" lag actual sync by up to a minute. Redis down
    # → no-op, TTL handles it.
    if user_id:
        try:
            from .cache import cache_delete_prefix

            await cache_delete_prefix(f"daily:detail:{user_id}:")
            await cache_delete_prefix(f"daily:dates:{user_id}:")
            if doc.project_id:
                await cache_delete_prefix(f"project:conv:{user_id}:{doc.project_id}:")
        except Exception:
            pass

    # Update tool stats
    tool.last_sync_at = now
    count_result = await db.execute(
        select(Document.id).where(Document.tool_id == tool_id)
    )
    tool.total_files = len(count_result.all())

    # Extract conversation messages into conversation_messages table
    # For DELTA mode, only parse new content; for FULL mode, re-parse all
    conversation_search_text = ""
    if category == "conversation" and (
        content_type == "jsonl" or (content_type == "json" and tool_id == "hermes")
    ):
        await _extract_messages(
            db,
            doc,
            content,
            mode,
            user_history=user_history,
            first_user_message=first_user_message,
        )
        from .conversation_activity import refresh_document_activity_at

        await refresh_document_activity_at(db, doc)
        # Build FTS from bounded normalized rows, never from a multi-hundred-
        # megabyte raw transcript. Ordering newest-first ensures a DELTA keeps
        # recent prompts searchable without loading every historical row.
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
        title = await _apply_friendly_conversation_title(db, doc) or title

    if category in _EMBEDDING_CATEGORIES:
        from .embedding_service import document_embedding_input

        _, incoming_embedding_content_hash = await document_embedding_input(db, doc)
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
        searchable_content = (doc.content or "")[:MAX_SEARCH_TEXT_CHARS]
    tsv_input = _tok(f"{doc.title or ''} {searchable_content}")
    await db.execute(
        _update(Document)
        .where(Document.id == doc.id)
        .values(content_tsv=_func.to_tsvector("simple", tsv_input))
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

    # Publish SSE event
    try:
        from .sse_service import publish_event

        publish_event(
            "file_synced",
            {
                "document_id": str(doc.id),
                "tool_id": tool_id,
                "category": category,
                "relative_path": relative_path,
                "title": title,
            },
            user_id=user_id,
        )
    except Exception:
        pass

    # Generate embeddings + extract knowledge graph (async, non-blocking)
    # Must keep a reference to the task to prevent GC
    if schedule_post_ingest:
        import asyncio

        try:
            # Large direct/multipart uploads must obey the same durable quiet
            # window as chunked spool ingestion. Small uploads retain the
            # lightweight in-process path used by development installs that do
            # not run Celery.
            from ..tasks.post_ingest import (
                initial_post_ingest_countdown,
                process_document_post_ingest,
            )

            countdown = initial_post_ingest_countdown(
                category,
                int(doc.file_size_bytes),
            )
            if countdown is not None:
                process_document_post_ingest.apply_async(
                    args=[
                        str(doc.id),
                        str(doc.tool_id),
                        category,
                        str(doc.content_hash),
                    ],
                    countdown=countdown,
                    retry=False,
                )
            else:
                loop = asyncio.get_running_loop()
                task = loop.create_task(
                    _run_post_ingest(doc.id, doc.tool_id, category)
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except Exception:
            pass

    return doc


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

            # Knowledge graph extraction
            try:
                from .graph_service import extract_knowledge_from_document

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


async def _extract_messages(
    db: AsyncSession,
    doc: Document,
    content: str,
    mode: str,
    *,
    user_history: list[dict] | None = None,
    first_user_message: str = "",
) -> str:
    """Store bounded normalized messages and return bounded FTS source text."""
    from .conversation_parser import (
        codex_assistant_transport_priority,
        is_codex_assistant_mirror_pair,
        is_codex_user_mirror_pair,
    )
    from .message_search import (
        MAX_LEXICON_TERMS_PER_INGEST,
        extract_search_terms,
        upsert_search_terms,
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
            search_terms.update(
                list(extract_search_terms(value))[:remaining_terms]
            )
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
        from sqlalchemy import delete
        from .conversation_parser import parse_conversation

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
                batch = []
                batch_bytes = 0
        if batch:
            db.add_all(batch)
            await db.flush()
        await upsert_search_terms(db, search_terms)
        return "".join(search_parts)

    # Get current max line number for delta mode
    if mode == "delta":
        result = await db.execute(
            select(ConversationMessage.line_number)
            .where(ConversationMessage.document_id == doc.id)
            .order_by(ConversationMessage.line_number.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        start_line = (row or 0) + 1
    else:
        # Full mode: clear existing messages
        from sqlalchemy import delete

        await db.execute(
            delete(ConversationMessage).where(ConversationMessage.document_id == doc.id)
        )
        start_line = 1

    tool_id = doc.tool_id
    line_num = start_line
    batch: list[ConversationMessage] = []
    batch_bytes = 0
    delta_tail = None
    initial_question_interactions: list[dict[str, object]] = []
    if mode == "delta" and start_line > 1:
        recent_rows = (
            await db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.document_id == doc.id)
                .order_by(ConversationMessage.line_number.desc())
                .limit(32)
            )
        ).scalars().all()
        delta_tail = recent_rows[0] if recent_rows else None
        initial_question_interactions = _pending_question_interactions(recent_rows)

    # The shared iterator is the single source of truth for semantic identity,
    # pagination, counting, and ingestion.  In particular, it preserves valid
    # repeated prompts and collapses only Codex's observed cross-transport pair.
    for normalized, clean_content, meta, ts in iter_stored_conversation_messages(
        content,
        tool_id,
        initial_question_interactions=initial_question_interactions,
    ):
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
            delta_tail = None
            continue
        batch.append(
            ConversationMessage(
                document_id=doc.id,
                line_number=line_num,
                message_type=_bounded_message_text(
                    normalized.raw_type or normalized.role,
                    50,
                ),
                role=normalized.role,
                content=clean_content,
                metadata_=meta,
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
            db.add_all(batch)
            await db.flush()
            batch = []
            batch_bytes = 0

    if batch:
        db.add_all(batch)
        await db.flush()

    # Codex user messages: supplement from history.jsonl and state_5.sqlite.
    # history.jsonl has ALL user inputs with timestamps; state_5.sqlite has first prompt.
    if user_history and isinstance(user_history, list):
        codex_normalizer = None
        if tool_id == "codex":
            from .conversation_parser import normalize_codex_user_payload

            codex_normalizer = normalize_codex_user_payload
        # history.jsonl is append-only within a session, so its per-session
        # ordinal is a stable source identity.  Retain a timestamp/content
        # multiset only as a compatibility bridge for rows ingested before
        # source IDs were persisted.  A plain set of content incorrectly
        # discarded valid repeated prompts and reused negative line numbers
        # on later DELTA uploads.
        from collections import Counter

        existing = await db.execute(
            select(
                ConversationMessage.content,
                ConversationMessage.timestamp,
                ConversationMessage.metadata_,
                ConversationMessage.line_number,
            ).where(
                ConversationMessage.document_id == doc.id,
                ConversationMessage.role == "user",
            )
        )
        existing_rows = existing.all()
        existing_identities = Counter(
            (
                row.content,
                int(row.timestamp.timestamp()) if row.timestamp else None,
            )
            for row in existing_rows
        )
        existing_source_ids = {
            str((row.metadata_ or {}).get("source_id"))
            for row in existing_rows
            if (row.metadata_ or {}).get("source_id")
        }
        used_history_lines = {
            row.line_number for row in existing_rows if row.line_number < 0
        }
        injected = 0
        next_free_history_index = 0
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
            identity = (
                clean_history,
                int(ts.timestamp()) if ts is not None else None,
            )
            if existing_identities[identity] > 0:
                existing_identities[identity] -= 1
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
    elif not user_history:
        # Fallback: first_user_message from state_5.sqlite
        first_user_msg = (first_user_message or "").strip()
        if tool_id == "codex" and first_user_msg:
            from .conversation_parser import normalize_codex_user_payload

            first_role, first_user_msg = normalize_codex_user_payload(
                first_user_msg
            )
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
                db.add(
                    ConversationMessage(
                        document_id=doc.id,
                        line_number=0,
                        message_type="first_user_message",
                        role="user",
                        content=clean_first_user,
                        metadata_={},
                        timestamp=doc.source_modified_at or doc.synced_at,
                    )
                )
                add_search_text("user", clean_first_user)
                await db.flush()

    await upsert_search_terms(db, search_terms)
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
            max(int(state.last_offset or 0), offset)
            if mode == "delta" or monotonic_offset
            else offset
        )
        state.last_synced_at = now
