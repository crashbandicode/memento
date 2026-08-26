"""Unified conversation parser — normalizes different JSONL formats into a common structure.

Supported formats:
- Claude Code: {type: "user"|"assistant"|"ai-title"|"system", message: {role, content}}
- Codex: {type: "response_item"|"event_msg"|"session_meta"|"turn_context", payload: {role, content: [{type, text}]}}
- OpenClaw: {type: "message", role: "user"|"assistant", content: "..."}
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar
from uuid import UUID

import orjson

from .subagent_lifecycle import (
    lifecycle_event_identity,
    merge_duplicate_lifecycle_events,
    normalized_subagent_runtime,
)
from .conversation_usage import (
    add_token_usage,
    claude_message_token_usage,
    codex_total_token_usage,
    normalize_token_usage,
    subtract_token_usage,
)


@dataclass
class NormalizedMessage:
    """A single conversation message in a unified format."""
    role: str           # "user", "assistant", "system", "tool"
    content: str        # Plain text content
    tool_name: str = "" # If role=="tool", the tool that was used
    tool_input: str = ""  # Tool input/command
    thinking: str = ""  # Optional thinking/reasoning text kept separate from final response
    session_context: str = ""  # Injected context kept separate from human text
    attachments: list[dict[str, str]] = field(default_factory=list)
    # Attachment references emitted by the source tool.  Only presentation
    # metadata (type and basename) is retained; host-specific absolute paths
    # are transport details and must not leak into the human prompt.
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    # Structured assistant tool calls. Each item has bounded ``name`` and
    # serialized ``input`` strings while the message itself remains one row.
    interaction: dict[str, object] | None = None
    # Normalized cross-tool interactive prompt (for example Claude's
    # AskUserQuestion, Cursor's AskQuestion, or Codex request_user_input).
    interaction_response: dict[str, object] | None = None
    # A response remains its own source row, but carries a stable link back to
    # the interaction so the viewer can present the pair as one decision card.
    tool_call_id: str = ""
    tool_status: str = ""
    timestamp: str = ""
    raw_type: str = ""  # Original message type
    # Stable identity from the source transcript when one exists.  This is
    # deliberately separate from rendered content: repeated prompts are valid
    # conversation events and must not be collapsed merely because their text
    # and wall-clock second happen to match.
    source_id: str = ""
    # Codex emits a stable turn ID separately from the per-transport source
    # ID.  Preserve it so interrupted and restarted attempts remain distinct
    # even when their prompt text and timestamps are identical.
    source_turn_id: str = ""
    # Internal signal for delta ingestion: the iterator already observed and
    # collapsed the adjacent Codex response/event transport pair in this
    # payload, so it must not be reconciled against an older database tail.
    source_paired: bool = False
    # The model and reasoning selection active for this assistant turn. These
    # are presentation metadata, not rendered content, and may be absent when
    # a source tool does not record them.
    model: str = ""
    reasoning_effort: str = ""
    service_tier: str = ""
    agent_mode: str = ""
    # Cross-tool task snapshot captured from Codex update_plan, Claude
    # TodoWrite/TaskUpdate, or Cursor composer todos.
    task_state: dict[str, object] | None = None
    # Safe semantic lifecycle metadata from Codex sub_agent_activity events.
    # The associated encrypted inter-agent payload is intentionally ignored.
    agent_event: dict[str, object] | None = None


@dataclass
class AssistantIdentityState:
    """Mutable model selection carried across incremental transcript chunks."""

    model: str = ""
    reasoning_effort: str = ""
    service_tier: str = ""
    agent_mode: str = ""
    started_at: str = ""
    last_activity_at: str = ""
    usage_segment_id: str = ""
    token_usage: dict[str, object] = field(default_factory=dict)
    token_usage_source_ids: set[str] = field(default_factory=set, repr=False)
    usage_observations: list[AssistantUsageObservation] = field(
        default_factory=list,
        repr=False,
    )


@dataclass(frozen=True)
class AssistantUsageObservation:
    """One native usage record ready for transactional persistence."""

    source_id: str
    timestamp: str
    source: str
    model: str
    reasoning_effort: str
    service_tier: str
    attribution_status: str
    token_usage: dict[str, object]


# Terminal programs commonly decorate matches and status text with ANSI CSI
# sequences (for example PowerShell Select-String emits ESC[7m / ESC[0m).
# Conversation viewers are not terminal emulators, so retaining these bytes
# produces visible replacement glyphs and misleading text.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\][^\x07]*(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]"
    r"|[ -/]+[0-~]|[0-~])"
    r"|\x9B[0-?]*[ -/]*[@-~]"
)

_CODEX_REQUEST_MARKER_RE = re.compile(
    r"(?im)^[ \t]*##[ \t]+My request for Codex:[ \t]*$"
)
_CODEX_SYSTEM_CONTEXT_RE = re.compile(
    r"^(?:"
    r"#\s*AGENTS\.md instructions(?:\s+for\b|\s*<INSTRUCTIONS>|\s*$)"
    r"|AGENTS\.md instructions(?:\s+for\b|\s*<INSTRUCTIONS>|\s*$)"
    r"|#\s*Context from my IDE setup\s*:"
    r"|Context from my IDE setup\s*:"
    r"|#\s*Files mentioned by the user\s*:"
    r"|Files mentioned by the user\s*:"
    r"|<(?:environment_context|turn_aborted|app-context|collaboration_mode"
    r"|skills_instructions|plugins_instructions|multi_agent_mode|INSTRUCTIONS)\b"
    r"|<(?:recommended_plugins|codex_internal_context)\b"
    r"|<permissions instructions>"
    r")",
    re.IGNORECASE,
)
_CODEX_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CURSOR_TIMESTAMP_ENVELOPE_RE = re.compile(
    r"\A\s*<timestamp>(?P<value>[^<\r\n]+)</timestamp>\s*",
    re.IGNORECASE,
)
_CURSOR_TIMESTAMP_VALUE_RE = re.compile(
    r"\A(?P<date>"
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2} (?:AM|PM)"
    r") \(UTC(?P<offset>[+-]\d{1,2}(?::\d{2})?)?\)\Z"
)
_CURSOR_TERMINAL_PATH_RE = re.compile(
    r"(?:^|/)\.cursor/projects/.+/terminals/[^/]+\.txt\Z",
    re.IGNORECASE,
)
_CURSOR_USER_QUERY_ENVELOPE_RE = re.compile(
    r"\A\s*<user_query>\s*(?P<content>[\s\S]*?)\s*</user_query>\s*\Z",
    re.IGNORECASE,
)
_CURSOR_SESSION_CONTEXT_RE = re.compile(
    r"\A\s*<(?P<tag>external_links|plugin_info|uploaded_documents|"
    r"system_notification)\b[^>]*>"
    r"[\s\S]*?</(?P=tag)>\s*",
    re.IGNORECASE,
)
_CURSOR_SESSION_CONTEXT_PREFIX_RE = re.compile(
    r"\A\s*<(?:external_links|plugin_info|uploaded_documents|"
    r"system_notification)(?:\s|>)",
    re.IGNORECASE,
)
# Cursor injects a synthetic follow-up prompt after shell/await notifications.
# It is product instruction to the model, not a human turn.
_CURSOR_TASK_RESULT_FOLLOWUP_RE = re.compile(
    r"\A\s*<user_query>\s*"
    r"(?:"
    r"Briefly inform the user about the task result\b"
    r"|Perform any necessary follow-up actions in response to the "
    r"(?:subagent|task) completion above\b"
    r")[\s\S]*?"
    r"</user_query>\s*\Z",
    re.IGNORECASE,
)


def _is_cursor_terminal_read(tool_name: object, tool_input: object) -> bool:
    """Identify Cursor's internal terminal snapshot reads.

    Cursor persists a terminal pane as ``.cursor/projects/.../terminals/<id>.txt``
    and exposes refreshes through the ordinary ``read_file_v2`` transport.  A
    normal file read must remain a Read card; only that product-owned terminal
    path is semantically a Terminal card.
    """
    normalized_name = re.sub(
        r"[^a-z0-9]",
        "",
        _coerce_text(tool_name).casefold(),
    )
    if normalized_name not in {"read", "readfile", "readfilev2"}:
        return False
    payload = _json_mapping(tool_input)
    path = _coerce_text(payload.get("path") or payload.get("file_path")).strip()
    if not path:
        return False
    return _CURSOR_TERMINAL_PATH_RE.search(path.replace("\\", "/")) is not None


_CURSOR_TASK_COMPLETION_RE = re.compile(
    r"<system_notification\b[^>]*>\s*"
    r"The following task has finished\b[\s\S]*?"
    r"<task>\s*(?P<body>[\s\S]*?)\s*</task>\s*"
    r"</system_notification>",
    re.IGNORECASE,
)
_CURSOR_TASK_SUMMARY_RE = re.compile(
    r"<user_visible_high_level_summary>\s*(?P<summary>[\s\S]*?)\s*"
    r"</user_visible_high_level_summary>",
    re.IGNORECASE,
)
_CURSOR_TASK_RESPONSE_RE = re.compile(
    r"<response>\s*(?P<response>[\s\S]*?)\s*</response>",
    re.IGNORECASE,
)
_CURSOR_ADDITIONAL_DIRECTIVES_ENVELOPE_RE = re.compile(
    r"\A\s*<additional_directives\b[^>]*>\s*(?P<content>[\s\S]*?)\s*"
    r"</additional_directives>\s*\Z",
    re.IGNORECASE,
)
_CURSOR_ADDITIONAL_DIRECTIVE_LABEL_RE = re.compile(
    r"(?mi)^[ \t]*ADDITIONAL DIRECTIVES?\s*:[ \t]*"
)
_CURSOR_IMAGE_FILES_ENVELOPE_RE = re.compile(
    r"\A\s*(?P<markers>(?:\[Image\]\s*)*)<image_files\b[^>]*>"
    r"(?P<body>[\s\S]*?)</image_files>\s*",
    re.IGNORECASE,
)
_CURSOR_IMAGE_PATH_RE = re.compile(
    r"(?m)^\s*\d+\.\s+(?P<path>[^\r\n]+?)\s*$"
)
_CURSOR_IMAGE_MARKERS_RE = re.compile(
    r"\A\s*(?P<markers>(?:\[Image\]\s*)+)(?=<(?:timestamp|user_query)\b)",
    re.IGNORECASE,
)

_MAX_STRUCTURED_TOOL_CALLS = 32
_MAX_STRUCTURED_TOOL_NAME_BYTES = 256
_MAX_STRUCTURED_TOOL_INPUT_BYTES = 64 * 1024
_MAX_STRUCTURED_TOOL_CALL_BYTES = 128 * 1024
_MAX_MESSAGE_ATTACHMENTS = 32
_TOOL_INPUT_TRUNCATION_MARKER = "\n\n[... tool input truncated by Memento ...]"
CODEX_ASSISTANT_TRANSPORT_PRIORITY = {
    "agent_message": 3,
    "response_item": 2,
    "task_complete": 1,
}
CODEX_ASSISTANT_EXACT_MIRROR_MAX_SECONDS = 12.0
_CURSOR_REDACTED_TRANSPORT_LINE_RE = re.compile(
    r"(^|\n)[ \t]*\[REDACTED\][ \t]*(?=\n|$)",
    re.IGNORECASE | re.MULTILINE,
)
_CLAUDE_QUEUE_MATCH_WINDOW_SECONDS = 24 * 60 * 60
_SCHEDULED_AUTOMATION_RE = re.compile(
    r"^\s*(?:\[(?:AUTO|CRON)\b|#\s*/(?:loop|cron)\b)",
    re.IGNORECASE,
)


class _ClaudeQueueCandidate(Protocol):
    content: str
    timestamp: object


_ClaudeQueueCandidateT = TypeVar(
    "_ClaudeQueueCandidateT",
    bound=_ClaudeQueueCandidate,
)


def normalize_codex_user_payload(content: str) -> tuple[str, str]:
    """Return ``(role, text)`` for a Codex payload labelled as user input.

    Codex Desktop and older IDE integrations serialize injected workspace
    context as ``role=user``.  Those envelopes are valuable provenance but
    are not human prompts.  Older wrappers also embed the actual prompt after
    a stable ``## My request for Codex:`` marker; retain only that suffix.
    """
    text = (content or "").strip()
    if not text:
        return "system", ""

    marker = _CODEX_REQUEST_MARKER_RE.search(text)
    if marker is not None:
        prefix = text[:marker.start()].strip()
        if not prefix or _CODEX_SYSTEM_CONTEXT_RE.match(prefix):
            request = text[marker.end():].strip()
            if request:
                return "user", request
            return "system", text

    if _CODEX_SYSTEM_CONTEXT_RE.match(text):
        return "system", text
    return "user", text


def is_scheduled_automation_content(content: str | None) -> bool:
    """Return whether text is an agent-scheduled instruction, not a human turn."""
    return bool(_SCHEDULED_AUTOMATION_RE.match(content or ""))


def is_claude_session_context_record(obj: dict) -> bool:
    """Return whether Claude marks a user-shaped record as injected context."""
    return any(
        obj.get(name) is True
        for name in ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")
    )


def is_claude_queue_user_pair(
    queue_content: str,
    queue_timestamp: object,
    canonical_content: str,
    canonical_timestamp: object,
) -> bool:
    """Return whether a queued Claude prompt later became a user record.

    Claude records a steer when it is submitted and can write the canonical
    ``user`` row much later, after the active turn finishes. Exact content,
    source order, and a bounded time window distinguish that transport pair
    without collapsing legitimately repeated prompts.
    """
    queued = (queue_content or "").strip()
    canonical = (canonical_content or "").strip()
    if not queued or queued != canonical:
        return False
    queued_at = _message_timestamp(queue_timestamp)
    canonical_at = _message_timestamp(canonical_timestamp)
    if queued_at is None or canonical_at is None:
        return True
    return abs((canonical_at - queued_at).total_seconds()) <= (
        _CLAUDE_QUEUE_MATCH_WINDOW_SECONDS
    )


def pop_matching_claude_queue_user(
    queued_by_content: dict[str, list[_ClaudeQueueCandidateT]],
    canonical_content: str,
    canonical_timestamp: object,
) -> _ClaudeQueueCandidateT | None:
    """Consume one queued occurrence represented by a canonical user row."""
    content = (canonical_content or "").strip()
    candidates = queued_by_content.get(content, [])
    for index, candidate in enumerate(candidates):
        if is_claude_queue_user_pair(
            str(getattr(candidate, "content", "")),
            getattr(candidate, "timestamp", None),
            content,
            canonical_timestamp,
        ):
            return candidates.pop(index)
    return None


def _parse_cursor_envelope_timestamp(value: str) -> str | None:
    """Return an ISO timestamp for Cursor's human-readable UTC envelope."""
    match = _CURSOR_TIMESTAMP_VALUE_RE.fullmatch(value.strip())
    if match is None:
        return None

    try:
        parsed = datetime.strptime(
            match.group("date"),
            "%A, %b %d, %Y, %I:%M %p",
        )
        raw_offset = match.group("offset")
        if raw_offset is None:
            tz = timezone.utc
        else:
            sign = -1 if raw_offset.startswith("-") else 1
            offset_parts = raw_offset[1:].split(":", 1)
            hours = int(offset_parts[0])
            minutes = int(offset_parts[1]) if len(offset_parts) == 2 else 0
            if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
                return None
            tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=tz).isoformat()


@dataclass(frozen=True)
class CursorUserPayload:
    content: str
    timestamp: str = ""
    session_context: str = ""
    attachments: tuple[dict[str, str], ...] = ()


def _cursor_attachment_name(path: str) -> str:
    """Return a bounded basename for a Cursor attachment path."""
    name = re.split(r"[\\/]", path.strip().strip('"'))[-1].strip()
    return name[:255] or "Image"


def normalize_message_attachments(value: object) -> list[dict[str, str]]:
    """Return bounded, presentation-safe attachment metadata."""
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[dict[str, str]] = []
    for item in value[:_MAX_MESSAGE_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        attachment_type = str(item.get("type") or "file").strip().lower()
        if attachment_type not in {"image", "file"}:
            attachment_type = "file"
        name = _cursor_attachment_name(str(item.get("name") or "Attachment"))
        normalized.append({"type": attachment_type, "name": name})
    return normalized


def parse_cursor_user_payload(content: str) -> CursorUserPayload:
    """Separate Cursor's leading context, timestamp, and human prompt.

    Only balanced, leading envelopes with names observed in Cursor exports are
    treated as product context. Literal tags inside a prompt remain untouched.
    """
    original = content or ""
    text = original
    context_parts: list[str] = []
    attachments: list[dict[str, str]] = []

    def consume_context() -> None:
        nonlocal text
        while True:
            context_match = _CURSOR_SESSION_CONTEXT_RE.match(text)
            if context_match is not None:
                context_parts.append(context_match.group(0).strip())
                text = text[context_match.end():]
                continue
            image_match = _CURSOR_IMAGE_FILES_ENVELOPE_RE.match(text)
            if image_match is not None:
                paths = [
                    match.group("path")
                    for match in _CURSOR_IMAGE_PATH_RE.finditer(
                        image_match.group("body")
                    )
                ]
                if paths:
                    attachments.extend(
                        {
                            "type": "image",
                            "name": _cursor_attachment_name(path),
                        }
                        for path in paths[:32]
                    )
                else:
                    marker_count = len(
                        re.findall(r"\[Image\]", image_match.group("markers"), re.I)
                    )
                    attachments.extend(
                        {"type": "image", "name": f"Image {index + 1}"}
                        for index in range(max(1, marker_count))
                    )
                text = text[image_match.end():]
                continue
            marker_match = _CURSOR_IMAGE_MARKERS_RE.match(text)
            if marker_match is not None:
                marker_count = len(
                    re.findall(r"\[Image\]", marker_match.group("markers"), re.I)
                )
                attachments.extend(
                    {"type": "image", "name": f"Image {index + 1}"}
                    for index in range(marker_count)
                )
                text = text[marker_match.end():]
                continue
            break

    consume_context()

    timestamp_match = _CURSOR_TIMESTAMP_ENVELOPE_RE.match(text)
    if timestamp_match is None:
        prompt = text.strip() if context_parts or attachments else text
        followup_match = _CURSOR_TASK_RESULT_FOLLOWUP_RE.fullmatch(prompt)
        if followup_match is not None and context_parts:
            context_parts.append(followup_match.group(0).strip())
            prompt = ""
        else:
            query_match = _CURSOR_USER_QUERY_ENVELOPE_RE.fullmatch(prompt)
            if query_match is not None:
                prompt = query_match.group("content").strip()
        return CursorUserPayload(
            content=prompt,
            session_context="\n\n".join(context_parts),
            attachments=tuple(attachments),
        )

    parsed_timestamp = _parse_cursor_envelope_timestamp(
        timestamp_match.group("value")
    )
    # Treat the tag as Cursor metadata only when its value has the exact
    # shape emitted by Cursor. A malformed leading tag may be user text.
    if parsed_timestamp is None:
        prompt = text.strip() if context_parts or attachments else original
        return CursorUserPayload(
            content=prompt,
            session_context="\n\n".join(context_parts),
            attachments=tuple(attachments),
        )
    text = text[timestamp_match.end():]
    # Cursor completion bubbles currently put their timestamp first and their
    # system_notification second. Accept the same context allowlist on either
    # side of a valid timestamp; malformed timestamps remain literal user text.
    consume_context()

    followup_match = _CURSOR_TASK_RESULT_FOLLOWUP_RE.fullmatch(text)
    if followup_match is not None and context_parts:
        context_parts.append(followup_match.group(0).strip())
        text = ""
    else:
        query_match = _CURSOR_USER_QUERY_ENVELOPE_RE.fullmatch(text)
        if query_match is not None:
            text = query_match.group("content")
    return CursorUserPayload(
        content=text.strip(),
        timestamp=parsed_timestamp,
        session_context="\n\n".join(context_parts),
        attachments=tuple(attachments),
    )


def split_cursor_user_payload(content: str) -> tuple[str, str, str]:
    """Compatibility tuple for callers that do not render attachments."""
    payload = parse_cursor_user_payload(content)
    return payload.content, payload.timestamp, payload.session_context


def has_cursor_session_context_prefix(content: str | None) -> bool:
    """Return whether text starts with a known Cursor context marker."""
    return bool(_CURSOR_SESSION_CONTEXT_PREFIX_RE.match(content or ""))


def normalize_cursor_user_payload(content: str) -> tuple[str, str]:
    """Return Cursor's human prompt and optional envelope timestamp."""
    payload = parse_cursor_user_payload(content)
    if not (
        payload.timestamp
        or payload.session_context
        or payload.attachments
    ):
        return content, ""
    return payload.content, payload.timestamp


def normalize_cursor_additional_directives(content: str) -> str | None:
    """Return clean product-provided Cursor directives, when present.

    Cursor subagent transcripts serialize their dispatch as a user-shaped
    ``<user_query>`` row.  Current builds mark product/developer additions with
    a stable ``ADDITIONAL DIRECTIVE:`` paragraph; future/alternate transports
    may use the equivalent XML envelope.  Neither form represents a human
    prompt, so remove only the transport label/envelope and preserve the
    Markdown body (including paths, lists, and code).
    """
    text = (content or "").strip()
    envelope = _CURSOR_ADDITIONAL_DIRECTIVES_ENVELOPE_RE.fullmatch(text)
    if envelope is not None:
        return envelope.group("content").strip()
    if _CURSOR_ADDITIONAL_DIRECTIVE_LABEL_RE.search(text) is None:
        return None
    return _CURSOR_ADDITIONAL_DIRECTIVE_LABEL_RE.sub("", text).strip()


def _codex_uuid(value: object) -> str | None:
    candidate = str(value or "").strip()
    if not _CODEX_UUID_RE.fullmatch(candidate):
        return None
    try:
        return str(UUID(candidate))
    except ValueError:
        return None


def _codex_session_metadata_from_payload(payload: dict) -> dict:
    source = payload.get("source")
    subagent: dict = {}
    if isinstance(source, dict):
        nested = source.get("subagent")
        if isinstance(nested, dict):
            spawn = nested.get("thread_spawn")
            if isinstance(spawn, dict):
                subagent = spawn

    current_id = _codex_uuid(
        payload.get("id") or payload.get("thread_id") or payload.get("session_id")
    )
    if current_id is None:
        return {}
    root_id = _codex_uuid(
        payload.get("root_session_id") or payload.get("session_id") or current_id
    ) or current_id

    result: dict[str, object] = {
        "session_id": current_id,
        "thread_id": current_id,
        "root_session_id": root_id,
    }
    for key, value in (
        ("parent_thread_id", payload.get("parent_thread_id") or subagent.get("parent_thread_id")),
        ("forked_from_id", payload.get("forked_from_id")),
    ):
        normalized = _codex_uuid(value)
        if normalized:
            result[key] = normalized

    thread_source = payload.get("thread_source")
    if isinstance(thread_source, str) and thread_source.strip():
        result["thread_source"] = thread_source.strip()[:64]
    for key, value in (
        ("agent_path", payload.get("agent_path") or subagent.get("agent_path")),
        ("agent_nickname", payload.get("agent_nickname") or subagent.get("agent_nickname")),
    ):
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:1024]

    depth = payload.get("agent_depth")
    if depth is None:
        depth = subagent.get("depth")
    if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
        result["agent_depth"] = depth
    return result


