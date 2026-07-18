"""Stage and atomically install normalized conversation rows.

The live collector remains authoritative while staging runs.  Every staged
document records the exact source hash and byte size that produced its rows;
cutover refuses to proceed if even one source revision has changed.  This
lets a large corpus be reparsed while agents are active, with only a brief
ingest-worker pause for the final catch-up and transaction.

Examples::

    python -m server.scripts.reparse_conversations --stage
    python -m server.scripts.reparse_conversations --stage --run-id UUID --changed-only
    python -m server.scripts.reparse_conversations --status --run-id UUID
    python -m server.scripts.reparse_conversations --cutover --run-id UUID
    python -m server.scripts.reparse_conversations --refresh --run-id UUID
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass

import asyncpg

from server.config import settings
from server.services.ingest_service import (
    MAX_SEARCH_TEXT_CHARS,
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    _bounded_message_text,
    iter_stored_conversation_messages,
)
from server.services.large_content_store import read_large_content
from server.services.history_recovery import (
    UserOccurrence,
    partition_recovered_occurrences,
    recovered_occurrence_anchors,
)


PARSER_REVISION = "task-state-v4"
SUPPORTED_TOOLS = ("codex", "claude_code", "cursor")
COPY_BATCH_SIZE = 2_000
SOURCE_READ_SLACK_BYTES = 1
SPECIAL_MESSAGE_TYPES = ("history_user_message", "first_user_message")


class SourceChangedError(RuntimeError):
    """Raised when a collector commits a new revision during staging."""


@dataclass(frozen=True)
class SourceRevision:
    document_id: uuid.UUID
    tool_id: str
    content_s3_key: str | None
    content_hash: str
    file_size_bytes: int
    stored_source_revision: str | None
    stored_source_hash: str | None
    stored_source_size: int | None


def _database_dsn() -> str:
    return str(settings.database_url).replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )


def source_payload_error(
    payload: str,
    *,
    expected_hash: str,
    expected_size: int,
) -> str | None:
    """Return why a raw transcript is incomplete, or ``None`` when exact."""
    encoded = payload.encode("utf-8")
    actual_size = len(encoded)
    if actual_size != expected_size:
        return f"source byte size {actual_size} does not match {expected_size}"
    actual_hash = hashlib.sha256(encoded).hexdigest()
    if actual_hash != expected_hash:
        return "source SHA-256 does not match the document revision"
    return None


def cutover_manifest_error(
    *,
    eligible: int,
    staged: int,
    unverified: int,
    extra_manifest: int,
    preserve_unverified: bool,
) -> str | None:
    """Validate that cutover accounts for every eligible document exactly."""
    preserved = unverified if preserve_unverified else 0
    if staged + preserved == eligible and extra_manifest == 0:
        return None
    return (
        "cutover requires an exact staged document set; "
        f"eligible={eligible} staged={staged} "
        f"preserved_unverified={preserved} extra={extra_manifest}"
    )


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(_database_dsn(), command_timeout=1_800)


async def _ensure_tables(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE UNLOGGED TABLE IF NOT EXISTS conversation_reparse_runs (
            run_id uuid PRIMARY KEY,
            parser_revision text NOT NULL,
            state text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            staged_at timestamptz,
            cutover_at timestamptz,
            refreshed_at timestamptz
        )
        """
    )
    await conn.execute(
        """
        CREATE UNLOGGED TABLE IF NOT EXISTS conversation_reparse_manifest (
            run_id uuid NOT NULL REFERENCES conversation_reparse_runs(run_id)
                ON DELETE CASCADE,
            document_id uuid NOT NULL,
            tool_id text NOT NULL,
            source_hash text NOT NULL,
            source_size bigint NOT NULL,
            status text NOT NULL,
            parsed_count integer NOT NULL DEFAULT 0,
            user_count integer NOT NULL DEFAULT 0,
            assistant_count integer NOT NULL DEFAULT 0,
            error text,
            staged_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, document_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE UNLOGGED TABLE IF NOT EXISTS conversation_messages_reparse_stage (
            run_id uuid NOT NULL,
            document_id uuid NOT NULL,
            line_number integer NOT NULL,
            message_type varchar(50),
            role varchar(50),
            content text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            timestamp timestamptz,
            PRIMARY KEY (run_id, document_id, line_number)
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conv_reparse_stage_document
        ON conversation_messages_reparse_stage (run_id, document_id)
        """
    )


