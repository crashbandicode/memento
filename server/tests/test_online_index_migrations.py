from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from server import main as server_main
from server.db import online_migrations
from server.db.models import ConversationMessage
from server.db.online_migrations import (
    online_migration_status,
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
    lifespan_source = inspect.getsource(server_main.lifespan)

    assert "idx_documents_content_tsv" not in source
    assert "idx_documents_content_trgm" not in source
    assert "run_online_index_migrations" not in lifespan_source


class _SchemaConnection:
    def __init__(self) -> None:
        self.sync_calls: list[object] = []

    async def run_sync(self, function) -> None:
        self.sync_calls.append(function)


class _SchemaContext:
    def __init__(self, connection: _SchemaConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


class _SchemaEngine:
    def __init__(self) -> None:
        self.connection = _SchemaConnection()
        self.disposed = False

    def begin(self):
        return _SchemaContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_api_lifespan_readiness_does_not_wait_for_online_indexes(
    monkeypatch,
) -> None:
    schema_engine = _SchemaEngine()
    online_runner_called = False

    async def never_finishes(_engine) -> None:
        nonlocal online_runner_called
        online_runner_called = True
        await asyncio.Event().wait()

    async def fast_embedding_ready() -> None:
        return None

    monkeypatch.setattr(server_main, "engine", schema_engine)
    monkeypatch.setattr(
        online_migrations,
        "run_online_index_migrations",
        never_finishes,
    )
    monkeypatch.setattr(
        server_main,
        "_require_fast_embedding_server",
        fast_embedding_ready,
    )
    monkeypatch.setattr(
        type(server_main.settings),
        "validate_production",
        lambda _self: None,
    )

    async with asyncio.timeout(0.5):
        async with server_main.lifespan(server_main.app):
            assert online_runner_called is False

    await asyncio.sleep(0)
    assert schema_engine.connection.sync_calls == [
        server_main._run_migrations,
        server_main.Base.metadata.create_all,
        server_main._configure_hot_storage,
        server_main._initialize_dashboard_projection_state,
    ]
    assert schema_engine.disposed is True


def test_model_keeps_only_unique_document_line_index() -> None:
    indexes = {index.name: index for index in ConversationMessage.__table__.indexes}

    assert "idx_conv_msg_document" not in indexes
    assert indexes["uq_conv_msg_doc_line"].unique is True
    assert [column.name for column in indexes["uq_conv_msg_doc_line"].columns] == [
        "document_id",
        "line_number",
    ]


class _FakeResult:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    def mappings(self):
        return self

    def all(self) -> list[dict]:
        return self.rows

    def one_or_none(self) -> dict | None:
        if not self.rows:
            return None
        assert len(self.rows) == 1
        return self.rows[0]


class _FakeConnection:
    def __init__(
        self,
        validity: dict[str, bool],
        *,
        lock_available: bool = True,
    ) -> None:
        self.validity = validity
        self.lock_available = lock_available
        self.isolation_level = None
        self.executed: list[str] = []
        self.state_table_exists = False
        self.state: dict[str, dict] = {}
        self.progress: list[dict] = []
        self.fail_on_create: str | None = None
        self.cancel_on_create: str | None = None
        self.create_becomes_valid = True
        self.replacement_matches = True

    async def execution_options(self, **options):
        self.isolation_level = options.get("isolation_level")
        return self

    async def scalar(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            return self.lock_available
        if "to_regclass" in sql:
            return self.state_table_exists
        if "access_method.amname" in sql:
            validity = self.validity.get(params["name"])
            if validity is None:
                return None
            return validity and self.replacement_matches
        if "catalog_index.indisvalid" in sql:
            return self.validity.get(params["name"])
        raise AssertionError(f"unexpected scalar statement: {sql}")

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.executed.append(sql)

        if "pg_get_indexdef" in sql:
            validity = self.validity.get(params["name"])
            if validity is None:
                return _FakeResult()
            return _FakeResult(
                [
                    {
                        "valid": validity,
                        "ready": validity,
                        "definition": f"INDEX {params['name']}",
                    }
                ]
            )
        if "SELECT migration_name, operation, status" in sql:
            return _FakeResult(
                [self.state[name].copy() for name in sorted(self.state)]
            )
        if "FROM pg_stat_progress_create_index" in sql:
            return _FakeResult([row.copy() for row in self.progress])
        if sql.startswith(
            "CREATE TABLE IF NOT EXISTS online_index_migration_state"
        ):
            self.state_table_exists = True
            return _FakeResult()
        if "INSERT INTO online_index_migration_state" in sql:
            name = params["name"]
            previous = self.state.get(name, {})
            if "status" in params:
                status = params["status"]
                attempts = previous.get("attempts", 0)
                error = params["error"]
            else:
                status = "running"
                attempts = previous.get("attempts", 0) + 1
                error = None
            self.state[name] = {
                "migration_name": name,
                "operation": params["operation"],
                "status": status,
                "attempts": attempts,
                "executor_id": params["executor_id"],
                "started_at": None,
                "finished_at": None,
                "error": error,
                "updated_at": None,
            }
            return _FakeResult()

        create = re.search(
            r"CREATE INDEX CONCURRENTLY IF NOT EXISTS ([a-z0-9_]+)",
            sql,
        )
        drop = re.search(
            r"DROP INDEX CONCURRENTLY IF EXISTS ([a-z0-9_]+)",
            sql,
        )
        if create:
            name = create.group(1)
            if self.cancel_on_create == name:
                self.validity[name] = False
                raise asyncio.CancelledError
            if self.fail_on_create == name:
                self.validity[name] = False
                raise RuntimeError("synthetic index build failure")
            self.validity[name] = self.create_becomes_valid
            self.replacement_matches = True
        elif drop:
            self.validity.pop(drop.group(1), None)
        return _FakeResult()


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args) -> None:
        return None


class _FakeEngine:
    def __init__(
        self,
        validity: dict[str, bool],
        *,
        lock_available: bool = True,
    ) -> None:
        self.connection = _FakeConnection(
            validity,
            lock_available=lock_available,
        )

    def connect(self):
        return _ConnectionContext(self.connection)


def _online_ddl(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if (
            "CREATE INDEX CONCURRENTLY" in statement
            or "DROP INDEX CONCURRENTLY" in statement
        )
    ]


@pytest.mark.asyncio
async def test_online_runner_is_autocommit_and_idempotent() -> None:
    engine = _FakeEngine(
        {
            "idx_documents_content_trgm": True,
            "idx_conv_msg_document": True,
        }
    )

    first = await run_online_index_migrations(engine)
    first_ddl = _online_ddl(engine.connection.executed)
    executed_before_second_run = len(engine.connection.executed)
    second = await run_online_index_migrations(engine)
    second_ddl = _online_ddl(
        engine.connection.executed[executed_before_second_run:]
    )

    assert engine.connection.isolation_level == "AUTOCOMMIT"
    assert "SET statement_timeout = 0" in engine.connection.executed
    assert "RESET statement_timeout" in engine.connection.executed
    assert first["locked"] is False
    assert first["applied"] == [
        "idx_documents_content_tsv",
        "idx_documents_content_trgm",
        "idx_conv_msg_document",
    ]
    assert first["skipped"] == []
    assert first_ddl == [
        (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ),
        "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_trgm",
        "DROP INDEX CONCURRENTLY IF EXISTS idx_conv_msg_document",
    ]
    assert second["locked"] is False
    assert second["applied"] == []
    assert second["skipped"] == [
        "idx_documents_content_tsv",
        "idx_documents_content_trgm",
        "idx_conv_msg_document",
    ]
    assert second_ddl == []
    assert {
        state["status"] for state in engine.connection.state.values()
    } == {"succeeded"}


@pytest.mark.asyncio
async def test_online_runner_does_not_drop_old_indexes_until_replacement_valid(
) -> None:
    engine = _FakeEngine(
        {
            "idx_documents_content_trgm": True,
            "idx_conv_msg_document": True,
        }
    )
    engine.connection.create_becomes_valid = False

    with pytest.raises(RuntimeError, match="did not become valid and ready"):
        await run_online_index_migrations(engine)

    assert _online_ddl(engine.connection.executed) == [
        (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        )
    ]
    assert engine.connection.validity["idx_documents_content_trgm"] is True
    assert engine.connection.validity["idx_conv_msg_document"] is True
    replacement_state = engine.connection.state["idx_documents_content_tsv"]
    assert replacement_state["status"] == "failed"


@pytest.mark.asyncio
async def test_wrong_replacement_definition_is_rebuilt_before_old_drops() -> None:
    engine = _FakeEngine(
        {
            "idx_documents_content_tsv": True,
            "idx_documents_content_trgm": True,
            "idx_conv_msg_document": True,
        }
    )
    engine.connection.replacement_matches = False

    result = await run_online_index_migrations(engine)

    assert result["applied"] == [
        "idx_documents_content_tsv",
        "idx_documents_content_trgm",
        "idx_conv_msg_document",
    ]
    assert _online_ddl(engine.connection.executed) == [
        "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_tsv",
        (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ),
        "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_trgm",
        "DROP INDEX CONCURRENTLY IF EXISTS idx_conv_msg_document",
    ]


@pytest.mark.asyncio
async def test_online_runner_returns_immediately_on_lock_contention() -> None:
    engine = _FakeEngine({}, lock_available=False)

    result = await run_online_index_migrations(engine)

    assert result["locked"] is True
    assert result["applied"] == []
    assert result["skipped"] == []
    assert engine.connection.executed == []


@pytest.mark.asyncio
async def test_online_runner_records_failure_and_preserves_old_indexes() -> None:
    engine = _FakeEngine(
        {
            "idx_documents_content_trgm": True,
            "idx_conv_msg_document": True,
        }
    )
    engine.connection.fail_on_create = "idx_documents_content_tsv"

    with pytest.raises(RuntimeError, match="synthetic index build failure"):
        await run_online_index_migrations(engine)

    assert engine.connection.state["idx_documents_content_tsv"]["status"] == "failed"
    assert (
        "synthetic index build failure"
        in engine.connection.state["idx_documents_content_tsv"]["error"]
    )
    assert engine.connection.validity["idx_documents_content_trgm"] is True
    assert engine.connection.validity["idx_conv_msg_document"] is True
    assert "RESET statement_timeout" in engine.connection.executed
    assert any(
        "pg_advisory_unlock" in statement
        for statement in engine.connection.executed
    )


@pytest.mark.asyncio
async def test_cancelled_build_is_recorded_and_next_run_repairs_it() -> None:
    engine = _FakeEngine(
        {
            "idx_documents_content_trgm": True,
            "idx_conv_msg_document": True,
        }
    )
    engine.connection.cancel_on_create = "idx_documents_content_tsv"

    with pytest.raises(asyncio.CancelledError):
        await run_online_index_migrations(engine)

    assert engine.connection.validity["idx_documents_content_tsv"] is False
    assert engine.connection.validity["idx_documents_content_trgm"] is True
    assert engine.connection.validity["idx_conv_msg_document"] is True
    assert (
        engine.connection.state["idx_documents_content_tsv"]["status"]
        == "interrupted"
    )
    assert "RESET statement_timeout" in engine.connection.executed
    assert any(
        "pg_advisory_unlock" in statement
        for statement in engine.connection.executed
    )

    engine.connection.cancel_on_create = None
    executed_before_restart = len(engine.connection.executed)
    result = await run_online_index_migrations(engine)

    assert _online_ddl(
        engine.connection.executed[executed_before_restart:]
    ) == [
        "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_tsv",
        (
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ),
        "DROP INDEX CONCURRENTLY IF EXISTS idx_documents_content_trgm",
        "DROP INDEX CONCURRENTLY IF EXISTS idx_conv_msg_document",
    ]
    assert result["applied"] == [
        "idx_documents_content_tsv",
        "idx_documents_content_trgm",
        "idx_conv_msg_document",
    ]
    assert engine.connection.state["idx_documents_content_tsv"]["attempts"] == 2
    assert {
        state["status"] for state in engine.connection.state.values()
    } == {"succeeded"}


@pytest.mark.asyncio
async def test_status_reports_durable_failure_catalog_and_live_progress() -> None:
    engine = _FakeEngine({"idx_documents_content_tsv": False})
    engine.connection.state_table_exists = True
    engine.connection.state["idx_documents_content_tsv"] = {
        "migration_name": "idx_documents_content_tsv",
        "operation": "create",
        "status": "failed",
        "attempts": 2,
        "executor_id": "migration-pod:1:test",
        "started_at": None,
        "finished_at": None,
        "error": "cancelled",
        "updated_at": None,
    }
    engine.connection.progress = [
        {
            "pid": 42,
            "command": "CREATE INDEX CONCURRENTLY",
            "phase": "building index",
            "table_name": "documents",
            "index_name": "idx_documents_content_tsv",
            "lockers_total": 0,
            "lockers_done": 0,
            "blocks_total": 100,
            "blocks_done": 25,
            "tuples_total": 0,
            "tuples_done": 0,
        }
    ]

    status = await online_migration_status(engine)

    replacement = status["migrations"][0]
    assert replacement["catalog"] == {
        "exists": True,
        "valid": False,
        "ready": False,
        "definition": "INDEX idx_documents_content_tsv",
    }
    assert replacement["state"]["status"] == "failed"
    assert status["progress"][0]["blocks_done"] == 25


def test_fleet_uses_a_single_bounded_migration_controller() -> None:
    test_parent = Path(__file__).resolve().parents[1]
    repository_root = (
        test_parent
        if (test_parent / "deploy" / "k8s").exists()
        else test_parent.parent
    )
    manifest = (
        repository_root / "deploy" / "k8s" / "online-index-migrations.yaml"
    ).read_text(encoding="utf-8")
    kustomization = (
        repository_root / "deploy" / "k8s" / "kustomization.yaml"
    ).read_text(encoding="utf-8")

    assert "kind: CronJob" in manifest
    assert "concurrencyPolicy: Forbid" in manifest
    assert "backoffLimit: 1" in manifest
    assert "activeDeadlineSeconds: 86400" in manifest
    assert "terminationGracePeriodSeconds: 120" in manifest
    assert "server.scripts.online_index_migrations" in manifest
    assert "- --apply" in manifest
    assert "- online-index-migrations.yaml" in kustomization