def extract_codex_session_metadata(raw_content: str) -> dict:
    """Extract bounded thread identity from the first Codex session_meta row."""
    if not raw_content:
        return {}
    first = next(iter(_iter_json_objects(raw_content)), None)
    if first is not None:
        try:
            obj = orjson.loads(first)
        except (orjson.JSONDecodeError, TypeError):
            obj = None
        if isinstance(obj, dict) and obj.get("type") == "session_meta":
            payload = obj.get("payload")
            if isinstance(payload, dict):
                return _codex_session_metadata_from_payload(payload)

    # A range-read prefix can end inside a very large base_instructions value.
    # The identity fields precede it, so recover only those early scalar keys
    # without ever accepting a non-session_meta object.
    prefix = raw_content.lstrip()[: 1024 * 1024]
    if not re.search(r'"type"\s*:\s*"session_meta"', prefix[:4096]):
        return {}

    def string_value(key: str) -> str | None:
        match = re.search(
            rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"',
            prefix,
        )
        if match is None:
            return None
        try:
            return orjson.loads(f'"{match.group(1)}"')
        except orjson.JSONDecodeError:
            return None

    payload = {
        key: value
        for key in (
            "id",
            "thread_id",
            "session_id",
            "root_session_id",
            "parent_thread_id",
            "forked_from_id",
            "thread_source",
            "agent_path",
            "agent_nickname",
        )
        if (value := string_value(key)) is not None
    }
    depth_match = re.search(r'"(?:agent_depth|depth)"\s*:\s*(\d+)', prefix)
    if depth_match is not None:
        payload["agent_depth"] = int(depth_match.group(1))
    return _codex_session_metadata_from_payload(payload)


def strip_terminal_sequences(text: str) -> str:
    """Remove ANSI/ECMA-48 terminal control sequences from plain text."""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    # Truncated command output can cut an escape sequence before its final
    # byte.  A plain-text viewer should never retain the orphan ESC/C1 byte.
    return stripped.replace("\x1b", "").replace("\x9b", "")


def _coerce_text(value: object) -> str:
    """Normalize nullable or scalar transcript fields without inventing text."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _as_mapping(value: object) -> dict:
    """Return a transcript object only when its runtime shape is a mapping."""
    return value if isinstance(value, dict) else {}


def _bounded_identity_text(value: object, limit: int = 128) -> str:
    """Return a compact, control-free identifier suitable for metadata."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    clean = strip_terminal_sequences(_coerce_text(value)).replace("\x00", "")
    return clean.strip()[:limit]


def _set_identity_field(
    state: AssistantIdentityState,
    payload: dict,
    keys: tuple[str, ...],
    attribute: str,
) -> bool:
    """Apply the first explicitly present identity field, including clears."""
    for key in keys:
        if key in payload:
            setattr(state, attribute, _bounded_identity_text(payload.get(key)))
            return True
    return False


def _is_internal_model_sentinel(value: object) -> bool:
    """Return whether a provider field is transport metadata, not a model.

    Claude emits a literal ``<synthetic>`` model for locally-generated
    no-op replies (for example the acknowledgement behind ``/model``).  It
    is not a selectable model and must never replace the last real assistant
    identity or escape into the UI/API telemetry surface.
    """
    return _bounded_identity_text(value).casefold() in {"<synthetic>"}


def _set_agent_mode(state: AssistantIdentityState, payload: dict) -> bool:
    """Apply a native collaboration mode while preserving explicit clears."""
    for key in ("collaboration_mode", "collaborationMode"):
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("mode") or value.get("kind")
        state.agent_mode = _bounded_identity_text(value)
        return True
    return _set_identity_field(
        state,
        payload,
        ("collaboration_mode_kind", "collaborationModeKind", "agent_mode"),
        "agent_mode",
    )


def _has_claude_thinking_block(content: object) -> bool:
    """Return whether a Claude response records extended-thinking use."""
    return isinstance(content, list) and any(
        isinstance(item, dict)
        and item.get("type") in {"thinking", "redacted_thinking"}
        for item in content
    )


def _usage_observation_source_id(obj: dict, tool_id: str) -> str:
    """Return a stable bounded identity for one native usage observation."""
    message = _as_mapping(obj.get("message"))
    payload = _as_mapping(obj.get("payload"))
    for value in (
        message.get("id"),
        obj.get("uuid"),
        obj.get("id"),
        payload.get("id"),
    ):
        candidate = _coerce_text(value).strip()
        if candidate:
            return f"{tool_id}:{candidate}"[:512]
    serialized = orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)
    return f"{tool_id}:sha256:{hashlib.sha256(serialized).hexdigest()}"


def _append_usage_observation(
    state: AssistantIdentityState,
    obj: dict,
    tool_id: str,
    usage: object,
    *,
    attribution_status: str = "attributed",
    source_id: str | None = None,
) -> None:
    normalized = normalize_token_usage(usage)
    timestamp = _coerce_text(
        obj.get("timestamp") or _as_mapping(obj.get("payload")).get("timestamp")
    ).strip()
    status = attribution_status
    if status == "attributed" and not timestamp:
        status = "missing_timestamp"
    if status == "attributed" and not state.model:
        status = "missing_model"
    observation = AssistantUsageObservation(
        source_id=(source_id or _usage_observation_source_id(obj, tool_id))[:512],
        timestamp=timestamp[:128],
        source=str(normalized.get("source") or tool_id)[:32],
        model=state.model[:200],
        reasoning_effort=state.reasoning_effort[:50],
        service_tier=state.service_tier[:50],
        attribution_status=status,
        token_usage=normalized,
    )
    if source_id and state.usage_observations:
        previous = state.usage_observations[-1]
        if (
            previous.source_id == observation.source_id
            and previous.model == observation.model
            and previous.reasoning_effort == observation.reasoning_effort
            and previous.service_tier == observation.service_tier
            and previous.attribution_status == observation.attribution_status
        ):
            state.usage_observations[-1] = AssistantUsageObservation(
                source_id=observation.source_id,
                timestamp=observation.timestamp or previous.timestamp,
                source=observation.source,
                model=observation.model,
                reasoning_effort=observation.reasoning_effort,
                service_tier=observation.service_tier,
                attribution_status=observation.attribution_status,
                token_usage=add_token_usage(
                    previous.token_usage,
                    observation.token_usage,
                ),
            )
            return
    state.usage_observations.append(observation)


def _update_assistant_identity(
    state: AssistantIdentityState,
    obj: object,
    tool_id: str,
) -> None:
    """Advance model/reasoning state from one native transcript record."""
    if not isinstance(obj, dict):
        return

    observed_at = _message_timestamp(
        obj.get("timestamp") or _as_mapping(obj.get("payload")).get("timestamp")
    )
    if observed_at is not None:
        started_at = _message_timestamp(state.started_at)
        last_activity_at = _message_timestamp(state.last_activity_at)
        if started_at is None or observed_at < started_at:
            state.started_at = observed_at.isoformat()
        if last_activity_at is None or observed_at > last_activity_at:
            state.last_activity_at = observed_at.isoformat()

    msg_type = _coerce_text(obj.get("type"))
    if tool_id == "codex":
        payload = _as_mapping(obj.get("payload"))
        if msg_type == "event_msg" and payload.get("type") == "token_count":
            previous = normalize_token_usage(state.token_usage)
            current = codex_total_token_usage(payload)
            if current:
                delta = subtract_token_usage(current, previous)
                state.token_usage = current
                if delta:
                    _append_usage_observation(
                        state,
                        obj,
                        tool_id,
                        delta,
                        source_id=state.usage_segment_id or None,
                    )
                elif previous and current != previous:
                    _append_usage_observation(
                        state,
                        obj,
                        tool_id,
                        {},
                        attribution_status="counter_reset",
                    )
            return
        if msg_type == "turn_context":
            turn_id = _bounded_identity_text(
                payload.get("turn_id")
                or payload.get("turnId")
                or payload.get("id"),
                420,
            )
            if not turn_id:
                turn_id = _usage_observation_source_id(obj, tool_id).split(
                    ":", 1
                )[-1]
            state.usage_segment_id = f"codex:turn:{turn_id}"[:512]
            _set_identity_field(state, payload, ("model",), "model")
            _set_identity_field(
                state,
                payload,
                ("effort", "reasoning_effort", "reasoningEffort"),
                "reasoning_effort",
            )
            _set_identity_field(
                state,
                payload,
                ("service_tier", "serviceTier"),
                "service_tier",
            )
            _set_agent_mode(state, payload)
            return
        if (
            msg_type == "event_msg"
            and payload.get("type") in {"thread_settings", "thread_settings_applied"}
        ):
            settings = _as_mapping(payload.get("thread_settings"))
            _set_identity_field(state, settings, ("model",), "model")
            _set_identity_field(
                state,
                settings,
                ("effort", "reasoning_effort", "reasoningEffort"),
                "reasoning_effort",
            )
            _set_identity_field(
                state,
                settings,
                ("service_tier", "serviceTier"),
                "service_tier",
            )
            _set_agent_mode(state, settings)
            return
        if msg_type == "event_msg" and payload.get("type") == "task_started":
            _set_agent_mode(state, payload)
            return

    if tool_id == "claude_code":
        # Claude stores extended-thinking blocks in the immutable transcript,
        # but its numeric effort level currently lives only in mutable global
        # settings.  Clear the inferred mode at each new turn, then carry a
        # directly observed thinking block through that turn's tool loop.
        if msg_type in {"user", "queue-operation"}:
            if state.reasoning_effort == "extended":
                state.reasoning_effort = ""
            return
        if msg_type != "assistant":
            return

        message = _as_mapping(obj.get("message"))
        if not _is_internal_model_sentinel(message.get("model")):
            _set_identity_field(state, message, ("model",), "model")
        explicit_effort = _set_identity_field(
            state,
            obj,
            ("effort", "effortLevel", "reasoning_effort", "thinking_level"),
            "reasoning_effort",
        )
        explicit_effort = _set_identity_field(
            state,
            message,
            ("effort", "effortLevel", "reasoning_effort", "thinking_level"),
            "reasoning_effort",
        ) or explicit_effort
        if (
            not explicit_effort
            and not state.reasoning_effort
            and _has_claude_thinking_block(message.get("content"))
        ):
            state.reasoning_effort = "extended"
        usage = claude_message_token_usage(message)
        source_id = _usage_observation_source_id(obj, tool_id)
        if usage and source_id not in state.token_usage_source_ids:
            state.token_usage_source_ids.add(source_id)
            state.token_usage = add_token_usage(state.token_usage, usage)
            _append_usage_observation(state, obj, tool_id, usage)
        return

    # Cursor exports and OpenClaw sessions vary by release. Only consume
    # explicit scalar identity fields; ordinary message content is ignored.
    _set_identity_field(
        state,
        obj,
        ("model", "model_id", "modelId"),
        "model",
    )
    _set_identity_field(
        state,
        obj,
        (
            "reasoning_effort",
            "reasoningEffort",
            "thinking_level",
            "thinkingLevel",
        ),
        "reasoning_effort",
    )


def observe_assistant_identity_record(
    state: AssistantIdentityState,
    record: object,
    tool_id: str,
) -> None:
    """Advance the shared live/backfill assistant telemetry state."""
    _update_assistant_identity(state, record, tool_id)


def _attach_assistant_identity(
    message: NormalizedMessage,
    state: AssistantIdentityState,
) -> None:
    """Copy active identity onto assistant rows and interactive prompts."""
    has_interaction = message.interaction is not None or any(
        isinstance(call, dict) and isinstance(call.get("interaction"), dict)
        for call in message.tool_calls
    )
    if message.role != "assistant" and not has_interaction:
        return
    message.model = state.model
    message.reasoning_effort = state.reasoning_effort
    message.service_tier = state.service_tier
    message.agent_mode = state.agent_mode


def _agent_activity_label(agent_path: object) -> str:
    """Turn a stable agent path into a compact human-readable label."""
    path = _bounded_identity_text(agent_path)
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[_\-]+", tail) if word]
    acronyms = {"ai", "api", "cli", "cpu", "db", "etl", "gpu", "rca", "rss", "slo", "ui"}
    return " ".join(
        word.upper() if word.casefold() in acronyms else word.title()
        for word in words
    )


def _agent_path_from_label(label: object) -> str:
    """Build a stable ``/root/<slug>`` path from a human task label."""
    text = _bounded_interaction_text(label, 256).strip()
    if not text:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    if not slug:
        return ""
    return f"/root/{slug[:120]}"


def _normalized_agent_activity_kind(value: object) -> str:
    kind = _coerce_text(value).strip().casefold().replace("-", "_")
    aliases = {
        "interacted": "updated",
        "finished": "completed",
        "complete": "completed",
        "stopped": "interrupted",
        "killed": "interrupted",
        "cancelled": "interrupted",
        "canceled": "interrupted",
        "error": "failed",
        "errored": "failed",
        "success": "completed",
        "done": "completed",
        "running": "started",
        "pending": "started",
        "loading": "started",
        "in_progress": "started",
    }
    kind = aliases.get(kind, kind)
    if kind in {"started", "updated", "completed", "interrupted", "failed"}:
        return kind
    return "updated"


def build_agent_lifecycle_event(
    *,
    agent_path: object,
    agent_thread_id: object,
    kind: object,
    label: object | None = None,
) -> dict[str, object] | None:
    """Build the shared v1 agent lifecycle payload used by badge merge."""
    path = _bounded_interaction_text(agent_path, 512).strip()
    thread_id = _bounded_interaction_text(agent_thread_id, 512).strip()
    if not path or not thread_id:
        return None
    resolved_label = _bounded_interaction_text(label, 256).strip()
    if not resolved_label:
        resolved_label = _agent_activity_label(path) or "Subagent"
    return {
        "version": 1,
        "agent_path": path,
        "agent_thread_id": thread_id,
        "label": resolved_label,
        "kind": _normalized_agent_activity_kind(kind),
    }


def _is_claude_agent_tool_name(value: object) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", _coerce_text(value).casefold())
    return compact == "agent"


def normalize_claude_agent_launch_event(
    tool_name: object,
    tool_input: object,
    tool_call_id: object,
) -> dict[str, object] | None:
    """Normalize a Claude Agent launch using its source tool-use identity."""
    if not _is_claude_agent_tool_name(tool_name):
        return None
    launch_id = _bounded_interaction_text(tool_call_id, 512).strip()
    payload = _json_mapping(tool_input)
    description = _bounded_interaction_text(
        payload.get("description"),
        1_024,
    ).strip()
    if not launch_id or not description:
        return None
    event: dict[str, object] = {
        "version": 2,
        "source": "claude_agent",
        "agent_tool_use_id": launch_id,
        "label": description,
        "kind": "started",
        "activity_type": "subagent",
        "task_kind": "subagent",
        "task_id": launch_id,
        "status": "running",
    }
    event.update(normalized_subagent_runtime(
        model=payload.get("model"),
        reasoning_effort=(
            payload.get("effort")
            or payload.get("reasoning_effort")
            or payload.get("thinking_level")
        ),
    ))
    agent_type = _bounded_interaction_text(
        payload.get("subagent_type")
        or payload.get("subagentType")
        or payload.get("agent_type")
        or payload.get("agentType"),
        128,
    ).strip()
    if agent_type:
        event["agent_type"] = agent_type
    if isinstance(payload.get("run_in_background"), bool):
        event["is_background"] = payload["run_in_background"]
    return event


def _claude_agent_result_kind(
    result: dict,
    result_item: dict,
) -> tuple[str, str]:
    raw_status = _bounded_interaction_text(
        result.get("status")
        or result.get("state")
        or result_item.get("status"),
        80,
    ).strip().casefold().replace("-", "_").replace(" ", "_")
    if raw_status in {"interrupted", "cancelled", "canceled", "stopped", "aborted"}:
        return "interrupted", raw_status
    if raw_status in {"failed", "error", "errored"}:
        return "failed", raw_status
    if raw_status in {"completed", "complete", "finished", "success", "succeeded", "done"}:
        return "completed", raw_status
    if raw_status in {
        "running",
        "started",
        "pending",
        "in_progress",
        "background",
        "async_launched",
    }:
        return "started", raw_status
    if (
        result_item.get("is_error") is True
        or result_item.get("isError") is True
        or result.get("is_error") is True
        or result.get("isError") is True
        or result.get("success") is False
        or bool(result.get("error"))
    ):
        return "failed", raw_status or "failed"
    if result.get("isBackground") is True or result.get("is_background") is True:
        return "started", raw_status or "running"
    # A non-background tool_result is the source-backed completion of the
    # exact Agent tool-use ID, not an inference from result text or timing.
    return "completed", raw_status or "completed"


