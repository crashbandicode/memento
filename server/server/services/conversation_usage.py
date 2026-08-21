"""Provider-neutral conversation token-usage normalization."""

from __future__ import annotations

from collections.abc import Mapping

TOKEN_USAGE_METADATA_KEY = "_assistant_token_usage"

_COUNT_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_token_usage(value: object) -> dict[str, object]:
    """Return the bounded, public token-usage contract or an empty mapping."""
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, object] = {
        key: _count(value.get(key))
        for key in _COUNT_FIELDS
        if _count(value.get(key)) > 0
    }
    source = str(value.get("source") or "").strip()[:32]
    if source:
        normalized["source"] = source
    if not normalized.get("input_tokens") and not normalized.get("output_tokens"):
        return {}
    if not normalized.get("total_tokens"):
        normalized["total_tokens"] = (
            _count(normalized.get("input_tokens"))
            + _count(normalized.get("output_tokens"))
        )
    return normalized


def codex_total_token_usage(payload: object) -> dict[str, object]:
    """Normalize Codex's latest cumulative ``total_token_usage`` snapshot."""
    if not isinstance(payload, Mapping):
        return {}
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return {}
    usage = info.get("total_token_usage")
    if not isinstance(usage, Mapping):
        return {}
    return normalize_token_usage({**usage, "source": "codex"})


def claude_message_token_usage(message: object) -> dict[str, object]:
    """Normalize one Claude API response's per-message usage.

    Claude reports uncached, cache-read, and cache-created prompt tokens as
    separate counters. ``input_tokens`` intentionally represents their sum so
    the UI's compact input/output pair is comparable across providers, while
    the exact breakdown remains available to MCP clients.
    """
    if not isinstance(message, Mapping):
        return {}
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return {}
    uncached = _count(usage.get("input_tokens"))
    cached = _count(usage.get("cache_read_input_tokens"))
    cache_write = _count(usage.get("cache_creation_input_tokens"))
    output = _count(usage.get("output_tokens"))
    details = usage.get("output_tokens_details")
    reasoning = (
        _count(details.get("thinking_tokens"))
        if isinstance(details, Mapping)
        else 0
    )
    return normalize_token_usage(
        {
            "source": "claude",
            "input_tokens": uncached + cached + cache_write,
            "uncached_input_tokens": uncached,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
        }
    )


def add_token_usage(
    current: object,
    addition: object,
) -> dict[str, object]:
    """Add one provider response to a cumulative conversation total."""
    left = normalize_token_usage(current)
    right = normalize_token_usage(addition)
    if not right:
        return left
    result: dict[str, object] = {
        key: _count(left.get(key)) + _count(right.get(key))
        for key in _COUNT_FIELDS
        if _count(left.get(key)) + _count(right.get(key)) > 0
    }
    result["source"] = str(right.get("source") or left.get("source") or "")
    return normalize_token_usage(result)


def token_usage_from_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, Mapping):
        return {}
    return normalize_token_usage(metadata.get(TOKEN_USAGE_METADATA_KEY))


def observe_token_usage(
    current: object,
    seen_source_ids: set[str],
    record: object,
    tool_id: str,
) -> dict[str, object]:
    """Advance usage from one native record using the live/backfill rules."""
    if not isinstance(record, Mapping):
        return normalize_token_usage(current)
    if tool_id == "codex":
        if record.get("type") != "event_msg":
            return normalize_token_usage(current)
        usage = codex_total_token_usage(record.get("payload"))
        return usage or normalize_token_usage(current)
    if tool_id != "claude_code" or record.get("type") != "assistant":
        return normalize_token_usage(current)

    message = record.get("message")
    usage = claude_message_token_usage(message)
    if not usage:
        return normalize_token_usage(current)
    message_mapping = message if isinstance(message, Mapping) else {}
    usage_id = str(
        message_mapping.get("id") or record.get("uuid") or ""
    ).strip()[:256]
    if usage_id and usage_id in seen_source_ids:
        return normalize_token_usage(current)
    if usage_id:
        seen_source_ids.add(usage_id)
    return add_token_usage(current, usage)