async def _create_or_load_run(
    conn: asyncpg.Connection,
    run_id: uuid.UUID | None,
) -> uuid.UUID:
    await _ensure_tables(conn)
    if run_id is None:
        run_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO conversation_reparse_runs
                (run_id, parser_revision, state)
            VALUES ($1, $2, 'staging')
            """,
            run_id,
            PARSER_REVISION,
        )
        return run_id

    row = await conn.fetchrow(
        "SELECT parser_revision, state FROM conversation_reparse_runs WHERE run_id=$1",
        run_id,
    )
    if row is None:
        await conn.execute(
            """
            INSERT INTO conversation_reparse_runs
                (run_id, parser_revision, state)
            VALUES ($1, $2, 'staging')
            """,
            run_id,
            PARSER_REVISION,
        )
    elif row["parser_revision"] != PARSER_REVISION:
        raise RuntimeError(
            "run uses a different parser revision; start a new reparse run"
        )
    elif row["state"] == "cutover":
        raise RuntimeError("run has already been cut over")
    return run_id


async def _document_ids(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    *,
    changed_only: bool,
) -> list[uuid.UUID]:
    if not changed_only:
        rows = await conn.fetch(
            """
            SELECT id
            FROM documents
            WHERE category='conversation' AND tool_id=ANY($1::text[])
            ORDER BY id
            """,
            list(SUPPORTED_TOOLS),
        )
    else:
        rows = await conn.fetch(
            """
            SELECT d.id
            FROM documents d
            LEFT JOIN conversation_reparse_manifest m
              ON m.run_id=$1 AND m.document_id=d.id
            WHERE d.category='conversation'
              AND d.tool_id=ANY($2::text[])
              AND (
                m.document_id IS NULL OR m.status <> 'staged'
                OR m.source_hash <> d.content_hash
                OR m.source_size <> d.file_size_bytes
              )
            ORDER BY d.id
            """,
            run_id,
            list(SUPPORTED_TOOLS),
        )
    return [row["id"] for row in rows]


async def _source_revision(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
) -> SourceRevision | None:
    row = await conn.fetchrow(
        """
        SELECT id, tool_id, content_s3_key, content_hash, file_size_bytes
             , metadata->>$3 AS stored_source_revision
             , metadata->>$4 AS stored_source_hash
             , (metadata->>$5)::bigint AS stored_source_size
        FROM documents
        WHERE id=$1 AND category='conversation' AND tool_id=ANY($2::text[])
        """,
        document_id,
        list(SUPPORTED_TOOLS),
        STORED_SOURCE_REVISION_KEY,
        STORED_SOURCE_HASH_KEY,
        STORED_SOURCE_SIZE_KEY,
    )
    if row is None:
        return None
    return SourceRevision(
        document_id=row["id"],
        tool_id=row["tool_id"],
        content_s3_key=row["content_s3_key"],
        content_hash=row["content_hash"],
        file_size_bytes=int(row["file_size_bytes"]),
        stored_source_revision=row["stored_source_revision"],
        stored_source_hash=row["stored_source_hash"],
        stored_source_size=(
            int(row["stored_source_size"])
            if row["stored_source_size"] is not None
            else None
        ),
    )


async def _source_payload(
    conn: asyncpg.Connection,
    source: SourceRevision,
) -> str:
    if source.content_s3_key:
        if source.stored_source_size is None:
            raise ValueError("stored source size is not recorded")
        return await asyncio.to_thread(
            read_large_content,
            source.content_s3_key,
            max_bytes=max(
                1,
                source.stored_source_size + SOURCE_READ_SLACK_BYTES,
            ),
        )
    content = await conn.fetchval(
        "SELECT content FROM documents WHERE id=$1",
        source.document_id,
    )
    return content or ""


async def _record_failure(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    source: SourceRevision,
    status: str,
    error: str,
) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            DELETE FROM conversation_messages_reparse_stage
            WHERE run_id=$1 AND document_id=$2
            """,
            run_id,
            source.document_id,
        )
        await conn.execute(
            """
            INSERT INTO conversation_reparse_manifest (
                run_id, document_id, tool_id, source_hash, source_size,
                status, parsed_count, user_count, assistant_count, error,
                staged_at
            ) VALUES ($1,$2,$3,$4,$5,$6,0,0,0,$7,now())
            ON CONFLICT (run_id, document_id) DO UPDATE SET
                tool_id=excluded.tool_id,
                source_hash=excluded.source_hash,
                source_size=excluded.source_size,
                status=excluded.status,
                parsed_count=0,
                user_count=0,
                assistant_count=0,
                error=excluded.error,
                staged_at=now()
            """,
            run_id,
            source.document_id,
            source.tool_id,
            source.content_hash,
            source.file_size_bytes,
            status,
            error[:2_000],
        )