def normalize_claude_agent_result_event(
    source_object: object,
    content: object,
    tool_call_id: object,
    launch_event: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Normalize an exact Claude Agent result without description matching."""
    launch_id = _bounded_interaction_text(tool_call_id, 512).strip()
    if not launch_id:
        return None
    result_item = next(
        (
            item
            for item in (content if isinstance(content, list) else [])
            if isinstance(item, dict)
            and item.get("type") in {"tool_result", "toolResult"}
            and _bounded_interaction_text(
                item.get("tool_use_id")
                or item.get("tool_call_id")
                or item.get("toolCallId")
                or item.get("call_id"),
                512,
            ).strip()
            == launch_id
        ),
        {},
    )
    if not result_item:
        return None
    source = _as_mapping(source_object)
    result = _as_mapping(
        source.get("toolUseResult")
        or source.get("tool_use_result")
        or result_item.get("toolUseResult")
        or result_item.get("tool_use_result")
    )
    agent_id = _bounded_interaction_text(
        result.get("agentId")
        or result.get("agent_id")
        or result_item.get("agentId")
        or result_item.get("agent_id"),
        512,
    ).strip()
    has_agent_evidence = bool(
        launch_event
        or agent_id
        or any(
            key in result
            for key in (
                "agentType",
                "agent_type",
                "isBackground",
                "is_background",
            )
        )
    )
    if not has_agent_evidence:
        return None
    kind, status = _claude_agent_result_kind(result, result_item)
    label = str((launch_event or {}).get("label") or "Subagent")
    event = {
        **(launch_event or {}),
        "version": 2,
        "source": "claude_agent",
        "agent_tool_use_id": launch_id,
        "label": label,
        "kind": kind,
        "activity_type": "subagent",
        "task_kind": "subagent",
        "task_id": launch_id,
        "status": status,
    }
    if agent_id:
        event["agent_thread_id"] = agent_id
    if isinstance(result.get("isBackground"), bool):
        event["is_background"] = result["isBackground"]
    elif isinstance(result.get("is_background"), bool):
        event["is_background"] = result["is_background"]
    result_details = _extract_tool_result_details(content)
    result_summary = _bounded_interaction_text(
        result_details[0] if result_details is not None else "",
        4_000,
    ).strip()
    if result_summary:
        event["result_summary"] = result_summary
    return event


def normalize_claude_task_notification_event(
    value: object,
) -> dict[str, object] | None:
    """Normalize Claude's exact background-agent terminal notification.

    ``TaskStop`` is a process-control tool, not a planner task mutation.  The
    later queue notification is the authoritative bridge between Claude's
    Agent tool-use id and the spawned agent/task id, so only this narrowly
    validated envelope is allowed to close a delegated-agent lifecycle.
    """
    text = strip_terminal_sequences(_coerce_text(value)).strip()
    outer = re.fullmatch(
        r"<task-notification>\s*(?P<body>.*?)\s*</task-notification>",
        text,
        flags=re.DOTALL,
    )
    if outer is None:
        return None
    body = outer.group("body")

    def field(name: str, limit: int) -> str:
        match = re.search(
            rf"<{re.escape(name)}>\s*(?P<value>.*?)\s*</{re.escape(name)}>",
            body,
            flags=re.DOTALL,
        )
        return _bounded_interaction_text(
            match.group("value") if match is not None else "",
            limit,
        ).strip()

    task_id = field("task-id", 512)
    tool_use_id = field("tool-use-id", 512)
    raw_status = field("status", 80).casefold().replace("-", "_")
    supported_statuses = {
        "running",
        "started",
        "pending",
        "in_progress",
        "completed",
        "complete",
        "finished",
        "success",
        "succeeded",
        "done",
        "interrupted",
        "cancelled",
        "canceled",
        "stopped",
        "aborted",
        "killed",
        "failed",
        "error",
        "errored",
    }
    if not task_id or not tool_use_id or raw_status not in supported_statuses:
        return None

    kind = _normalized_agent_activity_kind(raw_status)
    status = {
        "started": "running",
        "completed": "completed",
        "interrupted": "cancelled",
        "failed": "failed",
    }.get(kind, raw_status)
    summary = field("summary", 1_024)
    label_match = re.search(r'Agent\s+["\u201c](?P<label>.*?)["\u201d]', summary)
    label = _bounded_interaction_text(
        label_match.group("label") if label_match is not None else summary,
        256,
    ).strip() or "Subagent"
    event: dict[str, object] = {
        "version": 2,
        "source": "claude_agent",
        "agent_tool_use_id": tool_use_id,
        "agent_thread_id": task_id,
        "label": label,
        "kind": kind,
        "activity_type": "subagent",
        "task_kind": "subagent",
        "task_id": task_id,
        "status": status,
    }
    result_summary = field("result", 4_000)
    if result_summary:
        event["result_summary"] = result_summary
    return event


def normalize_cursor_task_completion_event(
    value: object,
) -> dict[str, object] | None:
    """Extract Cursor's synthetic task-finished envelope as a safe event."""
    match = _CURSOR_TASK_COMPLETION_RE.search(_coerce_text(value))
    if match is None:
        return None
    body = match.group("body")
    header = re.split(
        r"(?mi)^(?:detail\s*:|<response>)",
        body,
        maxsplit=1,
    )[0]
    fields: dict[str, str] = {}
    for key in (
        "kind",
        "status",
        "task_id",
        "title",
        "tool_call_id",
        "agent_id",
    ):
        field_match = re.search(
            rf"(?msi)^{key}\s*:\s*(?P<value>.*?)"
            r"(?=^[a-z_][a-z0-9_]*\s*:|^<|\Z)",
            header,
        )
        if field_match is not None:
            fields[key] = field_match.group("value").strip()

    task_id = _bounded_interaction_text(fields.get("task_id"), 512).strip()
    task_kind = _bounded_interaction_text(fields.get("kind"), 80).strip().casefold()
    raw_status = _bounded_interaction_text(fields.get("status"), 80).strip().casefold()
    if not task_id or not task_kind or not raw_status:
        return None
    label = (
        _bounded_interaction_text(fields.get("title"), 256).strip()
        or f"{task_kind.title()} task"
    )
    activity_type = "subagent" if task_kind == "subagent" else (
        "shell" if task_kind == "shell" else "task"
    )
    event: dict[str, object]
    if activity_type == "subagent":
        thread_id = _bounded_interaction_text(
            fields.get("agent_id") or task_id,
            512,
        ).strip()
        event = build_agent_lifecycle_event(
            agent_path=_agent_path_from_label(label) or f"/root/{thread_id[:8]}",
            agent_thread_id=thread_id,
            kind=raw_status,
            label=label,
        ) or {}
        if not event:
            return None
    else:
        event = {
            "version": 1,
            "label": label,
            "kind": _normalized_agent_activity_kind(raw_status),
        }
    event.update({
        "activity_type": activity_type,
        "task_kind": task_kind,
        "task_id": task_id,
        "status": raw_status,
    })

    tool_call_id = _bounded_interaction_text(
        fields.get("tool_call_id"),
        512,
    ).strip()
    if tool_call_id:
        event["tool_call_id"] = tool_call_id
    summary_match = _CURSOR_TASK_SUMMARY_RE.search(body)
    response_match = _CURSOR_TASK_RESPONSE_RE.search(body)
    summary = _bounded_interaction_text(
        summary_match.group("summary")
        if summary_match is not None
        else response_match.group("response")
        if response_match is not None
        else "",
        4_000,
    ).strip()
    if summary:
        event["result_summary"] = summary
    output_match = re.search(
        r"(?mi)^output_path\s*:\s*(?P<path>[^\r\n]+?)\s*$",
        body,
    )
    if output_match is not None:
        output_path = _bounded_interaction_text(
            output_match.group("path"),
            4_096,
        ).strip()
        if output_path:
            event["output_path"] = output_path
    return event


def _is_task_spawn_tool_name(value: object) -> bool:
    """Recognize Cursor Task spawn tools without matching todo Task* tools."""
    compact = re.sub(r"[^a-z0-9]", "", _coerce_text(value).casefold())
    return compact in {"task", "taskv2"}


def _status_prefix_from_tool_content(content: str) -> str:
    match = re.match(
        r"^Status:\s*(?P<status>[A-Za-z0-9_\- ]+)\s*(?:\n|$)",
        _coerce_text(content),
        re.IGNORECASE,
    )
    if match is None:
        return ""
    return match.group("status").strip()


def _tool_content_payload(content: object) -> dict:
    text = _coerce_text(content)
    parsed = _json_mapping(text)
    if parsed:
        return parsed
    stripped = re.sub(
        r"^Status:\s*[A-Za-z0-9_\- ]+\s*(?:\n+|$)",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return _json_mapping(stripped)


def normalize_task_spawn_agent_event(
    tool_name: object,
    tool_input: object,
    content: object = "",
    *,
    tool_status: object = "",
) -> dict[str, object] | None:
    """Map Cursor Task spawn rows onto the shared agent lifecycle event.

    Cursor records the child session id and task description on one
    ``task_v2`` / ``Task`` tool row.  Codex emits the same semantic shape from
    ``sub_agent_activity``; keeping one payload lets badge merge stay tool-agnostic.
    """
    if not _is_task_spawn_tool_name(tool_name):
        return None
    payload = _json_mapping(tool_input)
    result = _json_mapping(content)
    if not result:
        # ``Status: cancelled\\n\\n{...}`` keeps JSON after the status line.
        stripped = re.sub(
            r"^Status:\s*[A-Za-z0-9_\- ]+\s*(?:\n+|$)",
            "",
            _coerce_text(content),
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        result = _json_mapping(stripped)
    thread_id = _bounded_interaction_text(
        result.get("agentId")
        or result.get("agent_id")
        or payload.get("agentId")
        or payload.get("agent_id"),
        512,
    )
    if not thread_id:
        return None
    label = _bounded_interaction_text(
        payload.get("description")
        or payload.get("title")
        or payload.get("name")
        or payload.get("subagentType")
        or payload.get("subagent_type"),
        256,
    )
    agent_path = _agent_path_from_label(label) or f"/root/{thread_id[:8]}"
    if not label:
        label = _agent_activity_label(agent_path) or "Subagent"
    status_hint = (
        _coerce_text(tool_status).strip()
        or _status_prefix_from_tool_content(_coerce_text(content))
        or "completed"
    )
    if (
        result.get("isBackground") is True
        and _normalized_agent_activity_kind(status_hint) not in {
            "failed",
            "interrupted",
        }
    ):
        # The task_v2 tool call completed after enqueueing the child; the child
        # itself is only started until Cursor emits its completion notification.
        status_hint = "started"
    event = build_agent_lifecycle_event(
        agent_path=agent_path,
        agent_thread_id=thread_id,
        kind=status_hint,
        label=label,
    )
    if event is not None:
        event.update({
            "activity_type": "subagent",
            "task_kind": "subagent",
            "task_id": thread_id,
            "status": _coerce_text(status_hint).strip().casefold(),
        })
        event.update(normalized_subagent_runtime(
            model=(
                payload.get("model")
                or payload.get("modelSlug")
                or payload.get("model_slug")
            ),
            reasoning_effort=(
                payload.get("reasoning_effort")
                or payload.get("reasoningEffort")
                or payload.get("effort")
                or payload.get("thinking_level")
            ),
        ))
    return event


def normalize_codex_agent_snapshot(value: object) -> dict[str, object] | None:
    """Normalize Codex ``list_agents`` output without retaining private text.

    ``list_agents`` is persisted as an ordinary function result even though
    the desktop client presents it as subagent status chips.  The agent path
    is the stable, task-oriented identity; generated nicknames and completion
    payloads are deliberately not inferred from arbitrary tool output.
    """
    payload = _json_mapping(value)
    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list):
        return None

    agents: list[dict[str, str]] = []
    for raw_agent in raw_agents[:256]:
        if not isinstance(raw_agent, dict):
            continue
        agent_path = _bounded_interaction_text(raw_agent.get("agent_name"), 512)
        if not agent_path.startswith("/root/"):
            continue
        label = _agent_activity_label(agent_path)
        if not label:
            continue

        raw_status = raw_agent.get("agent_status", raw_agent.get("status"))
        if isinstance(raw_status, dict):
            status_keys = {str(key).strip().casefold() for key in raw_status}
            if status_keys & {"failed", "error", "errored"}:
                status = "failed"
            elif status_keys & {"interrupted", "cancelled", "canceled", "stopped"}:
                status = "interrupted"
            elif status_keys & {"completed", "complete", "finished"}:
                status = "completed"
            elif status_keys & {"running", "active", "working"}:
                status = "running"
            else:
                status = "unknown"
        else:
            normalized = _coerce_text(raw_status).strip().casefold().replace("-", "_")
            status = {
                "active": "running",
                "working": "running",
                "complete": "completed",
                "finished": "completed",
                "cancelled": "interrupted",
                "canceled": "interrupted",
                "stopped": "interrupted",
                "error": "failed",
                "errored": "failed",
            }.get(normalized, normalized or "unknown")
            if status not in {"running", "completed", "interrupted", "failed", "unknown"}:
                status = "unknown"

        agents.append({
            "agent_path": agent_path,
            "label": label,
            "status": status,
        })

    if not agents:
        return None
    return {
        "version": 2,
        "kind": "snapshot",
        "agents": agents,
    }


def codex_agent_snapshot_summary(snapshot: dict[str, object]) -> str:
    agents = snapshot.get("agents")
    safe_agents = agents if isinstance(agents, list) else []
    running_count = sum(
        agent.get("status") == "running"
        for agent in safe_agents
        if isinstance(agent, dict)
    )
    noun = "subagent" if len(safe_agents) == 1 else "subagents"
    return f"{len(safe_agents)} {noun} · {running_count} running"


def parse_conversation_line(raw_line: str, tool_id: str) -> NormalizedMessage | None:
    """Parse a single JSONL line into a NormalizedMessage, or None if it should be skipped."""
    try:
        obj = orjson.loads(raw_line)
    except orjson.JSONDecodeError:
        return None

    identity = AssistantIdentityState()
    _update_assistant_identity(identity, obj, tool_id)
    message = parse_conversation_object(obj, tool_id)
    if message is not None:
        _attach_assistant_identity(message, identity)
        TaskStateTracker(tool_id).apply(message)
    return message


def parse_conversation_object(
    obj: object,
    tool_id: str,
) -> NormalizedMessage | None:
    """Normalize an already-decoded source record.

    Bulk parsing uses this entry point so each multi-gigabyte corpus record is
    decoded once. ``parse_conversation_line`` remains the compatibility API
    for callers and tests that receive an isolated JSON string.
    """

    if not isinstance(obj, dict):
        return None

    msg_type = obj.get("type", "")
    timestamp = obj.get("timestamp", "")

    # --- Claude Code format ---
    if tool_id == "claude_code":
        source_id = str(obj.get("uuid") or obj.get("promptId") or "")
        if msg_type in ("user", "assistant"):
            message = _as_mapping(obj.get("message"))
            role = _coerce_text(message.get("role") or msg_type)
            raw_content = message.get("content", "")

            # Claude inserts this provider-internal acknowledgement after
            # certain local commands.  It carries no agent response and its
            # ``<synthetic>`` model is a transport sentinel, so rendering it
            # as an assistant message invents both content and model context.
            if (
                role == "assistant"
                and _is_internal_model_sentinel(message.get("model"))
                and _extract_content(raw_content).strip()
                == "No response requested."
                and not _extract_thinking_parts(raw_content).strip()
            ):
                return None

            # Claude Code records slash commands as synthetic user messages.
            # They are useful session context, but they are not human prompts;
            # normalize them into compact tool rows instead of purple bubbles.
            local_command = _extract_local_command(raw_content)
            if role == "user" and local_command is not None:
                tool_name, tool_input, output = local_command
                return NormalizedMessage(
                    role="tool",
                    content=output or f"[{tool_name}]",
                    tool_name=tool_name,
                    tool_input=tool_input,
                    timestamp=timestamp,
                    raw_type="local_command",
                    source_id=source_id,
                )

            # Claude's API represents a tool result as a message whose outer
            # role is "user".  It is not human input: the content blocks are
            # typed tool_result and must render as a tool card, otherwise large
            # terminal dumps become giant purple User bubbles.
            tool_result = _extract_tool_result_details(raw_content)
            if role == "user" and tool_result is not None:
                result_content, tool_call_id = tool_result
                agent_event = normalize_claude_agent_result_event(
                    obj,
                    raw_content,
                    tool_call_id,
                )
                if agent_event is not None and timestamp:
                    event_time_key = (
                        "completed_at"
                        if agent_event.get("kind") in {
                            "completed",
                            "interrupted",
                            "failed",
                        }
                        else "started_at"
                    )
                    agent_event[event_time_key] = timestamp
                return NormalizedMessage(
                    role="tool",
                    content=(
                        f"{agent_event['label']} {agent_event['kind']}"
                        if agent_event is not None
                        else result_content or "(tool returned no textual output)"
                    ),
                    tool_name=(
                        "Agent activity"
                        if agent_event is not None
                        else "Tool result"
                    ),
                    timestamp=timestamp,
                    raw_type="agent_event" if agent_event is not None else "tool_result",
                    source_id=source_id,
                    tool_call_id=tool_call_id,
                    agent_event=agent_event,
                )

            tool_use = _extract_tool_use(raw_content)
            if role == "assistant" and tool_use is not None:
                tool_name, tool_input, tool_call_id, interaction = tool_use
                agent_event = normalize_claude_agent_launch_event(
                    tool_name,
                    tool_input,
                    tool_call_id,
                )
                if agent_event is not None and timestamp:
                    agent_event["started_at"] = timestamp
                return NormalizedMessage(
                    role="tool",
                    content=(
                        f"{agent_event['label']} started"
                        if agent_event is not None
                        else f"[{tool_name}]"
                    ),
                    tool_name=(
                        "Agent activity"
                        if agent_event is not None
                        else tool_name
                    ),
                    tool_input=tool_input,
                    timestamp=timestamp,
                    raw_type="agent_event" if agent_event is not None else "tool_use",
                    source_id=source_id,
                    interaction=interaction,
                    tool_call_id=tool_call_id,
                    agent_event=agent_event,
                )

            # Extract thinking separately from final text (Claude extended thinking)
            thinking = _extract_thinking_parts(raw_content)
            content = _extract_content(raw_content)
            if not content.strip() and not thinking.strip():
                return None
            # If only thinking is present (no text reply), use thinking as content
            if not content.strip():
                content = thinking
                thinking = ""
            scheduled_automation = is_scheduled_automation_content(content)
            if role == "user" and (
                is_claude_session_context_record(obj) or scheduled_automation
            ):
                return NormalizedMessage(
                    role="system",
                    content=content,
                    thinking=thinking,
                    timestamp=timestamp,
                    raw_type=(
                        "scheduled_automation"
                        if scheduled_automation
                        else "claude_context"
                    ),
                    source_id=source_id,
                )
            return NormalizedMessage(
                role=role, content=content, thinking=thinking,
                timestamp=timestamp, raw_type=msg_type, source_id=source_id,
            )

        if msg_type == "ai-title":
            return None  # Skip title lines

        if msg_type == "system":
            message = _as_mapping(obj.get("message"))
            content = _extract_content(message.get("content", ""))
            if not content.strip() or "<command-name>" in content:
                return None  # Skip command metadata
            return NormalizedMessage(
                role="system",
                content=content,
                timestamp=timestamp,
                raw_type=msg_type,
                source_id=source_id,
            )

        if msg_type == "queue-operation":
            operation = _coerce_text(
                obj.get("operation") or obj.get("op")
            ).lower()
            if operation != "enqueue":
                return None
            raw_content = _coerce_text(obj.get("content"))
            task_notification = normalize_claude_task_notification_event(
                raw_content,
            )
            if task_notification is not None:
                if timestamp:
                    event_time_key = (
                        "completed_at"
                        if task_notification.get("kind") in {
                            "completed",
                            "interrupted",
                            "failed",
                        }
                        else "started_at"
                    )
                    task_notification[event_time_key] = timestamp
                label = _coerce_text(task_notification.get("label")) or "Subagent"
                kind = _coerce_text(task_notification.get("kind")) or "updated"
                queue_identity = "\x1f".join((
                    _coerce_text(obj.get("sessionId") or obj.get("session_id")),
                    timestamp,
                    raw_content,
                ))
                return NormalizedMessage(
                    role="tool",
                    content=f"{label} {kind}",
                    tool_name="Agent activity",
                    timestamp=timestamp,
                    raw_type="agent_event",
                    source_id=_coerce_text(obj.get("uuid")) or (
                        "claude-task-notification:"
                        + hashlib.sha256(
                            queue_identity.encode("utf-8")
                        ).hexdigest()
                    ),
                    agent_event=task_notification,
                )
            content = _strip_system_tags(raw_content)
            if not content:
                return None
            queue_identity = "\x1f".join((
                _coerce_text(obj.get("sessionId") or obj.get("session_id")),
                timestamp,
                content,
            ))
            queue_source_id = _coerce_text(obj.get("uuid")) or (
                "claude-queue:"
                + hashlib.sha256(queue_identity.encode("utf-8")).hexdigest()
            )
            scheduled_automation = is_scheduled_automation_content(content)
            return NormalizedMessage(
                role="system" if scheduled_automation else "user",
                content=content,
                timestamp=timestamp,
                raw_type=(
                    "queued_scheduled_automation"
                    if scheduled_automation
                    else "queued_user_message"
                ),
                source_id=queue_source_id,
            )

        # Skip: file-history-snapshot and other transport bookkeeping.
        return None

    # --- Codex format ---
    if tool_id == "codex":
        payload = _as_mapping(obj.get("payload"))

        if msg_type == "response_item":
            role = payload.get("role", "")
            if role in ("developer", "system"):
                return None  # Skip system prompts
            p_type = payload.get("type", "")
            if p_type in ("function_call", "custom_tool_call", "web_search_call"):
                tool_name = _coerce_text(payload.get("name")) or (
                    "web_search" if p_type == "web_search_call" else p_type
                )
                if "arguments" in payload:
                    raw_input = payload.get("arguments")
                elif "input" in payload:
                    raw_input = payload.get("input")
                elif "query" in payload:
                    raw_input = payload.get("query")
                else:
                    raw_input = {
                        key: value
                        for key, value in payload.items()
                        if key not in {
                            "type",
                            "id",
                            "call_id",
                            "name",
                            "namespace",
                            "status",
                            "internal_chat_message_metadata_passthrough",
                        }
                    }
                tool_call_id = _bounded_interaction_text(
                    payload.get("call_id") or payload.get("id"),
                    512,
                )
                interaction = normalize_question_interaction(
                    tool_name,
                    raw_input,
                    source="codex",
                    interaction_id=tool_call_id,
                )
                if interaction is not None:
                    return NormalizedMessage(
                        role="tool",
                        content=f"[{tool_name}]",
                        tool_name=tool_name,
                        tool_input=_serialize_tool_input(raw_input),
                        timestamp=timestamp,
                        raw_type="question_tool_call",
                        source_id=tool_call_id,
                        interaction=interaction,
                        tool_call_id=tool_call_id,
                    )
                return NormalizedMessage(
                    role="tool",
                    content=f"[{tool_name}]",
                    tool_name=tool_name,
                    tool_input=_serialize_tool_input(raw_input),
                    timestamp=timestamp,
                    raw_type="tool_call",
                    source_id=tool_call_id,
                    tool_call_id=tool_call_id,
                )
            if p_type in ("function_call_output", "custom_tool_call_output"):
                raw_output = payload.get("output", payload.get("result", ""))
                tool_call_id = _bounded_interaction_text(
                    payload.get("call_id") or payload.get("id"),
                    512,
                )
                agent_snapshot = normalize_codex_agent_snapshot(raw_output)
                if agent_snapshot is not None:
                    return NormalizedMessage(
                        role="tool",
                        content=codex_agent_snapshot_summary(agent_snapshot),
                        tool_name="Subagent status",
                        timestamp=timestamp,
                        raw_type="agent_event",
                        source_id=(
                            f"{tool_call_id}:agents" if tool_call_id else ""
                        ),
                        tool_call_id=tool_call_id,
                        agent_event=agent_snapshot,
                    )
                is_question_response = "answers" in _json_mapping(raw_output)
                return NormalizedMessage(
                    role="tool",
                    content=_extract_codex_tool_output(raw_output),
                    tool_name=(
                        "Question response" if is_question_response else "Tool result"
                    ),
                    timestamp=timestamp,
                    raw_type=(
                        "question_tool_output" if is_question_response else "tool_output"
                    ),
                    source_id=(
                        f"{tool_call_id}:response"
                        if is_question_response and tool_call_id
                        else f"{tool_call_id}:output" if tool_call_id else ""
                    ),
                    tool_call_id=tool_call_id,
                )
            if p_type == "reasoning":
                # Codex stores the user-visible "Thought for …" summaries
                # separately from encrypted internal reasoning. Preserve only
                # the explicit summary and never expose encrypted_content.
                thinking = _extract_codex_reasoning_summary(payload.get("summary"))
                if not thinking:
                    return None
                return NormalizedMessage(
                    role="assistant",
                    content="",
                    thinking=thinking,
                    timestamp=timestamp,
                    raw_type="reasoning",
                    source_id=str(payload.get("id") or ""),
                )
            if p_type == "message" and role == "assistant":
                content = _extract_codex_content(payload.get("content", []))
                if not content.strip():
                    return None
                return NormalizedMessage(
                    role="assistant",
                    content=content,
                    timestamp=timestamp,
                    raw_type=msg_type,
                    source_id=str(payload.get("id") or ""),
                )
            # User response_item/message — real user input (not system context)
            if p_type == "message" and role == "user":
                content = _extract_codex_content(payload.get("content", []))
                if not content.strip():
                    return None
                normalized_role, content = normalize_codex_user_payload(content)
                if not content:
                    return None
                return NormalizedMessage(
                    role=normalized_role,
                    content=content,
                    timestamp=timestamp,
                    raw_type=(
                        "codex_context"
                        if normalized_role == "system"
                        else msg_type
                    ),
                    source_id=str(payload.get("id") or ""),
                    source_turn_id=_coerce_text(
                        _as_mapping(
                            payload.get(
                                "internal_chat_message_metadata_passthrough"
                            )
                        ).get("turn_id")
                        or payload.get("turn_id")
                    ),
                )
            return None

        if msg_type == "event_msg":
            event_type = payload.get("type", "")
            if event_type == "task_started":
                return None
            if event_type == "sub_agent_activity":
                agent_path = _bounded_interaction_text(payload.get("agent_path"), 512)
                event_id = _bounded_interaction_text(payload.get("event_id"), 512)
                thread_id = _bounded_interaction_text(
                    payload.get("agent_thread_id"),
                    512,
                )
                agent_event = build_agent_lifecycle_event(
                    agent_path=agent_path,
                    agent_thread_id=thread_id,
                    kind=payload.get("kind"),
                )
                if agent_event is None:
                    return None
                label = str(agent_event["label"])
                kind = str(agent_event["kind"])
                return NormalizedMessage(
                    role="tool",
                    content=f"{label} {kind}",
                    tool_name="Agent activity",
                    timestamp=timestamp,
                    raw_type="agent_event",
                    source_id=event_id or f"{thread_id}:{timestamp}:{kind}",
                    agent_event=agent_event,
                )
            # User message — the actual user input in Codex
            if event_type == "user_message":
                text = _coerce_text(payload.get("message"))
                if text.strip():
                    normalized_role, text = normalize_codex_user_payload(text)
                    return NormalizedMessage(
                        role=normalized_role,
                        content=text,
                        timestamp=timestamp,
                        raw_type=(
                            "codex_context"
                            if normalized_role == "system"
                            else "user_message"
                        ),
                        source_id=str(
                            payload.get("client_id") or payload.get("id") or ""
                        ),
                        source_turn_id=_coerce_text(payload.get("turn_id")),
                    )
                return None
            # Agent message — intermediate commentary in new Codex, sole reply in old Codex.
            # Kept as assistant message; if task_complete also exists, ingest dedup handles it.
            if event_type == "agent_message":
                text = _coerce_text(payload.get("message"))
                if text.strip():
                    return NormalizedMessage(
                        role="assistant",
                        content=text,
                        timestamp=timestamp,
                        raw_type="agent_message",
                        source_id=str(
                            payload.get("client_id") or payload.get("id") or ""
                        ),
                    )
                return None
            if event_type == "task_complete":
                text = _coerce_text(payload.get("last_agent_message"))
                if text.strip():
                    return NormalizedMessage(
                        role="assistant",
                        content=text,
                        timestamp=timestamp,
                        raw_type="task_complete",
                        source_id=str(payload.get("turn_id") or ""),
                    )
                return None
            if event_type == "turn_aborted":
                turn_id = _bounded_interaction_text(payload.get("turn_id"), 512)
                reason = _bounded_interaction_text(payload.get("reason"), 120)
                duration_ms = payload.get("duration_ms")
                details: list[str] = []
                if reason:
                    details.append(f"Reason: {reason}")
                if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
                    details.append(f"Elapsed: {duration_ms / 1000:g}s")
                return NormalizedMessage(
                    role="system",
                    content=(
                        "Turn interrupted"
                        + (f" · {' · '.join(details)}" if details else "")
                    ),
                    timestamp=timestamp,
                    raw_type="turn_aborted",
                    source_id=f"{turn_id}:aborted" if turn_id else "",
                    source_turn_id=turn_id,
                )
            return None

        return None  # Skip session_meta, turn_context, etc.

    # --- OpenClaw format ---
    if tool_id == "openclaw":
        if msg_type == "message":
            raw_msg = obj.get("message", "")
            # OpenClaw stores message as Python repr string, try to parse
            msg_dict = None
            if isinstance(raw_msg, str):
                try:
                    msg_dict = orjson.loads(raw_msg)
                except orjson.JSONDecodeError:
                    try:
                        msg_dict = eval(raw_msg)  # noqa: S307 — OpenClaw uses repr format
                    except Exception:
                        pass
            elif isinstance(raw_msg, dict):
                msg_dict = raw_msg

            if msg_dict and isinstance(msg_dict, dict):
                role = msg_dict.get("role", "unknown")
                raw_content = msg_dict.get("content", "")
                # Extract thinking separately (OpenClaw uses Claude-style content array)
                thinking = _extract_thinking_parts(raw_content)
                content = _extract_content(raw_content)
                # Strip OpenClaw metadata prefix (Conversation info blocks)
                if content.startswith("Conversation info"):
                    # Extract actual user text after the JSON block
                    parts = content.split("```\n")
                    if len(parts) >= 3:
                        content = parts[-1].strip()
                    elif len(parts) >= 2:
                        content = parts[-1].strip()
                # Strip [[reply_to_current]] prefix
                content = content.replace("[[reply_to_current]] ", "")
                # Map OpenClaw's toolResult role (~27% of messages in a real
                # session) to our "tool" role so it participates in the
                # timeline. Without this, every tool step dropped and chat
                # looked like a disjointed user/assistant transcript.
                if role == "toolResult":
                    role = "tool"
                if role in ("user", "assistant", "tool"):
                    if not content.strip() and thinking.strip():
                        # Only thinking — use it as content
                        content = thinking
                        thinking = ""
                    if content.strip():
                        return NormalizedMessage(
                            role=role, content=content.strip(), thinking=thinking,
                            timestamp=timestamp, raw_type=msg_type,
                        )
            return None

        if msg_type == "compaction":
            # Summary line auto-generated when OpenClaw compacts context.
            # Surface as a system message so it's searchable + visible in the
            # transcript instead of being silently dropped.
            summary = _coerce_text(obj.get("summary"))
            if summary.strip():
                return NormalizedMessage(
                    role="system", content=summary.strip(),
                    timestamp=timestamp, raw_type=msg_type,
                )
            return None

        if msg_type in ("session", "model_change", "thinking_level_change", "custom"):
            return None

        if msg_type == "tool_call":
            name = obj.get("name", "tool")
            args = obj.get("arguments", obj.get("data", ""))
            return NormalizedMessage(
                role="tool", content=f"[{name}]", tool_name=name,
                tool_input=str(args), timestamp=timestamp, raw_type=msg_type,
            )
        if msg_type == "tool_result":
            output = str(obj.get("data", obj.get("output", "")))
            return NormalizedMessage(role="tool", content=output, timestamp=timestamp, raw_type="tool_output")

        return None

    # --- Antigravity format (generated by collector export) ---
    if tool_id == "antigravity":
        if msg_type == "session_meta":
            return None  # Skip metadata line

        if msg_type in ("user", "assistant"):
            message = _as_mapping(obj.get("message"))
            role = _coerce_text(message.get("role") or msg_type)
            content = _extract_content(message.get("content", ""))
            thinking = str(obj.get("thinking_text", "") or "").strip()
            raw_type = obj.get("content_source") or obj.get("fallback_source") or msg_type
            # pb_thinking = standalone thinking with no visible reply
            # Show as collapsible thinking (same UX as Claude Code thinking)
            if raw_type == "pb_thinking" and thinking:
                return NormalizedMessage(
                    role="assistant",
                    content="[AI 思考过程]",
                    thinking=thinking,
                    timestamp=timestamp,
                    raw_type=raw_type,
                )
            if not content.strip():
                content = thinking
            if not content.strip():
                return None
            return NormalizedMessage(
                role=role,
                content=content,
                thinking=thinking,
                timestamp=timestamp,
                raw_type=raw_type,
            )

        if msg_type == "tool":
            tool_name = _coerce_text(obj.get("tool_name") or "tool")
            tool_input = _coerce_text(obj.get("tool_input"))
            content = _extract_content(obj.get("content", f"[{tool_name}]"))
            return NormalizedMessage(
                role="tool", content=content, tool_name=tool_name,
                tool_input=tool_input, timestamp=timestamp, raw_type=msg_type,
            )

        if msg_type == "system":
            message = _as_mapping(obj.get("message"))
            content = _extract_content(message.get("content", ""))
            if content.strip():
                raw_type = obj.get("content_source") or obj.get("fallback_source") or msg_type
                return NormalizedMessage(
                    role="system",
                    content=content,
                    timestamp=timestamp,
                    raw_type=raw_type,
                )
            return None

        return None

    # --- Cursor format: {"role": "user/assistant", "message": {"content": [...]}} ---
    if tool_id == "cursor" or (not msg_type and "message" in obj and "role" in obj):
        role = obj.get("role", "")
        if msg_type in {
            "cursor_state_tool",
            "cursor_state_task",
            "cursor_state_status",
        }:
            content = _extract_content(obj.get("content", ""))
            tool_name = _coerce_text(obj.get("tool_name") or "Cursor")
            tool_input = _coerce_text(obj.get("tool_input"))
            if _is_cursor_terminal_read(tool_name, tool_input):
                tool_name = "Terminal"
            tool_call_id = _bounded_interaction_text(
                obj.get("tool_call_id") or obj.get("id"),
                512,
            )
            interaction = normalize_interaction(
                tool_name,
                tool_input,
                source="cursor",
                interaction_id=tool_call_id,
            )
            interaction_response = None
            if interaction is not None:
                interaction_response = build_cursor_interaction_response(
                    interaction,
                    content,
                    obj.get("tool_status"),
                    obj.get("tool_status_reason"),
                )
            agent_event = normalize_task_spawn_agent_event(
                tool_name,
                tool_input,
                content,
                tool_status=obj.get("tool_status"),
            )
            raw_type = "agent_event" if agent_event is not None else msg_type
            display_content = content or f"[{tool_name}]"
            display_tool_name = tool_name
            if agent_event is not None:
                display_tool_name = "Agent activity"
                display_content = (
                    f"{agent_event['label']} {agent_event['kind']}"
                )
                if timestamp:
                    event_time_key = (
                        "completed_at"
                        if agent_event.get("kind") in {
                            "completed",
                            "interrupted",
                            "failed",
                        }
                        else "started_at"
                    )
                    agent_event[event_time_key] = timestamp
            return NormalizedMessage(
                role="tool",
                content=display_content,
                tool_name=display_tool_name,
                tool_input=tool_input,
                tool_call_id=tool_call_id,
                tool_status=_bounded_interaction_text(
                    obj.get("tool_status"),
                    80,
                ).strip().casefold(),
                timestamp=timestamp,
                raw_type=raw_type,
                source_id=_cursor_source_id(obj, obj.get("message")),
                interaction=interaction,
                interaction_response=interaction_response,
                agent_event=agent_event,
            )
        message = obj.get("message", {})
        if isinstance(message, dict):
            raw_content = message.get("content", "")
        else:
            raw_content = message
        source_id = _cursor_source_id(obj, message)
        thinking = _extract_thinking_parts(raw_content)
        tool_calls: list[dict[str, str]] = []
        if role == "assistant":
            content, tool_calls = _extract_cursor_assistant_content(raw_content)
        else:
            content = _extract_content(raw_content)
        session_context = ""
        attachments: tuple[dict[str, str], ...] = ()
        if role == "user":
            payload = parse_cursor_user_payload(content)
            content = payload.content
            envelope_timestamp = payload.timestamp
            session_context = payload.session_context
            attachments = payload.attachments
            if not envelope_timestamp:
                # Older Cursor records can carry only the outer query wrapper.
                # Match the whole payload so literal tags within a prompt are
                # not treated as transport metadata.
                query_match = _CURSOR_USER_QUERY_ENVELOPE_RE.fullmatch(content)
                if query_match is not None:
                    content = query_match.group("content").strip()
            # Preserve a native machine timestamp if a future Cursor version
            # adds one; current transcripts carry it only in the envelope.
            timestamp = timestamp or envelope_timestamp
            directives = normalize_cursor_additional_directives(content)
            if directives is not None:
                return NormalizedMessage(
                    role="system",
                    content=directives,
                    session_context=session_context,
                    timestamp=timestamp,
                    raw_type="cursor_directives",
                    source_id=source_id,
                )
            if session_context and not content.strip():
                completion_event = normalize_cursor_task_completion_event(
                    session_context
                )
                if completion_event is not None:
                    if timestamp:
                        completion_event["completed_at"] = timestamp
                    summary = _coerce_text(
                        completion_event.get("result_summary")
                    ).strip()
                    display_content = (
                        f"{completion_event['label']} "
                        f"{completion_event['kind']}"
                    )
                    if summary:
                        display_content += f"\n\n{summary}"
                    task_key = ":".join((
                        _coerce_text(completion_event.get("task_kind")),
                        _coerce_text(completion_event.get("task_id")),
                        _coerce_text(completion_event.get("kind")),
                    ))
                    return NormalizedMessage(
                        role="tool",
                        content=display_content,
                        tool_name="Task completion",
                        tool_call_id=_coerce_text(
                            completion_event.get("tool_call_id")
                        ),
                        timestamp=timestamp,
                        raw_type="agent_event",
                        source_id=f"cursor-task-completion:{task_key}",
                        agent_event=completion_event,
                    )
                return NormalizedMessage(
                    role="system",
                    content=session_context,
                    timestamp=timestamp,
                    raw_type="cursor_context",
                    source_id=source_id,
                )
        if role in ("user", "assistant") and (
            content.strip() or thinking.strip() or tool_calls or attachments
        ):
            # Skip tool_result/tool_use noise
            if not tool_calls and (
                content.startswith("[Tool:") or content.startswith("[Result]")
            ):
                return None
            return NormalizedMessage(
                role=role, content=content, thinking=thinking,
                session_context=session_context,
                attachments=list(attachments),
                tool_calls=tool_calls, timestamp=timestamp,
                raw_type=msg_type or role, source_id=source_id,
            )
        return None

    # --- Generic fallback ---
    role = obj.get("role", msg_type)
    content = _extract_content(obj.get("content", obj.get("message", "")))
    if role in ("user", "assistant", "system") and content.strip():
        return NormalizedMessage(role=role, content=content, timestamp=timestamp, raw_type=msg_type)

    return None


_SYSTEM_TAGS = (
    "ide_opened_file|ide_selection|system-reminder|"
    "user-prompt-submit-hook|task-notification|"
    "command-name|command-message|command-args|"
    "local-command-caveat|local-command-stdout|local-command-stderr"
)
_SYSTEM_TAG_RE = re.compile(
    rf"<(?:{_SYSTEM_TAGS})[^>]*>.*?</(?:{_SYSTEM_TAGS})>",
    re.DOTALL,
)
# Plain-text system lines injected by Claude Code (not XML tags)
_SYSTEM_LINE_RE = re.compile(
    r"Read the output file to retrieve the result:\s*/\S+\.output\b",
)


def _strip_system_tags(text: str) -> str:
    """Remove IDE/system injection tags and system lines from message content."""
    text = strip_terminal_sequences(text)
    text = _SYSTEM_TAG_RE.sub("", text)
    text = _SYSTEM_LINE_RE.sub("", text)
    return text.strip()


def _extract_tool_result_details(content) -> tuple[str, str] | None:
    """Return Claude/OpenClaw tool-result text and its originating call ID."""
    if not isinstance(content, list):
        return None

    found = False
    parts: list[str] = []
    tool_call_id = ""
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in ("tool_result", "toolResult"):
            continue
        found = True
        if not tool_call_id:
            tool_call_id = _bounded_interaction_text(
                item.get("tool_use_id")
                or item.get("tool_call_id")
                or item.get("toolCallId")
                or item.get("call_id"),
                512,
            )
        result = item.get("content", item.get("output", ""))
        if isinstance(result, list):
            nested: list[str] = []
            for block in result:
                if isinstance(block, dict):
                    text = block.get("text", block.get("content", ""))
                    if text:
                        nested.append(str(text))
                elif block is not None:
                    nested.append(str(block))
            result = "\n".join(nested)
        elif isinstance(result, (dict, list)):
            result = json.dumps(result, ensure_ascii=False, indent=2)
        if result:
            parts.append(str(result))

    if not found:
        return None
    return strip_terminal_sequences("\n\n".join(parts)).strip(), tool_call_id


def _extract_tool_result_content(content) -> str | None:
    """Compatibility wrapper returning only Claude/OpenClaw result text."""
    details = _extract_tool_result_details(content)
    return details[0] if details is not None else None


def _extract_local_command(content) -> tuple[str, str, str] | None:
    """Return Claude Code slash-command context as (name, input, output)."""
    if not isinstance(content, str):
        return None

    def tag_value(name: str) -> str:
        match = re.search(
            rf"<{name}[^>]*>(.*?)</{name}>",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return strip_terminal_sequences(match.group(1)).strip() if match else ""

    command_name = tag_value("command-name")
    command_args = tag_value("command-args")
    stdout = tag_value("local-command-stdout")
    stderr = tag_value("local-command-stderr")

    if command_name:
        return command_name, command_args, stdout or stderr
    if stdout:
        return "Local command result", "", stdout
    if stderr:
        return "Local command error", "", stderr
    return None


def _extract_tool_use(
    content,
) -> tuple[str, str, str, dict[str, object] | None] | None:
    """Return a standalone Claude tool invocation and optional interaction."""
    if not isinstance(content, list):
        return None

    # If the assistant included visible prose alongside the invocation, keep
    # the whole message as assistant text rather than discarding that prose.
    if any(
        isinstance(item, dict)
        and item.get("type") == "text"
        and str(item.get("text", "")).strip()
        for item in content
    ):
        return None

    for item in content:
        if not isinstance(item, dict) or item.get("type") not in ("tool_use", "toolCall"):
            continue
        name = str(item.get("name") or "Tool")
        value = item.get("input") if "input" in item else item.get("arguments", {})
        if isinstance(value, str):
            tool_input = value
        else:
            tool_input = json.dumps(value, ensure_ascii=False, indent=2)
        tool_call_id = _bounded_interaction_text(
            item.get("id") or item.get("call_id"),
            512,
        )
        interaction = normalize_interaction(
            name,
            value,
            source="claude_code",
            interaction_id=tool_call_id,
        )
        return (
            name,
            strip_terminal_sequences(tool_input).strip(),
            tool_call_id,
            interaction,
        )
    return None


def _extract_thinking_parts(content) -> str:
    """Extract Claude-style thinking blocks from a content list.

    Claude Code extended thinking stores reasoning as:
        {"type": "thinking", "thinking": "..."}
    or as redacted thinking:
        {"type": "redacted_thinking", "data": "..."}
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type", "")
        if t == "thinking":
            text = _coerce_text(item.get("thinking"))
            if text:
                parts.append(text)
        elif t == "redacted_thinking":
            data = item.get("data", "")
            if data:
                if isinstance(data, (bytes, bytearray, str, list, dict)):
                    size = len(data)
                else:
                    size = len(_coerce_text(data))
                parts.append(f"[redacted thinking: {size} bytes]")
    return "\n\n".join(parts)


def _extract_codex_reasoning_summary(value: object) -> str:
    """Extract only Codex's explicit user-visible reasoning summaries."""
    items = value if isinstance(value, list) else [value]
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"summary_text", "text"}:
            continue
        text = _coerce_text(item.get("text")).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _bounded_tool_text(value: str, limit: int) -> str:
    """Bound structured tool metadata by UTF-8 bytes."""
    clean = strip_terminal_sequences(value).replace("\x00", "")
    encoded = clean.encode("utf-8")
    if len(encoded) <= limit:
        return clean

    marker = _TOOL_INPUT_TRUNCATION_MARKER.encode("utf-8")
    if len(marker) >= limit:
        return marker[:limit].decode("utf-8", "ignore")
    prefix = encoded[: limit - len(marker)].decode("utf-8", "ignore")
    return prefix + marker.decode("utf-8")


def _serialize_tool_input(value: object) -> str:
    if isinstance(value, str):
        serialized = value
    else:
        try:
            serialized = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError):
            serialized = str(value)
    return _bounded_tool_text(serialized, _MAX_STRUCTURED_TOOL_INPUT_BYTES)


_QUESTION_TOOL_NAMES = {
    "askquestion",
    "askuserquestion",
    "requestuserinput",
}
_CLAUDE_LIVE_PROMPT_TOOL_NAMES = {
    "elicitation",
    "notificationprompt",
    "permissionrequest",
}
_CURSOR_PLAN_MODE_TOOL_NAMES = {"switchmode"}
_CURSOR_PENDING_INTERACTION_STATUSES = {
    "",
    "awaiting",
    "loading",
    "pending",
    "requested",
    "running",
}
_CURSOR_CANCELLED_INTERACTION_STATUSES = {
    "aborted",
    "cancelled",
    "canceled",
    "dismissed",
    "error",
    "failed",
    "rejected",
    "skipped",
    "timeout",
}
_CURSOR_ANSWERED_INTERACTION_STATUSES = {
    "answered",
    "completed",
    "done",
    "submitted",
    "success",
}
_MAX_INTERACTION_QUESTIONS = 8
_MAX_INTERACTION_OPTIONS = 12
_MAX_PERMISSION_TOOL_INPUT_BYTES = 64 * 1024
CURSOR_QUESTION_RESPONSE_WINDOW = 4


def _json_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = orjson.loads(value)
    except (orjson.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bounded_permission_tool_input(value: object) -> dict | None:
    """Return an exact JSON mapping only while it fits the origin contract.

    Claude permission provenance is bound to the literal structured tool
    input.  Retaining that input lets later branch-lineage checks recompute the
    digest without trusting a collector-supplied classification.  The same
    64-KiB ceiling used by the collector prevents document metadata from
    growing without bound; larger/non-standard inputs remain hook-only and
    therefore fail open.
    """
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
    return orjson.loads(encoded)


_NESTED_CODEX_UPDATE_PLAN_RE = re.compile(
    r"\b(?:tools\.)?update_plan\s*\(",
    re.IGNORECASE,
)


def _balanced_js_value(source: str, start: int) -> str:
    """Return one balanced JS object/array without evaluating the source."""
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] not in "{[":
        return ""

    pairs = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    quote = ""
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}])":
            if not stack or char != stack.pop():
                return ""
            if not stack:
                return source[start:index + 1]
    return ""


