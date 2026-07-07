"""Unified conversation parser — normalizes different JSONL formats into a common structure.

Supported formats:
- Claude Code: {type: "user"|"assistant"|"ai-title"|"system", message: {role, content}}
- Codex: {type: "response_item"|"event_msg"|"session_meta"|"turn_context", payload: {role, content: [{type, text}]}}
- OpenClaw: {type: "message", role: "user"|"assistant", content: "..."}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class NormalizedMessage:
    """A single conversation message in a unified format."""
    role: str           # "user", "assistant", "system", "tool"
    content: str        # Plain text content
    tool_name: str = "" # If role=="tool", the tool that was used
    tool_input: str = ""  # Tool input/command
    thinking: str = ""  # Optional thinking/reasoning text kept separate from final response
    timestamp: str = ""
    raw_type: str = ""  # Original message type


# Terminal programs commonly decorate matches and status text with ANSI CSI
# sequences (for example PowerShell Select-String emits ESC[7m / ESC[0m).
# Conversation viewers are not terminal emulators, so retaining these bytes
# produces visible replacement glyphs and misleading text.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\][^\x07]*(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
    r"|\x9B[0-?]*[ -/]*[@-~]"
)


def strip_terminal_sequences(text: str) -> str:
    """Remove ANSI CSI/OSC terminal control sequences from plain text."""
    return _ANSI_ESCAPE_RE.sub("", text)


def parse_conversation_line(raw_line: str, tool_id: str) -> NormalizedMessage | None:
    """Parse a single JSONL line into a NormalizedMessage, or None if it should be skipped."""
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    msg_type = obj.get("type", "")
    timestamp = obj.get("timestamp", "")

    # --- Claude Code format ---
    if tool_id == "claude_code":
        if msg_type in ("user", "assistant"):
            message = obj.get("message", {})
            role = message.get("role", msg_type)
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
                )

            # Claude's API represents a tool result as a message whose outer
            # role is "user".  It is not human input: the content blocks are
            # typed tool_result and must render as a tool card, otherwise large
            # terminal dumps become giant purple User bubbles.
            tool_result = _extract_tool_result_content(raw_content)
            if role == "user" and tool_result is not None:
                return NormalizedMessage(
                    role="tool",
                    content=tool_result or "(tool returned no textual output)",
                    tool_name="Tool result",
                    timestamp=timestamp,
                    raw_type="tool_result",
                )

            tool_use = _extract_tool_use(raw_content)
            if role == "assistant" and tool_use is not None:
                tool_name, tool_input = tool_use
                return NormalizedMessage(
                    role="tool",
                    content=f"[{tool_name}]",
                    tool_name=tool_name,
                    tool_input=tool_input,
                    timestamp=timestamp,
                    raw_type="tool_use",
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
            return NormalizedMessage(
                role=role, content=content, thinking=thinking,
                timestamp=timestamp, raw_type=msg_type,
            )

        if msg_type == "ai-title":
            return None  # Skip title lines

        if msg_type == "system":
            content = _extract_content(obj.get("message", {}).get("content", ""))
            if not content.strip() or "<command-name>" in content:
                return None  # Skip command metadata
            return NormalizedMessage(role="system", content=content, timestamp=timestamp, raw_type=msg_type)

        # Skip: file-history-snapshot, queue-operation, etc.
        return None

    # --- Codex format ---
    if tool_id == "codex":
        payload = obj.get("payload", {})

        if msg_type == "response_item":
            role = payload.get("role", "")
            if role in ("developer", "system"):
                return None  # Skip system prompts
            p_type = payload.get("type", "")
            # Skip reasoning — AI internal thought process, not a reply
            if p_type == "reasoning":
                return None
            # Skip assistant response_item/message — duplicates event_msg/agent_message
            if p_type == "message" and role == "assistant":
                return None
            # User response_item/message — real user input (not system context)
            if p_type == "message" and role == "user":
                content = _extract_codex_content(payload.get("content", []))
                if not content.strip():
                    return None
                # Skip Codex system context injections (not real user text)
                if content.lstrip().startswith("<environment_context>"):
                    return None
                if content.lstrip().startswith("<turn_aborted>"):
                    return None
                return NormalizedMessage(role="user", content=content, timestamp=timestamp, raw_type=msg_type)
            return None

        if msg_type == "event_msg":
            event_type = payload.get("type", "")
            if event_type == "task_started":
                return None
            # User message — the actual user input in Codex
            if event_type == "user_message":
                text = payload.get("message", "")
                if text.strip():
                    return NormalizedMessage(role="user", content=text, timestamp=timestamp, raw_type="user_message")
                return None
            # Agent message — intermediate commentary in new Codex, sole reply in old Codex.
            # Kept as assistant message; if task_complete also exists, ingest dedup handles it.
            if event_type == "agent_message":
                text = payload.get("message", "")
                if text.strip():
                    return NormalizedMessage(role="assistant", content=text, timestamp=timestamp, raw_type="agent_message")
                return None
            # Task complete — last_agent_message duplicates the last agent_message, skip
            if event_type == "task_complete":
                return None
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
                    msg_dict = json.loads(raw_msg)
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
            summary = obj.get("summary") or ""
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
            message = obj.get("message", {})
            role = message.get("role", msg_type)
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
            tool_name = obj.get("tool_name", "tool")
            tool_input = obj.get("tool_input", "")
            content = obj.get("content", f"[{tool_name}]")
            return NormalizedMessage(
                role="tool", content=content, tool_name=tool_name,
                tool_input=tool_input, timestamp=timestamp, raw_type=msg_type,
            )

        if msg_type == "system":
            message = obj.get("message", {})
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
        thinking = _extract_thinking_parts(raw_content)
        content = _extract_content(raw_content)
        # Strip <user_query> tags
        if content:
            content = content.replace("<user_query>", "").replace("</user_query>", "").strip()
        if role in ("user", "assistant") and content.strip():
            # Skip tool_result/tool_use noise
            if content.startswith("[Tool:") or content.startswith("[Result]"):
                return None
            return NormalizedMessage(
                role=role, content=content, thinking=thinking,
                timestamp=timestamp, raw_type=msg_type or role,
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


def _extract_tool_result_content(content) -> str | None:
    """Return Claude/OpenClaw tool-result text, or None if no such block exists."""
    if not isinstance(content, list):
        return None

    found = False
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in ("tool_result", "toolResult"):
            continue
        found = True
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
    return strip_terminal_sequences("\n\n".join(parts)).strip()


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


def _extract_tool_use(content) -> tuple[str, str] | None:
    """Return a standalone Claude tool invocation as (name, formatted input)."""
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
        return name, strip_terminal_sequences(tool_input).strip()
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
            text = item.get("thinking", "")
            if text:
                parts.append(text)
        elif t == "redacted_thinking":
            data = item.get("data", "")
            if data:
                parts.append(f"[redacted thinking: {len(data)} bytes]")
    return "\n\n".join(parts)


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
                    parts.append(item.get("text", ""))
                elif t in ("tool_use", "toolCall"):
                    # Claude uses tool_use + input; OpenClaw uses toolCall + arguments.
                    name = item.get("name", "tool")
                    inp = item.get("input") if "input" in item else item.get("arguments", {})
                    inp_str = json.dumps(inp, ensure_ascii=False) if not isinstance(inp, str) else inp
                    parts.append(f"[Tool: {name}]\n{inp_str}")
                elif t in ("tool_result", "toolResult"):
                    result = item.get("content", item.get("output", ""))
                    if isinstance(result, list):
                        result = " ".join(r.get("text", "") for r in result if isinstance(r, dict))
                    parts.append(f"[Result]\n{str(result)}")
            elif isinstance(item, str):
                parts.append(item)
        return _strip_system_tags("\n".join(parts))
    if isinstance(content, dict):
        return content.get("text", json.dumps(content, ensure_ascii=False))
    return str(content)


def _extract_codex_content(content_list) -> str:
    """Extract text from Codex content array: [{type: "input_text"|"output_text", text: "..."}]"""
    if isinstance(content_list, str):
        return content_list
    if not isinstance(content_list, list):
        return str(content_list)
    parts = []
    for item in content_list:
        if isinstance(item, dict):
            parts.append(item.get("text", ""))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _iter_json_objects(raw_content: str):
    """Yield JSON object source strings from mixed compact JSONL / pretty-
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
            # Re-serialize as a single compact line so downstream
            # parse_conversation_line (which expects one JSON per string)
            # works unchanged.
            yield json.dumps(obj, ensure_ascii=False)
            i = end
        except json.JSONDecodeError:
            # Couldn't parse starting here — advance to next newline and
            # retry. This handles truncated fragments / concatenation noise.
            next_nl = raw_content.find("\n", i)
            if next_nl < 0:
                break
            i = next_nl + 1


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
        outer = json.loads(content_str)
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


