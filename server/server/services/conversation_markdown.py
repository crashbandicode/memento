"""Faithful, bounded Markdown rendering for normalized AI conversations.

The web viewer is intentionally visual, but its semantic structure is stable:
human prompts start turns, assistant prose remains Markdown, tool calls are
collapsible, and interactive questions carry their selected/custom answers.
This module mirrors that structure without importing any web-only concerns.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TextIO


_MAX_PROMPT_RANGE_LENGTH = 512
_MAX_PROMPT_RANGE_PARTS = 100
_MAX_PROMPT_NUMBER = 1_000_000
_BACKTICK_RUN = re.compile(r"`+")


@dataclass(frozen=True, slots=True)
class PromptSelection:
    """A normalized set of inclusive, one-based prompt intervals."""

    intervals: tuple[tuple[int, int | None], ...]

    def includes(self, prompt_number: int) -> bool:
        return any(
            prompt_number >= start and (end is None or prompt_number <= end)
            for start, end in self.intervals
        )


@dataclass(frozen=True, slots=True)
class MarkdownExportOptions:
    prompt_selection: PromptSelection | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    include_tools: bool = True
    include_thinking: bool = True
    include_session_context: bool = True
    include_timestamps: bool = True


@dataclass(frozen=True, slots=True)
class ConversationMarkdownInfo:
    title: str
    tool_id: str
    document_id: str
    relative_path: str
    activity_at: datetime | None
    message_count: int
    project_title: str | None = None
    machine_name: str | None = None
    is_subagent: bool = False


@dataclass(frozen=True, slots=True)
class ExportMessage:
    line_number: int
    role: str
    content: str
    metadata: Mapping[str, Any]
    timestamp: datetime | None = None
    message_type: str = ""
    prompt_number: int | None = None


@dataclass(frozen=True, slots=True)
class ConversationMarkdownStats:
    prompts_seen: int
    prompts_exported: int
    messages_exported: int


def parse_prompt_selection(value: str | None) -> PromptSelection | None:
    """Parse ``1-3,7,10-`` into merged inclusive intervals.

    Empty input means all prompts. Open-ended ranges are supported only on the
    right so prompt numbering stays unsurprising and validation stays strict.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if len(raw) > _MAX_PROMPT_RANGE_LENGTH:
        raise ValueError("prompt range is too long")
    parts = [part.strip() for part in raw.split(",")]
    if not parts or len(parts) > _MAX_PROMPT_RANGE_PARTS:
        raise ValueError("prompt range contains too many parts")

    parsed: list[tuple[int, int | None]] = []
    for part in parts:
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d*)?)?", part)
        if not match:
            raise ValueError(
                "invalid prompt range; use values such as 1-3,7,10-"
            )
        start = int(match.group(1))
        if start < 1 or start > _MAX_PROMPT_NUMBER:
            raise ValueError("prompt numbers must be between 1 and 1000000")
        has_dash = "-" in part
        end_text = match.group(2)
        end = int(end_text) if end_text else (None if has_dash else start)
        if end is not None:
            if end < 1 or end > _MAX_PROMPT_NUMBER:
                raise ValueError("prompt numbers must be between 1 and 1000000")
            if end < start:
                raise ValueError("prompt range end cannot be before its start")
        parsed.append((start, end))

    parsed.sort(key=lambda item: item[0])
    merged: list[tuple[int, int | None]] = []
    for start, end in parsed:
        if not merged:
            merged.append((start, end))
            continue
        prior_start, prior_end = merged[-1]
        if prior_end is None:
            continue
        if start <= prior_end + 1:
            merged[-1] = (
                prior_start,
                None if end is None else max(prior_end, end),
            )
        else:
            merged.append((start, end))
    return PromptSelection(tuple(merged))


def is_meaningful_human_prompt(
    content: str | None,
    metadata: Mapping[str, Any] | None,
    role: str | None = "user",
) -> bool:
    """Use one prompt definition for the navigator and Markdown exports."""
    clean = (content or "").strip()
    values = metadata or {}
    return bool(
        role == "user"
        and clean
        and not clean.startswith("[Subagent Context]")
        and not isinstance(values.get("interaction_response"), dict)
    )


def safe_markdown_filename(title: str, document_id: str) -> str:
    """Create a portable filename while retaining a stable uniqueness suffix."""
    clean = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", title)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    clean = clean[:100].rstrip(" .") or "conversation"
    suffix = re.sub(r"[^A-Za-z0-9]", "", document_id)[-8:] or "thread"
    return f"{clean}--{suffix}.md"


def _iso(value: datetime | None) -> str:
    if value is None:
        return "Unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _normalized_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _timestamp_matches(
    value: datetime | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is None and end_at is None:
        return True
    timestamp = _normalized_time(value)
    if timestamp is None:
        return False
    start = _normalized_time(start_at)
    end = _normalized_time(end_at)
    return bool(
        (start is None or timestamp >= start)
        and (end is None or timestamp <= end)
    )