def _simple_js_object_mapping(value: str) -> dict:
    """Decode Codex's JSON-compatible JS object literals without executing JS.

    Codex emits nested orchestration calls such as
    ``tools.update_plan({explanation:"...",plan:[...]})``. Their values are
    JSON strings/arrays but their property names are unquoted. Quote only
    identifier keys observed outside strings, then use the normal JSON parser.
    Unsupported single-quoted/template strings fail closed.
    """
    out: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        char = value[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char in {"'", "`"}:
            return {}
        if char == ",":
            lookahead = index + 1
            while lookahead < len(value) and value[lookahead].isspace():
                lookahead += 1
            if lookahead < len(value) and value[lookahead] in "}]":
                index += 1
                continue
        if char in "{,":
            out.append(char)
            index += 1
            whitespace_start = index
            while index < len(value) and value[index].isspace():
                index += 1
            out.append(value[whitespace_start:index])
            key_start = index
            if index < len(value) and (
                value[index].isalpha() or value[index] in "_$"
            ):
                index += 1
                while index < len(value) and (
                    value[index].isalnum() or value[index] in "_$"
                ):
                    index += 1
                key_end = index
                while index < len(value) and value[index].isspace():
                    index += 1
                if index < len(value) and value[index] == ":":
                    out.extend(('"', value[key_start:key_end], '"'))
                    out.append(value[key_end:index])
                    out.append(":")
                    index += 1
                    continue
                out.append(value[key_start:index])
            continue
        out.append(char)
        index += 1

    try:
        parsed = orjson.loads("".join(out))
    except (orjson.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_codex_update_plans(tool_name: str, tool_input: str) -> list[dict]:
    """Extract persisted ``tools.update_plan`` calls nested in Codex exec JS."""
    compact_name = re.sub(r"[^a-z0-9]", "", tool_name.casefold())
    if not compact_name.endswith("exec") or not tool_input:
        return []
    plans: list[dict] = []
    for match in _NESTED_CODEX_UPDATE_PLAN_RE.finditer(tool_input):
        literal = _balanced_js_value(tool_input, match.end())
        payload = _simple_js_object_mapping(literal) if literal else {}
        if isinstance(payload.get("plan"), list):
            plans.append(payload)
    return plans


_MAX_TASKS = 200
_MAX_TASK_TEXT_CHARS = 4000
_TASK_CREATED_RE = re.compile(
    r"Task\s+#(?P<id>[^\s:]+)\s+created\s+successfully(?:\s*:\s*(?P<title>.*))?",
    re.IGNORECASE,
)
_TASK_LIST_LINE_RE = re.compile(
    r"^#(?P<id>\S+)\s+\[(?P<status>pending|in[_ -]?progress|active|"
    r"working|complete|completed|done|succeeded|success|blocked|"
    r"cancelled|canceled)\]\s+(?P<content>.+)$",
    re.MULTILINE | re.IGNORECASE,
)


def _is_standalone_task_list_result(content: str) -> bool:
    """Recognize an unadorned Claude TaskList result across delta boundaries."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return bool(lines) and all(_TASK_LIST_LINE_RE.fullmatch(line) for line in lines)


def _normalized_task_status(value: object) -> str:
    status = _coerce_text(value).strip().lower().replace("-", "_").replace(" ", "_")
    if status in {"complete", "completed", "done", "succeeded", "success"}:
        return "completed"
    if status in {"inprogress", "in_progress", "active", "working"}:
        return "in_progress"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status == "blocked":
        return "blocked"
    return "pending"


def _normalized_task(
    value: object,
    *,
    index: int,
    fallback_id: str = "",
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    content = _bounded_interaction_text(
        value.get("content")
        or value.get("step")
        or value.get("subject")
        or value.get("title")
        or value.get("description"),
        _MAX_TASK_TEXT_CHARS,
    )
    if not content:
        return None
    task_id = _bounded_interaction_text(
        value.get("id")
        or value.get("taskId")
        or value.get("task_id")
        or fallback_id,
        256,
    )
    if not task_id:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        task_id = f"task-{index + 1}-{digest}"
    active_form = _bounded_interaction_text(
        value.get("activeForm") or value.get("active_form"),
        _MAX_TASK_TEXT_CHARS,
    )
    return {
        "id": task_id,
        "content": content,
        "status": _normalized_task_status(value.get("status")),
        "active_form": active_form,
    }


class TaskStateTracker:
    """Build immutable task-list snapshots from each tool's native events."""

    def __init__(
        self,
        source: str,
        initial_state: dict[str, object] | None = None,
        *,
        incremental: bool = False,
    ) -> None:
        self.source = source
        self.tasks: dict[str, dict[str, str]] = {}
        self.order: list[str] = []
        self.revision = 0
        self.pending_creates: list[str] = []
        self.pending_task_lists: set[str] = set()
        self.source_ids: list[str] = []
        self.partial = incremental
        self.carry_explicit_current = False
        if isinstance(initial_state, dict):
            raw_tasks = initial_state.get("tasks")
            if isinstance(raw_tasks, list):
                normalized = [
                    task
                    for index, item in enumerate(raw_tasks[:_MAX_TASKS])
                    if (
                        task := _normalized_task(
                            item,
                            index=index,
                            fallback_id=_coerce_text(
                                item.get("id") if isinstance(item, dict) else ""
                            ),
                        )
                    )
                    is not None
                ]
                self.tasks = {task["id"]: task for task in normalized}
                self.order = [task["id"] for task in normalized]
                self.partial = str(initial_state.get("quality") or "") == "partial"
                self.carry_explicit_current = bool(
                    incremental
                    and initial_state.get("is_current")
                    and not self.partial
                )
            raw_revision = initial_state.get("revision")
            try:
                self.revision = (
                    max(0, int(raw_revision))
                    if isinstance(raw_revision, (str, int, float))
                    else 0
                )
            except (TypeError, ValueError):
                self.revision = 0
            raw_source_ids = initial_state.get("source_ids")
            if isinstance(raw_source_ids, list):
                self.source_ids = [
                    _bounded_interaction_text(value, 256)
                    for value in raw_source_ids[-64:]
                    if _bounded_interaction_text(value, 256)
                ]

    def apply(self, message: NormalizedMessage) -> None:
        changed = False
        is_current = False
        observed_source_ids = [
            _bounded_interaction_text(value, 256)
            for value in (message.source_id, message.tool_call_id)
            if _bounded_interaction_text(value, 256)
        ]

        if message.role == "tool" and message.tool_name:
            name = self._normalized_name(message.tool_name)
            payload = _json_mapping(message.tool_input)
            # Cursor's state export can retain a TodoWrite result as one
            # combined row: the input contains only ``merge`` while the
            # authoritative replacement is returned as ``finalTodos`` in the
            # row content. Treat that narrow native shape exactly like the
            # equivalent TodoWrite input so current ingestion and historical
            # repair share one state transition.
            if (
                name == "todowrite"
                and not isinstance(payload.get("tasks"), list)
                and not isinstance(payload.get("todos"), list)
            ):
                result_payload = _json_mapping(message.content)
                final_todos = result_payload.get(
                    "finalTodos",
                    result_payload.get("final_todos"),
                )
                if isinstance(final_todos, list):
                    payload = {**payload, "todos": final_todos}
            changed = self._apply_event(
                name,
                payload,
                source_id=message.tool_call_id,
            )
            is_current = bool(payload.get("is_current"))
            if name in {"toolresult", "taskresult"}:
                result_call_id = message.tool_call_id
                allow_task_list = bool(
                    result_call_id
                    and result_call_id in self.pending_task_lists
                )
                if result_call_id:
                    self.pending_task_lists.discard(result_call_id)
                changed = self._apply_result(
                    message.content,
                    allow_task_list=allow_task_list,
                ) or changed
            # Current Codex records orchestration tools as JavaScript passed
            # to one outer ``exec`` call. The desktop client reconstructs its
            # task widget from nested ``tools.update_plan`` calls, so retain
            # the same persisted semantics instead of treating the plan as
            # opaque source text. The extractor is data-only and never evals.
            for nested_payload in _nested_codex_update_plans(
                message.tool_name,
                message.tool_input,
            ):
                # The Codex desktop task widget treats every persisted plan
                # replacement as the current snapshot until a later one is
                # observed. Multiple historical rows may therefore carry the
                # marker; the API resolves the newest line deterministically.
                nested_payload = {**nested_payload, "is_current": True}
                changed = self._apply_event(
                    "updateplan",
                    nested_payload,
                    source_id=message.tool_call_id,
                ) or changed
                is_current = bool(nested_payload.get("is_current")) or is_current

        # Cursor and some Claude releases retain tool uses inside the
        # assistant content block. They are semantically identical to a
        # standalone tool row and must participate in the same state machine.
        for call in message.tool_calls:
            if not isinstance(call, dict):
                continue
            name = self._normalized_name(call.get("name"))
            payload = _json_mapping(call.get("input"))
            call_id = _coerce_text(call.get("id"))
            if bounded_call_id := _bounded_interaction_text(call_id, 256):
                observed_source_ids.append(bounded_call_id)
            changed = self._apply_event(
                name,
                payload,
                source_id=call_id,
            ) or changed
            is_current = bool(payload.get("is_current")) or is_current

        if not changed:
            return
        # An explicitly-current projection is the authority for a live delta.
        # Carry that marker onto each seeded mutation so the newer transition
        # outranks the older snapshot. Full historical reparses deliberately
        # do not carry it: an embedded current snapshot may precede later
        # historical transport rows in the source export.
        is_current = is_current or self.carry_explicit_current
        for source_id in observed_source_ids:
            if source_id and source_id not in self.source_ids:
                self.source_ids.append(source_id)
        self.source_ids = self.source_ids[-64:]
        self.revision += 1
        tasks = [self.tasks[task_id] for task_id in self.order if task_id in self.tasks]
        completed_count = sum(task["status"] == "completed" for task in tasks)
        active = next(
            (task for task in tasks if task["status"] == "in_progress"),
            next((task for task in tasks if task["status"] == "pending"), None),
        )
        message.task_state = {
            "version": 1,
            "source": self.source,
            "revision": self.revision,
            "is_current": is_current,
            "quality": (
                "explicit_current"
                if is_current and not self.partial
                else "partial"
                if self.partial
                else "authoritative"
            ),
            "source_ids": list(self.source_ids),
            "completed_count": completed_count,
            "total_count": len(tasks),
            "active_task_id": active["id"] if active else "",
            "tasks": tasks,
        }

    @staticmethod
    def _normalized_name(value: object) -> str:
        compact = re.sub(r"[^a-z0-9]", "", _coerce_text(value).casefold())
        for canonical in (
            "updateplan",
            "todowrite",
            "tasklist",
            "taskcreate",
            "taskupdate",
        ):
            if compact.endswith(canonical):
                return canonical
        progress_index = compact.rfind("taskprogress")
        if progress_index >= 0:
            return compact[progress_index:]
        return compact

    def _apply_event(
        self,
        name: str,
        payload: dict,
        *,
        source_id: str = "",
    ) -> bool:
        if not name:
            return False

        # TaskList returns its state in a later generic tool-result row. Keep
        # the call identity so only that result may replace the current task
        # snapshot. Arbitrary command/web output can contain Markdown such as
        # ``#### [Button: Copy]`` and must never be interpreted as a task list.
        if name == "tasklist" and source_id:
            self.pending_task_lists.add(source_id)

        raw_tasks: object = None
        replace = False
        if name == "updateplan":
            raw_tasks = payload.get("plan")
            replace = True
        elif name == "todowrite" or name.startswith("taskprogress"):
            raw_tasks = payload.get("tasks", payload.get("todos"))
            replace = not bool(payload.get("merge"))
        elif name == "tasklist" and isinstance(payload.get("tasks"), list):
            raw_tasks = payload.get("tasks")
            replace = True

        if isinstance(raw_tasks, list):
            normalized = [
                task
                for index, item in enumerate(raw_tasks[:_MAX_TASKS])
                if (task := _normalized_task(item, index=index)) is not None
            ]
            if replace:
                self.tasks = {task["id"]: task for task in normalized}
                self.order = [task["id"] for task in normalized]
                # Replacement tools carry a complete list, including an
                # intentionally empty one, so they repair an unseeded delta.
                self.partial = False
            else:
                for task in normalized:
                    if task["id"] not in self.tasks:
                        self.order.append(task["id"])
                    self.tasks[task["id"]] = task
            return bool(normalized) or replace
        elif name == "taskcreate":
            explicit_id = _bounded_interaction_text(
                payload.get("id") or payload.get("taskId") or payload.get("task_id"),
                256,
            )
            fallback_id = explicit_id or source_id or (
                f"pending-{len(self.pending_creates) + 1}-{len(self.order) + 1}"
            )
            task = _normalized_task(
                payload,
                index=len(self.order),
                fallback_id=fallback_id,
            )
            if task is not None:
                if task["id"] not in self.tasks:
                    self.order.append(task["id"])
                self.tasks[task["id"]] = task
                if not explicit_id:
                    self.pending_creates.append(task["id"])
                return True
        elif name == "taskupdate":
            task_id = _bounded_interaction_text(
                payload.get("taskId") or payload.get("task_id") or payload.get("id"),
                256,
            )
            existing = self.tasks.get(task_id)
            if task_id:
                if existing is None:
                    self.partial = True
                    existing = {
                        "id": task_id,
                        "content": f"Task #{task_id}",
                        "status": "pending",
                        "active_form": "",
                    }
                    self.tasks[task_id] = existing
                    self.order.append(task_id)
                updated = dict(existing)
                content = _bounded_interaction_text(
                    payload.get("content")
                    or payload.get("subject")
                    or payload.get("description"),
                    _MAX_TASK_TEXT_CHARS,
                )
                if content:
                    updated["content"] = content
                if "status" in payload:
                    updated["status"] = _normalized_task_status(payload.get("status"))
                active_form = _bounded_interaction_text(
                    payload.get("activeForm") or payload.get("active_form"),
                    _MAX_TASK_TEXT_CHARS,
                )
                if active_form:
                    updated["active_form"] = active_form
                self.tasks[task_id] = updated
                return True
        return False

    def _apply_result(self, content: str, *, allow_task_list: bool = False) -> bool:
        changed = False
        created = _TASK_CREATED_RE.search(content)
        if created and self.pending_creates:
            provisional_id = self.pending_creates.pop(0)
            actual_id = _bounded_interaction_text(created.group("id"), 256)
            task = self.tasks.pop(provisional_id, None)
            if task is not None and actual_id:
                task = {**task, "id": actual_id}
                title = _bounded_interaction_text(
                    created.group("title"),
                    _MAX_TASK_TEXT_CHARS,
                )
                if title:
                    task["content"] = title
                self.tasks[actual_id] = task
                self.order = [
                    actual_id if task_id == provisional_id else task_id
                    for task_id in self.order
                ]
                changed = True

        listed = []
        if allow_task_list or (
            self.source == "claude_code"
            and _is_standalone_task_list_result(content)
        ):
            listed = [
                task
                for index, match in enumerate(_TASK_LIST_LINE_RE.finditer(content))
                if (task := _normalized_task(
                    {
                        "id": match.group("id"),
                        "status": match.group("status"),
                        "content": match.group("content"),
                    },
                    index=index,
                )) is not None
            ][:_MAX_TASKS]
        if listed:
            existing_active_forms = {
                task_id: task.get("active_form", "")
                for task_id, task in self.tasks.items()
            }
            for task in listed:
                if active_form := existing_active_forms.get(task["id"]):
                    task["active_form"] = active_form
            self.tasks = {task["id"]: task for task in listed}
            self.order = [task["id"] for task in listed]
            self.pending_creates.clear()
            changed = True
        return changed


def _bounded_interaction_text(value: object, limit: int) -> str:
    return _bounded_tool_text(_coerce_text(value).strip(), limit)


_UTF8_CP1252_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "ðŸ", "ï¿")


def _repair_detectable_utf8_cp1252_mojibake(value: str) -> str:
    """Undo one UTF-8-as-CP1252 decode only when strong markers improve."""
    marker_count = sum(value.count(marker) for marker in _UTF8_CP1252_MOJIBAKE_MARKERS)
    if marker_count == 0:
        return value
    try:
        repaired = value.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    repaired_marker_count = sum(
        repaired.count(marker) for marker in _UTF8_CP1252_MOJIBAKE_MARKERS
    )
    return repaired if repaired_marker_count < marker_count else value


def _bounded_question_text(value: object, limit: int) -> str:
    text = _coerce_text(value).strip()
    return _bounded_tool_text(
        _repair_detectable_utf8_cp1252_mojibake(text),
        limit,
    )


def normalize_question_interaction(
    tool_name: str,
    raw_input: object,
    *,
    source: str,
    interaction_id: object = "",
) -> dict[str, object] | None:
    """Normalize interactive-question payloads emitted by supported tools."""
    normalized_tool_name = re.sub(
        r"[^a-z0-9]",
        "",
        tool_name.strip().casefold(),
    )
    if normalized_tool_name not in _QUESTION_TOOL_NAMES:
        return None
    payload = _json_mapping(raw_input)
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return None

    questions: list[dict[str, object]] = []
    for index, raw_question in enumerate(raw_questions[:_MAX_INTERACTION_QUESTIONS]):
        if not isinstance(raw_question, dict):
            continue
        prompt = _bounded_question_text(
            raw_question.get("prompt") or raw_question.get("question"),
            4096,
        )
        if not prompt:
            continue
        question_id = _bounded_question_text(
            raw_question.get("id")
            or raw_question.get("header")
            or f"question-{index + 1}",
            256,
        )
        header = _bounded_question_text(
            raw_question.get("header") or raw_question.get("label_short"),
            512,
        )
        multiple = bool(
            raw_question.get("multiSelect")
            or raw_question.get("allowMultiple")
            or raw_question.get("allow_multiple")
            or raw_question.get("type") == "multi_select"
        )
        options: list[dict[str, str]] = []
        raw_options = raw_question.get("options")
        if isinstance(raw_options, list):
            for option_index, raw_option in enumerate(
                raw_options[:_MAX_INTERACTION_OPTIONS]
            ):
                if isinstance(raw_option, str):
                    raw_option = {"label": raw_option}
                if not isinstance(raw_option, dict):
                    continue
                label = _bounded_question_text(raw_option.get("label"), 1024)
                if not label:
                    continue
                option_id = _bounded_question_text(
                    raw_option.get("id") or label or f"option-{option_index + 1}",
                    512,
                )
                option = {"id": option_id, "label": label}
                description = _bounded_question_text(
                    raw_option.get("description") or raw_option.get("preview"),
                    4096,
                )
                short_label = _bounded_question_text(
                    raw_option.get("label_short"),
                    512,
                )
                if description:
                    option["description"] = description
                if short_label:
                    option["short_label"] = short_label
                options.append(option)

        if options:
            question_type = "multi_select" if multiple else "single_select"
        else:
            question_type = "free_text"
        questions.append({
            "id": question_id,
            "header": header,
            "prompt": prompt,
            "type": question_type,
            "allow_custom": True,
            "options": options,
        })

    if not questions:
        return None
    return {
        "kind": "question",
        "id": _bounded_interaction_text(interaction_id, 512),
        "source": _bounded_interaction_text(source, 64),
        "tool_name": _bounded_interaction_text(tool_name, 256),
        "questions": questions,
    }


def _claude_permission_detail(
    requested_tool: str,
    tool_input: dict,
) -> str:
    normalized_tool = re.sub(r"[^a-z0-9]", "", requested_tool.casefold())
    preferred_keys = (
        ("description", "prompt")
        if normalized_tool in {"agent", "task"}
        else (
            "command",
            "description",
            "file_path",
            "path",
            "url",
            "query",
            "prompt",
        )
    )
    for key in preferred_keys:
        detail = _bounded_interaction_text(tool_input.get(key), 4096)
        if detail:
            return detail
    if not tool_input:
        return ""
    return _bounded_interaction_text(
        _serialize_tool_input(tool_input),
        4096,
    )


def _claude_permission_suggestion_option(
    suggestions: object,
    requested_tool: str,
) -> dict[str, str]:
    entries = (
        [entry for entry in suggestions if isinstance(entry, dict)]
        if isinstance(suggestions, list)
        else []
    )
    targets: list[str] = []
    destinations: set[str] = set()
    changes: list[str] = []
    for entry in entries[:_MAX_INTERACTION_OPTIONS]:
        destination = _bounded_interaction_text(
            entry.get("destination"),
            64,
        )
        if destination:
            destinations.add(destination)
        update_type = _bounded_interaction_text(entry.get("type"), 64)
        if update_type in {"addRules", "replaceRules"}:
            rules = entry.get("rules")
            if isinstance(rules, list):
                for raw_rule in rules[:_MAX_INTERACTION_OPTIONS]:
                    if not isinstance(raw_rule, dict):
                        continue
                    rule_tool = _bounded_interaction_text(
                        raw_rule.get("toolName"),
                        256,
                    )
                    rule_content = _bounded_interaction_text(
                        raw_rule.get("ruleContent"),
                        1024,
                    )
                    if rule_tool:
                        targets.append(
                            f"{rule_tool}({rule_content})"
                            if rule_content
                            else rule_tool
                        )
            behavior = _bounded_interaction_text(entry.get("behavior"), 64)
            if behavior:
                changes.append(f"{update_type}: {behavior}")
        elif update_type == "setMode":
            mode = _bounded_interaction_text(entry.get("mode"), 64)
            if mode:
                targets.append(f"{mode} mode")
                changes.append(f"setMode: {mode}")
        elif update_type in {"addDirectories", "removeDirectories"}:
            directories = entry.get("directories")
            if isinstance(directories, list):
                paths = [
                    _bounded_interaction_text(path, 1024)
                    for path in directories[:_MAX_INTERACTION_OPTIONS]
                    if _bounded_interaction_text(path, 1024)
                ]
                targets.extend(paths)
                if paths:
                    changes.append(f"{update_type}: {', '.join(paths)}")

    unique_targets = list(dict.fromkeys(target for target in targets if target))
    target_label = ", ".join(unique_targets) or requested_tool
    if destinations == {"session"} or not destinations:
        scope = "for this session"
    elif destinations <= {"localSettings", "projectSettings"}:
        scope = "in this project"
    elif destinations == {"userSettings"}:
        scope = "in all projects"
    else:
        scope = "without asking again"
    label = _bounded_interaction_text(
        f"Yes, and allow Claude to use {target_label} {scope}",
        1024,
    )
    description = (
        "Future matching requests will be allowed without asking again."
    )
    if changes:
        description = f"{description} {'; '.join(changes)}."
    return {
        "id": "allow-always",
        "label": label,
        "description": _bounded_interaction_text(description, 4096),
    }


def normalize_claude_live_prompt_interaction(
    tool_name: str,
    raw_input: object,
    *,
    source: str,
    interaction_id: object = "",
) -> dict[str, object] | None:
    """Normalize hook-only Claude prompts that are absent from JSONL."""
    normalized_tool_name = re.sub(
        r"[^a-z0-9]",
        "",
        tool_name.strip().casefold(),
    )
    if (
        source.strip().casefold() != "claude_code"
        or normalized_tool_name not in _CLAUDE_LIVE_PROMPT_TOOL_NAMES
    ):
        return None
    payload = _json_mapping(raw_input)

    if normalized_tool_name == "permissionrequest":
        requested_tool = _bounded_interaction_text(
            payload.get("requested_tool") or payload.get("tool_name"),
            256,
        ) or "tool"
        normalized_requested_tool = re.sub(
            r"[^a-z0-9]",
            "",
            requested_tool.casefold(),
        )
        if normalized_requested_tool == "askuserquestion":
            question = normalize_question_interaction(
                "AskUserQuestion",
                payload.get("tool_input"),
                source=source,
                interaction_id=interaction_id,
            )
            if question is not None:
                return question
        tool_input = _json_mapping(payload.get("tool_input"))
        detail = _claude_permission_detail(requested_tool, tool_input)
        allow_option = {"id": "allow", "label": "Yes"}
        if detail:
            allow_option["description"] = detail
        suggestion_option = _claude_permission_suggestion_option(
            payload.get("permission_suggestions"),
            requested_tool,
        )
        questions = [{
            "id": "permission-decision",
            "header": requested_tool,
            "prompt": f"Claude Code wants permission to use {requested_tool}.",
            "type": "single_select",
            "allow_custom": False,
            "options": [
                allow_option,
                suggestion_option,
                {"id": "deny", "label": "No"},
            ],
        }]
        interaction_type = "permission_request"
        requested_tool_name = requested_tool
    elif normalized_tool_name == "elicitation":
        server_name = _bounded_interaction_text(
            payload.get("mcp_server_name"),
            256,
        )
        message = _bounded_interaction_text(
            payload.get("message"),
            4096,
        ) or "An MCP server is requesting input."
        url = _bounded_interaction_text(payload.get("url"), 2048)
        if url:
            message = f"{message}\n{url}"
        schema = _json_mapping(payload.get("requested_schema"))
        properties = schema.get("properties")
        required = schema.get("required")
        required_names = {
            str(value)
            for value in required
            if value
        } if isinstance(required, list) else set()
        questions = []
        if isinstance(properties, dict):
            for index, (field_name, raw_field) in enumerate(
                list(properties.items())[:_MAX_INTERACTION_QUESTIONS]
            ):
                field = raw_field if isinstance(raw_field, dict) else {}
                header = _bounded_interaction_text(
                    field.get("title") or field_name,
                    512,
                )
                prompt = _bounded_interaction_text(
                    field.get("description")
                    or field.get("title")
                    or field_name,
                    4096,
                )
                if str(field_name) in required_names:
                    prompt = f"{prompt} (required)"
                raw_options = field.get("enum")
                if isinstance(raw_options, list):
                    options = [
                        {
                            "id": _bounded_interaction_text(option, 512),
                            "label": _bounded_interaction_text(option, 1024),
                        }
                        for option in raw_options[:_MAX_INTERACTION_OPTIONS]
                        if _bounded_interaction_text(option, 1024)
                    ]
                elif field.get("type") == "boolean":
                    options = [
                        {"id": "yes", "label": "Yes"},
                        {"id": "no", "label": "No"},
                    ]
                else:
                    options = []
                questions.append({
                    "id": _bounded_interaction_text(
                        field_name or f"field-{index + 1}",
                        256,
                    ),
                    "header": header,
                    "prompt": prompt,
                    "type": "single_select" if options else "free_text",
                    "allow_custom": not options,
                    "options": options,
                })
        if not questions:
            questions = [{
                "id": "elicitation-decision",
                "header": server_name or "MCP request",
                "prompt": message,
                "type": "single_select",
                "allow_custom": False,
                "options": [
                    {"id": "continue", "label": "Continue"},
                    {"id": "decline", "label": "Decline"},
                ],
            }]
        interaction_type = "elicitation"
        requested_tool_name = server_name
    else:
        title = _bounded_interaction_text(payload.get("title"), 512)
        message = _bounded_interaction_text(payload.get("message"), 4096)
        if not message:
            message = "A Claude Code agent is waiting for your input."
        questions = [{
            "id": "agent-input",
            "header": title or "Agent needs input",
            "prompt": message,
            "type": "single_select",
            "allow_custom": True,
            "options": [
                {"id": "respond", "label": "Respond"},
                {"id": "dismiss", "label": "Dismiss"},
            ],
        }]
        interaction_type = "agent_needs_input"
        requested_tool_name = ""

    interaction = {
        "kind": "question",
        "interaction_type": interaction_type,
        "id": _bounded_interaction_text(interaction_id, 512),
        "source": "claude_code",
        "tool_name": _bounded_interaction_text(tool_name, 256),
        "requested_tool": requested_tool_name,
        "questions": questions,
    }
    if interaction_type == "permission_request":
        retained_tool_input = _bounded_permission_tool_input(tool_input)
        if retained_tool_input is not None:
            interaction["tool_input"] = retained_tool_input
    return interaction


def normalize_cursor_plan_mode_interaction(
    tool_name: str,
    raw_input: object,
    *,
    source: str,
    interaction_id: object = "",
) -> dict[str, object] | None:
    """Normalize Cursor's approval-gated request to enter Plan mode."""
    normalized_tool_name = re.sub(
        r"[^a-z0-9]",
        "",
        tool_name.strip().casefold(),
    )
    if (
        source.strip().casefold() != "cursor"
        or normalized_tool_name not in _CURSOR_PLAN_MODE_TOOL_NAMES
    ):
        return None
    payload = _json_mapping(raw_input)
    target_mode = _bounded_interaction_text(
        payload.get("toModeId")
        or payload.get("to_mode_id")
        or payload.get("to_mode"),
        64,
    )
    if target_mode.casefold() != "plan":
        return None
    source_mode = _bounded_interaction_text(
        payload.get("fromModeId")
        or payload.get("from_mode_id")
        or payload.get("from_mode"),
        64,
    )
    explanation = _bounded_interaction_text(
        payload.get("explanation"),
        4096,
    )
    return {
        "kind": "question",
        "interaction_type": "mode_switch",
        "id": _bounded_interaction_text(interaction_id, 512),
        "source": "cursor",
        "tool_name": _bounded_interaction_text(tool_name, 256),
        "from_mode": source_mode,
        "to_mode": "plan",
        "questions": [{
            "id": "enter-plan-mode",
            "header": "Plan mode",
            "prompt": explanation or "Cursor requested permission to enter Plan mode.",
            "type": "single_select",
            "allow_custom": False,
            "options": [{
                "id": "plan",
                "label": "Enter Plan mode",
            }],
        }],
    }


def normalize_interaction(
    tool_name: str,
    raw_input: object,
    *,
    source: str,
    interaction_id: object = "",
) -> dict[str, object] | None:
    """Normalize a supported human-response interaction."""
    question = normalize_question_interaction(
        tool_name,
        raw_input,
        source=source,
        interaction_id=interaction_id,
    )
    if question is not None:
        return question
    claude_prompt = normalize_claude_live_prompt_interaction(
        tool_name,
        raw_input,
        source=source,
        interaction_id=interaction_id,
    )
    if claude_prompt is not None:
        return claude_prompt
    return normalize_cursor_plan_mode_interaction(
        tool_name,
        raw_input,
        source=source,
        interaction_id=interaction_id,
    )


def _normalized_interaction_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().casefold())


