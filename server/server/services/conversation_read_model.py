"""Ingest-owned projections for bounded conversation refresh reads."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ..db.models import (
    ConversationMessage,
    ConversationPromptProjection,
    ConversationReadModel,
    Document,
)
from .conversation_hierarchy import (
    conversation_root_thread_id,
    current_thread_id,
    is_conversation_subagent,
)
from .conversation_markdown import (
    is_meaningful_human_prompt,
    is_meaningful_human_turn,
)
from .conversation_parser import (
    build_cursor_question_response,
    is_meta_tool_interaction,
    normalize_tool_calls,
)
from .conversation_usage import (
    LAST_ACTIVITY_AT_METADATA_KEY,
    STARTED_AT_METADATA_KEY,
    token_usage_from_metadata,
)
from .subagent_lifecycle import (
    lifecycle_event_identity,
    merge_duplicate_lifecycle_events,
    persisted_child_lifecycle,
    subagent_runtime_from_metadata,
)

READ_MODEL_VERSION = 1
READ_MODEL_BACKFILL_DOCUMENT_BATCH_SIZE = 100
READ_MODEL_BACKFILL_MESSAGE_BATCH_SIZE = 1_000
MAX_PENDING_INTERACTIONS = 64
MAX_INFERRED_RESPONSES = 64
MAX_LIVE_ACTIVITIES = 64
MAX_AGENT_EVENTS = 256
_SHELL_TOOL_NAMES = {
    "bash",
    "execcommand",
    "powershell",
    "runterminalcommand",
    "runterminalcommandv2",
    "shell",
    "shellcommand",
    "terminal",
}
_TERMINAL_TOOL_STATUSES = {
    "cancelled",
    "canceled",
    "completed",
    "done",
    "error",
    "failed",
    "interrupted",
    "success",
}
_TERMINAL_MESSAGE_TYPES = {
    "tool_result",
    "tool_output",
    "question_tool_output",
}
_BACKGROUND_OUTPUT_TOOL_NAMES = {"bashoutput", "taskoutput"}
_BACKGROUND_STOP_TOOL_NAMES = {"killshell", "taskstop"}
_BACKGROUND_TASK_ID_KEYS = (
    "backgroundShellId",
    "background_shell_id",
    "backgroundTaskId",
    "background_task_id",
    "taskId",
    "task_id",
    "bashId",
    "bash_id",
    "shellId",
    "shell_id",
)
_TERMINAL_BACKGROUND_EVENT_STATUSES = {
    "cancelled",
    "canceled",
    "completed",
    "done",
    "error",
    "failed",
    "finished",
    "interrupted",
    "success",
}


def _bounded(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    text = _bounded(value, 128)
    return text or None


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _at_or_before(value: object, boundary: object) -> bool:
    observed = _parse_timestamp(value)
    cutoff = _parse_timestamp(boundary)
    return observed is not None and cutoff is not None and observed <= cutoff


def _newest_timestamp(current: object, candidate: object) -> str:
    current_at = _parse_timestamp(current)
    candidate_at = _parse_timestamp(candidate)
    if candidate_at is None:
        return _bounded(current, 128)
    if current_at is not None and current_at >= candidate_at:
        return _bounded(current, 128)
    return candidate_at.isoformat()


def _shell_tool(value: object) -> bool:
    normalized = "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )
    return normalized in _SHELL_TOOL_NAMES


def _shell_command(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(payload, dict):
        return text
    for key in ("command", "cmd", "script"):
        command = payload.get(key)
        if isinstance(command, list):
            candidate = " ".join(str(part) for part in command)
        elif command is not None:
            candidate = str(command)
        else:
            continue
        if candidate.strip():
            return candidate.strip()
    return text


def _normalized_tool_name(value: object) -> str:
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _tool_input_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _background_task_target(metadata: dict) -> str:
    payload = _tool_input_mapping(metadata.get("tool_input"))
    for key in _BACKGROUND_TASK_ID_KEYS:
        target = _bounded(payload.get(key), 512)
        if target:
            return target
    return ""


def _background_result_is_terminal(content: object) -> bool:
    """Only close a polled background task when its result proves completion."""
    text = str(content or "").casefold()
    return any(marker in text for marker in (
        "[exited with code",
        "exited with code",
        "task completed",
        "task failed",
        "task cancelled",
        "task canceled",
        "task interrupted",
        "background command completed",
        "background command failed",
    ))


def _agent_event_tokens(event: dict) -> set[str]:
    return {
        value
        for value in (
            _bounded(event.get("agent_tool_use_id"), 512),
            _bounded(event.get("task_id"), 512),
            _bounded(event.get("agent_thread_id"), 512),
        )
        if value
    }


def _agent_event_is_terminal(event: dict) -> bool:
    return any(
        _bounded(event.get(key), 80).casefold()
        in _TERMINAL_BACKGROUND_EVENT_STATUSES
        for key in ("resolved_status", "status", "kind")
    )


def background_running_count(
    activities: Iterable[dict],
    agent_events: Iterable[dict],
) -> int:
    """Count active background shells and agent launches from read-model state."""
    shell_count = sum(
        1
        for activity in activities
        if activity.get("is_background") is True
        and _bounded(activity.get("status"), 80).casefold() == "running"
    )
    active_agents: dict[str, set[str]] = {}
    for item in agent_events:
        event = item.get("event") if isinstance(item, dict) else None
        if not isinstance(event, dict):
            continue
        tokens = _agent_event_tokens(event)
        if not tokens:
            continue
        if _agent_event_is_terminal(event):
            active_agents = {
                key: active_tokens
                for key, active_tokens in active_agents.items()
                if active_tokens.isdisjoint(tokens)
            }
            continue
        if event.get("is_background") is True and (
            _bounded(event.get("status"), 80).casefold() in {
                "", "background", "pending", "running", "started",
            }
            or _bounded(event.get("kind"), 80).casefold() == "started"
        ):
            active_agents[sorted(tokens)[0]] = tokens
    return shell_count + len(active_agents)


def _question_interactions(metadata: object) -> list[dict]:
    values = metadata if isinstance(metadata, dict) else {}
    interactions: list[dict] = []
    direct = values.get("interaction")
    if isinstance(direct, dict) and not is_meta_tool_interaction(direct):
        interactions.append(direct)
    for call in normalize_tool_calls(values.get("tool_calls")):
        interaction = call.get("interaction")
        if isinstance(interaction, dict) and not is_meta_tool_interaction(interaction):
            interactions.append(interaction)
    return interactions


def _interaction_item(row, interaction: dict) -> dict:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    return {
        "document_id": str(row.document_id),
        "message_id": int(row.id or 0),
        "line_number": int(row.line_number or 0),
        "interaction": interaction,
        "model": metadata.get("model", ""),
        "reasoning_effort": metadata.get("reasoning_effort", ""),
        "service_tier": metadata.get("service_tier", ""),
        "agent_mode": metadata.get("agent_mode", ""),
        "timestamp": _timestamp(row.timestamp),
    }


def _prompt_projection_value(row) -> dict | None:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    role = row.role or row.message_type
    clean = str(row.content or "").strip()
    if role != "user" or not is_meaningful_human_prompt(
        clean,
        metadata,
        role,
    ):
        return None
    return {
        "document_id": row.document_id,
        "message_id": int(row.id),
        "line_number": int(row.line_number or 0),
        "content": clean[:500],
        "timestamp": row.timestamp,
    }


class _Accumulator:
    def __init__(self, projection: ConversationReadModel | None = None) -> None:
        self.pending = {
            str((item.get("interaction") or {}).get("id") or ""): dict(item)
            for item in (
                (projection.pending_interactions or []) if projection else []
            )
            if isinstance(item, dict)
            and isinstance(item.get("interaction"), dict)
            and not is_meta_tool_interaction(item.get("interaction"))
            and (item.get("interaction") or {}).get("id")
        }
        self.inferred = {
            str((item.get("response") or {}).get("interaction_id") or ""): dict(item)
            for item in ((projection.inferred_responses or []) if projection else [])
            if isinstance(item, dict)
            and isinstance(item.get("response"), dict)
            and (item.get("response") or {}).get("interaction_id")
        }
        self.activities = {
            str(item.get("activity_id") or ""): dict(item)
            for item in ((projection.live_activities or []) if projection else [])
            if isinstance(item, dict) and item.get("activity_id")
        }
        self.agent_events: list[dict] = [
            dict(item)
            for item in ((projection.agent_events or []) if projection else [])
            if isinstance(item, dict) and isinstance(item.get("event"), dict)
        ]
        self.event_indexes = self._event_indexes()
        self.latest_human_at = projection.latest_human_at if projection else ""

    def _event_indexes(self) -> dict[tuple[str, str, str], int]:
        indexes: dict[tuple[str, str, str], int] = {}
        for index, item in enumerate(self.agent_events):
            key = lifecycle_event_identity(item.get("event"))
            if key is not None:
                indexes[key] = index
        return indexes

    def observe(self, row) -> None:
        metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
        role = row.role or row.message_type
        message_id = int(row.id or 0)

        interactions = _question_interactions(metadata)
        for interaction in interactions:
            interaction_id = _bounded(interaction.get("id"), 512)
            if not interaction_id:
                continue
            if _at_or_before(row.timestamp, self.latest_human_at):
                self.pending.pop(interaction_id, None)
            else:
                self.pending[interaction_id] = _interaction_item(row, interaction)

        response = metadata.get("interaction_response")
        if isinstance(response, dict):
            interaction_id = _bounded(response.get("interaction_id"), 512)
            if interaction_id:
                self.pending.pop(interaction_id, None)
        else:
            for interaction in interactions:
                inferred = build_cursor_question_response(interaction, row.content)
                if inferred is None:
                    continue
                interaction_id = _bounded(interaction.get("id"), 512)
                if interaction_id:
                    self.pending.pop(interaction_id, None)

        if response is None and is_meaningful_human_prompt(
            row.content,
            metadata,
            role,
        ):
            for interaction_id in list(self.pending):
                self.inferred[interaction_id] = {
                    "document_id": str(row.document_id),
                    "message_id": message_id,
                    "line_number": int(row.line_number or 0),
                    "response": {
                        "kind": "question_response",
                        "interaction_id": interaction_id,
                        "status": "answered",
                        "answers": [],
                        "raw_text": str(row.content or "")[:4000],
                    },
                    "timestamp": _timestamp(row.timestamp),
                }
            self.pending.clear()

        if is_meaningful_human_turn(row.content, metadata, role):
            if isinstance(response, dict):
                self.pending.clear()
            self.latest_human_at = _newest_timestamp(
                self.latest_human_at,
                row.timestamp,
            )

        self._observe_activity(row, metadata)
        self._observe_agent_event(row, metadata)

    def _observe_activity(self, row, metadata: dict) -> None:
        activity_id = _bounded(metadata.get("tool_call_id"), 512)
        if not activity_id:
            return
        raw_type = _bounded(row.message_type, 80).casefold()
        status = _bounded(metadata.get("tool_status"), 80).casefold()
        self._observe_background_control(activity_id, raw_type, metadata)
        if raw_type in _TERMINAL_MESSAGE_TYPES or status in _TERMINAL_TOOL_STATUSES:
            activity = self.activities.get(activity_id)
            if activity is not None and activity.get("is_background") is True:
                background_task_id = _bounded(
                    metadata.get("background_task_id"),
                    512,
                )
                if background_task_id:
                    activity["background_task_id"] = background_task_id
                activity["updated_at"] = _timestamp(row.timestamp)
                return
            if self._complete_background_output(activity_id, row.content):
                return
            self.activities.pop(activity_id, None)
            return
        tool_name = _bounded(metadata.get("tool_name"), 256)
        command = _shell_command(metadata.get("tool_input"))
        if not _shell_tool(tool_name) or not command:
            return
        observed_at = _timestamp(row.timestamp)
        self.activities[activity_id] = {
            "document_id": str(row.document_id),
            "message_id": int(row.id or 0),
            "line_number": int(row.line_number or 0),
            "activity_id": activity_id,
            "activity_type": "shell",
            "status": "running",
            "tool_name": tool_name,
            "command": command,
            "started_at": observed_at,
            "updated_at": observed_at,
            "is_background": metadata.get("is_background") is True,
        }

    def _matching_background_activities(self, target_id: str) -> list[str]:
        if not target_id:
            return []
        return [
            activity_id
            for activity_id, activity in self.activities.items()
            if activity.get("is_background") is True
            and target_id in {
                _bounded(activity_id, 512),
                _bounded(activity.get("background_task_id"), 512),
            }
        ]

    def _observe_background_control(
        self,
        activity_id: str,
        raw_type: str,
        metadata: dict,
    ) -> None:
        tool_name = _normalized_tool_name(metadata.get("tool_name"))
        if tool_name not in (
            _BACKGROUND_OUTPUT_TOOL_NAMES | _BACKGROUND_STOP_TOOL_NAMES
        ):
            return
        targets = self._matching_background_activities(
            _background_task_target(metadata),
        )
        if tool_name in _BACKGROUND_STOP_TOOL_NAMES:
            for target in targets:
                self.activities.pop(target, None)
            return
        for target in targets:
            self.activities[target]["output_tool_call_id"] = activity_id

    def _complete_background_output(
        self,
        activity_id: str,
        content: object,
    ) -> bool:
        if not _background_result_is_terminal(content):
            return False
        targets = [
            target
            for target, activity in self.activities.items()
            if activity.get("is_background") is True
            and _bounded(activity.get("output_tool_call_id"), 512) == activity_id
        ]
        for target in targets:
            self.activities.pop(target, None)
        return bool(targets)

    def _complete_background_activities_from_event(self, event: dict) -> None:
        if not _agent_event_is_terminal(event):
            return
        tokens = _agent_event_tokens(event)
        if not tokens:
            return
        for activity_id in [
            activity_id
            for activity_id, activity in self.activities.items()
            if activity.get("is_background") is True
            and tokens.intersection({
                _bounded(activity_id, 512),
                _bounded(activity.get("background_task_id"), 512),
            })
        ]:
            self.activities.pop(activity_id, None)

    def _observe_agent_event(self, row, metadata: dict) -> None:
        event = metadata.get("agent_event")
        if not isinstance(event, dict):
            return
        self._complete_background_activities_from_event(event)
        item = {
            "event": event,
            "line_number": int(row.line_number or 0),
            "timestamp": _timestamp(row.timestamp),
        }
        key = lifecycle_event_identity(event)
        if key is None or key not in self.event_indexes:
            self.agent_events.append(item)
            if key is not None:
                self.event_indexes[key] = len(self.agent_events) - 1
            return
        index = self.event_indexes[key]
        current = self.agent_events[index]
        current_event = current.get("event")
        if isinstance(current_event, dict):
            item["event"] = merge_duplicate_lifecycle_events(current_event, event)
        self.agent_events[index] = item

    def values(self) -> dict:
        pending = sorted(
            self.pending.values(),
            key=lambda item: (
                str(item.get("timestamp") or ""),
                int(item.get("line_number") or 0),
            ),
        )[-MAX_PENDING_INTERACTIONS:]
        inferred = sorted(
            self.inferred.values(),
            key=lambda item: (
                str(item.get("timestamp") or ""),
                int(item.get("line_number") or 0),
            ),
        )[-MAX_INFERRED_RESPONSES:]
        activities = sorted(
            self.activities.values(),
            key=lambda item: (
                str(item.get("started_at") or ""),
                int(item.get("line_number") or 0),
            ),
        )[-MAX_LIVE_ACTIVITIES:]
        return {
            "pending_interactions": pending,
            "inferred_responses": inferred,
            "live_activities": activities,
            "agent_events": self.agent_events[-MAX_AGENT_EVENTS:],
            "background_running_count": background_running_count(
                activities,
                self.agent_events[-MAX_AGENT_EVENTS:],
            ),
            "latest_human_at": self.latest_human_at,
        }


def _identity_values(document: Document) -> dict:
    metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
    thread_id = current_thread_id(metadata)
    root_id = conversation_root_thread_id(
        document.tool_id,
        document.relative_path,
        metadata,
    )
    try:
        depth = max(0, int(metadata.get("agent_depth") or 0))
    except (TypeError, ValueError):
        depth = 0
    is_subagent = is_conversation_subagent(
        document.tool_id,
        document.relative_path,
        metadata,
    )
    if is_subagent:
        depth = max(1, depth)
    runtime = subagent_runtime_from_metadata(metadata)
    service_tier = _bounded(
        metadata.get("_assistant_service_tier")
        or metadata.get("service_tier"),
        128,
    )
    if service_tier:
        runtime["service_tier"] = service_tier
    token_usage = token_usage_from_metadata(metadata)
    if token_usage:
        runtime["token_usage"] = token_usage
    for runtime_key, metadata_key in (
        ("started_at", STARTED_AT_METADATA_KEY),
        ("last_activity_at", LAST_ACTIVITY_AT_METADATA_KEY),
    ):
        value = _bounded(metadata.get(metadata_key), 128)
        if value:
            runtime[runtime_key] = value
    return {
        "machine_id": document.machine_id,
        "tool_id": document.tool_id,
        "thread_id": _bounded(thread_id, 512) or None,
        "root_thread_id": _bounded(root_id, 512) or None,
        "parent_thread_id": _bounded(metadata.get("parent_thread_id"), 512) or None,
        "agent_id": _bounded(metadata.get("agent_id"), 512) or None,
        "agent_tool_use_id": (
            _bounded(metadata.get("agent_tool_use_id"), 512) or None
        ),
        "agent_depth": depth,
        "is_subagent": is_subagent,
        "runtime": runtime,
        "lifecycle": persisted_child_lifecycle(metadata),
    }


def conversation_read_rows_statement(
    document_id: object,
    *,
    after_line: int | None = None,
    dirty_line_numbers: Iterable[int] = (),
):
    """Compile the bounded row read used by incremental projection refresh."""
    statement = select(ConversationMessage).where(
        ConversationMessage.document_id == document_id
    )
    if after_line is not None:
        criteria = [ConversationMessage.line_number > after_line]
        dirty = sorted({int(value) for value in dirty_line_numbers})
        if dirty:
            criteria.append(ConversationMessage.line_number.in_(dirty))
        statement = statement.where(or_(*criteria))
    return statement.order_by(
        ConversationMessage.line_number,
        ConversationMessage.id,
    )


def conversation_prompt_rows_statement(
    document_id: object,
    *,
    after_line: int | None = None,
):
    """Compile the keyset prompt read served to steady-state refreshes."""
    statement = select(ConversationPromptProjection).where(
        ConversationPromptProjection.document_id == document_id
    )
    if after_line is not None:
        statement = statement.where(
            ConversationPromptProjection.line_number > after_line
        )
    return statement.order_by(
        ConversationPromptProjection.line_number,
        ConversationPromptProjection.message_id,
    )


async def _refresh_prompt_projections(
    db: AsyncSession,
    document_id: object,
    rows: Iterable[ConversationMessage],
    *,
    replace: bool,
) -> None:
    candidates = list(rows)
    message_ids = sorted({int(row.id) for row in candidates})
    if replace:
        await db.execute(
            delete(ConversationPromptProjection).where(
                ConversationPromptProjection.document_id == document_id
            )
        )
    elif message_ids:
        await db.execute(
            delete(ConversationPromptProjection).where(
                ConversationPromptProjection.document_id == document_id,
                ConversationPromptProjection.message_id.in_(message_ids),
            )
        )
    db.add_all(
        ConversationPromptProjection(**value)
        for row in candidates
        if (value := _prompt_projection_value(row)) is not None
    )


async def refresh_conversation_read_model(
    db: AsyncSession,
    document: Document,
    *,
    mode: str,
    dirty_line_numbers: Iterable[int] = (),
    force_full: bool = False,
    row_limit: int | None = None,
) -> ConversationReadModel:
    """Refresh one projection, scanning only the newly committed delta."""
    projection = await db.get(ConversationReadModel, document.id)
    incremental = (
        mode == "delta"
        and projection is not None
        and projection.projection_version == READ_MODEL_VERSION
        and not force_full
    )
    previous_through = (
        int(projection.projected_through_line or 0) if incremental else 0
    )
    dirty = sorted({int(value) for value in dirty_line_numbers})
    statement = conversation_read_rows_statement(
        document.id,
        after_line=previous_through if incremental else None,
        dirty_line_numbers=dirty,
    )
    if row_limit is not None:
        statement = statement.limit(max(1, int(row_limit)))
    rows = (
        (
            await db.execute(statement)
        )
        .scalars()
        .all()
    )

    await _refresh_prompt_projections(
        db,
        document.id,
        rows,
        replace=not incremental,
    )
    accumulator = _Accumulator(projection if incremental else None)
    for row in rows:
        accumulator.observe(row)

    new_rows = (
        [row for row in rows if int(row.line_number or 0) > previous_through]
        if incremental
        else rows
    )
    if incremental and dirty:
        stats = (
            await db.execute(
                select(
                    func.count(),
                    func.count().filter(ConversationMessage.role == "user"),
                    func.count().filter(ConversationMessage.role == "assistant"),
                    func.coalesce(
                        func.sum(func.length(ConversationMessage.content)).filter(
                            ConversationMessage.role.in_(("user", "assistant"))
                        ),
                        0,
                    ),
                ).where(ConversationMessage.document_id == document.id)
            )
        ).one()
        message_count, user_count, assistant_count, human_characters = (
            int(value or 0) for value in stats
        )
    else:
        new_user_count = sum(row.role == "user" for row in new_rows)
        new_assistant_count = sum(row.role == "assistant" for row in new_rows)
        new_human_characters = sum(
            len(row.content or "")
            for row in new_rows
            if row.role in ("user", "assistant")
        )
        if incremental and projection is not None:
            message_count = int(projection.message_count or 0) + len(new_rows)
            user_count = (
                int(projection.user_message_count or 0) + new_user_count
            )
            assistant_count = (
                int(projection.assistant_message_count or 0)
                + new_assistant_count
            )
            human_characters = (
                int(projection.human_character_count or 0)
                + new_human_characters
            )
        else:
            message_count = len(rows)
            user_count = new_user_count
            assistant_count = new_assistant_count
            human_characters = new_human_characters
    projected_through = max(
        [previous_through, *(int(row.line_number or 0) for row in rows)],
        default=previous_through,
    )
    latest_assistant = (
        projection.latest_assistant_line
        if incremental and projection is not None
        else None
    )
    for row in rows:
        if (row.role or row.message_type) == "assistant":
            latest_assistant = max(
                int(latest_assistant or 0),
                int(row.line_number or 0),
            )

    previous_generation = int(projection.generation or 0) if projection else 0
    accumulator_values = accumulator.values()
    # This is intentionally derived at read time: freshness bounds running
    # activities, so persisting a count would turn a stale value into state.
    accumulator_values.pop("background_running_count", None)
    values = {
        **_identity_values(document),
        "message_count": message_count,
        "user_message_count": user_count,
        "assistant_message_count": assistant_count,
        "human_character_count": human_characters,
        "projected_through_line": projected_through,
        "latest_assistant_line": latest_assistant,
        "generation": (
            previous_generation
            if incremental and projection is not None and not dirty
            else previous_generation + 1
        ),
        "projection_version": READ_MODEL_VERSION,
        **accumulator_values,
    }
    if projection is None:
        projection = ConversationReadModel(document_id=document.id, **values)
        db.add(projection)
    else:
        for key, value in values.items():
            setattr(projection, key, value)

    from .conversation_tasks import refresh_task_projection

    await refresh_task_projection(
        db,
        document,
        candidate_rows=rows,
        replace=not incremental,
    )
    await db.flush()
    return projection


async def refresh_conversation_read_model_in_batches(
    db: AsyncSession,
    document: Document,
    *,
    batch_size: int = READ_MODEL_BACKFILL_MESSAGE_BATCH_SIZE,
) -> ConversationReadModel:
    """Rebuild one historical projection without materializing every message."""
    max_line = await db.scalar(
        select(func.max(ConversationMessage.line_number)).where(
            ConversationMessage.document_id == document.id
        )
    )
    first_batch = True
    previous_line = -1
    while True:
        projection = await refresh_conversation_read_model(
            db,
            document,
            mode="full" if first_batch else "delta",
            force_full=first_batch,
            row_limit=batch_size,
        )
        projected_line = int(projection.projected_through_line or 0)
        if max_line is None or projected_line >= int(max_line):
            return projection
        if projected_line <= previous_line:
            raise RuntimeError(
                f"conversation read-model backfill stalled for {document.id}"
            )
        previous_line = projected_line
        first_batch = False


def conversation_backfill_documents_statement(
    *,
    document_ids: Iterable[object] = (),
    after_id: object | None = None,
    batch_size: int = READ_MODEL_BACKFILL_DOCUMENT_BATCH_SIZE,
):
    """Select only projection inputs, keyset-bounded by document ID."""
    statement = (
        select(Document)
        .options(load_only(
            Document.id,
            Document.machine_id,
            Document.tool_id,
            Document.relative_path,
            Document.metadata_,
        ))
        .where(Document.category == "conversation")
        .order_by(Document.id)
        .limit(max(1, int(batch_size)))
    )
    ids = list(document_ids)
    if ids:
        statement = statement.where(Document.id.in_(ids))
    if after_id is not None:
        statement = statement.where(Document.id > after_id)
    return statement


async def backfill_conversation_read_models(
    db: AsyncSession,
    document_ids: Iterable[object] | None = None,
    *,
    message_batch_size: int = READ_MODEL_BACKFILL_MESSAGE_BATCH_SIZE,
) -> dict[str, int]:
    """Build historical projections with bounded document and message reads."""
    ids = list(document_ids or [])
    last_id = None
    visited = 0
    updated = 0
    while True:
        documents = (
            await db.execute(
                conversation_backfill_documents_statement(
                    document_ids=ids,
                    after_id=last_id,
                )
            )
        ).scalars().all()
        if not documents:
            break
        for document in documents:
            await refresh_conversation_read_model_in_batches(
                db,
                document,
                batch_size=message_batch_size,
            )
            # A full historical rebuild deterministically replaces this row.
            # Do not inspect server-managed updated_at after a flush: SQLAlchemy
            # expires on-update columns, and implicit async lazy IO is invalid.
            updated += 1
            visited += 1
        last_id = documents[-1].id
        await db.flush()
        if ids:
            break
    return {"documents": visited, "created_or_updated": updated}
