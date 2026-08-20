"""Idempotent cross-tool orchestration lifecycle reconciliation."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ConversationMessage,
    ConversationReadModel,
    DashboardDocumentProjection,
    Document,
    OrchestrationAgent,
    OrchestrationEventReceipt,
    OrchestrationRun,
)
from ..db.session import queue_realtime_event
from .document_delivery import document_metadata, store_document_metadata


_RUN_KEY_PATTERN = re.compile(
    r"(?:orchestrationRunId|orchestration_run_id)"
    r"(?:\\?[\"'])?(?:\*{1,2})?\s*[:=]\s*"
    r"(?:\*{1,2})?\s*(?:\\?[\"'`])?\s*"
    r"([A-Za-z0-9][A-Za-z0-9_.:-]{7,255})",
    re.IGNORECASE,
)
_ENGINE_TOOL_IDS = {
    "claude": "claude_code",
    "codex": "codex",
    "codex-app": "codex",
    "cursor": "cursor",
}
_RUN_TERMINAL = frozenset({"completed", "failed", "aborted"})
_AGENT_TERMINAL = frozenset({"completed", "failed", "aborted"})
_PROJECTION_METADATA_KEYS = frozenset(
    {
        "orchestration",
        "orchestration_run_id",
        "orchestration_run_kind",
        "orchestration_parent_document_id",
        "orchestration_relation_resolved",
        "orchestration_agent_key",
        "orchestration_agent_name",
        "orchestration_agent_codename",
        "is_subagent",
        "agent_id",
        "agent_tool_use_id",
        "agent_nickname",
        "agent_launch_description",
        "agent_path",
        "agent_depth",
        "subagent_model",
        "subagent_reasoning_effort",
        "subagent_lifecycle_status",
        "subagent_lifecycle_source",
        "subagent_lifecycle_at",
        "subagent_lifecycle_evidence",
    }
)


def extract_orchestration_run_ids(value: object) -> set[str]:
    """Extract only explicit correlation fields, including nested JSON strings."""
    found: set[str] = set()
    seen_strings: set[str] = set()

    def visit(item: object, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if key in {"orchestrationRunId", "orchestration_run_id"}:
                    text = str(nested or "").strip()
                    if 8 <= len(text) <= 256:
                        found.add(text)
                else:
                    visit(nested, depth + 1)
            return
        if isinstance(item, list):
            for nested in item[:256]:
                visit(nested, depth + 1)
            return
        if not isinstance(item, str) or not item or item in seen_strings:
            return
        seen_strings.add(item)
        found.update(match.group(1) for match in _RUN_KEY_PATTERN.finditer(item))
        stripped = item.strip()
        if stripped[:1] not in {"{", "[", '"'}:
            return
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if decoded != item:
            visit(decoded, depth + 1)

    visit(value)
    return found


def _event_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _native_id_candidates(engine: str, native_session_id: str) -> set[str]:
    candidates = {native_session_id}
    if engine == "cursor" and native_session_id.startswith("cursor-live-"):
        candidates.add(native_session_id.removeprefix("cursor-live-"))
    return {candidate for candidate in candidates if candidate}


def _normalized_agent_status(status: str) -> str:
    return {
        "declared": "running",
        "idle": "running",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "aborted": "interrupted",
    }.get(status, "unknown")


def orchestration_agent_summary(
    run: OrchestrationRun,
    agent: OrchestrationAgent,
) -> dict[str, object]:
    """Project one authoritative orchestration agent into the shared card shape."""
    status = _normalized_agent_status(agent.status)
    terminal = status in {"completed", "failed", "interrupted"}
    return {
        "id": str(agent.document_id) if agent.document_id else None,
        "session_id": agent.native_session_id,
        "agent_id": agent.native_session_id,
        "agent_tool_use_id": f"{run.external_run_id}:{agent.agent_key}",
        "title": agent.agent_name or agent.codename or "Delegated agent",
        "agent_nickname": agent.codename,
        "orchestration": run.orchestrator,
        "orchestration_run_id": run.external_run_id,
        "orchestration_run_kind": run.run_kind,
        "orchestration_agent_key": agent.agent_key,
        "tool_id": _ENGINE_TOOL_IDS.get(agent.engine),
        "agent_path": f"{run.orchestrator}/{run.run_kind}/{agent.agent_key}",
        "agent_depth": 1,
        "parent_thread_id": None,
        "relative_path": None,
        "timestamp": agent.last_event_at.isoformat(),
        "activity_at": agent.last_event_at.isoformat(),
        "synced_at": None,
        "document_ready": bool(agent.document_id),
        "user_role_origin": "parent_agent",
        "model": agent.model,
        "model_family": None,
        "reasoning_effort": agent.effort,
        "status": status,
        "status_source": f"{run.orchestrator}_orchestrator",
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": (
            (run.ended_at or agent.last_event_at).isoformat()
            if terminal
            else None
        ),
        "last_event_at": agent.last_event_at.isoformat(),
    }


async def _find_native_document(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    engine: str,
    native_session_id: str,
) -> uuid.UUID | None:
    tool_id = _ENGINE_TOOL_IDS.get(engine)
    if not tool_id:
        return None
    candidates = sorted(_native_id_candidates(engine, native_session_id))
    document_id = (
        await db.execute(
            select(ConversationReadModel.document_id)
            .where(
                ConversationReadModel.machine_id == machine_id,
                ConversationReadModel.tool_id == tool_id,
                ConversationReadModel.thread_id.in_(candidates),
            )
            .order_by(ConversationReadModel.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if document_id is not None:
        return document_id

    return (
        await db.execute(
            select(Document.id)
            .where(
                Document.machine_id == machine_id,
                Document.tool_id == tool_id,
                Document.category == "conversation",
                or_(
                    Document.metadata_["session_id"].astext.in_(candidates),
                    Document.metadata_["thread_id"].astext.in_(candidates),
                ),
            )
            .order_by(Document.synced_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _find_parent_document(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    external_run_id: str,
) -> uuid.UUID | None:
    rows = (
        await db.execute(
            select(ConversationMessage.document_id, ConversationMessage.content)
            .join(Document, Document.id == ConversationMessage.document_id)
            .where(
                Document.machine_id == machine_id,
                Document.category == "conversation",
                ConversationMessage.content.contains(external_run_id),
            )
            .order_by(ConversationMessage.id.desc())
            .limit(16)
        )
    ).all()
    for document_id, content in rows:
        if external_run_id in extract_orchestration_run_ids(content):
            return document_id
    return None


async def _apply_agent_projection(
    db: AsyncSession,
    run: OrchestrationRun,
    agent: OrchestrationAgent,
) -> bool:
    """Mirror a proven normalized relation into existing read projections."""
    if run.parent_document_id is None or agent.document_id is None:
        return False
    if run.parent_document_id == agent.document_id:
        return False
    child = await db.get(Document, agent.document_id)
    if child is None:
        return False

    metadata = document_metadata(child)
    projected = {
        **metadata,
        "orchestration": "claw",
        "orchestration_run_id": run.external_run_id,
        "orchestration_run_kind": run.run_kind,
        "orchestration_parent_document_id": str(run.parent_document_id),
        "orchestration_relation_resolved": True,
        "orchestration_agent_key": agent.agent_key,
        "orchestration_agent_name": agent.agent_name,
        "orchestration_agent_codename": agent.codename,
        "is_subagent": True,
        "agent_id": agent.native_session_id,
        "agent_tool_use_id": f"{run.external_run_id}:{agent.agent_key}",
        "agent_nickname": agent.codename,
        "agent_launch_description": agent.agent_name,
        "agent_path": f"claw/{run.run_kind}/{agent.agent_key}",
        "agent_depth": 1,
        "subagent_model": agent.model,
        "subagent_reasoning_effort": agent.effort,
        "subagent_lifecycle_status": _normalized_agent_status(agent.status),
        "subagent_lifecycle_source": "claw_orchestrator",
        "subagent_lifecycle_at": agent.last_event_at.isoformat(),
        "subagent_lifecycle_evidence": f"claw.agent.status={agent.status}",
    }
    projected = {key: value for key, value in projected.items() if value is not None}
    changed = store_document_metadata(child, projected)

    read_model = await db.get(ConversationReadModel, child.id)
    if read_model is not None and not read_model.is_subagent:
        read_model.is_subagent = True
        read_model.agent_depth = max(1, int(read_model.agent_depth or 0))
        changed = True
    dashboard = await db.get(DashboardDocumentProjection, child.id)
    if dashboard is not None:
        hierarchy_metadata = dict(dashboard.hierarchy_metadata or {})
        projection_patch = {
            key: value
            for key, value in projected.items()
            if key in _PROJECTION_METADATA_KEYS
        }
        merged_hierarchy = {**hierarchy_metadata, **projection_patch}
        if merged_hierarchy != hierarchy_metadata or not dashboard.is_subagent:
            dashboard.hierarchy_metadata = merged_hierarchy
            dashboard.is_subagent = True
            changed = True
    if changed:
        for document_id in {run.parent_document_id, child.id}:
            queue_realtime_event(
                db,
                "file_synced",
                {
                    "document_id": str(document_id),
                    "changes": ["conversation.metadata", "dashboard", "project"],
                },
                user_id=str(run.user_id),
            )
    return changed


async def _reconcile_run_documents(db: AsyncSession, run: OrchestrationRun) -> int:
    changed = 0
    if run.parent_document_id is None:
        run.parent_document_id = await _find_parent_document(
            db,
            machine_id=run.machine_id,
            external_run_id=run.external_run_id,
        )
    agents = (
        await db.execute(
            select(OrchestrationAgent)
            .where(OrchestrationAgent.run_id == run.id)
            .order_by(OrchestrationAgent.created_at)
        )
    ).scalars().all()
    for agent in agents:
        if agent.document_id is None and agent.native_session_id:
            agent.document_id = await _find_native_document(
                db,
                machine_id=run.machine_id,
                engine=agent.engine,
                native_session_id=agent.native_session_id,
            )
        changed += int(await _apply_agent_projection(db, run, agent))
    return changed


async def ingest_orchestration_events(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    user_id: uuid.UUID,
    events: Iterable[Mapping[str, object]],
) -> dict[str, int]:
    accepted = duplicates = linked = 0
    touched_runs: dict[uuid.UUID, OrchestrationRun] = {}
    for event in events:
        occurred_at = _event_time(event["occurred_at"])
        receipt_id = (
            await db.execute(
                pg_insert(OrchestrationEventReceipt)
                .values(
                    machine_id=machine_id,
                    event_id=str(event["event_id"]),
                    occurred_at=occurred_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["machine_id", "event_id"]
                )
                .returning(OrchestrationEventReceipt.id)
            )
        ).scalar_one_or_none()
        if receipt_id is None:
            duplicates += 1
            continue

        run_seed_id = uuid.uuid4()
        await db.execute(
            pg_insert(OrchestrationRun)
            .values(
                id=run_seed_id,
                machine_id=machine_id,
                user_id=user_id,
                installation_id=str(event["installation_id"]),
                external_run_id=str(event["run_id"]),
                orchestrator=str(event["orchestrator"]),
                orchestrator_version=str(event.get("orchestrator_version") or "unknown"),
                run_kind=str(event["run_kind"]),
                status=str(event.get("run_status") or "running"),
                started_at=occurred_at,
                last_event_at=occurred_at,
            )
            .on_conflict_do_nothing(
                index_elements=["machine_id", "installation_id", "external_run_id"]
            )
        )
        run = (
            await db.execute(
                select(OrchestrationRun)
                .where(
                    OrchestrationRun.machine_id == machine_id,
                    OrchestrationRun.installation_id == str(event["installation_id"]),
                    OrchestrationRun.external_run_id == str(event["run_id"]),
                )
                .with_for_update()
            )
        ).scalar_one()
        if occurred_at >= run.last_event_at:
            run.orchestrator_version = str(event.get("orchestrator_version") or "unknown")
            run.run_kind = str(event["run_kind"])
            if event.get("run_status"):
                run.status = str(event["run_status"])
                if run.status in _RUN_TERMINAL:
                    run.ended_at = occurred_at
            run.last_event_at = occurred_at
        touched_runs[run.id] = run

        agent_key = str(event.get("agent_key") or "").strip()
        if agent_key and agent_key != "__run__":
            agent_seed_id = uuid.uuid4()
            await db.execute(
                pg_insert(OrchestrationAgent)
                .values(
                    id=agent_seed_id,
                    run_id=run.id,
                    agent_key=agent_key,
                    agent_name=str(event.get("agent_name") or agent_key),
                    codename=event.get("codename"),
                    engine=str(event.get("engine") or "unknown"),
                    model=event.get("model"),
                    effort=event.get("effort"),
                    cwd=event.get("cwd"),
                    status=str(event.get("agent_status") or "declared"),
                    native_session_id=event.get("native_session_id"),
                    last_event_at=occurred_at,
                )
                .on_conflict_do_nothing(index_elements=["run_id", "agent_key"])
            )
            agent = (
                await db.execute(
                    select(OrchestrationAgent)
                    .where(
                        OrchestrationAgent.run_id == run.id,
                        OrchestrationAgent.agent_key == agent_key,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if occurred_at >= agent.last_event_at:
                agent.agent_name = str(event.get("agent_name") or agent.agent_name)
                agent.codename = str(event.get("codename") or "").strip() or agent.codename
                agent.engine = str(event.get("engine") or agent.engine)
                agent.model = str(event.get("model") or "").strip() or agent.model
                agent.effort = str(event.get("effort") or "").strip() or agent.effort
                agent.cwd = str(event.get("cwd") or "").strip() or agent.cwd
                agent.status = str(event.get("agent_status") or agent.status)
                agent.native_session_id = (
                    str(event.get("native_session_id") or "").strip()
                    or agent.native_session_id
                )
                agent.last_event_at = occurred_at
        accepted += 1

    await db.flush()
    for run in touched_runs.values():
        linked += await _reconcile_run_documents(db, run)
    await db.flush()
    return {"accepted": accepted, "duplicates": duplicates, "linked": linked}


async def reconcile_orchestration_for_document(
    db: AsyncSession,
    document: Document,
) -> int:
    """Retry both parent and child linkage when a native transcript advances."""
    if document.category != "conversation" or document.machine_id is None:
        return 0
    run_ids = set()
    rows = (
        await db.execute(
            select(ConversationMessage.content).where(
                ConversationMessage.document_id == document.id,
                or_(
                    ConversationMessage.content.contains("orchestrationRunId"),
                    ConversationMessage.content.contains("orchestration_run_id"),
                ),
            )
        )
    ).scalars().all()
    for content in rows:
        run_ids.update(extract_orchestration_run_ids(content))
    tool_engine = {
        "claude_code": "claude",
        "codex": "codex",
        "cursor": "cursor",
    }.get(document.tool_id)
    metadata = document_metadata(document)
    native_ids = {
        str(value).strip()
        for value in (
            metadata.get("session_id"),
            metadata.get("thread_id"),
            metadata.get("conversation_id"),
        )
        if value
    }
    read_model = await db.get(ConversationReadModel, document.id)
    if read_model is not None and read_model.thread_id:
        native_ids.add(str(read_model.thread_id))

    predicates = [
        OrchestrationRun.parent_document_id == document.id,
    ]
    if run_ids:
        predicates.append(OrchestrationRun.external_run_id.in_(sorted(run_ids)))
    candidate_run_ids: set[uuid.UUID] = set()
    if tool_engine and native_ids:
        candidate_run_ids.update(
            (
                await db.execute(
                    select(OrchestrationAgent.run_id).where(
                        OrchestrationAgent.engine == tool_engine,
                        OrchestrationAgent.native_session_id.in_(sorted(native_ids)),
                    )
                )
            ).scalars().all()
        )
    if candidate_run_ids:
        predicates.append(OrchestrationRun.id.in_(sorted(candidate_run_ids)))
    candidate_runs = (
        await db.execute(
            select(OrchestrationRun).where(
                OrchestrationRun.machine_id == document.machine_id,
                or_(*predicates),
            )
        )
    ).scalars().all()
    for run in candidate_runs:
        if run.external_run_id in run_ids and run.parent_document_id is None:
            run.parent_document_id = document.id
    changed = 0
    for run in candidate_runs:
        changed += await _reconcile_run_documents(db, run)
    return changed