def is_claude_ask_user_permission_wrapper(interaction: object) -> bool:
    """True when a live/persisted card is a PermissionRequest for AskUserQuestion."""
    if not isinstance(interaction, dict):
        return False
    if _normalized_interaction_name(interaction.get("interaction_type")) != (
        "permissionrequest"
    ):
        return False
    requested = _normalized_interaction_name(interaction.get("requested_tool"))
    if requested == "askuserquestion":
        return True
    questions = interaction.get("questions")
    if not isinstance(questions, list) or not questions:
        return False
    header = questions[0].get("header") if isinstance(questions[0], dict) else ""
    return _normalized_interaction_name(header) == "askuserquestion"


def interaction_question_fingerprint(interaction: object) -> str:
    """Stable fingerprint used to dedupe duplicate pending question cards."""
    if not isinstance(interaction, dict):
        return ""
    questions = interaction.get("questions")
    if not isinstance(questions, list):
        return ""
    compact = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        option_labels = []
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    label = _bounded_interaction_text(option.get("label"), 1024)
                else:
                    label = _bounded_interaction_text(option, 1024)
                if label:
                    option_labels.append(label)
        compact.append({
            "header": _bounded_interaction_text(question.get("header"), 512),
            "prompt": _bounded_interaction_text(
                question.get("prompt") or question.get("question"),
                4096,
            ),
            "options": option_labels,
        })
    if not compact:
        return ""
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _recover_ask_user_question_from_permission_interaction(
    interaction: dict[str, object],
) -> dict[str, object] | None:
    """Rebuild an AskUserQuestion card from a malformed permission Yes dump."""
    questions = interaction.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0] if isinstance(questions[0], dict) else {}
    options = first.get("options") if isinstance(first.get("options"), list) else []
    source = _bounded_interaction_text(
        interaction.get("source") or "claude_code",
        64,
    ) or "claude_code"
    interaction_id = interaction.get("id")
    for option in options:
        if not isinstance(option, dict):
            continue
        payload = _json_mapping(option.get("description"))
        if not payload:
            continue
        recovered = normalize_question_interaction(
            "AskUserQuestion",
            payload,
            source=source,
            interaction_id=interaction_id,
        )
        if recovered is not None:
            return recovered
        if isinstance(payload.get("question") or payload.get("prompt"), str):
            recovered = normalize_question_interaction(
                "AskUserQuestion",
                {"questions": [payload]},
                source=source,
                interaction_id=interaction_id,
            )
            if recovered is not None:
                return recovered
    return None


