from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from server.api import memory
from server.services import embedding_service
from server.services.ingest_service import _invalidate_embeddings_for_revision
from server.tasks.post_ingest import process_document_post_ingest


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _install_http_client(monkeypatch, response: _Response, calls: list[dict]) -> None:
    class _Client:
        def __init__(self, *, timeout: float) -> None:
            calls.append({"timeout": timeout})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, *, json: dict) -> _Response:
            calls.append({"url": url, "json": json})
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.fixture(autouse=True)
def _reset_server_availability() -> None:
    embedding_service._server_available = None
    embedding_service._last_check_time = 0


@pytest.mark.asyncio
async def test_interactive_busy_response_fails_fast_without_marking_server_down(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    _install_http_client(monkeypatch, _Response(503), calls)

    result = await embedding_service._call_embedding_server(
        ["query"],
        timeout=30,
    )

    assert result is None
    assert embedding_service._server_available is True
    assert calls == [
        {"timeout": 30},
        {
            "url": f"{embedding_service.EMBEDDING_SERVER_URL}/embed",
            "json": {"texts": ["query"]},
        },
    ]


@pytest.mark.asyncio
async def test_background_busy_response_is_transient(monkeypatch) -> None:
    calls: list[dict] = []
    _install_http_client(monkeypatch, _Response(503), calls)

    with pytest.raises(embedding_service.EmbeddingServerBusy):
        await embedding_service._call_embedding_server(
            ["background"],
            raise_on_busy=True,
        )

    assert embedding_service._server_available is True


@pytest.mark.asyncio
async def test_whole_document_uses_one_admission_request(monkeypatch) -> None:
    texts = [f"chunk-{index}" for index in range(50)]
    vectors = [[float(index)] for index in range(50)]
    calls: list[dict] = []
    _install_http_client(
        monkeypatch,
        _Response(200, {"embeddings": vectors}),
        calls,
    )

    result = await embedding_service._call_embedding_server(texts)

    assert result == vectors
    post_calls = [call for call in calls if "url" in call]
    assert post_calls == [
        {
            "url": f"{embedding_service.EMBEDDING_SERVER_URL}/embed",
            "json": {"texts": texts},
        }
    ]


class _RecordingDB:
    def __init__(self, rowcounts: list[int] | None = None) -> None:
        self.statements: list = []
        self.update_params: list[dict] = []
        self.rowcounts = list(rowcounts or [])

    async def execute(self, statement):
        self.statements.append(statement)
        self.update_params.append(statement.compile().params)
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return SimpleNamespace(rowcount=rowcount)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


def _document(*, attempts: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        embedding_status="failed",
        embedding_attempts=attempts,
        content_hash="a" * 64,
        content_type="text/plain",
        content="durable conversation text " * 20,
        category="conversation",
        relative_path="sessions/test.jsonl",
        tool_id="codex",
    )


@pytest.mark.asyncio
async def test_busy_document_stays_pending_without_consuming_attempt(monkeypatch) -> None:
    async def _busy(*_args, **_kwargs):
        raise embedding_service.EmbeddingServerBusy("busy")

    monkeypatch.setattr(embedding_service, "_call_embedding_server", _busy)
    db = _RecordingDB()

    result = await embedding_service.generate_document_embeddings(db, _document())

    assert result == 0
    assert db.update_params[-1]["embedding_status"] == "pending"
    assert all(
        "embedding_attempts"
        not in {getattr(column, "key", str(column)) for column in statement._values}
        for statement in db.statements
    )
    final_sql = str(db.statements[-1].compile())
    assert "documents.content_hash" in final_sql
    assert "documents.embedding_claim_token" in final_sql


@pytest.mark.asyncio
async def test_real_embedding_failure_consumes_attempt(monkeypatch) -> None:
    async def _failed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(embedding_service, "_call_embedding_server", _failed)
    db = _RecordingDB()

    result = await embedding_service.generate_document_embeddings(db, _document())

    assert result == 0
    assert db.update_params[-1]["embedding_status"] == "failed"
    assigned = {
        getattr(column, "key", str(column))
        for column in db.statements[-1]._values
    }
    assert "embedding_attempts" in assigned


@pytest.mark.asyncio
async def test_stale_revision_cannot_claim_or_call_embedding_server(monkeypatch) -> None:
    async def _unexpected(*_args, **_kwargs):
        pytest.fail("stale revision reached the embedding server")

    monkeypatch.setattr(embedding_service, "_call_embedding_server", _unexpected)
    db = _RecordingDB(rowcounts=[0])

    result = await embedding_service.generate_document_embeddings(db, _document())

    assert result == 0
    assert len(db.statements) == 1
    claim_sql = str(db.statements[0].compile())
    assert "documents.content_hash" in claim_sql
    assert "documents.embedding_status" in claim_sql


@pytest.mark.asyncio
async def test_revision_change_deletes_vectors_and_resets_claim() -> None:
    class _DB:
        def __init__(self) -> None:
            self.statements: list = []

        async def execute(self, statement) -> None:
            self.statements.append(statement)

    db = _DB()
    doc = SimpleNamespace(
        id=uuid4(),
        content_hash="old",
        embedding_status="processing",
        embedding_attempts=4,
        embedding_claim_token="claim",
        embedding_claimed_at=object(),
    )

    changed = await _invalidate_embeddings_for_revision(db, doc, "new")

    assert changed is True
    assert len(db.statements) == 1
    assert "DELETE FROM document_embeddings" in str(db.statements[0].compile())
    assert doc.embedding_status == "pending"
    assert doc.embedding_attempts == 0
    assert doc.embedding_claim_token is None
    assert doc.embedding_claimed_at is None


@pytest.mark.asyncio
async def test_same_revision_keeps_existing_vectors() -> None:
    class _DB:
        async def execute(self, _statement) -> None:
            pytest.fail("same revision deleted embeddings")

    doc = SimpleNamespace(content_hash="same")

    changed = await _invalidate_embeddings_for_revision(_DB(), doc, "same")

    assert changed is False


@pytest.mark.asyncio
async def test_semantic_search_filters_non_current_embedding_status(monkeypatch) -> None:
    class _Rows:
        def all(self) -> list:
            return []

    class _DB:
        def __init__(self) -> None:
            self.statements: list = []

        async def execute(self, statement) -> _Rows:
            self.statements.append(statement)
            return _Rows()

    async def _machines(*_args, **_kwargs):
        return None

    async def _embed(*_args, **_kwargs):
        return [[0.0] * embedding_service.EMBEDDING_DIM]

    monkeypatch.setattr(memory, "user_machine_ids", _machines)
    monkeypatch.setattr(embedding_service, "_call_embedding_server", _embed)
    db = _DB()

    result = await memory.semantic_search(
        q="query",
        limit=5,
        tool_filter=None,
        days=None,
        db=db,
        _user=SimpleNamespace(id=uuid4()),
    )

    assert result == {"results": []}
    assert len(db.statements) == 1
    query_sql = str(db.statements[0].compile())
    assert "documents.embedding_status" in query_sql
    assert "row_number" in query_sql.lower()
    assert "chunk_rank" in query_sql


@pytest.mark.asyncio
async def test_semantic_search_pages_documents_past_large_subagent_group(
    monkeypatch,
) -> None:
    root_id = uuid4()
    child_ids = [uuid4() for _ in range(283)]
    note_id = uuid4()
    now = datetime.now(timezone.utc)

    child_rows = [
        (
            f"matching child chunk {index}",
            child_id,
            "codex",
            "Inherited root prompt",
            f"sessions/{child_id}.jsonl",
            "conversation",
            now,
            now,
            {
                "session_id": str(child_id),
                "root_session_id": str(root_id),
                "thread_source": "subagent",
                "agent_path": f"/root/worker_{index}",
            },
            100,
            0.1 + index / 10_000,
        )
        for index, child_id in enumerate(child_ids)
    ]
    note_row = (
        "matching independent note",
        note_id,
        "obsidian",
        "Independent note",
        "notes/independent.md",
        "note",
        now,
        now,
        {},
        100,
        0.5,
    )
    root_companion = (
        root_id,
        "codex",
        "Root conversation",
        f"sessions/{root_id}.jsonl",
        "conversation",
        now,
        now,
        {"session_id": str(root_id), "thread_source": "root"},
        500,
    )

    class _Rows:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        def all(self) -> list:
            return self._rows

    class _DB:
        def __init__(self) -> None:
            self.statements: list = []

        async def execute(self, statement) -> _Rows:
            self.statements.append(statement)
            call = len(self.statements)
            if call == 1:
                return _Rows(child_rows[:100])
            if call == 2:
                return _Rows(child_rows[100:200])
            if call == 3:
                return _Rows(child_rows[200:] + [note_row])
            return _Rows([root_companion])

    async def _machines(*_args, **_kwargs):
        return None

    async def _embed(*_args, **_kwargs):
        return [[0.0] * embedding_service.EMBEDDING_DIM]

    monkeypatch.setattr(memory, "user_machine_ids", _machines)
    monkeypatch.setattr(embedding_service, "_call_embedding_server", _embed)
    db = _DB()

    result = await memory.semantic_search(
        q="query",
        limit=2,
        tool_filter=None,
        days=None,
        db=db,
        _user=SimpleNamespace(id=uuid4()),
    )

    assert len(db.statements) == 4
    assert [item["id"] for item in result["results"]] == [
        str(root_id),
        str(note_id),
    ]
    assert result["results"][0]["matched_subagent_id"] == str(child_ids[0])
    assert result["results"][0]["subagent_count"] == 283


def test_post_ingest_limit_exceeds_background_embedding_deadline() -> None:
    timeout = inspect.signature(
        embedding_service._call_embedding_server
    ).parameters["timeout"].default

    assert process_document_post_ingest.time_limit >= timeout + 300