def _write(writer: TextIO, value: str = "") -> None:
    writer.write(value)
    writer.write("\n")


def _fenced(value: object, language: str = "text") -> str:
    text = value if isinstance(value, str) else json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    text = text.rstrip()
    longest = max((len(run.group(0)) for run in _BACKTICK_RUN.finditer(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _jsonish(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str), "json"
    stripped = value.strip()
    if not stripped:
        return "", "text"
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return stripped, "text"
    return json.dumps(parsed, ensure_ascii=False, indent=2, default=str), "json"


def _write_details(
    writer: TextIO,
    summary: str,
    content: str,
    *,
    language: str | None = None,
) -> None:
    _write(writer, "<details>")
    _write(writer, f"<summary>{summary}</summary>")
    _write(writer)
    _write(writer, _fenced(content, language or "text") if language else content)
    _write(writer)
    _write(writer, "</details>")
    _write(writer)


def _answer_for(question_id: str, response: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not response:
        return {}
    answers = response.get("answers")
    if not isinstance(answers, list):
        return {}
    for answer in answers:
        if isinstance(answer, dict) and str(answer.get("question_id") or "") == question_id:
            return answer
    return {}


def _write_interaction(
    writer: TextIO,
    interaction: Mapping[str, Any],
    response: Mapping[str, Any] | None,
) -> None:
    source = str(interaction.get("source") or "agent").replace("_", " ").title()
    status = str((response or {}).get("status") or "pending").replace("_", " ").title()
    _write(writer, f"> [!NOTE] Interactive question · {source} · {status}")
    questions = interaction.get("questions")
    if not isinstance(questions, list):
        questions = []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "")
        answer = _answer_for(question_id, response)
        selected = {
            str(item)
            for item in answer.get("selected_option_ids", [])
            if isinstance(item, (str, int, float))
        } if isinstance(answer, dict) else set()
        header = str(question.get("header") or f"Question {index}").strip()
        prompt = str(question.get("prompt") or "").strip()
        _write(writer, f"> **{header}** — {prompt}" if header else f"> **{prompt}**")
        options = question.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                option_id = str(option.get("id") or "")
                label = str(option.get("label") or option_id)
                checked = option_id in selected or label in selected
                description = str(option.get("description") or "").strip()
                suffix = f" — {description}" if description else ""
                _write(writer, f"> - [{'x' if checked else ' '}] {label}{suffix}")
        answer_text = str(answer.get("text") or "").strip() if isinstance(answer, dict) else ""
        if answer_text:
            answer_lines = answer_text.splitlines() or [answer_text]
            _write(writer, ">")
            _write(writer, f"> **Response:** {answer_lines[0]}")
            for line in answer_lines[1:]:
                _write(writer, f"> {line}")
    _write(writer)


def _write_tool(
    writer: TextIO,
    name: str,
    tool_input: object,
    output: str = "",
) -> None:
    input_text, input_language = _jsonish(tool_input)
    _write(writer, "<details>")
    _write(writer, f"<summary><strong>Tool</strong> · {name or 'Tool'}</summary>")
    _write(writer)
    if input_text:
        _write(writer, "**Input**")
        _write(writer)
        _write(writer, _fenced(input_text, input_language))
        _write(writer)
    if output.strip():
        _write(writer, "**Output**")
        _write(writer)
        _write(writer, _fenced(output.strip(), "text"))
        _write(writer)
    _write(writer, "</details>")
    _write(writer)


def _write_message(
    writer: TextIO,
    message: ExportMessage,
    options: MarkdownExportOptions,
    responses: Mapping[str, Mapping[str, Any]],
    rendered_interactions: set[str],
) -> bool:
    metadata = message.metadata or {}
    if isinstance(metadata.get("interaction_response"), dict):
        response = metadata["interaction_response"]
        interaction_id = str(response.get("interaction_id") or "")
        if interaction_id in rendered_interactions:
            return False
        _write(writer, "### Your response")
        _write(writer)
        raw_text = str(response.get("raw_text") or message.content).strip()
        _write(writer, raw_text or "_No response text was recorded._")
        _write(writer)
        return True

    role = (message.role or message.message_type or "system").lower()
    timestamp = f" · {_iso(message.timestamp)}" if options.include_timestamps and message.timestamp else ""
    session_context = str(metadata.get("session_context") or "").strip()
    if session_context and options.include_session_context:
        _write_details(writer, "Session context", session_context)

    if role == "assistant":
        content = message.content.strip()
        thinking = str(metadata.get("thinking") or "").strip()
        tool_calls = metadata.get("tool_calls")
        has_tools = options.include_tools and isinstance(tool_calls, list) and bool(tool_calls)
        if content or (thinking and options.include_thinking):
            _write(writer, f"### Assistant{timestamp}")
            _write(writer)
            if content:
                _write(writer, content)
                _write(writer)
            if thinking and options.include_thinking and thinking != content:
                _write_details(writer, "Thinking", thinking)
        if has_tools:
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                interaction = call.get("interaction")
                if isinstance(interaction, dict):
                    interaction_id = str(interaction.get("id") or "")
                    _write_interaction(writer, interaction, responses.get(interaction_id))
                    if interaction_id:
                        rendered_interactions.add(interaction_id)
                else:
                    _write_tool(
                        writer,
                        str(call.get("name") or "Tool"),
                        call.get("input", ""),
                    )
        return bool(content or thinking or has_tools)

    if role == "tool":
        if not options.include_tools:
            return False
        interaction = metadata.get("interaction")
        if isinstance(interaction, dict):
            interaction_id = str(interaction.get("id") or "")
            _write_interaction(writer, interaction, responses.get(interaction_id))
            if interaction_id:
                rendered_interactions.add(interaction_id)
        else:
            _write_tool(
                writer,
                str(metadata.get("tool_name") or "Tool result"),
                metadata.get("tool_input", ""),
                message.content,
            )
        return True

    is_context = bool(
        role not in {"user", "assistant", "tool"}
        and (
            re.search(r"(?:^|_)(?:codex|claude|cursor)_context$", message.message_type or "", re.I)
            or re.search(r"^(?:\s*<(?:recommended_plugins|codex_internal_context)\b|\s*#\s*AGENTS\.md instructions)", message.content, re.I)
        )
    )
    if is_context or message.content.lstrip().startswith("[Subagent Context]"):
        if options.include_session_context:
            _write_details(writer, "Subagent context" if "Subagent Context" in message.content else "Session context", message.content.strip())
            return True
        return False

    heading = "You" if role == "user" else role.title()
    _write(writer, f"### {heading}{timestamp}")
    _write(writer)
    _write(writer, message.content.strip() or "_Empty message._")
    _write(writer)
    return True


async def write_conversation_markdown(
    writer: TextIO,
    info: ConversationMarkdownInfo,
    messages: AsyncIterable[ExportMessage],
    options: MarkdownExportOptions,
    interaction_responses: Mapping[str, Mapping[str, Any]] | None = None,
) -> ConversationMarkdownStats:
    """Write one conversation incrementally and return useful export counts."""
    responses = interaction_responses or {}

    _write(writer, f"# {info.title or 'Untitled conversation'}")
    _write(writer)
    labels = [
        f"**Tool:** `{info.tool_id}`",
        f"**Messages:** {info.message_count}",
        f"**Last activity:** {_iso(info.activity_at)}",
    ]
    if info.project_title:
        labels.append(f"**Project:** {info.project_title}")
    if info.machine_name:
        labels.append(f"**Device:** {info.machine_name}")
    if info.is_subagent:
        labels.append("**Thread type:** Subagent")
    _write(writer, "  ·  ".join(labels))
    _write(writer, f"**Source:** `{info.relative_path}`")
    _write(writer, f"**Memento document:** `{info.document_id}`")
    _write(writer)
    _write(writer, "---")
    _write(writer)

    prompt_number = 0
    prompts_exported = 0
    messages_exported = 0
    include_current = bool(
        options.prompt_selection is None
        and options.start_at is None
        and options.end_at is None
    )
    rendered_interactions: set[str] = set()

    async for message in messages:
        if is_meaningful_human_prompt(message.content, message.metadata, message.role):
            prompt_number = message.prompt_number or (prompt_number + 1)
            include_current = bool(
                (options.prompt_selection is None or options.prompt_selection.includes(prompt_number))
                and _timestamp_matches(message.timestamp, options.start_at, options.end_at)
            )
            if not include_current:
                continue
            prompts_exported += 1
            timestamp = f" · {_iso(message.timestamp)}" if options.include_timestamps and message.timestamp else ""
            _write(writer, f"## Prompt {prompt_number} — You{timestamp}")
            _write(writer)
            session_context = str(message.metadata.get("session_context") or "").strip()
            if session_context and options.include_session_context:
                _write_details(writer, "Session context", session_context)
            _write(writer, message.content.strip())
            _write(writer)
            messages_exported += 1
            continue
        if include_current and _write_message(
            writer,
            message,
            options,
            responses,
            rendered_interactions,
        ):
            messages_exported += 1

    if prompts_exported == 0:
        _write(writer, "_No prompts matched the selected filters._")
        _write(writer)
    return ConversationMarkdownStats(
        prompts_seen=prompt_number,
        prompts_exported=prompts_exported,
        messages_exported=messages_exported,
    )
