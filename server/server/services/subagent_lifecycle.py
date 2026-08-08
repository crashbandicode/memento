"""Shared identity and runtime metadata rules for subagent lifecycle events."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


_MAX_MODEL_CHARS = 256
_MAX_EFFORT_CHARS = 128
SUBAGENT_LIFECYCLE_STATUS_KEY = "subagent_lifecycle_status"
SUBAGENT_LIFECYCLE_SOURCE_KEY = "subagent_lifecycle_source"
SUBAGENT_LIFECYCLE_AT_KEY = "subagent_lifecycle_at"
SUBAGENT_LIFECYCLE_EVIDENCE_KEY = "subagent_lifecycle_evidence"
SUBAGENT_TERMINAL_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "interrupted",
})
SUBAGENT_VISIBLE_STATUSES = frozenset({
    "running",
    *SUBAGENT_TERMINAL_STATUSES,
    "unknown",
    "disconnected",
})


def _safe_scalar(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        char for char in value.strip() if ord(char) >= 32 and ord(char) != 127
    )
    return cleaned[:limit] or None


def normalized_subagent_status(value: object) -> str | None:
    """Normalize only source-backed lifecycle states used by the public API."""
    status = (_safe_scalar(value, 80) or "").casefold().replace("-", "_")
    aliases = {
        "active": "running",
        "async_launched": "running",
        "background": "running",
        "in_progress": "running",
        "pending": "running",
        "started": "running",
        "working": "running",
        "complete": "completed",
        "done": "completed",
        "finished": "completed",
        "success": "completed",
        "succeeded": "completed",
        "error": "failed",
        "errored": "failed",
        "canceled": "cancelled",
        "aborted": "interrupted",
        "stopped": "interrupted",
        "missing": "disconnected",
        "source_missing": "disconnected",
    }
    normalized = aliases.get(status, status)
    return normalized if normalized in SUBAGENT_VISIBLE_STATUSES else None


def _lifecycle_timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_scalar(value, 128)


def _lifecycle_evidence(
    status: str,
    *,
    source: str,
    timestamp: object = None,
    evidence: object = None,
) -> dict[str, str]:
    result = {
        "status": status,
        "source": source,
    }
    safe_timestamp = _lifecycle_timestamp(timestamp)
    safe_evidence = _safe_scalar(evidence, 256)
    if safe_timestamp:
        result["timestamp"] = safe_timestamp
    if safe_evidence:
        result["evidence"] = safe_evidence
    return result


def _jsonl_objects(content: object) -> list[dict[str, Any]]:
    if not isinstance(content, str) or not content:
        return []
    records: list[dict[str, Any]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            records.append(decoded)
    return records


def _claude_child_lifecycle(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, str] | None:
    terminal: dict[str, str] | None = None
    for record in records:
        timestamp = record.get("timestamp")
        record_type = str(record.get("type") or "").strip().casefold()
        message = record.get("message")
        message = message if isinstance(message, dict) else {}
        stop_reason = str(message.get("stop_reason") or "").strip().casefold()
        if record_type == "assistant" and stop_reason == "end_turn":
            terminal = _lifecycle_evidence(
                "completed",
                source="claude_child_transcript",
                timestamp=timestamp,
                evidence="assistant.stop_reason=end_turn",
            )
        elif record_type == "assistant" and (
            record.get("isApiErrorMessage") is True
            or record.get("is_api_error_message") is True
        ):
            terminal = _lifecycle_evidence(
                "failed",
                source="claude_child_transcript",
                timestamp=timestamp,
                evidence="assistant.isApiErrorMessage=true",
            )
        elif record_type == "system":
            subtype = str(
                record.get("subtype") or record.get("event_type") or ""
            ).strip().casefold()
            if subtype in {"turn_aborted", "cancelled", "canceled"}:
                status = "cancelled" if "cancel" in subtype else "interrupted"
                terminal = _lifecycle_evidence(
                    status,
                    source="claude_child_transcript",
                    timestamp=timestamp,
                    evidence=f"system.subtype={subtype}",
                )
            elif subtype in {"api_error", "fatal_error"}:
                terminal = _lifecycle_evidence(
                    "failed",
                    source="claude_child_transcript",
                    timestamp=timestamp,
                    evidence=f"system.subtype={subtype}",
                )
    return terminal


def _codex_child_lifecycle(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, str] | None:
    latest: dict[str, str] | None = None
    for record in records:
        if str(record.get("type") or "").strip().casefold() != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "").strip().casefold()
        timestamp = record.get("timestamp")
        if event_type == "task_started" and latest is None:
            latest = _lifecycle_evidence(
                "running",
                source="codex_child_transcript",
                timestamp=timestamp,
                evidence="event_msg.task_started",
            )
        elif event_type == "task_complete":
            latest = _lifecycle_evidence(
                "completed",
                source="codex_child_transcript",
                timestamp=timestamp,
                evidence="event_msg.task_complete",
            )
        elif event_type == "turn_aborted":
            reason = str(payload.get("reason") or "").strip().casefold()
            status = "cancelled" if "cancel" in reason else "interrupted"
            latest = _lifecycle_evidence(
                status,
                source="codex_child_transcript",
                timestamp=timestamp,
                evidence=(
                    f"event_msg.turn_aborted:{reason}"
                    if reason
                    else "event_msg.turn_aborted"
                ),
            )
        elif event_type in {"task_failed", "turn_failed"}:
            latest = _lifecycle_evidence(
                "failed",
                source="codex_child_transcript",
                timestamp=timestamp,
                evidence=f"event_msg.{event_type}",
            )
    return latest


def child_lifecycle_evidence(
    tool_id: object,
    metadata: Mapping[str, Any] | None,
    content: object = None,
    *,
    source_timestamp: object = None,
) -> dict[str, str] | None:
    """Return authoritative child state, never an age-based lifecycle guess."""
    values = metadata or {}
    missing_state = normalized_subagent_status(
        values.get("subagent_source_state")
    )
    if (
        missing_state == "disconnected"
        and values.get("subagent_source_state_authoritative") is True
    ):
        return _lifecycle_evidence(
            "disconnected",
            source="collector_source_inventory",
            timestamp=(
                values.get("subagent_source_state_at") or source_timestamp
            ),
            evidence="authoritative child source missing",
        )

    tool = str(tool_id or "").strip().casefold()
    if tool == "cursor":
        status = normalized_subagent_status(values.get("composer_status"))
        if status:
            return _lifecycle_evidence(
                status,
                source="cursor_composer_state",
                timestamp=values.get("last_timestamp") or source_timestamp,
                evidence=f"composer_status={values.get('composer_status')}",
            )
    if tool == "claude_code":
        return _claude_child_lifecycle(_jsonl_objects(content))
    if tool == "codex":
        return _codex_child_lifecycle(_jsonl_objects(content))
    return None


def child_lifecycle_evidence_from_objects(
    tool_id: object,
    metadata: Mapping[str, Any] | None,
    records: Iterable[object],
    *,
    source_timestamp: object = None,
) -> dict[str, str] | None:
    """Stream authoritative child-state evidence from decoded JSONL records."""
    values = metadata or {}
    missing_state = normalized_subagent_status(
        values.get("subagent_source_state")
    )
    if (
        missing_state == "disconnected"
        and values.get("subagent_source_state_authoritative") is True
    ):
        return _lifecycle_evidence(
            "disconnected",
            source="collector_source_inventory",
            timestamp=(
                values.get("subagent_source_state_at") or source_timestamp
            ),
            evidence="authoritative child source missing",
        )

    tool = str(tool_id or "").strip().casefold()
    if tool == "cursor":
        status = normalized_subagent_status(values.get("composer_status"))
        if status:
            return _lifecycle_evidence(
                status,
                source="cursor_composer_state",
                timestamp=values.get("last_timestamp") or source_timestamp,
                evidence=f"composer_status={values.get('composer_status')}",
            )
        return None

    mappings = (
        record for record in records if isinstance(record, Mapping)
    )
    if tool == "claude_code":
        return _claude_child_lifecycle(mappings)
    if tool == "codex":
        return _codex_child_lifecycle(mappings)
    return None


def persisted_child_lifecycle(
    metadata: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Read server-owned child lifecycle metadata for API reconciliation."""
    values = metadata or {}
    status = normalized_subagent_status(
        values.get(SUBAGENT_LIFECYCLE_STATUS_KEY)
    )
    source = _safe_scalar(values.get(SUBAGENT_LIFECYCLE_SOURCE_KEY), 128)
    if not status or not source:
        return None
    result = {
        "status": status,
        "source": source,
    }
    timestamp = _lifecycle_timestamp(values.get(SUBAGENT_LIFECYCLE_AT_KEY))
    evidence = _safe_scalar(
        values.get(SUBAGENT_LIFECYCLE_EVIDENCE_KEY),
        256,
    )
    if timestamp:
        result["timestamp"] = timestamp
    if evidence:
        result["evidence"] = evidence
    return result