def _repair_claude_question_interaction_text(
    interaction: dict[str, object],
) -> dict[str, object]:
    """Repair safely-detectable legacy mojibake in stored Claude prompt fields."""
    if _normalized_interaction_name(interaction.get("source")) != "claudecode":
        return interaction
    raw_questions = interaction.get("questions")
    if not isinstance(raw_questions, list):
        return interaction

    changed = False
    repaired_questions: list[object] = []
    for raw_question in raw_questions:
        if not isinstance(raw_question, dict):
            repaired_questions.append(raw_question)
            continue
        question = dict(raw_question)
        for field_name in ("id", "header", "prompt"):
            value = question.get(field_name)
            if not isinstance(value, str):
                continue
            repaired = _repair_detectable_utf8_cp1252_mojibake(value)
            if repaired != value:
                question[field_name] = repaired
                changed = True

        raw_options = question.get("options")
        if isinstance(raw_options, list):
            repaired_options: list[object] = []
            for raw_option in raw_options:
                if not isinstance(raw_option, dict):
                    repaired_options.append(raw_option)
                    continue
                option = dict(raw_option)
                for field_name in ("id", "label", "short_label", "description"):
                    value = option.get(field_name)
                    if not isinstance(value, str):
                        continue
                    repaired = _repair_detectable_utf8_cp1252_mojibake(value)
                    if repaired != value:
                        option[field_name] = repaired
                        changed = True
                repaired_options.append(option)
            question["options"] = repaired_options
        repaired_questions.append(question)

    if not changed:
        return interaction
    repaired_interaction = dict(interaction)
    repaired_interaction["questions"] = repaired_questions
    return repaired_interaction


def coerce_claude_live_interaction(
    interaction: object,
) -> dict[str, object] | None:
    """Return a display-safe live interaction, recovering AskUserQuestion wrappers."""
    if not isinstance(interaction, dict):
        return None
    candidate = interaction
    if is_claude_ask_user_permission_wrapper(interaction):
        recovered = _recover_ask_user_question_from_permission_interaction(
            interaction,
        )
        if recovered is None:
            return None
        candidate = recovered
    return _repair_claude_question_interaction_text(candidate)


