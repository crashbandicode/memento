from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.api import mcp_mount
from mcp_server import db as mcp_db
from mcp_server import search as mcp_search
from mcp_server import server as mcp_server


def test_direct_mcp_search_url_falls_back_to_primary(monkeypatch) -> None:
    monkeypatch.delenv("MEMENTO_SEARCH_DATABASE_URL", raising=False)
    assert mcp_db.get_search_db_url("postgresql+asyncpg://primary/db") == (
        "postgresql+asyncpg://primary/db"
    )


def test_direct_mcp_search_url_accepts_replica(monkeypatch) -> None:
    monkeypatch.setenv(
        "MEMENTO_SEARCH_DATABASE_URL",
        "postgresql+asyncpg://replica/db",
    )
    assert mcp_db.get_search_db_url("postgresql+asyncpg://primary/db") == (
        "postgresql+asyncpg://replica/db"
    )


def test_direct_mcp_uses_distinct_read_only_search_factory(monkeypatch) -> None:
    primary = object()
    replica = object()
    calls: list[tuple[str, bool]] = []

    def _factory(url: str, *, read_only: bool = False):
        calls.append((url, read_only))
        return replica if read_only else primary

    monkeypatch.setenv(
        "MEMENTO_SEARCH_DATABASE_URL",
        "postgresql+asyncpg://replica/db",
    )
    monkeypatch.setattr(mcp_db, "create_engine_and_session", _factory)

    mcp_server.init_server(db_url="postgresql+asyncpg://primary/db")

    assert mcp_server._session_factory is primary
    assert mcp_server._search_session_factory is replica
    assert calls == [
        ("postgresql+asyncpg://primary/db", False),
        ("postgresql+asyncpg://replica/db", True),
    ]


def test_api_mount_passes_database_url_by_keyword(monkeypatch) -> None:
    db_url = "postgresql+asyncpg://postgres:secret@postgres/memento"
    captured: dict[str, object] = {}

    def _init_server(*args, **kwargs) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setenv("MEMENTO_DATABASE_URL", db_url)
    monkeypatch.setattr(mcp_server, "init_server", _init_server)
    app = SimpleNamespace(mount=lambda *_args, **_kwargs: None)

    mcp_mount.mount_mcp(app)

    assert captured == {
        "args": (),
        "kwargs": {"db_url": db_url},
    }


@pytest.mark.asyncio
async def test_direct_mcp_semantic_search_hides_stale_vectors(monkeypatch) -> None:
    class _DB:
        def __init__(self) -> None:
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return SimpleNamespace(all=lambda: [], first=lambda: None)

    async def _embedding(_query: str, *, tier: str = "quality"):
        return [0.0] * 1024

    monkeypatch.setattr(mcp_search, "_get_embedding", _embedding)
    db = _DB()

    assert await mcp_search._semantic_search(
        db,
        "query",
        5,
        None,
        None,
        None,
    ) == []
    sql = str(db.statement.compile())
    assert "documents.embedding_status" in sql
    assert "documents.embedding_tier" in sql


@pytest.mark.asyncio
async def test_direct_mcp_semantic_search_includes_fast_tier(monkeypatch) -> None:
    fast_sql: list[str] = []
    fast_row = SimpleNamespace(
        chunk_text="fast result",
        document_id=object(),
        title="Fast",
        tool_id="cursor",
        relative_path="fast/doc",
        synced_at=None,
        distance=0.1,
    )

    class _DB:
        async def execute(self, statement):
            sql = str(statement.compile())
            if "document_embeddings_fast" not in sql:
                return SimpleNamespace(all=lambda: [])
            fast_sql.append(sql)
            if "chunk_text" in sql:
                return SimpleNamespace(all=lambda: [fast_row])
            return SimpleNamespace(first=lambda: ("row",))

    async def _embedding(_query: str, *, tier: str = "quality"):
        return [0.0] * (384 if tier == "fast" else 1024)

    monkeypatch.setattr(mcp_search, "_get_embedding", _embedding)
    results = await mcp_search._semantic_search(
        _DB(),
        "query",
        5,
        None,
        None,
        None,
    )

    assert [row["relative_path"] for row in results] == ["fast/doc"]
    assert any("documents.embedding_tier" in sql for sql in fast_sql)
