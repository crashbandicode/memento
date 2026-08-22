"""Repair source-verifiable Claude permission responses.

Collectors released before the exact PreToolUse response capture could close
an answered PermissionRequest without retaining which option was selected.
This repair never guesses from timing or prose.  It records ``allow`` only
when the same machine/root session contains an exact requested tool/input row
and a native tool-result row with the same tool-call id.

Dry-run is the default::

    python -m server.scripts.backfill_claude_permission_responses
    python -m server.scripts.backfill_claude_permission_responses --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import asyncpg

from server.scripts.reparse_conversations import _database_dsn
from server.services.claude_lineage import (
    EXACT_PERMISSION_RESPONSE_BACKFILL,
    canonical_permission_fingerprint,
)


@dataclass(frozen=True)
class PermissionDocument:
    id: uuid.UUID
    machine_id: uuid.UUID
    root_session_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ExecutedToolCall:
    machine_id: uuid.UUID
    root_session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_call_id: str


@dataclass(frozen=True)
class PermissionRepair:
    document_id: uuid.UUID
    history_index: int
    expected_entry: dict[str, Any]
    repaired_entry: dict[str, Any]


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _missing_recorded_answer(entry: dict[str, Any]) -> bool:
    if str(entry.get("status") or "").casefold() != "answered":
        return False
    response = entry.get("response")
    if not isinstance(response, dict):
        return True
    answers = response.get("answers")
    return not isinstance(answers, list) or not answers


def _executed_fingerprints(
    calls: Iterable[ExecutedToolCall],
) -> set[tuple[uuid.UUID, str, str]]:
    fingerprints: set[tuple[uuid.UUID, str, str]] = set()
    for call in calls:
        fingerprint = canonical_permission_fingerprint(
            call.tool_name,
            call.tool_input,
        )
        if fingerprint and call.root_session_id and call.tool_call_id:
            fingerprints.add((call.machine_id, call.root_session_id, fingerprint))
    return fingerprints


def plan_permission_response_repairs(
    documents: Iterable[PermissionDocument],
    executed_calls: Iterable[ExecutedToolCall],
) -> list[PermissionRepair]:
    """Plan only exact, execution-proven ``allow`` response overlays."""
    executed = _executed_fingerprints(executed_calls)
    repairs: list[PermissionRepair] = []
    for document in documents:
        history = document.metadata.get("_interaction_history")
        if not isinstance(history, list):
            continue
        for index, raw_entry in enumerate(history):
            if not isinstance(raw_entry, dict) or not _missing_recorded_answer(raw_entry):
                continue
            interaction = raw_entry.get("interaction")
            if (
                not isinstance(interaction, dict)
                or interaction.get("interaction_type") != "permission_request"
            ):
                continue
            tool_name = interaction.get("requested_tool")
            tool_input = interaction.get("tool_input")
            if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
                continue
            fingerprint = canonical_permission_fingerprint(tool_name, tool_input)
            if (
                not fingerprint
                or (
                    document.machine_id,
                    document.root_session_id,
                    fingerprint,
                )
                not in executed
            ):
                continue
            interaction_id = str(interaction.get("id") or "").strip()
            if not interaction_id:
                continue
            repaired = dict(raw_entry)
            repaired["response"] = {
                "kind": "question_response",
                "interaction_id": interaction_id,
                "status": "answered",
                "answers": [{
                    "question_id": "permission-decision",
                    "text": "Yes",
                    "selected_option_ids": ["allow"],
                }],
                "raw_text": "Yes",
            }
            repaired["response_backfill"] = EXACT_PERMISSION_RESPONSE_BACKFILL
            repairs.append(PermissionRepair(
                document_id=document.id,
                history_index=index,
                expected_entry=dict(raw_entry),
                repaired_entry=repaired,
            ))
    return repairs


async def _candidate_documents(
    conn: asyncpg.Connection,
    document_ids: list[uuid.UUID] | None,
) -> list[PermissionDocument]:
    rows = await conn.fetch(
        """
        SELECT document.id, document.machine_id, effective.metadata,
               COALESCE(NULLIF(effective.metadata->>'root_session_id', ''),
                        NULLIF(effective.metadata->>'session_id', ''))
                    AS root_session_id
        FROM documents document
        LEFT JOIN document_delivery_state delivery
          ON delivery.document_id=document.id
        CROSS JOIN LATERAL (
          SELECT COALESCE(delivery.delivery_metadata, document.metadata)
                    AS metadata
        ) effective
        WHERE document.tool_id='claude_code'
          AND document.category='conversation'
          AND jsonb_typeof(effective.metadata->'_interaction_history')='array'
          AND EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
              effective.metadata->'_interaction_history'
            ) entry
            WHERE entry->>'status'='answered'
              AND entry->'interaction'->>'interaction_type'='permission_request'
              AND CASE
                    WHEN jsonb_typeof(entry->'response'->'answers')='array'
                    THEN jsonb_array_length(entry->'response'->'answers')=0
                    ELSE true
                  END
          )
          AND ($1::uuid[] IS NULL OR document.id=ANY($1::uuid[]))
        ORDER BY document.id
        """,
        document_ids,
    )
    return [PermissionDocument(
        id=row["id"],
        machine_id=row["machine_id"],
        root_session_id=str(row["root_session_id"] or ""),
        metadata=_mapping(row["metadata"]),
    ) for row in rows if row["root_session_id"]]


async def _executed_tool_calls(
    conn: asyncpg.Connection,
    documents: list[PermissionDocument],
) -> list[ExecutedToolCall]:
    scopes = sorted({
        (document.machine_id, document.root_session_id)
        for document in documents
    })
    if not scopes:
        return []
    machine_ids = [scope[0] for scope in scopes]
    root_ids = [scope[1] for scope in scopes]
    rows = await conn.fetch(
        """
        WITH scoped_documents AS (
          SELECT document.id, document.machine_id,
                 COALESCE(
                   NULLIF(effective.metadata->>'root_session_id', ''),
                   NULLIF(effective.metadata->>'session_id', '')
                 ) AS root_session_id
          FROM documents document
          LEFT JOIN document_delivery_state delivery
            ON delivery.document_id=document.id
          CROSS JOIN LATERAL (
            SELECT COALESCE(delivery.delivery_metadata, document.metadata)
                      AS metadata
          ) effective
          WHERE document.tool_id='claude_code'
            AND document.category='conversation'
            AND document.machine_id=ANY($1::uuid[])
            AND (
              effective.metadata->>'root_session_id'=ANY($2::text[])
              OR effective.metadata->>'session_id'=ANY($2::text[])
            )
        ), tool_rows AS (
          SELECT cm.document_id, sd.machine_id, sd.root_session_id,
                 cm.metadata->>'tool_name' AS tool_name,
                 cm.metadata->>'tool_input' AS tool_input,
                 cm.metadata->>'tool_call_id' AS tool_call_id
          FROM conversation_messages cm
          JOIN scoped_documents sd ON sd.id=cm.document_id
          WHERE cm.role='tool'
            AND cm.metadata ? 'tool_input'
            AND cm.metadata ? 'tool_call_id'
        )
        SELECT tr.machine_id, tr.root_session_id, tr.tool_name,
               tr.tool_input, tr.tool_call_id
        FROM tool_rows tr
        WHERE EXISTS (
          SELECT 1
          FROM conversation_messages result
          WHERE result.document_id=tr.document_id
            AND result.role='tool'
            AND result.metadata->>'tool_call_id'=tr.tool_call_id
            AND NOT (result.metadata ? 'tool_input')
        )
        """,
        machine_ids,
        root_ids,
    )
    calls: list[ExecutedToolCall] = []
    allowed_scopes = set(scopes)
    for row in rows:
        scope = (row["machine_id"], str(row["root_session_id"] or ""))
        if scope not in allowed_scopes:
            continue
        tool_input = _mapping(row["tool_input"])
        if not tool_input:
            continue
        calls.append(ExecutedToolCall(
            machine_id=row["machine_id"],
            root_session_id=scope[1],
            tool_name=str(row["tool_name"] or ""),
            tool_input=tool_input,
            tool_call_id=str(row["tool_call_id"] or ""),
        ))
    return calls


async def _apply_repairs(
    conn: asyncpg.Connection,
    repairs: list[PermissionRepair],
) -> int:
    applied = 0
    async with conn.transaction():
        for repair in repairs:
            path = ["_interaction_history", str(repair.history_index)]
            document_row = await conn.fetchrow(
                """
                SELECT metadata
                FROM documents
                WHERE id=$1
                FOR UPDATE
                """,
                repair.document_id,
            )
            if document_row is None:
                continue
            delivery_row = await conn.fetchrow(
                """
                SELECT delivery_metadata
                FROM document_delivery_state
                WHERE document_id=$1
                FOR UPDATE
                """,
                repair.document_id,
            )
            effective_metadata = _mapping(
                delivery_row["delivery_metadata"]
                if delivery_row is not None
                else document_row["metadata"]
            )
            history = effective_metadata.get("_interaction_history")
            if (
                not isinstance(history, list)
                or repair.history_index >= len(history)
                or history[repair.history_index] != repair.expected_entry
            ):
                continue
            repaired_json = json.dumps(repair.repaired_entry)
            expected_json = json.dumps(repair.expected_entry)
            if delivery_row is not None:
                await conn.execute(
                    """
                    UPDATE document_delivery_state
                    SET delivery_metadata=jsonb_set(
                          delivery_metadata,
                          $2::text[],
                          $3::jsonb,
                          false
                        ),
                        updated_at=now()
                    WHERE document_id=$1
                    """,
                    repair.document_id,
                    path,
                    repaired_json,
                )
                # Keep the canonical snapshot aligned when it still contains
                # the same legacy entry. Runtime reads use delivery metadata.
                await conn.execute(
                    """
                    UPDATE documents
                    SET metadata=jsonb_set(metadata, $2::text[], $3::jsonb, false)
                    WHERE id=$1
                      AND metadata #> $2::text[] = $4::jsonb
                    """,
                    repair.document_id,
                    path,
                    repaired_json,
                    expected_json,
                )
            else:
                await conn.execute(
                    """
                    UPDATE documents
                    SET metadata=jsonb_set(metadata, $2::text[], $3::jsonb, false)
                    WHERE id=$1
                    """,
                    repair.document_id,
                    path,
                    repaired_json,
                )
            applied += 1
    return applied


async def run(
    *,
    apply: bool,
    document_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    conn = await asyncpg.connect(_database_dsn(), command_timeout=1_800)
    try:
        documents = await _candidate_documents(conn, document_ids)
        calls = await _executed_tool_calls(conn, documents)
        repairs = plan_permission_response_repairs(documents, calls)
        applied = await _apply_repairs(conn, repairs) if apply and repairs else 0
        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_documents": len(documents),
            "executed_tool_calls": len(calls),
            "planned": len(repairs),
            "applied": applied,
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit source-verifiable response overlays",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        type=uuid.UUID,
        dest="document_ids",
        help="limit repair to one document (may be repeated)",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(
        apply=args.apply,
        document_ids=args.document_ids,
    )), indent=2))


if __name__ == "__main__":
    main()