def _answer_texts(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("answers", value.get("answer", value.get("value")))
    if isinstance(value, list):
        return [
            text
            for item in value
            if (text := _bounded_interaction_text(item, 4096))
        ]
    text = _bounded_interaction_text(value, 4096)
    return [text] if text else []


def _claude_answer_for_prompt(
    raw_text: str,
    prompt: str,
    next_prompt: str | None,
) -> str:
    marker = f'"{prompt}"="'
    start = raw_text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    if next_prompt:
        end = raw_text.find(f'", "{next_prompt}"="', start)
    else:
        end = raw_text.find('". You can now continue', start)
    if end < 0:
        end = len(raw_text)
    return _bounded_interaction_text(raw_text[start:end].rstrip('". '), 4096)


def extract_claude_question_notes(raw_text: str, prompt: str) -> str:
    """Extract Claude's legacy note-only wrapper without exposing boilerplate."""
    marker = f'"{prompt}"='
    start = raw_text.find(marker)
    if start < 0:
        return ""
    segment = raw_text[start + len(marker):]
    match = re.match(
        r"\s*\((?:no option selected|notes only)\)\s+notes:\s*(?P<notes>.*)",
        segment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return ""
    notes = re.split(
        r"\.\s+Read the answers carefully\b",
        match.group("notes"),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _bounded_interaction_text(notes.strip().rstrip("."), 4096)


def build_question_response(
    interaction: dict[str, object],
    raw_output: object,
) -> dict[str, object]:
    """Build a shared answer model from structured or human-readable output."""
    if isinstance(raw_output, str):
        raw_text = _bounded_interaction_text(raw_output, 16 * 1024)
        parsed = _tool_content_payload(raw_output)
    else:
        raw_text = _bounded_interaction_text(
            json.dumps(raw_output, ensure_ascii=False, default=str),
            16 * 1024,
        )
        parsed = raw_output if isinstance(raw_output, dict) else {}
    structured_answers = parsed.get("answers")
    has_structured_answer_payload = isinstance(structured_answers, (dict, list))
    notes_by_question: dict[str, str] = {}
    if isinstance(structured_answers, list):
        answers_by_question: dict[str, list[str]] = {}
        for item in structured_answers:
            if not isinstance(item, dict):
                continue
            question_id = _coerce_text(
                item.get("questionId") or item.get("question_id") or item.get("id")
            )
            if not question_id:
                continue
            values = _answer_texts(
                item.get("selectedOptionIds", item.get("selected_option_ids"))
            )
            freeform = _bounded_interaction_text(
                item.get("freeformText", item.get("freeform_text")),
                4096,
            )
            if freeform:
                values.append(freeform)
            answers_by_question[question_id] = values
            note = _bounded_interaction_text(
                item.get("notes", item.get("note")),
                4096,
            ).strip()
            if note:
                notes_by_question[question_id] = note
        structured_answers = answers_by_question
    elif not isinstance(structured_answers, dict):
        structured_answers = {}

    structured_annotations = parsed.get("annotations")
    if not isinstance(structured_annotations, dict):
        structured_annotations = {}

    raw_questions = interaction.get("questions")
    questions = raw_questions if isinstance(raw_questions, list) else []
    answers: list[dict[str, object]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        question_id = _coerce_text(question.get("id"))
        prompt = _coerce_text(question.get("prompt"))
        header = _coerce_text(question.get("header"))
        lookup_keys = [
            key for key in dict.fromkeys((question_id, prompt, header)) if key
        ]
        structured_answer = next(
            (
                structured_answers[key]
                for key in lookup_keys
                if key in structured_answers
            ),
            None,
        )
        answer_values = [
            value
            for value in _answer_texts(structured_answer)
            if value.strip().casefold() not in {"(notes only)", "notes only"}
        ]
        annotation = next(
            (
                structured_annotations[key]
                for key in lookup_keys
                if key in structured_annotations
            ),
            None,
        )
        if isinstance(annotation, dict):
            annotation_note = _bounded_interaction_text(
                annotation.get("notes", annotation.get("note")),
                4096,
            ).strip()
        else:
            annotation_note = ""
        legacy_notes = (
            extract_claude_question_notes(raw_text, prompt)
            if raw_text and not has_structured_answer_payload
            else ""
        )
        notes = annotation_note or next(
            (
                notes_by_question[key]
                for key in lookup_keys
                if key in notes_by_question
            ),
            "",
        ) or legacy_notes
        if not answer_values and raw_text and not has_structured_answer_payload:
            next_prompt = None
            if index + 1 < len(questions) and isinstance(questions[index + 1], dict):
                next_prompt = _coerce_text(questions[index + 1].get("prompt"))
            claude_answer = _claude_answer_for_prompt(raw_text, prompt, next_prompt)
            if claude_answer:
                answer_values = [claude_answer]
        if (
            not answer_values
            and len(questions) == 1
            and raw_text
            and not has_structured_answer_payload
            and not notes
        ):
            answer_values = [raw_text]

        combined = "\n".join(answer_values)
        selected: list[str] = []
        options = question.get("options")
        if isinstance(options, list):
            folded = combined.casefold()
            exact = folded.strip()
            for option in options:
                if not isinstance(option, dict):
                    continue
                option_id = _coerce_text(option.get("id"))
                label = _coerce_text(option.get("label"))
                candidates = [item.casefold() for item in (option_id, label) if item]
                if any(candidate == exact for candidate in candidates) or any(
                    len(candidate) > 1
                    and re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", folded)
                    for candidate in candidates
                ):
                    selected.append(option_id or label)
        if answer_values or selected or notes:
            answer = {
                "question_id": question_id,
                "text": combined,
                "selected_option_ids": selected,
            }
            if notes:
                answer["notes"] = notes
            answers.append(answer)

    lowered = raw_text.casefold()
    status = (
        "cancelled"
        if not answers
        and (has_structured_answer_payload or "cancel" in lowered)
        else "answered"
    )
    return {
        "kind": "question_response",
        "interaction_id": _bounded_interaction_text(interaction.get("id"), 512),
        "status": status,
        "answers": answers,
        "raw_text": raw_text,
    }


def build_cursor_question_response(
    interaction: dict[str, object],
    content: object,
    tool_status: object = "",
) -> dict[str, object] | None:
    """Recognize Cursor state rows whose submitted answer follows a status line."""
    text = _coerce_text(content)
    payload = _tool_content_payload(text)
    status = (
        _coerce_text(tool_status).strip().casefold()
        or _status_prefix_from_tool_content(text).casefold()
    )
    if (
        "answers" not in payload
        and "cancel" not in text.casefold()
        and status not in {"answered", "completed", "submitted", "success"}
    ):
        return None
    return build_question_response(interaction, payload or text)


def _is_cursor_plan_mode_interaction(interaction: dict[str, object]) -> bool:
    tool_name = _coerce_text(interaction.get("tool_name"))
    normalized_tool_name = re.sub(r"[^a-z0-9]", "", tool_name.casefold())
    return (
        interaction.get("source") == "cursor"
        and interaction.get("interaction_type") == "mode_switch"
        and normalized_tool_name in _CURSOR_PLAN_MODE_TOOL_NAMES
        and _coerce_text(interaction.get("to_mode")).casefold() == "plan"
    )


def build_cursor_interaction_response(
    interaction: dict[str, object],
    content: object,
    tool_status: object = "",
    tool_status_reason: object = "",
) -> dict[str, object] | None:
    """Resolve Cursor questions and approval-gated mode-switch requests."""
    if not _is_cursor_plan_mode_interaction(interaction):
        return build_cursor_question_response(interaction, content, tool_status)

    text = _coerce_text(content).strip()
    status = (
        _coerce_text(tool_status).strip().casefold()
        or _status_prefix_from_tool_content(text).casefold()
    )
    if status in _CURSOR_PENDING_INTERACTION_STATUSES:
        return None
    reason = _bounded_interaction_text(tool_status_reason, 512)
    rejected = bool(
        re.search(r'"rejected"\s*:\s*true', text, re.IGNORECASE)
        or "reject" in reason.casefold()
    )
    if status in _CURSOR_CANCELLED_INTERACTION_STATUSES or rejected:
        raw_text = "Skipped"
        if reason:
            raw_text += f" ({reason})"
        return {
            "kind": "question_response",
            "interaction_id": _bounded_interaction_text(interaction.get("id"), 512),
            "status": "cancelled",
            "answers": [],
            "raw_text": raw_text,
        }
    if status not in _CURSOR_ANSWERED_INTERACTION_STATUSES:
        return None

    questions = interaction.get("questions")
    question = (
        questions[0]
        if isinstance(questions, list)
        and questions
        and isinstance(questions[0], dict)
        else {}
    )
    question_id = _bounded_interaction_text(question.get("id"), 256)
    return {
        "kind": "question_response",
        "interaction_id": _bounded_interaction_text(interaction.get("id"), 512),
        "status": "answered",
        "answers": [{
            "question_id": question_id,
            "text": "Entered Plan mode",
            "selected_option_ids": ["plan"],
        }],
        "raw_text": "Entered Plan mode",
    }


def normalize_tool_calls(value: object) -> list[dict[str, object]]:
    """Return the safe, bounded public representation of assistant tools.

    This accepts both raw Cursor ``tool_use`` blocks and the ``name``/``input``
    dictionaries persisted in ConversationMessage metadata. Keeping one
    normalizer for both paths prevents raw-content and DB-fallback responses
    from drifting apart.
    """
    if not isinstance(value, list):
        return []

    calls: list[dict[str, object]] = []
    total_bytes = 0
    for raw_call in value:
        if len(calls) >= _MAX_STRUCTURED_TOOL_CALLS:
            break
        if not isinstance(raw_call, dict):
            continue

        raw_name = raw_call.get("name")
        name = raw_name if isinstance(raw_name, str) else "Tool"
        name = _bounded_tool_text(
            name.strip() or "Tool",
            _MAX_STRUCTURED_TOOL_NAME_BYTES,
        )
        if "input" in raw_call:
            raw_input = raw_call.get("input")
        else:
            raw_input = raw_call.get("arguments", {})
        serialized_input = _serialize_tool_input(raw_input)

        name_bytes = len(name.encode("utf-8"))
        remaining = _MAX_STRUCTURED_TOOL_CALL_BYTES - total_bytes - name_bytes
        if remaining <= 0:
            break
        serialized_input = _bounded_tool_text(serialized_input, remaining)
        total_bytes += name_bytes + len(serialized_input.encode("utf-8"))
        normalized_call: dict[str, object] = {
            "name": name,
            "input": serialized_input,
        }
        interaction = raw_call.get("interaction")
        if isinstance(interaction, dict):
            interaction = normalize_interaction(
                _coerce_text(interaction.get("tool_name") or name),
                interaction,
                source=_coerce_text(interaction.get("source")),
                interaction_id=interaction.get("id"),
            )
        else:
            interaction = normalize_interaction(
                name,
                raw_input,
                source="cursor",
                interaction_id=raw_call.get("id") or raw_call.get("call_id"),
            )
        if interaction:
            normalized_call["interaction"] = interaction
        calls.append(normalized_call)
    return calls


def _extract_cursor_assistant_content(
    content: object,
) -> tuple[str, list[dict[str, object]]]:
    """Separate Cursor prose from structured assistant tool invocations."""
    if not isinstance(content, list):
        prose = _CURSOR_REDACTED_TRANSPORT_LINE_RE.sub(
            r"\1",
            _extract_content(content),
        ).strip("\r\n")
        return prose, []

    prose_parts: list[str] = []
    raw_calls: list[dict] = []
    for item in content:
        if isinstance(item, str):
            item = _CURSOR_REDACTED_TRANSPORT_LINE_RE.sub(
                r"\1",
                item,
            ).strip("\r\n")
            if item:
                prose_parts.append(item)
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "text":
            text = _coerce_text(item.get("text"))
            # Cursor can append the transport placeholder to real prose in the
            # same text block, so remove only exact standalone lines.
            text = _CURSOR_REDACTED_TRANSPORT_LINE_RE.sub(
                r"\1",
                text,
            ).strip("\r\n")
            if text:
                prose_parts.append(text)
        elif item_type in ("tool_use", "toolCall"):
            if len(raw_calls) < _MAX_STRUCTURED_TOOL_CALLS:
                raw_calls.append(item)

    prose = _CURSOR_REDACTED_TRANSPORT_LINE_RE.sub(
        r"\1",
        _strip_system_tags("\n".join(prose_parts)),
    ).strip("\r\n")
    return prose, normalize_tool_calls(raw_calls)


def _cursor_source_id(obj: dict, message: object) -> str:
    """Return Cursor's stable bubble identity when an export provides one."""
    message_mapping = _as_mapping(message)
    return str(
        obj.get("id")
        or obj.get("uuid")
        or obj.get("bubbleId")
        or message_mapping.get("id")
        or message_mapping.get("uuid")
        or message_mapping.get("bubbleId")
        or ""
    )


def _cursor_part_source_id(source_id: str, part_type: str, index: int) -> str:
    """Keep child identities unique when one Cursor bubble expands to rows."""
    if not source_id:
        return ""
    return f"{source_id}:{part_type}:{index}"


def _claude_part_source_id(source_id: str, part_type: str, index: int) -> str:
    """Keep semantic children unique when one Claude record expands to rows."""
    if not source_id:
        return ""
    return f"{source_id}:{part_type}:{index}"


def _parse_claude_record_messages(obj: object) -> list[NormalizedMessage]:
    """Expand one mixed Claude content array without flattening tool blocks.

    Modern Claude transcripts can store prose, thinking, and several tool
    calls in one source record. A one-record/one-row parser either loses the
    prose after selecting the first tool call or renders tool JSON inline as
    assistant text. Split only records containing typed tool blocks; ordinary
    records keep the long-standing compatibility path.
    """
    if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant"):
        parsed = parse_conversation_object(obj, "claude_code")
        return [parsed] if parsed is not None else []

    message = _as_mapping(obj.get("message"))
    content = message.get("content")
    if not isinstance(content, list) or not any(
        isinstance(item, dict)
        and item.get("type") in ("tool_use", "toolCall", "tool_result", "toolResult")
        for item in content
    ):
        parsed = parse_conversation_object(obj, "claude_code")
        return [parsed] if parsed is not None else []

    timestamp = _coerce_text(obj.get("timestamp"))
    source_id = _coerce_text(obj.get("uuid") or obj.get("promptId"))
    messages: list[NormalizedMessage] = []
    semantic_items: list[object] = []
    semantic_start = 0

    def flush_semantic_items() -> None:
        nonlocal semantic_items
        if not semantic_items:
            return
        semantic_obj = dict(obj)
        semantic_message = dict(message)
        semantic_message["content"] = semantic_items
        semantic_obj["message"] = semantic_message
        parsed = parse_conversation_object(semantic_obj, "claude_code")
        if parsed is not None:
            parsed.source_id = _claude_part_source_id(
                source_id,
                "message",
                semantic_start,
            )
            messages.append(parsed)
        semantic_items = []

    for index, item in enumerate(content):
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type not in ("tool_use", "toolCall", "tool_result", "toolResult"):
            if not semantic_items:
                semantic_start = index
            semantic_items.append(item)
            continue

        flush_semantic_items()
        if item_type in ("tool_use", "toolCall"):
            tool_use = _extract_tool_use([item])
            if tool_use is None:
                continue
            tool_name, tool_input, tool_call_id, interaction = tool_use
            agent_event = normalize_claude_agent_launch_event(
                tool_name,
                tool_input,
                tool_call_id,
            )
            if agent_event is not None and timestamp:
                agent_event["started_at"] = timestamp
            messages.append(NormalizedMessage(
                role="tool",
                content=(
                    f"{agent_event['label']} started"
                    if agent_event is not None
                    else f"[{tool_name}]"
                ),
                tool_name=(
                    "Agent activity"
                    if agent_event is not None
                    else tool_name
                ),
                tool_input=tool_input,
                timestamp=timestamp,
                raw_type="agent_event" if agent_event is not None else "tool_use",
                source_id=_claude_part_source_id(source_id, "tool_use", index),
                interaction=interaction,
                tool_call_id=tool_call_id,
                agent_event=agent_event,
            ))
            continue

        result = _extract_tool_result_details([item])
        if result is None:
            continue
        result_content, tool_call_id = result
        agent_event = normalize_claude_agent_result_event(
            obj,
            [item],
            tool_call_id,
        )
        if agent_event is not None and timestamp:
            event_time_key = (
                "completed_at"
                if agent_event.get("kind") in {
                    "completed",
                    "interrupted",
                    "failed",
                }
                else "started_at"
            )
            agent_event[event_time_key] = timestamp
        messages.append(NormalizedMessage(
            role="tool",
            content=(
                f"{agent_event['label']} {agent_event['kind']}"
                if agent_event is not None
                else result_content or "(tool returned no textual output)"
            ),
            tool_name=(
                "Agent activity"
                if agent_event is not None
                else "Tool result"
            ),
            timestamp=timestamp,
            raw_type="agent_event" if agent_event is not None else "tool_result",
            source_id=_claude_part_source_id(source_id, "tool_result", index),
            tool_call_id=tool_call_id,
            agent_event=agent_event,
        ))

    flush_semantic_items()
    return messages


def _iter_claude_conversation_messages(
    source_objects: Iterable[object],
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity: AssistantIdentityState | None = None,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
) -> Iterator[NormalizedMessage]:
    """Yield Claude semantic rows with queue and question correlation."""
    identity = assistant_identity or AssistantIdentityState()
    task_tracker = TaskStateTracker(
        "claude_code",
        initial_task_state,
        incremental=incremental,
    )
    seen_source_ids: set[str] = set()
    pending_queue: dict[str, list[NormalizedMessage]] = defaultdict(list)
    agent_launches: dict[str, dict[str, object]] = {}
    pending_lifecycle_message: NormalizedMessage | None = None
    pending_questions = {
        _coerce_text(interaction.get("id")): interaction
        for interaction in (initial_question_interactions or [])
        if isinstance(interaction, dict) and interaction.get("id")
    }

    def should_emit(message: NormalizedMessage) -> bool:
        if message.source_id:
            source_key = f"claude_code:{message.source_id}"
            if source_key in seen_source_ids:
                return False
            seen_source_ids.add(source_key)
        task_tracker.apply(message)
        return True

    def coalesce_lifecycle_message(
        message: NormalizedMessage,
    ) -> list[NormalizedMessage]:
        """Collapse only adjacent events with one exact source identity."""
        nonlocal pending_lifecycle_message
        identity_key = lifecycle_event_identity(message.agent_event)
        if pending_lifecycle_message is None:
            if identity_key is not None:
                pending_lifecycle_message = message
                return []
            return [message]

        pending_key = lifecycle_event_identity(
            pending_lifecycle_message.agent_event
        )
        if identity_key is not None and identity_key == pending_key:
            assert pending_lifecycle_message.agent_event is not None
            assert message.agent_event is not None
            merged_event = merge_duplicate_lifecycle_events(
                pending_lifecycle_message.agent_event,
                message.agent_event,
            )
            pending_lifecycle_message.agent_event = merged_event
            pending_lifecycle_message.content = (
                f"{merged_event.get('label') or 'Subagent'} "
                f"{merged_event.get('kind') or 'updated'}"
            )
            return []

        emitted = [pending_lifecycle_message]
        pending_lifecycle_message = None
        if identity_key is not None:
            pending_lifecycle_message = message
        else:
            emitted.append(message)
        return emitted

    for record_index, source_object in enumerate(source_objects):
        _update_assistant_identity(identity, source_object, "claude_code")
        for message in _parse_claude_record_messages(source_object):
            _attach_assistant_identity(message, identity)
            event = message.agent_event
            event_tool_use_id = _coerce_text(
                (event or {}).get("agent_tool_use_id")
            ).strip()
            if (
                event is not None
                and event.get("source") == "claude_agent"
                and event_tool_use_id
            ):
                launch = agent_launches.get(event_tool_use_id)
                if launch is not None:
                    result_label = _coerce_text(event.get("label")).strip()
                    event = {
                        **launch,
                        **event,
                        "label": (
                            _coerce_text(launch.get("label")).strip()
                            if not result_label or result_label == "Subagent"
                            else result_label
                        ),
                    }
                    message.agent_event = event
                    message.content = f"{event['label']} {event['kind']}"
                if event.get("kind") == "started":
                    agent_launches[event_tool_use_id] = dict(event)
            elif message.raw_type == "tool_result" and message.tool_call_id:
                launch = agent_launches.get(message.tool_call_id)
                if launch is not None:
                    source_message = _as_mapping(
                        _as_mapping(source_object).get("message")
                    )
                    event = normalize_claude_agent_result_event(
                        source_object,
                        source_message.get("content"),
                        message.tool_call_id,
                        launch,
                    )
                    if event is not None:
                        if message.timestamp:
                            event_time_key = (
                                "completed_at"
                                if event.get("kind") in {
                                    "completed",
                                    "interrupted",
                                    "failed",
                                }
                                else "started_at"
                            )
                            event[event_time_key] = message.timestamp
                        message.agent_event = event
                        message.raw_type = "agent_event"
                        message.tool_name = "Agent activity"
                        message.content = f"{event['label']} {event['kind']}"

            if (
                (message.role == "user" and message.raw_type == "user")
                or (
                    message.role == "system"
                    and message.raw_type == "scheduled_automation"
                )
            ):
                if pop_matching_claude_queue_user(
                    pending_queue,
                    message.content,
                    message.timestamp,
                ) is not None:
                    continue
            if message.raw_type in {
                "queued_user_message",
                "queued_scheduled_automation",
            }:
                pending_queue[message.content.strip()].append(message)

            if message.tool_call_id and message.tool_call_id in pending_questions:
                interaction = pending_questions.pop(message.tool_call_id)
                source_mapping = _as_mapping(source_object)
                message.interaction_response = build_question_response(
                    interaction,
                    source_mapping.get("toolUseResult")
                    or source_mapping.get("tool_use_result")
                    or message.content,
                )
                message.tool_name = "Question response"

            if message.interaction is not None:
                interaction_id = _coerce_text(message.interaction.get("id"))
                if not interaction_id:
                    interaction_id = f"claude_code:{record_index}:question"
                    message.interaction["id"] = interaction_id
                pending_questions[interaction_id] = message.interaction

            for call in message.tool_calls:
                interaction = call.get("interaction")
                if not isinstance(interaction, dict):
                    continue
                interaction_id = _coerce_text(interaction.get("id"))
                if not interaction_id:
                    interaction_id = f"claude_code:{record_index}:question"
                    interaction["id"] = interaction_id
                pending_questions[interaction_id] = interaction

            if should_emit(message):
                yield from coalesce_lifecycle_message(message)

    if pending_lifecycle_message is not None:
        yield pending_lifecycle_message


def _parse_cursor_record_messages(obj: object) -> list[NormalizedMessage]:
    """Expand one composite Cursor record into ordered semantic messages.

    Cursor stores visible assistant prose and multiple tool invocations in one
    ``message.content`` array.  The normalized store is message-oriented, so a
    one-record/one-row parser turns tool-only bubbles into empty assistant rows
    and hides every invocation in assistant metadata.  Split only records that
    contain structured tool blocks; ordinary user/assistant records continue
    through the compatibility parser unchanged.
    """
    if not isinstance(obj, dict):
        return []
    message = obj.get("message")
    message_mapping = _as_mapping(message)
    content = message_mapping.get("content") if message_mapping else message
    if not isinstance(content, list) or not any(
        isinstance(item, dict)
        and item.get("type") in ("tool_use", "toolCall", "tool_result", "toolResult")
        for item in content
    ):
        parsed = parse_conversation_object(obj, "cursor")
        return [parsed] if parsed is not None else []

    timestamp = _coerce_text(obj.get("timestamp"))
    source_id = _cursor_source_id(obj, message)
    messages: list[NormalizedMessage] = []
    text_items: list[object] = []
    text_start = 0

    def flush_text() -> None:
        nonlocal text_items
        if not text_items:
            return
        text_obj = dict(obj)
        text_message = dict(message_mapping)
        text_message["content"] = text_items
        text_obj["message"] = text_message
        parsed = parse_conversation_object(text_obj, "cursor")
        if parsed is not None:
            parsed.source_id = _cursor_part_source_id(
                source_id,
                "text",
                text_start,
            )
            messages.append(parsed)
        text_items = []

    for index, item in enumerate(content):
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type not in ("tool_use", "toolCall", "tool_result", "toolResult"):
            if not text_items:
                text_start = index
            text_items.append(item)
            continue

        flush_text()
        if item_type in ("tool_use", "toolCall"):
            normalized_calls = normalize_tool_calls([item])
            if not normalized_calls:
                continue
            call = normalized_calls[0]
            tool_name = _coerce_text(call.get("name")) or "Tool"
            messages.append(NormalizedMessage(
                role="tool",
                content=f"[{tool_name}]",
                tool_name=tool_name,
                tool_input=_coerce_text(call.get("input")),
                interaction=(
                    call.get("interaction")
                    if isinstance(call.get("interaction"), dict)
                    else None
                ),
                tool_call_id=_bounded_interaction_text(
                    item.get("id") or item.get("call_id"),
                    512,
                ),
                timestamp=timestamp,
                raw_type="tool_call",
                source_id=_cursor_part_source_id(source_id, "tool_call", index),
            ))
            continue

        result = _extract_tool_result_details([item])
        if result is None:
            continue
        result_content, tool_call_id = result
        messages.append(NormalizedMessage(
            role="tool",
            content=result_content or "(tool returned no textual output)",
            tool_name="Tool result",
            tool_call_id=tool_call_id,
            timestamp=timestamp,
            raw_type="tool_output",
            source_id=_cursor_part_source_id(source_id, "tool_output", index),
        ))

    flush_text()
    return messages


def _iter_cursor_conversation_messages(
    source_objects: Iterable[object],
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity: AssistantIdentityState | None = None,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
) -> Iterator[NormalizedMessage]:
    """Yield Cursor semantic rows while linking interactive answers."""
    identity = assistant_identity or AssistantIdentityState()
    task_tracker = TaskStateTracker(
        "cursor",
        initial_task_state,
        incremental=incremental,
    )
    seen_source_ids: set[str] = set()
    seen_directives: set[str] = set()
    subagent_models: dict[str, str] = {}
    pending_question: tuple[int, dict[str, object]] | None = None
    for interaction in reversed(initial_question_interactions or []):
        if isinstance(interaction, dict) and interaction.get("source") == "cursor":
            pending_question = (-1, interaction)
            break

    for record_index, source_object in enumerate(source_objects):
        _update_assistant_identity(identity, source_object, "cursor")
        if (
            pending_question is not None
            and record_index - pending_question[0] > CURSOR_QUESTION_RESPONSE_WINDOW
        ):
            pending_question = None

        for message in _parse_cursor_record_messages(source_object):
            _attach_assistant_identity(message, identity)
            if message.raw_type == "cursor_directives":
                directive_key = hashlib.sha256(
                    message.content.strip().encode("utf-8", "replace")
                ).hexdigest()
                if directive_key in seen_directives:
                    continue
                seen_directives.add(directive_key)
            if message.agent_event is not None:
                thread_id = _coerce_text(
                    message.agent_event.get("agent_thread_id")
                ).strip()
                event_model = _coerce_text(
                    message.agent_event.get("model")
                ).strip()
                if thread_id and event_model:
                    subagent_models[thread_id] = event_model
                elif thread_id and thread_id in subagent_models:
                    message.agent_event["model"] = subagent_models[thread_id]
            if message.role == "user" and pending_question is not None:
                _pending_index, interaction = pending_question
                if not _is_cursor_plan_mode_interaction(interaction):
                    message.interaction_response = build_question_response(
                        interaction,
                        message.content,
                    )
                    pending_question = None
            elif message.role == "tool" and pending_question is not None:
                _pending_index, interaction = pending_question
                if (
                    message.tool_call_id
                    and message.tool_call_id == _coerce_text(interaction.get("id"))
                ):
                    result_status = (
                        "cancelled"
                        if message.content == "(tool returned no textual output)"
                        else "completed"
                    )
                    message.interaction_response = build_cursor_interaction_response(
                        interaction,
                        message.content,
                        result_status,
                    )
                    if message.interaction_response is not None:
                        pending_question = None

            if message.interaction is not None:
                interaction_id = _coerce_text(message.interaction.get("id"))
                if not interaction_id:
                    interaction_id = f"cursor:{record_index}:question"
                    message.interaction["id"] = interaction_id
                if message.interaction_response is None:
                    pending_question = (record_index, message.interaction)
                else:
                    pending_question = None

            if message.source_id:
                if message.source_id in seen_source_ids:
                    continue
                seen_source_ids.add(message.source_id)
            task_tracker.apply(message)
            yield message


def _extract_content(content) -> str:
    """Extract text from content that could be string, list, or dict.

    Also strips any IDE/system injection tags.
    """
    if isinstance(content, str):
        return _strip_system_tags(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t == "text":
                    parts.append(_coerce_text(item.get("text")))
                elif t in ("tool_use", "toolCall"):
                    # Claude uses tool_use + input; OpenClaw uses toolCall + arguments.
                    name = item.get("name", "tool")
                    inp = item.get("input") if "input" in item else item.get("arguments", {})
                    inp_str = json.dumps(inp, ensure_ascii=False) if not isinstance(inp, str) else inp
                    parts.append(f"[Tool: {name}]\n{inp_str}")
                elif t in ("tool_result", "toolResult"):
                    result = item.get("content", item.get("output", ""))
                    if isinstance(result, list):
                        result = " ".join(
                            _coerce_text(block.get("text"))
                            for block in result
                            if isinstance(block, dict)
                        )
                    parts.append(f"[Result]\n{str(result)}")
            elif isinstance(item, str):
                parts.append(item)
        return _strip_system_tags("\n".join(parts))
    if isinstance(content, dict):
        if "text" in content:
            return _strip_system_tags(_coerce_text(content.get("text")))
        return json.dumps(content, ensure_ascii=False)
    return _coerce_text(content)


def _extract_codex_content(content_list) -> str:
    """Extract text from Codex content array: [{type: "input_text"|"output_text", text: "..."}]"""
    if isinstance(content_list, str):
        return content_list
    if not isinstance(content_list, list):
        return _coerce_text(content_list)
    parts = []
    for item in content_list:
        if isinstance(item, dict):
            parts.append(_coerce_text(item.get("text")))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _extract_codex_tool_output(value: object) -> str:
    """Render Codex tool results without leaking their transport envelope."""
    if isinstance(value, list):
        extracted = _extract_codex_content(value).strip()
        if extracted:
            return extracted
    serialized = _serialize_tool_input(value).strip()
    return serialized or "(tool returned no textual output)"


def _iter_decoded_json_objects(raw_content: str):
    """Yield decoded values from mixed compact JSONL / pretty-
    printed content. Claude Code's VS Code extension on Windows sometimes
    writes entries as indented multi-line JSON in the same ``.jsonl`` file
    as compact ones; splitting on newlines loses those multi-line objects
    entirely. Using json.JSONDecoder.raw_decode walks the stream and
    tolerates arbitrary whitespace between objects.
    """
    if not raw_content:
        return
    decoder = json.JSONDecoder()
    i = 0
    n = len(raw_content)
    while i < n:
        # skip any whitespace (incl newlines, CR, tabs) between objects
        while i < n and raw_content[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(raw_content, i)
            yield obj
            i = end
        except json.JSONDecodeError:
            # Couldn't parse starting here — advance to next newline and
            # retry. This handles truncated fragments / concatenation noise.
            next_nl = raw_content.find("\n", i)
            if next_nl < 0:
                break
            i = next_nl + 1


def _iter_json_objects(raw_content: str):
    """Compatibility iterator returning one compact JSON string per value."""
    for obj in _iter_decoded_json_objects(raw_content):
        yield json.dumps(obj, ensure_ascii=False)


def _pretty_leading_json(text: str) -> str:
    """If text starts with a JSON object/array, pretty-print just that prefix
    and append any trailing non-JSON text unchanged. Otherwise return as-is."""
    s = text.lstrip()
    if not s or s[0] not in "{[":
        return text
    try:
        obj, end = json.JSONDecoder().raw_decode(s)
    except json.JSONDecodeError:
        return text
    pretty = json.dumps(obj, ensure_ascii=False, indent=2)
    rest = s[end:].strip()
    return pretty + "\n\n" + rest if rest else pretty


def _format_hermes_tool_content(content_str: str) -> str:
    """Hermes tool result is `{"output": ..., "exit_code": 0, "error": null}`.
    Extract output (parse inner JSON if applicable), prepend error/exit_code notes.
    """
    try:
        outer = orjson.loads(content_str)
    except (orjson.JSONDecodeError, TypeError):
        return content_str
    if not isinstance(outer, dict):
        return content_str

    output = outer.get("output", "")
    error = outer.get("error")
    exit_code = outer.get("exit_code")

    # output may be a JSON-encoded string, optionally followed by non-JSON
    # trailing text (e.g. terminal output prints a JSON line then a stack
    # trace). Pretty-print just the leading JSON value with raw_decode and
    # preserve whatever comes after.
    pretty: str
    if isinstance(output, str):
        pretty = _pretty_leading_json(output)
    elif isinstance(output, (dict, list)):
        pretty = json.dumps(output, ensure_ascii=False, indent=2)
    else:
        pretty = str(output) if output is not None else ""

    parts = []
    if error:
        parts.append(f"⚠️  {error}")
    if pretty:
        parts.append(pretty)
    if exit_code not in (None, 0):
        parts.append(f"(exit_code={exit_code})")
    return "\n\n".join(parts) if parts else content_str


def _parse_hermes_session(
    raw_content: str,
    offset: int,
    limit: int | None,
    assistant_identity: AssistantIdentityState | None = None,
) -> list[NormalizedMessage]:
    """Hermes stores a whole session as a single top-level JSON, not JSONL."""
    try:
        d = orjson.loads(raw_content)
    except orjson.JSONDecodeError:
        return []
    if not isinstance(d, dict):
        return []
    identity = assistant_identity or AssistantIdentityState()
    _update_assistant_identity(identity, d, "hermes")
    msgs = d.get("messages") or []
    timestamp = d.get("last_updated") or d.get("session_start") or ""

    # Pre-scan: build call_id → tool_name from assistant.tool_calls
    tool_name_by_id: dict[str, str] = {}
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            cid = tc.get("id") or tc.get("call_id")
            if cid and name:
                tool_name_by_id[str(cid)] = str(name)

    out: list[NormalizedMessage] = []
    skipped = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        text = content.strip()

        if role == "system" or not text:
            continue
        if role == "user":
            norm = NormalizedMessage(role="user", content=text, timestamp=timestamp)
        elif role == "assistant":
            norm = NormalizedMessage(role="assistant", content=text, timestamp=timestamp)
            _attach_assistant_identity(norm, identity)
        elif role == "tool":
            tcid = str(m.get("tool_call_id") or "")
            tool_name = tool_name_by_id.get(tcid, "tool")
            formatted = _format_hermes_tool_content(text)
            display = formatted if len(formatted) <= 4000 else formatted[:4000] + "\n…(truncated)"
            norm = NormalizedMessage(role="tool", content=display, tool_name=tool_name, timestamp=timestamp)
        else:
            continue

        if skipped < offset:
            skipped += 1
            continue
        out.append(norm)
        if limit and len(out) >= limit:
            break
    return out


def _count_hermes_messages(raw_content: str) -> int:
    try:
        d = orjson.loads(raw_content)
    except orjson.JSONDecodeError:
        return 0
    if not isinstance(d, dict):
        return 0
    n = 0
    for m in d.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if role == "system" or not content.strip() or role not in ("user", "assistant", "tool"):
            continue
        n += 1
    return n


def _message_timestamp(value: object) -> datetime | None:
    """Parse a transcript timestamp without discarding sub-second identity."""
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


def is_codex_user_mirror_pair(
    first_type: str | None,
    first_content: str,
    first_timestamp: object,
    second_type: str | None,
    second_content: str,
    second_timestamp: object,
) -> bool:
    """Return whether two rows are Codex's two transports for one prompt.

    The records are paired structurally, not by a coarse timestamp/content
    fingerprint.  ``response_item`` can include attachment annotations that
    are absent from the following ``user_message``, so a prefix relationship
    is accepted, but only for the known cross-type pair within one second.
    Older Codex builds occasionally wrote the event copy 300-850 ms later.
    """
    if {first_type, second_type} != {"response_item", "user_message"}:
        return False
    first_time = _message_timestamp(first_timestamp)
    second_time = _message_timestamp(second_timestamp)
    if first_time is None or second_time is None:
        return False
    if abs((second_time - first_time).total_seconds()) > 1.0:
        return False
    left = (first_content or "").strip()
    right = (second_content or "").strip()
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def is_codex_assistant_mirror_pair(
    first_type: str | None,
    first_content: str,
    first_timestamp: object,
    second_type: str | None,
    second_content: str,
    second_timestamp: object,
) -> bool:
    """Return whether two Codex assistant transports represent one message.

    Current Codex writes ``agent_message``, ``response_item``, and (for the
    final reply) ``task_complete`` copies.  A copy is collapsed only inside
    that known transport family, with exact/prefix content and close native
    timestamps.  A lone transport is retained instead of being discarded on
    the assumption that another copy must exist.
    """
    pair = {first_type, second_type}
    if pair not in (
        {"agent_message", "response_item"},
        {"agent_message", "task_complete"},
        {"response_item", "task_complete"},
    ):
        return False
    left = (first_content or "").strip()
    right = (second_content or "").strip()
    if not left or not right:
        return False
    first_time = _message_timestamp(first_timestamp)
    second_time = _message_timestamp(second_timestamp)
    if first_time is None or second_time is None:
        return False
    separation = abs((second_time - first_time).total_seconds())
    if left == right:
        # Some Codex builds flush the exact event/response transport copy only
        # after tool accounting completes. Source adjacency plus the known
        # cross-transport family still identifies one message, even when that
        # flush is several seconds late.
        return separation <= CODEX_ASSISTANT_EXACT_MIRROR_MAX_SECONDS
    # Prefix matching is needed for attachment annotations, but is less
    # specific than exact equality and therefore retains the tight window.
    return separation <= 1.0 and (
        left.startswith(right) or right.startswith(left)
    )


def codex_assistant_transport_priority(raw_type: str | None) -> int:
    """Return the presentation preference for a Codex assistant transport."""
    return CODEX_ASSISTANT_TRANSPORT_PRIORITY.get(raw_type or "", 0)


def iter_conversation_messages(
    raw_content: str,
    tool_id: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity: AssistantIdentityState | None = None,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
) -> Iterator[NormalizedMessage]:
    """Compatibility wrapper for callers that already hold a complete string."""
    identity = assistant_identity or AssistantIdentityState()
    if tool_id == "hermes":
        yield from _parse_hermes_session(
            raw_content,
            0,
            None,
            assistant_identity=identity,
        )
        return
    yield from iter_conversation_messages_from_objects(
        _iter_decoded_json_objects(raw_content),
        tool_id,
        initial_question_interactions=initial_question_interactions,
        assistant_identity=identity,
        initial_task_state=initial_task_state,
        incremental=incremental,
    )


def iter_conversation_messages_from_objects(
    source_objects: Iterable[object],
    tool_id: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity: AssistantIdentityState | None = None,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
) -> Iterator[NormalizedMessage]:
    """Yield semantic messages once, using identities supplied by each tool.

    Claude UUIDs and Codex client IDs are authoritative identities. Cursor's
    exported JSONL currently has neither mirrored transport rows nor stable
    IDs, so each source item is preserved.  This intentionally avoids any
    role/content/second heuristic: two identical prompts are still two turns.
    """
    identity = assistant_identity or AssistantIdentityState()
    if tool_id == "cursor":
        yield from _iter_cursor_conversation_messages(
            source_objects,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=identity,
            initial_task_state=initial_task_state,
            incremental=incremental,
        )
        return
    if tool_id == "claude_code":
        yield from _iter_claude_conversation_messages(
            source_objects,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=identity,
            initial_task_state=initial_task_state,
            incremental=incremental,
        )
        return

    seen_source_ids: set[str] = set()
    pending_claude_queue: dict[str, list[NormalizedMessage]] = defaultdict(list)
    pending_codex_user: tuple[int, NormalizedMessage, str] | None = None
    pending_codex_assistant: tuple[int, NormalizedMessage] | None = None
    pending_codex_reasoning: tuple[int, NormalizedMessage] | None = None
    current_codex_turn_id = ""
    task_tracker = TaskStateTracker(
        tool_id,
        initial_task_state,
        incremental=incremental,
    )
    pending_questions = {
        _coerce_text(interaction.get("id")): interaction
        for interaction in (initial_question_interactions or [])
        if isinstance(interaction, dict) and interaction.get("id")
    }

    def should_emit(message: NormalizedMessage) -> bool:
        if not message.source_id:
            task_tracker.apply(message)
            return True
        source_key = f"{tool_id}:{message.source_id}"
        if source_key in seen_source_ids:
            return False
        seen_source_ids.add(source_key)
        task_tracker.apply(message)
        return True

    for record_index, source_object in enumerate(source_objects):
        _update_assistant_identity(identity, source_object, tool_id)
        if tool_id == "codex":
            source_payload = _as_mapping(source_object.get("payload"))
            if source_object.get("type") == "event_msg":
                event_type = source_payload.get("type")
                event_turn_id = _coerce_text(source_payload.get("turn_id"))
                if event_type == "task_started" and event_turn_id:
                    current_codex_turn_id = event_turn_id
                elif event_type in {"task_complete", "turn_aborted"}:
                    current_codex_turn_id = ""

        message = parse_conversation_object(source_object, tool_id)
        if message is not None:
            _attach_assistant_identity(message, identity)

        if tool_id == "codex" and message is not None and message.raw_type == "reasoning":
            if pending_codex_reasoning is not None:
                _pending_index, pending = pending_codex_reasoning
                if pending.source_id != message.source_id:
                    if should_emit(pending):
                        yield pending
            pending_codex_reasoning = (record_index, message)
            continue

        if pending_codex_reasoning is not None:
            pending = pending_codex_reasoning[1]
            pending_codex_reasoning = None
            if should_emit(pending):
                yield pending

        if (
            tool_id == "claude_code"
            and message is not None
            and (
                (message.role == "user" and message.raw_type == "user")
                or (
                    message.role == "system"
                    and message.raw_type == "scheduled_automation"
                )
            )
        ):
            if pop_matching_claude_queue_user(
                pending_claude_queue,
                message.content,
                message.timestamp,
            ) is not None:
                message = None

        if (
            tool_id == "claude_code"
            and message is not None
            and message.raw_type
            in {"queued_user_message", "queued_scheduled_automation"}
        ):
            pending_claude_queue[message.content.strip()].append(message)

        if message is not None:
            if message.tool_call_id and message.tool_call_id in pending_questions:
                interaction = pending_questions.pop(message.tool_call_id)
                message.interaction_response = build_question_response(
                    interaction,
                    message.content,
                )
                message.tool_name = "Question response"

            if message.interaction is not None:
                interaction_id = _coerce_text(message.interaction.get("id"))
                if not interaction_id:
                    interaction_id = f"{tool_id}:{record_index}:question"
                    message.interaction["id"] = interaction_id
                pending_questions[interaction_id] = message.interaction

            for call in message.tool_calls:
                interaction = call.get("interaction")
                if not isinstance(interaction, dict):
                    continue
                interaction_id = _coerce_text(interaction.get("id"))
                if not interaction_id:
                    interaction_id = f"{tool_id}:{record_index}:question"
                    interaction["id"] = interaction_id
                pending_questions[interaction_id] = interaction

        if (
            pending_codex_user is not None
            and record_index - pending_codex_user[0] > 2
        ):
            pending = pending_codex_user[1]
            pending_codex_user = None
            if should_emit(pending):
                yield pending

        if (
            pending_codex_assistant is not None
            and record_index - pending_codex_assistant[0] > 4
        ):
            pending = pending_codex_assistant[1]
            pending_codex_assistant = None
            if should_emit(pending):
                yield pending

        is_codex_response_user = (
            tool_id == "codex"
            and message is not None
            and message.role == "user"
            and message.raw_type == "response_item"
        )
        if is_codex_response_user:
            if pending_codex_assistant is not None:
                pending = pending_codex_assistant[1]
                pending_codex_assistant = None
                if should_emit(pending):
                    yield pending
            if pending_codex_user is not None:
                pending = pending_codex_user[1]
                if should_emit(pending):
                    yield pending
            turn_id = message.source_turn_id or current_codex_turn_id
            message.source_turn_id = turn_id
            pending_codex_user = (record_index, message, turn_id)
            continue

        is_codex_event_user = (
            tool_id == "codex"
            and message is not None
            and message.role == "user"
            and message.raw_type == "user_message"
        )
        if is_codex_event_user:
            if pending_codex_assistant is not None:
                pending = pending_codex_assistant[1]
                pending_codex_assistant = None
                if should_emit(pending):
                    yield pending
            if pending_codex_user is not None:
                pending_index, pending, pending_turn_id = pending_codex_user
                if not (
                    record_index - pending_index <= 2
                    and is_codex_user_mirror_pair(
                        pending.raw_type,
                        pending.content,
                        pending.timestamp,
                        message.raw_type,
                        message.content,
                        message.timestamp,
                    )
                ):
                    if should_emit(pending):
                        yield pending
                else:
                    message.source_paired = True
                    message.source_turn_id = (
                        pending_turn_id
                        or message.source_turn_id
                        or current_codex_turn_id
                    )
                pending_codex_user = None
            if not message.source_turn_id:
                message.source_turn_id = current_codex_turn_id
            if should_emit(message):
                yield message
            continue

        is_codex_assistant_transport = (
            tool_id == "codex"
            and message is not None
            and message.role == "assistant"
            and codex_assistant_transport_priority(message.raw_type) > 0
        )
        if is_codex_assistant_transport:
            if pending_codex_user is not None:
                pending = pending_codex_user[1]
                pending_codex_user = None
                if should_emit(pending):
                    yield pending
            if pending_codex_assistant is not None:
                pending_index, pending = pending_codex_assistant
                if (
                    record_index - pending_index <= 4
                    and is_codex_assistant_mirror_pair(
                        pending.raw_type,
                        pending.content,
                        pending.timestamp,
                        message.raw_type,
                        message.content,
                        message.timestamp,
                    )
                ):
                    if (
                        codex_assistant_transport_priority(message.raw_type)
                        > codex_assistant_transport_priority(pending.raw_type)
                    ):
                        pending = message
                    pending.source_paired = True
                    pending_codex_assistant = (record_index, pending)
                    continue
                if should_emit(pending):
                    yield pending
            pending_codex_assistant = (record_index, message)
            continue

        if message is None:
            continue
        if pending_codex_user is not None:
            pending = pending_codex_user[1]
            pending_codex_user = None
            if should_emit(pending):
                yield pending
        if pending_codex_assistant is not None:
            pending = pending_codex_assistant[1]
            pending_codex_assistant = None
            if should_emit(pending):
                yield pending
        if should_emit(message):
            yield message

    if pending_codex_user is not None:
        pending = pending_codex_user[1]
        if should_emit(pending):
            yield pending
    if pending_codex_assistant is not None:
        pending = pending_codex_assistant[1]
        if should_emit(pending):
            yield pending
    if pending_codex_reasoning is not None:
        pending = pending_codex_reasoning[1]
        if should_emit(pending):
            yield pending


def parse_conversation(
    raw_content: str,
    tool_id: str,
    offset: int = 0,
    limit: int | None = None,
    *,
    initial_task_state: dict[str, object] | None = None,
    incremental: bool = False,
) -> list[NormalizedMessage]:
    """Parse a conversation into the same semantic sequence used by ingest."""
    if tool_id == "hermes":
        return _parse_hermes_session(raw_content, offset, limit)
    if limit is not None and limit <= 0:
        return []
    messages: list[NormalizedMessage] = []
    for index, message in enumerate(
        iter_conversation_messages(
            raw_content,
            tool_id,
            initial_task_state=initial_task_state,
            incremental=incremental,
        )
    ):
        if index < offset:
            continue
        messages.append(message)
        if limit is not None and len(messages) >= limit:
            break
    return messages


def count_conversation_messages(raw_content: str, tool_id: str) -> int:
    """Count exactly the semantic sequence returned by ``parse_conversation``."""
    if tool_id == "hermes":
        return _count_hermes_messages(raw_content)
    return sum(1 for _ in iter_conversation_messages(raw_content, tool_id))
