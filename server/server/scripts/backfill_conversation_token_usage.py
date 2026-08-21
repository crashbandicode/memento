"""Backfill exact native token totals without reparsing conversation rows.

The default is a read-only scan. Pass ``--apply`` to persist confirmed Codex
or Claude totals into document metadata and the conversation read projection.
Cursor transcripts currently contain no exact token accounting and are not
assigned an invented value.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
import orjson

from server.config import settings
from server.services.conversation_parser import (
    AssistantIdentityState,
    AssistantUsageObservation,
    observe_assistant_identity_record,
)
from server.services.conversation_usage import (
    LAST_ACTIVITY_AT_METADATA_KEY,
    STARTED_AT_METADATA_KEY,
    TOKEN_USAGE_METADATA_KEY,
    normalize_token_usage,
    usage_observation_values,
)
from server.services.large_content_store import iter_large_content_lines


@dataclass(frozen=True)
class TokenUsageUpdate:
    document_id: UUID
    machine_id: UUID | None
    tool_id: str
    content_hash: str
    usage: dict[str, object]
    started_at: str
    last_activity_at: str
    observations: tuple[AssistantUsageObservation, ...]


def _database_dsn() -> str:
    return str(settings.database_url).replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )


def _metadata_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def extract_token_usage_from_lines(
    lines: Iterable[str],
    tool_id: str,
) -> dict[str, object]:
    return normalize_token_usage(
        extract_assistant_identity_from_lines(lines, tool_id).token_usage
    )


def extract_assistant_identity_from_lines(
    lines: Iterable[str],
    tool_id: str,
) -> AssistantIdentityState:
    identity = AssistantIdentityState()
    for line in lines:
        try:
            record = orjson.loads(line)
        except (orjson.JSONDecodeError, TypeError, ValueError):
            continue
        observe_assistant_identity_record(identity, record, tool_id)
    return identity


def _scan_document(row: asyncpg.Record) -> TokenUsageUpdate | None:
    content_s3_key = row["content_s3_key"]
    if content_s3_key:
        lines = iter_large_content_lines(content_s3_key)
    else:
        lines = io.StringIO(str(row["content"] or ""))
    identity = extract_assistant_identity_from_lines(lines, row["tool_id"])
    usage = normalize_token_usage(identity.token_usage)
    if not usage:
        return None
    return TokenUsageUpdate(
        document_id=row["id"],
        machine_id=row["machine_id"],
        tool_id=row["tool_id"],
        content_hash=row["content_hash"],
        usage=usage,
        started_at=identity.started_at,
        last_activity_at=identity.last_activity_at,
        observations=tuple(identity.usage_observations),
    )


async def _apply_updates(
    conn: asyncpg.Connection,
    updates: list[TokenUsageUpdate],
) -> tuple[int, int, int]:
    if not updates:
        return 0, 0, 0
    metadata_changed = 0
    applied_documents = 0
    event_records: dict[tuple[UUID, str], tuple[object, ...]] = {}
    async with conn.transaction():
        locked_rows = await conn.fetch(
            """
            SELECT id, content_hash, metadata
            FROM documents
            WHERE id=ANY($1::uuid[])
            FOR UPDATE
            """,
            [update.document_id for update in updates],
        )
        locked_hashes = {row["id"]: row["content_hash"] for row in locked_rows}
        document_metadata = {
            row["id"]: _metadata_mapping(row["metadata"]) for row in locked_rows
        }
        valid_updates = [
            update
            for update in updates
            if locked_hashes.get(update.document_id) == update.content_hash
        ]
        if not valid_updates:
            return 0, 0, 0
        delivery_rows = await conn.fetch(
            """
            SELECT document_id, delivery_metadata
            FROM document_delivery_state
            WHERE document_id=ANY($1::uuid[])
            FOR UPDATE
            """,
            [update.document_id for update in valid_updates],
        )
        delivery_metadata = {
            row["document_id"]: _metadata_mapping(row["delivery_metadata"])
            for row in delivery_rows
        }
        applied_documents = len(valid_updates)
        for update in valid_updates:
            effective_metadata = delivery_metadata.get(
                update.document_id,
                document_metadata.get(update.document_id, {}),
            )
            existing_usage = normalize_token_usage(
                effective_metadata.get(TOKEN_USAGE_METADATA_KEY)
            )
            preferred_usage = (
                existing_usage
                if int(existing_usage.get("total_tokens") or 0)
                >= int(update.usage.get("total_tokens") or 0)
                else update.usage
            )
            metadata_patch: dict[str, object] = {
                TOKEN_USAGE_METADATA_KEY: preferred_usage,
            }
            runtime_patch: dict[str, object] = {"token_usage": preferred_usage}
            for metadata_key, runtime_key, value in (
                (STARTED_AT_METADATA_KEY, "started_at", update.started_at),
                (
                    LAST_ACTIVITY_AT_METADATA_KEY,
                    "last_activity_at",
                    update.last_activity_at,
                ),
            ):
                if value:
                    existing_value = str(effective_metadata.get(metadata_key) or "")
                    selected_value = value
                    if existing_value:
                        selected_value = (
                            min(existing_value, value)
                            if metadata_key == STARTED_AT_METADATA_KEY
                            else max(existing_value, value)
                        )
                    metadata_patch[metadata_key] = selected_value
                    runtime_patch[runtime_key] = selected_value
            serialized_metadata = json.dumps(metadata_patch, separators=(",", ":"))
            serialized_runtime = json.dumps(runtime_patch, separators=(",", ":"))
            result = await conn.execute(
                """
                UPDATE documents
                SET metadata=COALESCE(metadata, '{}'::jsonb) || $3::jsonb,
                    updated_at=now()
                WHERE id=$1
                  AND content_hash=$2
                  AND COALESCE(metadata, '{}'::jsonb)
                      IS DISTINCT FROM (
                          COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                      )
                """,
                update.document_id,
                update.content_hash,
                serialized_metadata,
            )
            if result == "UPDATE 1":
                metadata_changed += 1
            await conn.execute(
                """
                UPDATE document_delivery_state
                SET delivery_metadata=(
                        COALESCE(delivery_metadata, '{}'::jsonb) || $2::jsonb
                    ),
                    updated_at=now()
                WHERE document_id=$1
                  AND COALESCE(delivery_metadata, '{}'::jsonb)
                      IS DISTINCT FROM (
                          COALESCE(delivery_metadata, '{}'::jsonb) || $2::jsonb
                      )
                """,
                update.document_id,
                serialized_metadata,
            )
            await conn.execute(
                """
                UPDATE conversation_read_models
                SET runtime=COALESCE(runtime, '{}'::jsonb) || $2::jsonb,
                    updated_at=now()
                WHERE document_id=$1
                  AND COALESCE(runtime, '{}'::jsonb)
                      IS DISTINCT FROM (
                          COALESCE(runtime, '{}'::jsonb) || $2::jsonb
                      )
                """,
                update.document_id,
                serialized_runtime,
            )
            for observation in update.observations:
                values = usage_observation_values(observation)
                source_id = str(values["source_id"])
                event_records[(update.document_id, source_id)] = (
                    update.document_id,
                    update.machine_id,
                    update.tool_id,
                    source_id,
                    values["source"],
                    values["occurred_at"],
                    values["model"],
                    values["reasoning_effort"],
                    values["service_tier"],
                    values["attribution_status"],
                    values["input_tokens"],
                    values["uncached_input_tokens"],
                    values["cached_input_tokens"],
                    values["cache_write_input_tokens"],
                    values["output_tokens"],
                    values["reasoning_output_tokens"],
                    values["total_tokens"],
                )
        if event_records:
            await conn.execute(
                """
                CREATE TEMP TABLE conversation_usage_backfill_events
                (LIKE conversation_usage_events INCLUDING DEFAULTS)
                ON COMMIT DROP
                """
            )
            await conn.copy_records_to_table(
                "conversation_usage_backfill_events",
                records=list(event_records.values()),
                columns=(
                    "document_id",
                    "machine_id",
                    "tool_id",
                    "source_id",
                    "source",
                    "occurred_at",
                    "model",
                    "reasoning_effort",
                    "service_tier",
                    "attribution_status",
                    "input_tokens",
                    "uncached_input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                ),
            )
            await conn.execute(
                """
                INSERT INTO conversation_usage_events (
                    document_id, machine_id, tool_id, source_id, source,
                    occurred_at, model, reasoning_effort, service_tier,
                    attribution_status, input_tokens, uncached_input_tokens,
                    cached_input_tokens, cache_write_input_tokens,
                    output_tokens, reasoning_output_tokens, total_tokens
                )
                SELECT
                    document_id, machine_id, tool_id, source_id, source,
                    occurred_at, model, reasoning_effort, service_tier,
                    attribution_status, input_tokens, uncached_input_tokens,
                    cached_input_tokens, cache_write_input_tokens,
                    output_tokens, reasoning_output_tokens, total_tokens
                FROM conversation_usage_backfill_events
                ON CONFLICT (document_id, source_id) DO UPDATE SET
                    machine_id=EXCLUDED.machine_id,
                    tool_id=EXCLUDED.tool_id,
                    source=EXCLUDED.source,
                    occurred_at=EXCLUDED.occurred_at,
                    model=EXCLUDED.model,
                    reasoning_effort=EXCLUDED.reasoning_effort,
                    service_tier=EXCLUDED.service_tier,
                    attribution_status=EXCLUDED.attribution_status,
                    input_tokens=EXCLUDED.input_tokens,
                    uncached_input_tokens=EXCLUDED.uncached_input_tokens,
                    cached_input_tokens=EXCLUDED.cached_input_tokens,
                    cache_write_input_tokens=EXCLUDED.cache_write_input_tokens,
                    output_tokens=EXCLUDED.output_tokens,
                    reasoning_output_tokens=EXCLUDED.reasoning_output_tokens,
                    total_tokens=EXCLUDED.total_tokens
                """
            )
    return applied_documents, metadata_changed, len(event_records)


async def run(*, apply: bool, limit: int | None) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    write_conn = (
        await asyncpg.connect(_database_dsn(), command_timeout=1_800)
        if apply
        else None
    )
    try:
        total = int(
            await conn.fetchval(
                """
                SELECT count(*)
                FROM documents
                WHERE category='conversation'
                  AND tool_id=ANY($1::text[])
                """,
                ["codex", "claude_code"],
            )
            or 0
        )
        if limit is not None:
            total = min(total, limit)

        statement = await conn.prepare(
            """
            SELECT id, machine_id, tool_id, content, content_s3_key, content_hash
            FROM documents
            WHERE category='conversation'
              AND tool_id=ANY($1::text[])
            ORDER BY id
            LIMIT $2
            """
        )
        updates: list[TokenUsageUpdate] = []
        pending_events = 0
        scanned = 0
        eligible = 0
        observations = 0
        applied_documents = 0
        metadata_changed = 0
        events_written = 0
        # asyncpg cursors require a transaction.  Keep only a very small row
        # window resident: an inline transcript can itself be large, and a
        # bulk ``fetch`` would otherwise materialize the entire corpus.
        async with conn.transaction(readonly=True):
            async for row in statement.cursor(
                ["codex", "claude_code"],
                limit,
                prefetch=4,
            ):
                update = await asyncio.to_thread(_scan_document, row)
                scanned += 1
                if update is not None:
                    eligible += 1
                    observations += len(update.observations)
                    if apply:
                        updates.append(update)
                        pending_events += len(update.observations)
                if apply and (len(updates) >= 16 or pending_events >= 5_000):
                    applied, changed, written = await _apply_updates(
                        write_conn,
                        updates,
                    )
                    applied_documents += applied
                    metadata_changed += changed
                    events_written += written
                    updates = []
                    pending_events = 0
                if scanned % 100 == 0:
                    print(
                        json.dumps(
                            {
                                "scanned": scanned,
                                "eligible": eligible,
                                "observations": observations,
                                "events_written": events_written,
                                "total": total,
                            }
                        ),
                        flush=True,
                    )
        if apply and updates:
            applied, changed, written = await _apply_updates(write_conn, updates)
            applied_documents += applied
            metadata_changed += changed
            events_written += written
        return {
            "mode": "apply" if apply else "dry_run",
            "scanned": scanned,
            "eligible": eligible,
            "observations": observations,
            "applied_documents": applied_documents,
            "metadata_changed": metadata_changed,
            "events_written": events_written,
        }
    finally:
        if write_conn is not None:
            await write_conn.close()
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    print(
        json.dumps(asyncio.run(run(apply=args.apply, limit=args.limit))),
        flush=True,
    )


if __name__ == "__main__":
    main()
