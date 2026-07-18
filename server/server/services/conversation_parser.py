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
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar
from uuid import UUID

import orjson


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


@dataclass
class AssistantIdentityState:
    """Mutable model selection carried across incremental transcript chunks."""

    model: str = ""
    reasoning_effort: str = ""


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
_CURSOR_USER_QUERY_ENVELOPE_RE = re.compile(
    r"\A\s*<user_query>\s*(?P<content>[\s\S]*?)\s*</user_query>\s*\Z",
    re.IGNORECASE,
)
_CURSOR_SESSION_CONTEXT_RE = re.compile(
    r"\A\s*<(?P<tag>external_links|plugin_info|uploaded_documents)\b[^>]*>"
    r"[\s\S]*?</(?P=tag)>\s*",
    re.IGNORECASE,
)
_CURSOR_SESSION_CONTEXT_PREFIX_RE = re.compile(
    r"\A\s*<(?:external_links|plugin_info|uploaded_documents)(?:\s|>)",
    re.IGNORECASE,
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
_CODEX_ASSISTANT_TRANSPORT_PRIORITY = {
    "agent_message": 3,
    "response_item": 2,
    "task_complete": 1,
}
_CURSOR_REDACTED_TRANSPORT_LINE_RE = re.compile(
    r"(^|\n)[ \t]*\[REDACTED\][ \t]*(?=\n|$)",
    re.IGNORECASE | re.MULTILINE,
)
_CLAUDE_QUEUE_MATCH_WINDOW_SECONDS = 24 * 60 * 60


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

    timestamp_match = _CURSOR_TIMESTAMP_ENVELOPE_RE.match(text)
    if timestamp_match is None:
        prompt = text.strip() if context_parts or attachments else text
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
        except (json.JSONDecodeError, TypeError):
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
        except json.JSONDecodeError:
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


def _has_claude_thinking_block(content: object) -> bool:
    """Return whether a Claude response records extended-thinking use."""
    return isinstance(content, list) and any(
        isinstance(item, dict)
        and item.get("type") in {"thinking", "redacted_thinking"}
        for item in content
    )


def _update_assistant_identity(
    state: AssistantIdentityState,
    obj: object,
    tool_id: str,
) -> None:
    """Advance model/reasoning state from one native transcript record."""
    if not isinstance(obj, dict):
        return

    msg_type = _coerce_text(obj.get("type"))
    if tool_id == "codex" and msg_type == "turn_context":
        payload = _as_mapping(obj.get("payload"))
        _set_identity_field(state, payload, ("model",), "model")
        _set_identity_field(
            state,
            payload,
            ("effort", "reasoning_effort", "reasoningEffort"),
            "reasoning_effort",
        )
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


def _attach_assistant_identity(
    message: NormalizedMessage,
    state: AssistantIdentityState,
) -> None:
    """Copy the active model selection onto assistant presentation rows."""
    if message.role != "assistant":
        return
    message.model = state.model
    message.reasoning_effort = state.reasoning_effort


def parse_conversation_line(raw_line: str, tool_id: str) -> NormalizedMessage | None:
    """Parse a single JSONL line into a NormalizedMessage, or None if it should be skipped."""
    try:
        obj = orjson.loads(raw_line)
    except json.JSONDecodeError:
        return None

    identity = AssistantIdentityState()
    _update_assistant_identity(identity, obj, tool_id)
    message = parse_conversation_object(obj, tool_id)
    if message is not None:
        _attach_assistant_identity(message, identity)
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
                return NormalizedMessage(
                    role="tool",
                    content=result_content or "(tool returned no textual output)",
                    tool_name="Tool result",
                    timestamp=timestamp,
                    raw_type="tool_result",
                    source_id=source_id,
                    tool_call_id=tool_call_id,
                )

            tool_use = _extract_tool_use(raw_content)
            if role == "assistant" and tool_use is not None:
                tool_name, tool_input, tool_call_id, interaction = tool_use
                return NormalizedMessage(
                    role="tool",
                    content=f"[{tool_name}]",
                    tool_name=tool_name,
                    tool_input=tool_input,
                    timestamp=timestamp,
                    raw_type="tool_use",
                    source_id=source_id,
                    interaction=interaction,
                    tool_call_id=tool_call_id,
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
            if role == "user" and is_claude_session_context_record(obj):
                return NormalizedMessage(
                    role="system",
                    content=content,
                    thinking=thinking,
                    timestamp=timestamp,
                    raw_type="claude_context",
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
            content = _strip_system_tags(_coerce_text(obj.get("content")))
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
            return NormalizedMessage(
                role="user",
                content=content,
                timestamp=timestamp,
                raw_type="queued_user_message",
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
            # Skip reasoning — AI internal thought process, not a reply
            if p_type == "reasoning":
                return None
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
                except json.JSONDecodeError:
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
            if session_context and not content.strip():
                return NormalizedMessage(
                    role="system",
                    content=session_context,
                    timestamp=timestamp,
                    raw_type="cursor_context",
                    source_id=source_id,
                )
        if role in ("user", "assistant") and (
            content.strip() or tool_calls or attachments
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
                item.get("tool_use_id") or item.get("tool_call_id"),
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
        interaction = normalize_question_interaction(
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
    "request_user_input",
}
_MAX_INTERACTION_QUESTIONS = 8
_MAX_INTERACTION_OPTIONS = 12
CURSOR_QUESTION_RESPONSE_WINDOW = 4


def _json_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = orjson.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bounded_interaction_text(value: object, limit: int) -> str:
    return _bounded_tool_text(_coerce_text(value).strip(), limit)


def normalize_question_interaction(
    tool_name: str,
    raw_input: object,
    *,
    source: str,
    interaction_id: object = "",
) -> dict[str, object] | None:
    """Normalize interactive-question payloads emitted by supported tools."""
    if tool_name.strip().casefold() not in _QUESTION_TOOL_NAMES:
        return None
    payload = _json_mapping(raw_input)
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        return None

    questions: list[dict[str, object]] = []
    for index, raw_question in enumerate(raw_questions[:_MAX_INTERACTION_QUESTIONS]):
        if not isinstance(raw_question, dict):
            continue
        prompt = _bounded_interaction_text(
            raw_question.get("prompt") or raw_question.get("question"),
            4096,
        )
        if not prompt:
            continue
        question_id = _bounded_interaction_text(
            raw_question.get("id")
            or raw_question.get("header")
            or f"question-{index + 1}",
            256,
        )
        header = _bounded_interaction_text(
            raw_question.get("header") or raw_question.get("label_short"),
            512,
        )
        multiple = bool(
            raw_question.get("multiSelect")
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
                label = _bounded_interaction_text(raw_option.get("label"), 1024)
                if not label:
                    continue
                option_id = _bounded_interaction_text(
                    raw_option.get("id") or label or f"option-{option_index + 1}",
                    512,
                )
                option = {"id": option_id, "label": label}
                description = _bounded_interaction_text(
                    raw_option.get("description") or raw_option.get("preview"),
                    4096,
                )
                short_label = _bounded_interaction_text(
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


def build_question_response(
    interaction: dict[str, object],
    raw_output: object,
) -> dict[str, object]:
    """Build a shared answer model from structured or human-readable output."""
    if isinstance(raw_output, str):
        raw_text = _bounded_interaction_text(raw_output, 16 * 1024)
        parsed = _json_mapping(raw_output)
    else:
        raw_text = _bounded_interaction_text(
            json.dumps(raw_output, ensure_ascii=False, default=str),
            16 * 1024,
        )
        parsed = raw_output if isinstance(raw_output, dict) else {}
    structured_answers = parsed.get("answers")
    if not isinstance(structured_answers, dict):
        structured_answers = {}

    raw_questions = interaction.get("questions")
    questions = raw_questions if isinstance(raw_questions, list) else []
    answers: list[dict[str, object]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        question_id = _coerce_text(question.get("id"))
        answer_values = _answer_texts(structured_answers.get(question_id))
        if not answer_values and raw_text:
            prompt = _coerce_text(question.get("prompt"))
            next_prompt = None
            if index + 1 < len(questions) and isinstance(questions[index + 1], dict):
                next_prompt = _coerce_text(questions[index + 1].get("prompt"))
            claude_answer = _claude_answer_for_prompt(raw_text, prompt, next_prompt)
            if claude_answer:
                answer_values = [claude_answer]
        if not answer_values and len(questions) == 1 and raw_text:
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
        if answer_values or selected:
            answers.append({
                "question_id": question_id,
                "text": combined,
                "selected_option_ids": selected,
            })

    lowered = raw_text.casefold()
    status = "cancelled" if "cancel" in lowered and not answers else "answered"
    return {
        "kind": "question_response",
        "interaction_id": _bounded_interaction_text(interaction.get("id"), 512),
        "status": status,
        "answers": answers,
        "raw_text": raw_text,
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
            interaction = normalize_question_interaction(
                _coerce_text(interaction.get("tool_name") or name),
                {"questions": interaction.get("questions")},
                source=_coerce_text(interaction.get("source")),
                interaction_id=interaction.get("id"),
            )
        else:
            interaction = normalize_question_interaction(
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
    raw_content: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity: AssistantIdentityState | None = None,
) -> Iterator[NormalizedMessage]:
    """Yield Cursor semantic rows while linking interactive answers."""
    identity = assistant_identity or AssistantIdentityState()
    seen_source_ids: set[str] = set()
    pending_question: tuple[int, dict[str, object]] | None = None
    for interaction in reversed(initial_question_interactions or []):
        if isinstance(interaction, dict) and interaction.get("source") == "cursor":
            pending_question = (-1, interaction)
            break

    for record_index, source_object in enumerate(
        _iter_decoded_json_objects(raw_content)
    ):
        _update_assistant_identity(identity, source_object, "cursor")
        if (
            pending_question is not None
            and record_index - pending_question[0] > CURSOR_QUESTION_RESPONSE_WINDOW
        ):
            pending_question = None

        for message in _parse_cursor_record_messages(source_object):
            _attach_assistant_identity(message, identity)
            if message.role == "user" and pending_question is not None:
                _pending_index, interaction = pending_question
                message.interaction_response = build_question_response(
                    interaction,
                    message.content,
                )
                pending_question = None

            if message.interaction is not None:
                interaction_id = _coerce_text(message.interaction.get("id"))
                if not interaction_id:
                    interaction_id = f"cursor:{record_index}:question"
                    message.interaction["id"] = interaction_id
                pending_question = (record_index, message.interaction)

            if message.source_id:
                if message.source_id in seen_source_ids:
                    continue
                seen_source_ids.add(message.source_id)
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
    except (json.JSONDecodeError, TypeError):
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
    except json.JSONDecodeError:
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
    except json.JSONDecodeError:
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


def codex_assistant_transport_priority(raw_type: str | None) -> int:
    """Return the presentation preference for a Codex assistant transport."""
    return _CODEX_ASSISTANT_TRANSPORT_PRIORITY.get(raw_type or "", 0)


def iter_conversation_messages(
    raw_content: str,
    tool_id: str,
    *,
    initial_question_interactions: list[dict[str, object]] | None = None,
    assistant_identity: AssistantIdentityState | None = None,
) -> Iterator[NormalizedMessage]:
    """Yield semantic messages once, using identities supplied by each tool.

    Claude UUIDs and Codex client IDs are authoritative identities. Cursor's
    exported JSONL currently has neither mirrored transport rows nor stable
    IDs, so each source item is preserved.  This intentionally avoids any
    role/content/second heuristic: two identical prompts are still two turns.
    """
    identity = assistant_identity or AssistantIdentityState()
    if tool_id == "hermes":
        yield from _parse_hermes_session(
            raw_content,
            0,
            None,
            assistant_identity=identity,
        )
        return
    if tool_id == "cursor":
        yield from _iter_cursor_conversation_messages(
            raw_content,
            initial_question_interactions=initial_question_interactions,
            assistant_identity=identity,
        )
        return

    seen_source_ids: set[str] = set()
    pending_claude_queue: dict[str, list[NormalizedMessage]] = defaultdict(list)
    pending_codex_user: tuple[int, NormalizedMessage, str] | None = None
    pending_codex_assistant: tuple[int, NormalizedMessage] | None = None
    current_codex_turn_id = ""
    pending_questions = {
        _coerce_text(interaction.get("id")): interaction
        for interaction in (initial_question_interactions or [])
        if isinstance(interaction, dict) and interaction.get("id")
    }

    def should_emit(message: NormalizedMessage) -> bool:
        if not message.source_id:
            return True
        source_key = f"{tool_id}:{message.source_id}"
        if source_key in seen_source_ids:
            return False
        seen_source_ids.add(source_key)
        return True

    for record_index, source_object in enumerate(
        _iter_decoded_json_objects(raw_content)
    ):
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

        if (
            tool_id == "claude_code"
            and message is not None
            and message.role == "user"
            and message.raw_type == "user"
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
            and message.raw_type == "queued_user_message"
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


def parse_conversation(
    raw_content: str,
    tool_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> list[NormalizedMessage]:
    """Parse a conversation into the same semantic sequence used by ingest."""
    if tool_id == "hermes":
        return _parse_hermes_session(raw_content, offset, limit)
    if limit is not None and limit <= 0:
        return []
    messages: list[NormalizedMessage] = []
    for index, message in enumerate(iter_conversation_messages(raw_content, tool_id)):
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
