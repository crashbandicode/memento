from __future__ import annotations

import inspect
import re

import pytest

from server import main as server_main
from server.db.models import ConversationMessage
from server.db.online_migrations import (
    online_migration_plan,
    run_online_index_migrations,
)


def test_online_plan_creates_required_index_before_concurrent_drops() -> None:
    plan = online_migration_plan()

    assert [step["name"] for step in plan] == [
        "idx_documents_content_tsv",
        "idx_documents_content_trgm",
        "idx_conv_msg_document",
    ]
    assert plan[0] == {
        "name": "idx_documents_content_tsv",
        "operation": "create",
        "ddl": (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ),
    }
    assert all("CONCURRENTLY" in step["ddl"] for step in plan)


def test_startup_transaction_has_no_document_body_index_ddl() -> None:
    source = inspect.getsource(server_main._run_migrations)

    assert "idx_documents_content_tsv" not in source
    assert "idx_documents_content_trgm" not in source


def test_model_keeps_only_unique_document_line_index() -> None:
    indexes = {index.name: index for index in ConversationMessage.__table__.indexes}

    assert "idx_conv_msg_document" not in indexes
    assert indexes["uq_conv_msg_doc_line"].unique is True
    assert [column.name for column in indexes["uq_conv_msg_doc_line"].columns] == [
        "document_id",
        "line_number",
    ]


class _FakeConnection:
    def __init__(self, validity: dict[str, bool]) -> None:
        self.validity = validity
        self.isolation_level = None
        self.executed: list[str] = []

    async def execution_options(self, **options):
        self.isolation_level = options.get("isolation_level")
        return self

    async def scalar(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            return True
        if "catalog_index.indisvalid" in sql:
            return self.validity.get(params["name"])
        raise AssertionError(f"unexpected scalar statement: {sql}")

    async def execute(self, statement, _params=None):
        sql = str(statement)
        self.executed.append(sql)
        create = re.search(
            r"CREATE INDEX CONCURRENTLY IF NOT EXISTS ([a-z0-9_]+)",
            sql,
        )
        drop = re.search(
            r"DROP INDEX CONCURRENTLY IF EXISTS ([a-z0-9_]+)",
            sql,
        )
        if create:
            self.validity[create.group(1)] = True
        elif drop:
            self.validity.pop(drop.group(1), None)


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


class _FakeEngine:
    def __init__(self, validity: dict[str, bool]) -> None:
        self.connection = _FakeConnection(validity)

    def connect(self):
        return _ConnectionContext(self.connection)


@pytest.mark.asyncio
async def test_online_runner_is_autocommit_and_idempotent() -> None:
    engine = _FakeEngine(
        {
            "idx_documents_content_trgm": True,
            "idx_conv_msg_document": True,
        }
    )

    first = await run_online_index_migrations(engine)
    second = await run_online_index_migrations(engine)

    assert engine.connection.isolation_level == "AUTOCOMMIT"
    assert "SET statement_timeout = 0" in engine.connection.executed
    assert "RESET statement_timeout" in engine.connection.executed
    assert first == {
        "locked": False,
        "applied": [
            "idx_documents_content_tsv",
            "idx_documents_content_trgm",
            "idx_conv_msg_document",
        ],
        "skipped": [],
    }
    assert second == {
        "locked": False,
        "applied": [],
        "skipped": [
            "idx_documents_content_tsv",
            "idx_documents_content_trgm",
            "idx_conv_msg_document",
        ],
    }


@pytest.mark.asyncio
async def test_online_runner_repairs_interrupted_invalid_build() -> None:
    engine = _FakeEngine({"idx_documents_content_tsv": False})

    result = await run_online_index_migrations(engine)
    body_ddl = [
        statement
        for statement in engine.connection.executed
        if "idx_documents_content_tsv" in statement
    ]

    assert body_ddl == [
        "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_tsv",
        (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ),
    ]
    assert result["applied"] == ["idx_documents_content_tsv"]
