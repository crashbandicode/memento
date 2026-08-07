"""Out-of-transaction PostgreSQL index migrations.

Large index builds must not run inside the startup schema transaction: ordinary
``CREATE INDEX`` blocks writers, while ``CONCURRENTLY`` is rejected in a
transaction block. This module owns the small, explicit rollout plan and uses a
session advisory lock so multiple API replicas cannot race the same DDL.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("memento.online_migrations")

ONLINE_MIGRATION_LOCK_KEY = 0x4D454D4F494E4458


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
    "SELECT catalog_index.indisvalid "
    "FROM pg_class AS relation "
    "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
    "JOIN pg_index AS catalog_index ON catalog_index.indexrelid = relation.oid "
    "WHERE namespace.nspname = current_schema() "
    "AND relation.relname = :name"
)


def online_migration_plan() -> list[dict[str, str]]:
    """Return a serializable, ordered rollout plan for operators and tests."""
    return [asdict(migration) for migration in ONLINE_INDEX_MIGRATIONS]


async def _index_validity(connection, name: str) -> bool | None:
    value = await connection.scalar(_INDEX_VALIDITY_SQL, {"name": name})
    return None if value is None else bool(value)


async def run_online_index_migrations(engine: AsyncEngine) -> dict:
    """Apply the online plan on one AUTOCOMMIT connection.

    Interrupted concurrent builds can leave an invalid index relation. A later
    run drops that invalid relation concurrently before retrying the build;
    ``IF NOT EXISTS`` alone would incorrectly treat it as complete.
    """
    applied: list[str] = []
    skipped: list[str] = []
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
                "applied": applied,
                "skipped": skipped,
            }

        try:
            # The deployment-wide two-minute query timeout is appropriate for
            # requests but can cancel a legitimate large concurrent build.
            # Reset it before this pooled connection is returned.
            await connection.execute(text("SET statement_timeout = 0"))
            for migration in ONLINE_INDEX_MIGRATIONS:
                validity = await _index_validity(connection, migration.name)
                if migration.operation == "create":
                    if validity is True:
                        skipped.append(migration.name)
                        continue
                    if validity is False:
                        await connection.execute(
                            text(
                                "DROP INDEX CONCURRENTLY IF EXISTS "
                                f"{migration.name}"
                            )
                        )
                    await connection.execute(text(migration.ddl))
                    applied.append(migration.name)
                    continue

                if validity is None:
                    skipped.append(migration.name)
                    continue
                await connection.execute(text(migration.ddl))
                applied.append(migration.name)
        finally:
            try:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": ONLINE_MIGRATION_LOCK_KEY},
                )
            finally:
                await connection.execute(text("RESET statement_timeout"))

    logger.info(
        "Online index migrations complete: applied=%s skipped=%s",
        applied,
        skipped,
    )
    return {
        "locked": False,
        "applied": applied,
        "skipped": skipped,
    }