def reconcile_child_lifecycle_metadata(
    metadata: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Persist authoritative state while preventing terminal regression."""
    reconciled = dict(metadata or {})
    if not evidence:
        return reconciled, False
    status = normalized_subagent_status(evidence.get("status"))
    source = _safe_scalar(evidence.get("source"), 128)
    if not status or not source:
        return reconciled, False

    current = persisted_child_lifecycle(reconciled)
    if (
        current
        and current["status"] in SUBAGENT_TERMINAL_STATUSES
        and status not in SUBAGENT_TERMINAL_STATUSES
    ):
        return reconciled, False

    reconciled[SUBAGENT_LIFECYCLE_STATUS_KEY] = status
    reconciled[SUBAGENT_LIFECYCLE_SOURCE_KEY] = source
    timestamp = _lifecycle_timestamp(evidence.get("timestamp"))
    if timestamp:
        reconciled[SUBAGENT_LIFECYCLE_AT_KEY] = timestamp
    evidence_label = _safe_scalar(evidence.get("evidence"), 256)
    if evidence_label:
        reconciled[SUBAGENT_LIFECYCLE_EVIDENCE_KEY] = evidence_label
    return reconciled, reconciled != dict(metadata or {})


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


def enrich_lifecycle_status(
    event: Mapping[str, Any],
    lifecycle: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach authoritative child resolution without rewriting source event kind."""
    enriched = dict(event)
    kind_status = normalized_subagent_status(event.get("kind"))
    current = normalized_subagent_status(event.get("resolved_status"))
    if kind_status in SUBAGENT_TERMINAL_STATUSES:
        current = kind_status
    incoming = normalized_subagent_status(
        (lifecycle or {}).get("status")
    )
    if (
        current in SUBAGENT_TERMINAL_STATUSES
        and incoming not in SUBAGENT_TERMINAL_STATUSES
    ):
        incoming = current
    if not incoming:
        return enriched

    enriched["resolved_status"] = incoming
    source = _safe_scalar((lifecycle or {}).get("source"), 128)
    timestamp = _lifecycle_timestamp((lifecycle or {}).get("timestamp"))
    if source:
        enriched["status_source"] = source
    if timestamp:
        enriched["status_updated_at"] = timestamp
        if incoming in SUBAGENT_TERMINAL_STATUSES:
            enriched["completed_at"] = timestamp
    return enriched


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