async def _stage_once(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
) -> tuple[str, int]:
    source = await _source_revision(conn, document_id)
    if source is None:
        return "deleted", 0
    if (
        source.stored_source_revision != source.content_hash
        or not source.stored_source_hash
        or source.stored_source_size is None
    ):
        await _record_failure(
            conn,
            run_id,
            source,
            "incomplete",
            "persisted raw blob is not a verified full snapshot of this revision",
        )
        return "incomplete", 0
    try:
        payload = await _source_payload(conn, source)
    except Exception as exc:
        await _record_failure(conn, run_id, source, "incomplete", str(exc))
        return "incomplete", 0

    payload_error = source_payload_error(
        payload,
        expected_hash=source.stored_source_hash,
        expected_size=source.stored_source_size,
    )
    if payload_error:
        await _record_failure(
            conn,
            run_id,
            source,
            "incomplete",
            payload_error,
        )
        return "incomplete", 0

    parsed_count = 0
    user_count = 0
    assistant_count = 0
    batch: list[tuple] = []
    async with conn.transaction():
        await conn.execute(
            """
            DELETE FROM conversation_messages_reparse_stage
            WHERE run_id=$1 AND document_id=$2
            """,
            run_id,
            document_id,
        )
        for normalized, content, metadata, timestamp in (
            iter_stored_conversation_messages(payload, source.tool_id)
        ):
            parsed_count += 1
            user_count += normalized.role == "user"
            assistant_count += normalized.role == "assistant"
            batch.append(
                (
                    run_id,
                    document_id,
                    parsed_count,
                    _bounded_message_text(
                        normalized.raw_type or normalized.role,
                        50,
                    ),
                    normalized.role,
                    content,
                    json.dumps(metadata, ensure_ascii=False),
                    timestamp,
                )
            )
            if len(batch) >= COPY_BATCH_SIZE:
                await conn.copy_records_to_table(
                    "conversation_messages_reparse_stage",
                    records=batch,
                    columns=(
                        "run_id",
                        "document_id",
                        "line_number",
                        "message_type",
                        "role",
                        "content",
                        "metadata",
                        "timestamp",
                    ),
                )
                batch.clear()
        if batch:
            await conn.copy_records_to_table(
                "conversation_messages_reparse_stage",
                records=batch,
                columns=(
                    "run_id",
                    "document_id",
                    "line_number",
                    "message_type",
                    "role",
                    "content",
                    "metadata",
                    "timestamp",
                ),
            )

        current = await _source_revision(conn, document_id)
        if current != source:
            raise SourceChangedError("document changed while it was being staged")
        await conn.execute(
            """
            INSERT INTO conversation_reparse_manifest (
                run_id, document_id, tool_id, source_hash, source_size,
                status, parsed_count, user_count, assistant_count, error,
                staged_at
            ) VALUES ($1,$2,$3,$4,$5,'staged',$6,$7,$8,NULL,now())
            ON CONFLICT (run_id, document_id) DO UPDATE SET
                tool_id=excluded.tool_id,
                source_hash=excluded.source_hash,
                source_size=excluded.source_size,
                status='staged',
                parsed_count=excluded.parsed_count,
                user_count=excluded.user_count,
                assistant_count=excluded.assistant_count,
                error=NULL,
                staged_at=now()
            """,
            run_id,
            document_id,
            source.tool_id,
            source.content_hash,
            source.file_size_bytes,
            parsed_count,
            user_count,
            assistant_count,
        )
    return "staged", parsed_count


async def _stage_document(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
) -> tuple[str, int]:
    for attempt in range(3):
        try:
            return await _stage_once(conn, run_id, document_id)
        except SourceChangedError:
            if attempt == 2:
                source = await _source_revision(conn, document_id)
                if source is not None:
                    await _record_failure(
                        conn,
                        run_id,
                        source,
                        "changed",
                        "source changed during three consecutive stage attempts",
                    )
                return "changed", 0
        except Exception as exc:
            source = await _source_revision(conn, document_id)
            if source is not None:
                await _record_failure(
                    conn,
                    run_id,
                    source,
                    "error",
                    f"{type(exc).__name__}: {exc}",
                )
            return "error", 0
    raise AssertionError("unreachable")


