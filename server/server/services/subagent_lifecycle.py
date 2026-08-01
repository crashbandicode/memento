"""Shared identity and runtime metadata rules for subagent lifecycle events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_MAX_MODEL_CHARS = 256
_MAX_EFFORT_CHARS = 128


def _safe_scalar(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        char for char in value.strip() if ord(char) >= 32 and ord(char) != 127
    )
    return cleaned[:limit] or None


def subagent_model_family(model: object) -> str | None:
    """Return a small provider family only when the model name proves it."""
    value = (_safe_scalar(model, _MAX_MODEL_CHARS) or "").casefold()
    if not value:
        return None
    if "claude" in value or "anthropic" in value:
        return "anthropic"
    if (
        "openai" in value
        or "codex" in value
        or value.startswith("gpt-")
        or value.startswith(("o1", "o3", "o4"))
    ):
        return "openai"
    if "grok" in value or "xai" in value:
        return "xai"
    if "gemini" in value or "google" in value:
        return "google"
    return None


def normalized_subagent_runtime(
    *,
    model: object = None,
    reasoning_effort: object = None,
) -> dict[str, str]:
    """Normalize authoritative runtime fields without guessing missing values."""
    normalized: dict[str, str] = {}
    safe_model = _safe_scalar(model, _MAX_MODEL_CHARS)
    safe_effort = _safe_scalar(reasoning_effort, _MAX_EFFORT_CHARS)
    if safe_model:
        normalized["model"] = safe_model
        family = subagent_model_family(safe_model)
        if family:
            normalized["model_family"] = family
    if safe_effort:
        normalized["reasoning_effort"] = safe_effort
    return normalized


def subagent_runtime_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Recover actual child runtime identity from persisted source metadata."""
    values = metadata or {}
    model = (
        values.get("subagent_model")
        or values.get("_assistant_model")
        or values.get("model")
    )
    effort = (
        values.get("subagent_reasoning_effort")
        or values.get("_assistant_reasoning_effort")
        or values.get("reasoning_effort")
    )
    return normalized_subagent_runtime(
        model=model,
        reasoning_effort=effort,
    )


def lifecycle_event_identity(
    event: Mapping[str, Any] | None,
) -> tuple[str, str, str] | None:
    """Return the source lifecycle identity, never a human-description key."""
    if not event:
        return None
    activity_type = str(event.get("activity_type") or "subagent").strip().casefold()
    if activity_type != "subagent":
        return None
    source_identity = str(
        event.get("agent_tool_use_id")
        or event.get("agent_thread_id")
        or event.get("task_id")
        or ""
    ).strip()
    kind = str(event.get("kind") or "").strip().casefold()
    if not source_identity or not kind or kind == "snapshot":
        return None
    return activity_type, source_identity, kind


def merge_duplicate_lifecycle_events(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    """Coalesce two proven-identical source events while retaining enrichment."""
    first_identity = lifecycle_event_identity(first)
    if first_identity is None or first_identity != lifecycle_event_identity(second):
        raise ValueError("lifecycle events do not share one source identity")

    merged = {**first, **second}
    first_label = str(first.get("label") or "").strip()
    second_label = str(second.get("label") or "").strip()
    if first_label and (not second_label or second_label == "Subagent"):
        merged["label"] = first_label

    if first_identity[2] == "started":
        first_started_at = first.get("started_at")
        if first_started_at:
            merged["started_at"] = first_started_at

    runtime = normalized_subagent_runtime(
        model=second.get("model") or first.get("model"),
        reasoning_effort=(
            second.get("reasoning_effort") or first.get("reasoning_effort")
        ),
    )
    merged.update(runtime)
    return merged


def enrich_lifecycle_runtime(
    event: Mapping[str, Any],
    runtime: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Overlay authoritative child runtime metadata onto one lifecycle event."""
    if not runtime:
        return dict(event)
    enriched = dict(event)
    normalized = normalized_subagent_runtime(
        model=runtime.get("model"),
        reasoning_effort=runtime.get("reasoning_effort"),
    )
    enriched.update(normalized)
    return enriched
