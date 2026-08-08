"""Deployment-run PostgreSQL index migrations.

Large index builds must not run inside the API lifespan or its schema
transaction.  This module is invoked by the deployment migration controller (or
the operator command), owns a small explicit plan, and uses a session advisory
lock as the deployment-wide concurrency boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("memento.online_migrations")

ONLINE_MIGRATION_LOCK_KEY = 0x4D454D4F494E4458
REPLACEMENT_INDEX_NAME = "idx_documents_content_tsv"


@dataclass(frozen=True, slots=True)
class OnlineIndexMigration:
    name: str
    operation: Literal["create", "drop"]
    ddl: str


ONLINE_INDEX_MIGRATIONS = (
    # The document search predicate is content_tsv @@ to_tsquery(...). Build
    # its required GIN index before removing the write-heavy raw-content index.
    OnlineIndexMigration(
        name="idx_documents_content_tsv",
        operation="create",
        ddl=(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ),
    ),
    # No query uses documents.content ILIKE after the bounded tsvector path
    # landed, so this large GIN index only adds ingest/update maintenance.
    OnlineIndexMigration(
        name="idx_documents_content_trgm",
        operation="drop",
        ddl="DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_trgm",
    ),
    # The unique index has the identical leading columns and supports every
    # lookup/order use of this redundant non-unique index.
    OnlineIndexMigration(
        name="idx_conv_msg_document",
        operation="drop",
        ddl="DROP INDEX CONCURRENTLY IF EXISTS idx_conv_msg_document",
    ),
)

_INDEX_VALIDITY_SQL = text(
    "SELECT catalog_index.indisvalid AND catalog_index.indisready "
    "FROM pg_class AS relation "
    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
    "JOIN pg_index AS catalog_index ON catalog_index.indexrelid = relation.oid "
    "WHERE namespace.nspname = current_schema() "
    "AND relation.relname = :name"
)

_REPLACEMENT_VALIDITY_SQL = text(
    "SELECT catalog_index.indisvalid AND catalog_index.indisready "
    "AND access_method.amname = 'gin' "
    "AND catalog_index.indnkeyatts = 1 "
    "AND catalog_index.indnatts = 1 "
    "AND catalog_index.indexprs IS NULL "
    "AND catalog_index.indpred IS NULL "
    "AND catalog_index.indkey[0] = column_attribute.attnum "
    "FROM pg_class AS index_relation "
    "JOIN pg_namespace AS namespace "
    "ON namespace.oid = index_relation.relnamespace "
    "JOIN pg_index AS catalog_index "
    "ON catalog_index.indexrelid = index_relation.oid "
    "JOIN pg_class AS table_relation "
    "ON table_relation.oid = catalog_index.indrelid "
    "JOIN pg_am AS access_method "
    "ON access_method.oid = index_relation.relam "
    "JOIN pg_attribute AS column_attribute "
    "ON column_attribute.attrelid = table_relation.oid "
    "AND column_attribute.attname = 'content_tsv' "
    "AND NOT column_attribute.attisdropped "
    "WHERE namespace.nspname = current_schema() "
    "AND index_relation.relname = :name "
    "AND table_relation.relname = 'documents'"
)

_INDEX_STATUS_SQL = text(
    "SELECT "
    "catalog_index.indisvalid AS valid, "
    "catalog_index.indisready AS ready, "
    "pg_get_indexdef(relation.oid) AS definition "
    "FROM pg_class AS relation "
    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
    "JOIN pg_index AS catalog_index ON catalog_index.indexrelid = relation.oid "
    "WHERE namespace.nspname = current_schema() "
    "AND relation.relname = :name"
)

_STATE_TABLE_DDL = text(
    "CREATE TABLE IF NOT EXISTS online_index_migration_state ("
    "migration_name TEXT PRIMARY KEY, "
    "operation TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0, "
    "executor_id TEXT, "
    "started_at TIMESTAMPTZ, "
    "finished_at TIMESTAMPTZ, "
    "error TEXT, "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()"
    ")"
)

_STATE_RUNNING_SQL = text(
    "INSERT INTO online_index_migration_state ("
    "migration_name, operation, status, attempts, executor_id, started_at, "
    "finished_at, error, updated_at"
    ") VALUES ("
    ":name, :operation, 'running', 1, :executor_id, clock_timestamp(), "
    "NULL, NULL, clock_timestamp()"
    ") ON CONFLICT (migration_name) DO UPDATE SET "
    "operation = EXCLUDED.operation, "
    "status = 'running', "
    "attempts = online_index_migration_state.attempts + 1, "
    "executor_id = EXCLUDED.executor_id, "
    "started_at = EXCLUDED.started_at, "
    "finished_at = NULL, "
    "error = NULL, "
    "updated_at = EXCLUDED.updated_at"
)

_STATE_TERMINAL_SQL = text(
    "INSERT INTO online_index_migration_state ("
    "migration_name, operation, status, attempts, executor_id, started_at, "
    "finished_at, error, updated_at"
    ") VALUES ("
    ":name, :operation, :status, 0, :executor_id, NULL, "
    "clock_timestamp(), :error, clock_timestamp()"
    ") ON CONFLICT (migration_name) DO UPDATE SET "
    "operation = EXCLUDED.operation, "
    "status = EXCLUDED.status, "
    "executor_id = EXCLUDED.executor_id, "
    "finished_at = EXCLUDED.finished_at, "
    "error = EXCLUDED.error, "
    "updated_at = EXCLUDED.updated_at"
)

_STATE_TABLE_EXISTS_SQL = text(
    "SELECT to_regclass('online_index_migration_state') IS NOT NULL"
)

_STATE_STATUS_SQL = text(
    "SELECT migration_name, operation, status, attempts, executor_id, "
    "started_at, finished_at, error, updated_at "
    "FROM online_index_migration_state ORDER BY migration_name"
)

_PROGRESS_SQL = text(
    "SELECT progress.pid, progress.command, progress.phase, "
    "table_relation.relname AS table_name, "
    "index_relation.relname AS index_name, "
    "progress.lockers_total, progress.lockers_done, "
    "progress.blocks_total, progress.blocks_done, "
    "progress.tuples_total, progress.tuples_done "
    "FROM pg_stat_progress_create_index AS progress "
    "LEFT JOIN pg_class AS table_relation "
    "ON table_relation.oid = progress.relid "
    "LEFT JOIN pg_class AS index_relation "
    "ON index_relation.oid = progress.index_relid "
    "WHERE progress.datid = ("
    "SELECT oid FROM pg_database WHERE datname = current_database()"
    ") ORDER BY progress.pid"
)


def online_migration_plan() -> list[dict[str, str]]:
    """Return a serializable, ordered rollout plan for operators and tests."""
    return [asdict(migration) for migration in ONLINE_INDEX_MIGRATIONS]


async def _index_validity(connection, name: str) -> bool | None:
    value = await connection.scalar(_INDEX_VALIDITY_SQL, {"name": name})
    return None if value is None else bool(value)


async def _replacement_validity(connection) -> bool | None:
    value = await connection.scalar(
        _REPLACEMENT_VALIDITY_SQL,
        {"name": REPLACEMENT_INDEX_NAME},
    )
    return None if value is None else bool(value)


def _executor_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


async def _mark_running(
    connection,
    migration: OnlineIndexMigration,
    executor: str,
) -> None:
    await connection.execute(
        _STATE_RUNNING_SQL,
        {
            "name": migration.name,
            "operation": migration.operation,
            "executor_id": executor,
        },
    )


async def _mark_terminal(
    connection,
    migration: OnlineIndexMigration,
    executor: str,
    status: Literal["succeeded", "failed", "interrupted"],
    error: str | None = None,
) -> None:
    await connection.execute(
        _STATE_TERMINAL_SQL,
        {
            "name": migration.name,
            "operation": migration.operation,
            "status": status,
            "executor_id": executor,
            "error": error[:4000] if error else None,
        },
    )


async def _mark_terminal_safely(
    connection,
    migration: OnlineIndexMigration,
    executor: str,
    status: Literal["failed", "interrupted"],
    error: str,
) -> None:
    try:
        await _mark_terminal(connection, migration, executor, status, error)
    except Exception:
        logger.exception(
            "Could not persist %s state for online migration %s",
            status,
            migration.name,
        )


async def _mark_interrupted_safely(
    engine: AsyncEngine,
    migration: OnlineIndexMigration,
    executor: str,
    error: str,
) -> None:
    """Persist cancellation on a fresh connection with a bounded wait.

    A driver may invalidate the connection whose concurrent DDL was cancelled.
    The catalog remains the restart authority, but a separate connection makes
    the durable operator state reliable during graceful pod termination.
    """

    async def write_state() -> None:
        async with engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await _mark_terminal(
                connection,
                migration,
                executor,
                "interrupted",
                error,
            )

    try:
        await asyncio.wait_for(write_state(), timeout=5)
    except Exception:
        logger.exception(
            "Could not persist interrupted state for online migration %s",
            migration.name,
        )


def _json_safe_row(row) -> dict:
    values = dict(row)
    for key, value in values.items():
        if isinstance(value, (date, datetime)):
            values[key] = value.isoformat()
    return values


async def online_migration_status(engine: AsyncEngine) -> dict:
    """Return durable step state, catalog validity, and live build progress."""
    async with engine.connect() as connection:
        persisted: dict[str, dict] = {}
        if bool(await connection.scalar(_STATE_TABLE_EXISTS_SQL)):
            state_result = await connection.execute(_STATE_STATUS_SQL)
            persisted = {
                row["migration_name"]: _json_safe_row(row)
                for row in state_result.mappings().all()
            }

        migrations: list[dict] = []
        for migration in ONLINE_INDEX_MIGRATIONS:
            result = await connection.execute(
                _INDEX_STATUS_SQL,
                {"name": migration.name},
            )
            catalog_row = result.mappings().one_or_none()
            migrations.append(
                {
                    **asdict(migration),
                    "catalog": (
                        {
                            "exists": True,
                            **_json_safe_row(catalog_row),
                        }
                        if catalog_row is not None
                        else {
                            "exists": False,
                            "valid": None,
                            "ready": None,
                            "definition": None,
                        }
                    ),
                    "state": persisted.get(migration.name),
                }
            )

        progress_result = await connection.execute(_PROGRESS_SQL)
        progress = [
            _json_safe_row(row) for row in progress_result.mappings().all()
        ]

    return {
        "lock_key": ONLINE_MIGRATION_LOCK_KEY,
        "migrations": migrations,
        "progress": progress,
    }


async def run_online_index_migrations(engine: AsyncEngine) -> dict:
    """Apply the online plan on one AUTOCOMMIT connection.

    Interrupted concurrent builds can leave an invalid index relation. A later
    run drops that invalid relation concurrently before retrying the build;
    ``IF NOT EXISTS`` alone would incorrectly treat it as complete.
    """
    applied: list[str] = []
    skipped: list[str] = []
    executor = _executor_id()
    async with engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": ONLINE_MIGRATION_LOCK_KEY},
            )
        )
        if not acquired:
            logger.info("Online index migrations already running on another replica")
            return {
                "locked": True,
                "executor": executor,
                "applied": applied,
                "skipped": skipped,
            }

        try:
            # The deployment-wide two-minute query timeout is appropriate for
            # requests but can cancel a legitimate large concurrent build.
            # Reset it before this pooled connection is returned.
            await connection.execute(text("SET statement_timeout = 0"))
            await connection.execute(
                text(
                    "SET application_name = "
                    "'memento-online-index-migration'"
                )
            )
            # This state table is deliberately created on the AUTOCOMMIT
            # migration connection, never in the API schema transaction.
            await connection.execute(_STATE_TABLE_DDL)
            for migration in ONLINE_INDEX_MIGRATIONS:
                validity = await _index_validity(connection, migration.name)
                if migration.operation == "create":
                    replacement_validity = await _replacement_validity(connection)
                    if replacement_validity is True:
                        skipped.append(migration.name)
                        await _mark_terminal(
                            connection,
                            migration,
                            executor,
                            "succeeded",
                        )
                        continue
                    await _mark_running(connection, migration, executor)
                    logger.info(
                        "Starting online index migration %s executor=%s",
                        migration.name,
                        executor,
                    )
                    try:
                        # A valid but structurally wrong relation with the
                        # expected name must not satisfy IF NOT EXISTS.
                        if validity is not None:
                            await connection.execute(
                                text(
                                    "DROP INDEX CONCURRENTLY IF EXISTS "
                                    f"{migration.name}"
                                )
                            )
                        await connection.execute(text(migration.ddl))
                        if await _replacement_validity(connection) is not True:
                            raise RuntimeError(
                                f"Replacement index {migration.name} did not "
                                "become valid and ready"
                            )
                    except asyncio.CancelledError:
                        await _mark_interrupted_safely(
                            engine,
                            migration,
                            executor,
                            "executor cancelled; a later run will repair any "
                            "invalid concurrent index",
                        )
                        logger.warning(
                            "Online index migration %s was interrupted",
                            migration.name,
                        )
                        raise
                    except Exception as exc:
                        await _mark_terminal_safely(
                            connection,
                            migration,
                            executor,
                            "failed",
                            str(exc),
                        )
                        logger.exception(
                            "Online index migration %s failed",
                            migration.name,
                        )
                        raise
                    await _mark_terminal(
                        connection,
                        migration,
                        executor,
                        "succeeded",
                    )
                    applied.append(migration.name)
                    continue

                try:
                    # The replacement must be usable at the exact point each
                    # old/redundant index is removed, not merely earlier in the
                    # process.
                    replacement_validity = await _replacement_validity(connection)
                    if replacement_validity is not True:
                        await _mark_running(connection, migration, executor)
                        raise RuntimeError(
                            f"Refusing to drop {migration.name}: replacement "
                            f"{REPLACEMENT_INDEX_NAME} is not valid and ready"
                        )
                    if validity is None:
                        skipped.append(migration.name)
                        await _mark_terminal(
                            connection,
                            migration,
                            executor,
                            "succeeded",
                        )
                        continue
                    await _mark_running(connection, migration, executor)
                    logger.info(
                        "Starting online index migration %s executor=%s",
                        migration.name,
                        executor,
                    )
                    await connection.execute(text(migration.ddl))
                    if await _index_validity(connection, migration.name) is not None:
                        raise RuntimeError(
                            f"Dropped index {migration.name} is still present"
                        )
                except asyncio.CancelledError:
                    await _mark_interrupted_safely(
                        engine,
                        migration,
                        executor,
                        "executor cancelled while dropping an old index",
                    )
                    logger.warning(
                        "Online index migration %s was interrupted",
                        migration.name,
                    )
                    raise
                except Exception as exc:
                    await _mark_terminal_safely(
                        connection,
                        migration,
                        executor,
                        "failed",
                        str(exc),
                    )
                    logger.exception(
                        "Online index migration %s failed",
                        migration.name,
                    )
                    raise
                await _mark_terminal(
                    connection,
                    migration,
                    executor,
                    "succeeded",
                )
                applied.append(migration.name)
        finally:
            for cleanup_statement in (
                "RESET statement_timeout",
                "RESET application_name",
            ):
                try:
                    await connection.execute(text(cleanup_statement))
                except Exception:
                    logger.exception(
                        "Online migration connection cleanup failed: %s",
                        cleanup_statement,
                    )
            try:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": ONLINE_MIGRATION_LOCK_KEY},
                )
            except Exception:
                # Closing the connection also releases session advisory locks.
                logger.exception("Could not explicitly release online migration lock")

    logger.info(
        "Online index migrations complete: applied=%s skipped=%s",
        applied,
        skipped,
    )
    return {
        "locked": False,
        "executor": executor,
        "applied": applied,
        "skipped": skipped,
    }