async def stage(
    run_id: uuid.UUID | None,
    *,
    changed_only: bool,
    document_ids: list[uuid.UUID] | None = None,
) -> uuid.UUID:
    conn = await _connect()
    started = time.perf_counter()
    try:
        run_id = await _create_or_load_run(conn, run_id)
        if document_ids is None:
            document_ids = await _document_ids(
                conn,
                run_id,
                changed_only=changed_only,
            )
        totals = {
            "staged": 0,
            "incomplete": 0,
            "changed": 0,
            "deleted": 0,
            "error": 0,
        }
        messages = 0
        for index, document_id in enumerate(document_ids, start=1):
            status, count = await _stage_document(conn, run_id, document_id)
            totals[status] = totals.get(status, 0) + 1
            messages += count
            if index % 25 == 0 or index == len(document_ids):
                elapsed = time.perf_counter() - started
                print(
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "progress": f"{index}/{len(document_ids)}",
                            "messages": messages,
                            "elapsed_seconds": round(elapsed, 2),
                            **totals,
                        }
                    ),
                    flush=True,
                )
        await conn.execute(
            """
            UPDATE conversation_reparse_runs
            SET state='staged', staged_at=now()
            WHERE run_id=$1
            """,
            run_id,
        )
        print(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "state": "staged",
                    "documents_considered": len(document_ids),
                    "messages": messages,
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                    **totals,
                }
            ),
            flush=True,
        )
        return run_id
    finally:
        await conn.close()


async def status(run_id: uuid.UUID) -> dict:
    conn = await _connect()
    try:
        await _ensure_tables(conn)
        run = await conn.fetchrow(
            "SELECT * FROM conversation_reparse_runs WHERE run_id=$1",
            run_id,
        )
        if run is None:
            raise RuntimeError("reparse run not found")
        rows = await conn.fetch(
            """
            SELECT status, count(*) AS documents,
                   coalesce(sum(parsed_count), 0) AS messages,
                   coalesce(sum(user_count), 0) AS users,
                   coalesce(sum(assistant_count), 0) AS assistants
            FROM conversation_reparse_manifest
            WHERE run_id=$1
            GROUP BY status ORDER BY status
            """,
            run_id,
        )
        changed = await conn.fetchval(
            """
            SELECT count(*)
            FROM documents d
            JOIN conversation_reparse_manifest m
              ON m.run_id=$1 AND m.document_id=d.id
            WHERE m.status='staged'
              AND (m.source_hash <> d.content_hash
                   OR m.source_size <> d.file_size_bytes)
            """,
            run_id,
        )
        eligible = await conn.fetchval(
            """
            SELECT count(*) FROM documents
            WHERE category='conversation' AND tool_id=ANY($1::text[])
            """,
            list(SUPPORTED_TOOLS),
        )
        result = {
            "run_id": str(run_id),
            "state": run["state"],
            "parser_revision": run["parser_revision"],
            "eligible_documents": eligible,
            "changed_since_stage": changed,
            "manifest": [dict(row) for row in rows],
        }
        print(json.dumps(result, default=str), flush=True)
        return result
    finally:
        await conn.close()


async def _open_reparse_line_range(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    *,
    anchor: int,
    count: int,
    current_max: int,
) -> int:
    """Open a positive line range during atomic reparse cutover."""
    if anchor <= current_max:
        await conn.execute(
            """
            UPDATE conversation_messages
            SET line_number=-line_number
            WHERE document_id=$1 AND line_number >= $2
            """,
            document_id,
            anchor,
        )
        await conn.execute(
            """
            UPDATE conversation_messages
            SET line_number=-line_number + $4
            WHERE document_id=$1
              AND line_number BETWEEN $2 AND $3
            """,
            document_id,
            -current_max,
            -anchor,
            count,
        )
    return current_max + count


