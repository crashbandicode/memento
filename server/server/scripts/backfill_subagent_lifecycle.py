"""Guarded, idempotent repair for subagent lifecycle identity and runtime metadata.

The dry-run default computes the exact row-level plan and rolls it back. Apply
mode updates only lifecycle rows proven by source agent/tool-use identity and
exact Claude transcript/sidecar sibling paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import ConversationMessage, Document
from server.db.session import async_session_factory, engine
from server.services.document_delivery import delivery_metadata_expression
from server.services.ingest_service import _claude_subagent_sidecar_evidence
from server.services.large_content_store import document_content
from server.services.subagent_lifecycle import (
    SUBAGENT_TERMINAL_STATUSES,
    child_lifecycle_evidence,
    enrich_lifecycle_runtime,
    enrich_lifecycle_status,
    lifecycle_event_identity,
    merge_duplicate_lifecycle_events,
    normalized_subagent_status,
    persisted_child_lifecycle,
    reconcile_child_lifecycle_metadata,
    subagent_runtime_from_metadata,
)


@dataclass
class BackfillStats:
    scanned: int = 0
    child_documents_scanned: int = 0
    child_metadata_updated: int = 0
    lifecycle_rows_updated: int = 0
    model_recovered: int = 0
    effort_recovered: int = 0
    duplicate_events_coalesced: int = 0
    distinct_same_description_agents_preserved: int = 0
    missing_model_metadata: int = 0
    genuinely_active: int = 0
    completed: int = 0
    failed: int = 0
    cancelled_interrupted: int = 0
    unknown_disconnected: int = 0
    unchanged: int = 0
    repaired: int = 0


@dataclass(frozen=True)
class LifecycleRow:
    id: int
    document_id: uuid.UUID
    machine_id: uuid.UUID
    tool_id: str
    line_number: int
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LifecycleUpdate:
    id: int
    metadata: dict[str, Any]
    content: str


def _runtime_key(
    machine_id: uuid.UUID,
    tool_id: str,
    identity: object,
) -> tuple[uuid.UUID, str, str] | None:
    value = str(identity or "").strip()
    return (machine_id, tool_id, value) if value else None


def plan_lifecycle_repairs(
    rows: list[LifecycleRow],
    *,
    runtime_by_tool_use: dict[tuple[uuid.UUID, str, str], dict[str, str]],
    runtime_by_thread: dict[tuple[uuid.UUID, str, str], dict[str, str]],
    lifecycle_by_tool_use: dict[
        tuple[uuid.UUID, str, str],
        dict[str, str],
    ] | None = None,
    lifecycle_by_thread: dict[
        tuple[uuid.UUID, str, str],
        dict[str, str],
    ] | None = None,
) -> tuple[list[LifecycleUpdate], list[int], BackfillStats]:
    """Return deterministic updates/deletes for exact lifecycle identities."""
    stats = BackfillStats(scanned=len(rows))
    lifecycle_by_tool_use = lifecycle_by_tool_use or {}
    lifecycle_by_thread = lifecycle_by_thread or {}
    grouped: dict[
        tuple[uuid.UUID, str, str, str],
        list[LifecycleRow],
    ] = defaultdict(list)
    same_description: dict[
        tuple[uuid.UUID, str, str],
        set[str],
    ] = defaultdict(set)

    for row in rows:
        event = row.metadata.get("agent_event")
        identity = lifecycle_event_identity(
            event if isinstance(event, dict) else None
        )
        if identity is None:
            continue
        grouped[(row.document_id, *identity)].append(row)
        label = str(event.get("label") or "").strip().casefold()
        if label:
            same_description[
                (row.document_id, label, identity[2])
            ].add(identity[1])

    stats.distinct_same_description_agents_preserved = sum(
        len(identities)
        for identities in same_description.values()
        if len(identities) > 1
    )

    updates: list[LifecycleUpdate] = []
    deletes: list[int] = []
    resolved_agent_statuses: dict[tuple[uuid.UUID, str], str] = {}
    changed_agent_keys: set[tuple[uuid.UUID, str]] = set()
    for group_rows in grouped.values():
        ordered = sorted(group_rows, key=lambda item: (item.line_number, item.id))
        canonical = ordered[0]
        original_event = canonical.metadata["agent_event"]
        merged_event = dict(original_event)
        for duplicate in ordered[1:]:
            merged_event = merge_duplicate_lifecycle_events(
                merged_event,
                duplicate.metadata["agent_event"],
            )
            deletes.append(duplicate.id)

        tool_use_key = _runtime_key(
            canonical.machine_id,
            canonical.tool_id,
            merged_event.get("agent_tool_use_id"),
        )
        thread_key = _runtime_key(
            canonical.machine_id,
            canonical.tool_id,
            merged_event.get("agent_thread_id"),
        )
        runtime = (
            runtime_by_tool_use.get(tool_use_key) if tool_use_key else None
        ) or (
            runtime_by_thread.get(thread_key) if thread_key else None
        )
        lifecycle = (
            lifecycle_by_tool_use.get(tool_use_key) if tool_use_key else None
        ) or (
            lifecycle_by_thread.get(thread_key) if thread_key else None
        )
        enriched_event = enrich_lifecycle_status(
            enrich_lifecycle_runtime(merged_event, runtime),
            lifecycle,
        )
        agent_key = (
            canonical.document_id,
            str(
                merged_event.get("agent_tool_use_id")
                or merged_event.get("agent_thread_id")
                or merged_event.get("task_id")
                or ""
            ),
        )
        kind = str(merged_event.get("kind") or "").strip().casefold()
        event_status = normalized_subagent_status(kind)
        raw_status = normalized_subagent_status(merged_event.get("status"))
        if kind == "interrupted" and raw_status == "cancelled":
            event_status = "cancelled"
        resolved_status = normalized_subagent_status(
            enriched_event.get("resolved_status")
        ) or event_status or "unknown"
        previous_resolved = resolved_agent_statuses.get(agent_key)
        if (
            previous_resolved not in SUBAGENT_TERMINAL_STATUSES
            or resolved_status in SUBAGENT_TERMINAL_STATUSES
        ):
            resolved_agent_statuses[agent_key] = resolved_status
        if not original_event.get("model") and enriched_event.get("model"):
            stats.model_recovered += 1
        if (
            not original_event.get("reasoning_effort")
            and enriched_event.get("reasoning_effort")
        ):
            stats.effort_recovered += 1
        if not enriched_event.get("model"):
            stats.missing_model_metadata += 1

        metadata = dict(canonical.metadata)
        metadata["agent_event"] = enriched_event
        content = (
            f"{enriched_event.get('label') or 'Subagent'} "
            f"{enriched_event.get('kind') or 'updated'}"
        )
        if metadata != canonical.metadata or content != canonical.content:
            changed_agent_keys.add(agent_key)
            updates.append(LifecycleUpdate(
                id=canonical.id,
                metadata=metadata,
                content=content,
            ))

    stats.lifecycle_rows_updated = len(updates)
    stats.duplicate_events_coalesced = len(deletes)
    for status in resolved_agent_statuses.values():
        if status == "running":
            stats.genuinely_active += 1
        elif status == "completed":
            stats.completed += 1
        elif status == "failed":
            stats.failed += 1
        elif status in {"cancelled", "interrupted"}:
            stats.cancelled_interrupted += 1
        else:
            stats.unknown_disconnected += 1
    stats.repaired = len(changed_agent_keys)
    stats.unchanged = max(0, len(resolved_agent_statuses) - stats.repaired)
    return updates, deletes, stats


async def backfill_subagent_lifecycle(
    db: AsyncSession,
    *,
    document_ids: list[uuid.UUID] | None = None,
) -> BackfillStats:
    message_query = (
        select(ConversationMessage, Document.machine_id, Document.tool_id)
        .join(Document, Document.id == ConversationMessage.document_id)
        .where(
            Document.category == "conversation",
            ConversationMessage.metadata_.op("?")("agent_event"),
            or_(
                ConversationMessage.metadata_["agent_event"][
                    "activity_type"
                ].astext
                == "subagent",
                ConversationMessage.metadata_["agent_event"]["task_kind"].astext
                == "subagent",
            ),
        )
        .order_by(
            ConversationMessage.document_id,
            ConversationMessage.line_number,
        )
    )
    if document_ids:
        message_query = message_query.where(
            ConversationMessage.document_id.in_(document_ids)
        )
    message_results = (await db.execute(message_query)).all()
    rows = [
        LifecycleRow(
            id=message.id,
            document_id=message.document_id,
            machine_id=machine_id,
            tool_id=tool_id,
            line_number=message.line_number,
            content=message.content,
            metadata=(
                dict(message.metadata_)
                if isinstance(message.metadata_, dict)
                else {}
            ),
        )
        for message, machine_id, tool_id in message_results
    ]
    if not rows:
        return BackfillStats()

    machines = {row.machine_id for row in rows}
    tools = {row.tool_id for row in rows}
    child_documents = (
        await db.execute(
            select(Document).where(
                Document.machine_id.in_(machines),
                Document.tool_id.in_(tools),
                Document.category == "conversation",
            )
        )
    ).scalars().all()
    sidecars = (
        await db.execute(
            select(Document).where(
                Document.machine_id.in_(machines),
                Document.tool_id == "claude_code",
                Document.category == "state",
                delivery_metadata_expression()["is_subagent_meta"].astext
                == "true",
            )
        )
    ).scalars().all()
    sidecar_metadata: dict[
        tuple[uuid.UUID, str],
        dict[str, object],
    ] = {}
    for sidecar in sidecars:
        evidence = _claude_subagent_sidecar_evidence(
            sidecar.relative_path,
            await document_content(db, sidecar),
        )
        if evidence is None:
            continue
        transcript_path, launch_metadata = evidence
        sidecar_metadata[(sidecar.machine_id, transcript_path)] = launch_metadata

    runtime_by_tool_use: dict[
        tuple[uuid.UUID, str, str],
        dict[str, str],
    ] = {}
    runtime_by_thread: dict[
        tuple[uuid.UUID, str, str],
        dict[str, str],
    ] = {}
    lifecycle_by_tool_use: dict[
        tuple[uuid.UUID, str, str],
        dict[str, str],
    ] = {}
    lifecycle_by_thread: dict[
        tuple[uuid.UUID, str, str],
        dict[str, str],
    ] = {}
    target_tool_use_ids: set[str] = set()
    target_thread_ids: set[str] = set()
    for row in rows:
        event = row.metadata.get("agent_event")
        if not isinstance(event, dict):
            continue
        tool_use_id = str(event.get("agent_tool_use_id") or "").strip()
        thread_id = str(event.get("agent_thread_id") or "").strip()
        if tool_use_id:
            target_tool_use_ids.add(tool_use_id)
        if thread_id:
            target_thread_ids.add(thread_id)
            target_thread_ids.add(f"agent-{thread_id}")
    child_metadata_updated = 0
    child_documents_scanned = 0
    for child in child_documents:
        metadata = dict(child.metadata_ or {})
        launch_metadata = sidecar_metadata.get(
            (child.machine_id, child.relative_path),
            {},
        )
        is_subagent = bool(launch_metadata) or (
            metadata.get("is_subagent") is True
            or str(metadata.get("is_subagent") or "").strip().casefold() == "true"
            or str(metadata.get("thread_source") or "").strip().casefold()
            == "subagent"
        )
        if not is_subagent:
            continue
        merged_metadata = {**metadata, **launch_metadata}
        child_tool_use_id = str(
            merged_metadata.get("agent_tool_use_id") or ""
        ).strip()
        child_threads = {
            str(merged_metadata.get(field) or "").strip()
            for field in ("agent_id", "session_id", "thread_id")
        }
        child_threads.discard("")
        child_threads.update({
            value[len("agent-"):]
            for value in tuple(child_threads)
            if value.startswith("agent-")
        })
        if document_ids and not (
            child_tool_use_id in target_tool_use_ids
            or child_threads.intersection(target_thread_ids)
        ):
            continue
        child_documents_scanned += 1
        lifecycle = child_lifecycle_evidence(
            child.tool_id,
            merged_metadata,
            await document_content(db, child),
            source_timestamp=child.source_modified_at,
        )
        merged_metadata, _ = reconcile_child_lifecycle_metadata(
            merged_metadata,
            lifecycle,
        )
        lifecycle = persisted_child_lifecycle(merged_metadata)
        runtime = subagent_runtime_from_metadata(merged_metadata)
        if runtime.get("model"):
            merged_metadata["subagent_model"] = runtime["model"]
        if runtime.get("model_family"):
            merged_metadata["subagent_model_family"] = runtime["model_family"]
        if runtime.get("reasoning_effort"):
            merged_metadata["subagent_reasoning_effort"] = runtime[
                "reasoning_effort"
            ]
        if merged_metadata != metadata:
            child.metadata_ = merged_metadata
            child_metadata_updated += 1

        tool_use_key = _runtime_key(
            child.machine_id,
            child.tool_id,
            merged_metadata.get("agent_tool_use_id"),
        )
        if tool_use_key and runtime:
            runtime_by_tool_use[tool_use_key] = runtime
        if tool_use_key and lifecycle:
            lifecycle_by_tool_use[tool_use_key] = lifecycle
        for field in ("agent_id", "session_id", "thread_id"):
            identity = str(merged_metadata.get(field) or "").strip()
            if not identity:
                continue
            aliases = {identity}
            if identity.startswith("agent-"):
                aliases.add(identity[len("agent-"):])
            else:
                aliases.add(f"agent-{identity}")
            for alias in aliases:
                thread_key = _runtime_key(child.machine_id, child.tool_id, alias)
                if thread_key and runtime:
                    runtime_by_thread[thread_key] = runtime
                if thread_key and lifecycle:
                    lifecycle_by_thread[thread_key] = lifecycle

    updates, deletes, stats = plan_lifecycle_repairs(
        rows,
        runtime_by_tool_use=runtime_by_tool_use,
        runtime_by_thread=runtime_by_thread,
        lifecycle_by_tool_use=lifecycle_by_tool_use,
        lifecycle_by_thread=lifecycle_by_thread,
    )
    stats.child_documents_scanned = child_documents_scanned
    stats.child_metadata_updated = child_metadata_updated
    stats.repaired += child_metadata_updated

    messages_by_id = {message.id: message for message, _, _ in message_results}
    for planned in updates:
        message = messages_by_id[planned.id]
        message.metadata_ = planned.metadata
        message.content = planned.content
    if deletes:
        await db.execute(
            delete(ConversationMessage).where(
                ConversationMessage.id.in_(deletes)
            )
        )
    await db.flush()
    return stats


async def _run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None,
) -> BackfillStats:
    try:
        async with async_session_factory() as db:
            stats = await backfill_subagent_lifecycle(
                db,
                document_ids=document_ids,
            )
            if apply:
                await db.commit()
            else:
                await db.rollback()
            return stats
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the repair; the default is a rolled-back dry run",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="limit repair to an exact parent document UUID (repeatable)",
    )
    args = parser.parse_args()
    document_ids = [uuid.UUID(value) for value in args.document_id] or None
    stats = asyncio.run(_run(
        apply=args.apply,
        document_ids=document_ids,
    ))
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        **asdict(stats),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