def _parse_hermes_session(raw_content: str, offset: int, limit: int | None) -> list[NormalizedMessage]:
    """Hermes stores a whole session as a single top-level JSON, not JSONL."""
    try:
        d = json.loads(raw_content)
    except json.JSONDecodeError:
        return []
    if not isinstance(d, dict):
        return []
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
        d = json.loads(raw_content)
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


def parse_conversation(raw_content: str, tool_id: str, offset: int = 0, limit: int | None = None) -> list[NormalizedMessage]:
    """Parse JSONL conversation into normalized messages. Supports pagination."""
    if tool_id == "hermes":
        return _parse_hermes_session(raw_content, offset, limit)
    import hashlib
    messages = []
    seen: set[str] = set()
    skipped = 0
    for line in _iter_json_objects(raw_content):
        if not line.strip():
            continue
        msg = parse_conversation_line(line.strip(), tool_id)
        if msg and msg.role in ("user", "assistant"):
            # Deduplicate: same role + content + timestamp (within same second)
            # Prevents event_msg/user_message and response_item/user duplicates
            ts_bucket = (msg.timestamp or "")[:19]
            dedupe_key = hashlib.md5(f"{msg.role}:{ts_bucket}:{msg.content}".encode()).hexdigest()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if skipped < offset:
                skipped += 1
                continue
            messages.append(msg)
            if limit and len(messages) >= limit:
                break
        elif msg:
            if skipped < offset:
                skipped += 1
                continue
            messages.append(msg)
            if limit and len(messages) >= limit:
                break
    return messages


def count_conversation_messages(raw_content: str, tool_id: str) -> int:
    """Count messages without building full list — memory efficient."""
    if tool_id == "hermes":
        return _count_hermes_messages(raw_content)
    import hashlib
    count = 0
    seen: set[str] = set()
    for line in _iter_json_objects(raw_content):
        if not line.strip():
            continue
        msg = parse_conversation_line(line.strip(), tool_id)
        if msg and msg.role in ("user", "assistant"):
            ts_bucket = (msg.timestamp or "")[:19]
            dedupe_key = hashlib.md5(f"{msg.role}:{ts_bucket}:{msg.content}".encode()).hexdigest()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            count += 1
        elif msg:
            count += 1
    return count