async def _restore_recovered_history(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
) -> int:
    """Restore only history prompts absent from freshly parsed rollout rows."""
    history_rows = await conn.fetch(
        """
        SELECT s.*
        FROM reparse_special_messages s
        JOIN conversation_reparse_manifest m
          ON m.run_id=$1 AND m.document_id=s.document_id
        WHERE m.status='staged'
          AND s.message_type='history_user_message'
        ORDER BY s.document_id, s.timestamp, s.line_number, s.id
        """,
        run_id,
    )
    by_document: dict[uuid.UUID, list[asyncpg.Record]] = {}
    for row in history_rows:
        by_document.setdefault(row["document_id"], []).append(row)

    restored = 0
    for document_id, recovered_rows in by_document.items():
        source_user_rows = await conn.fetch(
            """
            SELECT id, content, timestamp, line_number
            FROM conversation_messages
            WHERE document_id=$1 AND role='user'
              AND message_type IS DISTINCT FROM 'history_user_message'
            ORDER BY line_number
            """,
            document_id,
        )
        _, missing = partition_recovered_occurrences(
            [
                UserOccurrence(
                    key=row["id"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    line_number=row["line_number"],
                )
                for row in source_user_rows
            ],
            [
                UserOccurrence(
                    key=row["id"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    line_number=row["line_number"],
                )
                for row in recovered_rows
            ],
        )
        if not missing:
            continue

        timeline_rows = await conn.fetch(
            """
            SELECT id, content, timestamp, line_number
            FROM conversation_messages
            WHERE document_id=$1 AND line_number >= 1
              AND message_type IS DISTINCT FROM 'history_user_message'
            ORDER BY line_number
            """,
            document_id,
        )
        anchors = recovered_occurrence_anchors(
            [
                UserOccurrence(
                    key=row["id"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    line_number=row["line_number"],
                )
                for row in timeline_rows
            ],
            missing,
        )
        max_line = max(
            (row["line_number"] for row in timeline_rows),
            default=0,
        )
        recovered_by_id = {row["id"]: row for row in recovered_rows}
        temporary_start = -(max_line + len(missing) + 1)
        for index, occurrence in enumerate(missing):
            await conn.execute(
                """
                INSERT INTO conversation_messages (
                    id, document_id, line_number, message_type, role,
                    content, metadata, timestamp, created_at
                )
                SELECT id, document_id, $2, message_type, role,
                       content, metadata, timestamp, created_at
                FROM reparse_special_messages
                WHERE id=$1
                """,
                occurrence.key,
                temporary_start + index,
            )

        groups: dict[int, list[asyncpg.Record]] = {}
        for occurrence in missing:
            groups.setdefault(anchors[occurrence.key], []).append(
                recovered_by_id[occurrence.key]
            )
        current_max = max_line
        for anchor in sorted(groups, reverse=True):
            rows = sorted(
                groups[anchor],
                key=lambda row: (
                    row["timestamp"].timestamp()
                    if row["timestamp"] is not None
                    else float("inf"),
                    row["id"],
                ),
            )
            count = len(rows)
            current_max = await _open_reparse_line_range(
                conn,
                document_id,
                anchor=anchor,
                count=count,
                current_max=current_max,
            )
            for index, row in enumerate(rows):
                await conn.execute(
                    """
                    UPDATE conversation_messages
                    SET line_number=$2
                    WHERE id=$1
                    """,
                    row["id"],
                    anchor + index,
                )
        restored += len(missing)
    return restored


async def _restore_first_user_messages(
    conn: asyncpg.Connection,
    run_id: uuid.UUID,
) -> int:
    """Restore sparse-source user fallbacks at a reader-addressable line."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (s.document_id) s.*
        FROM reparse_special_messages s
        JOIN conversation_reparse_manifest m
          ON m.run_id=$1 AND m.document_id=s.document_id
        WHERE m.status='staged'
          AND s.message_type='first_user_message'
        ORDER BY s.document_id, s.line_number, s.id
        """,
        run_id,
    )
    restored = 0
    for row in rows:
        document_id = row["document_id"]
        has_user = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM conversation_messages
                WHERE document_id=$1 AND role='user'
            )
            """,
            document_id,
        )
        if has_user:
            continue
        placement = await conn.fetchrow(
            """
            SELECT min(line_number) FILTER (
                       WHERE role IS DISTINCT FROM 'system'
                   ) AS first_non_system,
                   coalesce(max(line_number), 0) AS max_line
            FROM conversation_messages
            WHERE document_id=$1 AND line_number >= 1
            """,
            document_id,
        )
        max_line = placement["max_line"]
        anchor = placement["first_non_system"] or max_line + 1
        await _open_reparse_line_range(
            conn,
            document_id,
            anchor=anchor,
            count=1,
            current_max=max_line,
        )
        await conn.execute(
            """
            INSERT INTO conversation_messages (
                id, document_id, line_number, message_type, role,
                content, metadata, timestamp, created_at
            )
            SELECT id, document_id, $2, message_type, role,
                   content, metadata, timestamp, created_at
            FROM reparse_special_messages
            WHERE id=$1
            """,
            row["id"],
            anchor,
        )
        restored += 1
    return restored


async def cutover(
    run_id: uuid.UUID,
    *,
    preserve_unverified: bool,
) -> dict:
    """Atomically replace normalized rows for an exact, stable staged set.

    When explicitly requested, stable legacy sources that cannot be verified as
    full raw snapshots are left untouched.  Every other eligible document must
    still have a staged replacement; errors, deleted sources, and newly added
    documents remain hard failures.
    """
    conn = await _connect()
    started = time.perf_counter()
    try:
        await _ensure_tables(conn)
        async with conn.transaction(isolation="serializable"):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('memento-conversation-reparse'))"
            )
            # Cutover is a bounded maintenance operation over the complete
            # normalized corpus.  The API's normal statement timeout is too
            # short for a large atomic replacement, so give each statement a
            # generous local ceiling without weakening the transaction or
            # changing the timeout for any other connection.
            await conn.execute("SET LOCAL statement_timeout = '15min'")
            run = await conn.fetchrow(
                """
                SELECT parser_revision, state
                FROM conversation_reparse_runs WHERE run_id=$1 FOR UPDATE
                """,
                run_id,
            )
            if run is None:
                raise RuntimeError("reparse run not found")
            if run["parser_revision"] != PARSER_REVISION:
                raise RuntimeError("reparse parser revision no longer matches")
            if run["state"] == "cutover":
                raise RuntimeError("run has already been cut over")

            eligible = await conn.fetchval(
                """
                SELECT count(*) FROM documents
                WHERE category='conversation' AND tool_id=ANY($1::text[])
                """,
                list(SUPPORTED_TOOLS),
            )
            staged = await conn.fetchval(
                """
                SELECT count(*)
                FROM conversation_reparse_manifest m
                JOIN documents d ON d.id=m.document_id
                WHERE m.run_id=$1 AND m.status='staged'
                  AND d.category='conversation'
                  AND d.tool_id=ANY($2::text[])
                """,
                run_id,
                list(SUPPORTED_TOOLS),
            )
            unverified = await conn.fetchval(
                """
                SELECT count(*)
                FROM conversation_reparse_manifest m
                JOIN documents d ON d.id=m.document_id
                WHERE m.run_id=$1 AND m.status='incomplete'
                  AND d.category='conversation'
                  AND d.tool_id=ANY($2::text[])
                """,
                run_id,
                list(SUPPORTED_TOOLS),
            )
            extra_manifest = await conn.fetchval(
                """
                SELECT count(*)
                FROM conversation_reparse_manifest m
                LEFT JOIN documents d ON d.id=m.document_id
                WHERE m.run_id=$1
                  AND m.status=ANY($2::text[])
                  AND (
                    d.id IS NULL OR d.category <> 'conversation'
                    OR d.tool_id <> ALL($3::text[])
                  )
                """,
                run_id,
                ['staged', 'incomplete'],
                list(SUPPORTED_TOOLS),
            )
            manifest_error = cutover_manifest_error(
                eligible=eligible,
                staged=staged,
                unverified=unverified,
                extra_manifest=extra_manifest,
                preserve_unverified=preserve_unverified,
            )
            if manifest_error:
                raise RuntimeError(manifest_error)
            stale = await conn.fetch(
                """
                SELECT d.id
                FROM documents d
                JOIN conversation_reparse_manifest m
                  ON m.run_id=$1 AND m.document_id=d.id
                WHERE m.status=ANY($2::text[])
                  AND (m.source_hash <> d.content_hash
                       OR m.source_size <> d.file_size_bytes)
                LIMIT 10
                """,
                run_id,
                (
                    ['staged', 'incomplete']
                    if preserve_unverified
                    else ['staged']
                ),
            )
            if stale:
                raise RuntimeError(
                    "staged sources changed; run --stage --changed-only before cutover"
                )

            await conn.fetch(
                """
                SELECT d.id
                FROM documents d
                JOIN conversation_reparse_manifest m
                  ON m.run_id=$1 AND m.document_id=d.id
                WHERE m.status='staged'
                FOR UPDATE OF d
                """,
                run_id,
            )
            await conn.execute("DROP TABLE IF EXISTS reparse_special_messages")
            await conn.execute(
                """
                CREATE TEMP TABLE reparse_special_messages ON COMMIT DROP AS
                SELECT cm.*
                FROM conversation_messages cm
                JOIN conversation_reparse_manifest m
                  ON m.run_id=$1 AND m.document_id=cm.document_id
                WHERE m.status='staged'
                  AND cm.message_type=ANY($2::text[])
                """,
                run_id,
                list(SPECIAL_MESSAGE_TYPES),
            )
            deleted = await conn.fetchval(
                """
                WITH removed AS (
                    DELETE FROM conversation_messages cm
                    USING conversation_reparse_manifest m
                    WHERE m.run_id=$1 AND m.status='staged'
                      AND m.document_id=cm.document_id
                    RETURNING 1
                ) SELECT count(*) FROM removed
                """,
                run_id,
            )
            inserted = await conn.fetchval(
                """
                WITH added AS (
                    INSERT INTO conversation_messages (
                        document_id, line_number, message_type, role,
                        content, metadata, timestamp
                    )
                    SELECT document_id, line_number, message_type, role,
                           content, metadata, timestamp
                    FROM conversation_messages_reparse_stage
                    WHERE run_id=$1
                    RETURNING 1
                ) SELECT count(*) FROM added
                """,
                run_id,
            )
            restored_history = await _restore_recovered_history(conn, run_id)
            restored_first = await _restore_first_user_messages(conn, run_id)
            invalidated_embeddings = await conn.fetchval(
                """
                WITH removed AS (
                    DELETE FROM document_embeddings e
                    USING documents d, conversation_reparse_manifest m
                    WHERE m.run_id=$1 AND m.status='staged'
                      AND m.document_id=d.id
                      AND d.content_s3_key IS NOT NULL
                      AND e.document_id=d.id
                    RETURNING 1
                ) SELECT count(*) FROM removed
                """,
                run_id,
            )
            await conn.execute(
                """
                UPDATE documents d SET
                    embedding_status='pending',
                    embedding_attempts=0,
                    embedding_claim_token=NULL,
                    embedding_claimed_at=NULL
                FROM conversation_reparse_manifest m
                WHERE m.run_id=$1 AND m.status='staged'
                  AND m.document_id=d.id
                  AND d.content_s3_key IS NOT NULL
                """,
                run_id,
            )
            await conn.execute(
                """
                UPDATE conversation_reparse_runs
                SET state='cutover', cutover_at=now()
                WHERE run_id=$1
                """,
                run_id,
            )
        result = {
            "run_id": str(run_id),
            "state": "cutover",
            "documents": staged,
            "preserved_unverified_documents": (
                unverified if preserve_unverified else 0
            ),
            "deleted_rows": deleted,
            "inserted_rows": inserted,
            "restored_history_rows": restored_history,
            "restored_first_user_rows": restored_first,
            "invalidated_embeddings": invalidated_embeddings,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }
        print(json.dumps(result), flush=True)
        return result
    finally:
        await conn.close()


async def refresh(run_id: uuid.UUID, *, batch_size: int) -> None:
    """Refresh activity timestamps and bounded search after the row swap.

    Search inputs are prepared in bounded read batches, then copied into a
    temporary table and applied with one set-based update.  This avoids an
    indexed ``documents`` rewrite for every conversation.
    """
    from server.services.tokenize import tokenize_for_index

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            SELECT document_id FROM conversation_reparse_manifest
            WHERE run_id=$1 AND status='staged' ORDER BY document_id
            """,
            run_id,
        )
        document_ids = [row["document_id"] for row in rows]
        refresh_records: list[tuple[uuid.UUID, object, str]] = []
        for offset in range(0, len(document_ids), batch_size):
            batch_ids = document_ids[offset:offset + batch_size]
            message_rows = await conn.fetch(
                """
                WITH target AS (
                    SELECT document_id, ordinal
                    FROM unnest($1::uuid[]) WITH ORDINALITY
                         AS ids(document_id, ordinal)
                )
                SELECT target.document_id, target.ordinal, d.title,
                       activity.activity_at,
                       recent.content, recent.line_number
                FROM target
                JOIN documents d ON d.id=target.document_id
                LEFT JOIN LATERAL (
                    SELECT cm.timestamp AS activity_at
                    FROM conversation_messages cm
                    WHERE cm.document_id=target.document_id
                      AND cm.role IN ('user','assistant')
                      AND cm.timestamp IS NOT NULL
                    ORDER BY cm.timestamp DESC
                    LIMIT 1
                ) activity ON true
                LEFT JOIN LATERAL (
                    SELECT recent_rows.content, recent_rows.line_number
                    FROM (
                        SELECT cm.content, cm.line_number
                        FROM conversation_messages cm
                        WHERE cm.document_id=target.document_id
                          AND cm.role IN ('user','assistant')
                        ORDER BY cm.line_number DESC
                        LIMIT 200
                    ) recent_rows
                    ORDER BY recent_rows.line_number
                ) recent ON true
                ORDER BY target.ordinal, recent.line_number NULLS FIRST
                """,
                batch_ids,
            )
            prepared: dict[uuid.UUID, dict[str, object]] = {}
            for row in message_rows:
                document_id = row["document_id"]
                record = prepared.setdefault(
                    document_id,
                    {
                        "title": row["title"],
                        "activity_at": row["activity_at"],
                        "messages": [],
                    },
                )
                content = row["content"]
                if content:
                    record["messages"].append(content)
            missing = set(batch_ids).difference(prepared)
            if missing:
                raise RuntimeError(
                    f"refresh documents disappeared: {len(missing)}"
                )
            for document_id in batch_ids:
                record = prepared[document_id]
                search_text = _bounded_message_text(
                    "\n".join(record["messages"]),
                    MAX_SEARCH_TEXT_CHARS,
                )
                tsv_input = tokenize_for_index(
                    f"{record['title'] or ''} {search_text}"
                )
                refresh_records.append(
                    (document_id, record["activity_at"], tsv_input)
                )
            print(
                json.dumps(
                    {
                        "run_id": str(run_id),
                        "prepared": min(
                            offset + batch_size,
                            len(document_ids),
                        ),
                        "total": len(document_ids),
                    }
                ),
                flush=True,
            )

        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '30min'")
            await conn.execute(
                """
                CREATE TEMP TABLE conversation_reparse_refresh (
                    document_id uuid PRIMARY KEY,
                    activity_at timestamptz,
                    tsv_input text NOT NULL
                ) ON COMMIT DROP
                """
            )
            await conn.copy_records_to_table(
                "conversation_reparse_refresh",
                records=refresh_records,
                columns=("document_id", "activity_at", "tsv_input"),
            )
            matched = await conn.fetchval(
                """
                SELECT count(*)
                FROM documents d
                JOIN conversation_reparse_refresh r
                  ON r.document_id=d.id
                """
            )
            if matched != len(document_ids):
                raise RuntimeError(
                    "refresh document count changed; "
                    f"expected={len(document_ids)} actual={matched}"
                )
            updated = await conn.fetchval(
                """
                WITH prepared AS MATERIALIZED (
                    SELECT document_id, activity_at,
                           to_tsvector('simple', tsv_input) AS content_tsv
                    FROM conversation_reparse_refresh
                ), changed AS (
                    UPDATE documents d SET
                        activity_at=p.activity_at,
                        content_tsv=p.content_tsv,
                        updated_at=now()
                    FROM prepared p
                    WHERE d.id=p.document_id
                      AND (
                        d.activity_at IS DISTINCT FROM p.activity_at
                        OR d.content_tsv IS DISTINCT FROM p.content_tsv
                      )
                    RETURNING 1
                ) SELECT count(*) FROM changed
                """
            )
            await conn.execute(
                """
                UPDATE conversation_reparse_runs SET refreshed_at=now()
                WHERE run_id=$1
                """,
                run_id,
            )
        print(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "refreshed": matched,
                    "changed": updated,
                    "total": len(document_ids),
                }
            ),
            flush=True,
        )
    finally:
        await conn.close()


def _parse_run_id(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--cutover", action="store_true")
    action.add_argument("--refresh", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--changed-only", action="store_true")
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="stage only this document (repeatable; intended for guarded retries)",
    )
    parser.add_argument(
        "--preserve-unverified",
        "--allow-incomplete",
        dest="preserve_unverified",
        action="store_true",
        help=(
            "preserve stable legacy documents without a verified raw snapshot; "
            "all other eligible documents must still be staged"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()
    run_id = _parse_run_id(args.run_id)

    if args.stage:
        document_ids = [uuid.UUID(value) for value in args.document_id] or None
        asyncio.run(stage(
            run_id,
            changed_only=args.changed_only,
            document_ids=document_ids,
        ))
        return
    if run_id is None:
        parser.error("--run-id is required for this action")
    if args.status:
        asyncio.run(status(run_id))
    elif args.cutover:
        asyncio.run(cutover(
            run_id,
            preserve_unverified=args.preserve_unverified,
        ))
    else:
        asyncio.run(refresh(run_id, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
