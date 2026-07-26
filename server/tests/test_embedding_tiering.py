from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from server.api import memory
from server.db.models import DocumentEmbedding, DocumentEmbeddingFast
from server.services import embedding_service


@pytest.fixture(autouse=True)
def _reset_tier_state(monkeypatch):
    embedding_service.reset_embedding_server_state()
    monkeypatch.setattr(embedding_service, "EMBEDDING_TIERING_ENABLED", True)
    yield
    embedding_service.reset_embedding_server_state()


def test_policy_routes_important_categories_to_quality():
    for category in ("memory", "identity", "plan", "learning", "note"):
        assert embedding_service.desired_embedding_tier(category) == "quality"


def test_policy_routes_ordinary_categories_to_fast():
    assert embedding_service.desired_embedding_tier("conversation") == "fast"
    assert embedding_service.desired_embedding_tier("backlog") == "fast"


def test_policy_never_demotes_quality(monkeypatch):
    monkeypatch.setattr(embedding_service, "EMBEDDING_TIERING_ENABLED", True)
    doc = SimpleNamespace(category="conversation", embedding_tier="quality")
    assert embedding_service.resolve_embedding_tier(doc) == "quality"
    assert embedding_service.apply_embedding_tier_policy(doc) is False
    assert doc.embedding_tier == "quality"


def test_policy_promotes_fast_to_quality_and_invalidates():
    doc = SimpleNamespace(
        category="memory",
        embedding_tier="fast",
        embedding_status="ok",
        embedding_attempts=2,
        embedding_claim_token="tok",
        embedding_claimed_at=object(),
    )
    assert embedding_service.apply_embedding_tier_policy(doc) is True
    assert doc.embedding_tier == "quality"
    assert doc.embedding_status == "pending"
    assert doc.embedding_attempts == 0
    assert doc.embedding_claim_token is None
    assert doc.embedding_claimed_at is None


def test_profiles_use_separate_dimensions_and_tables():
    quality = embedding_service.embedding_profile("quality")
    fast = embedding_service.embedding_profile("fast")
    assert quality.dimension == 1024
    assert fast.dimension == 384
    assert quality.orm_model is DocumentEmbedding
    assert fast.orm_model is DocumentEmbeddingFast
    assert quality.server_url != fast.server_url
    assert quality.model_name == "BAAI/bge-m3"
    assert fast.model_name == "intfloat/multilingual-e5-small"
    assert fast.query_prefix == "query: "
    assert fast.document_prefix == "passage: "


def test_profile_health_requires_exact_model_dimension_and_backend():
    profile = embedding_service.embedding_profile("fast")
    healthy = {
        "status": "ok",
        "model": True,
        "model_name": profile.model_name,
        "backend": profile.backend,
        "dimension": profile.dimension,
        "profile_signature": profile.profile_signature,
    }
    assert (
        embedding_service.embedding_profile_health_mismatch(profile, healthy)
        is None
    )
    for field, value in [
        ("model_name", "wrong/model"),
        ("backend", "wrong"),
        ("dimension", 1024),
        ("profile_signature", "sha256:wrong"),
    ]:
        payload = {**healthy, field: value}
        assert field in (
            embedding_service.embedding_profile_health_mismatch(
                profile,
                payload,
            )
            or ""
        )


def test_chunk_reuse_is_profile_model_specific():
    chunk = "stable chunk text for reuse"
    fast = embedding_service.embedding_profile("fast")
    assert embedding_service.chunk_embedding_is_reusable(
        chunk=chunk,
        stored_text=chunk,
        stored_hash=embedding_service.chunk_content_hash(chunk),
        stored_model=embedding_service.EMBEDDING_FAST_MODEL_NAME,
        model_name=embedding_service.EMBEDDING_FAST_MODEL_NAME,
        stored_backend=fast.backend,
        backend=fast.backend,
        stored_profile_signature=fast.profile_signature,
        profile_signature=fast.profile_signature,
    )
    assert not embedding_service.chunk_embedding_is_reusable(
        chunk=chunk,
        stored_text=chunk,
        stored_hash=embedding_service.chunk_content_hash(chunk),
        stored_model=embedding_service.EMBEDDING_FAST_MODEL_NAME,
        model_name=embedding_service.EMBEDDING_MODEL_NAME,
    )


def test_legacy_rows_are_reused_only_by_the_historical_quality_profile():
    chunk = "legacy BGE-M3 chunk"
    quality = embedding_service.embedding_profile("quality")
    assert embedding_service.chunk_embedding_is_reusable(
        chunk=chunk,
        stored_text=chunk,
        stored_hash=None,
        stored_model=None,
        stored_backend=None,
        stored_profile_signature=None,
        model_name=quality.model_name,
        backend=quality.backend,
        profile_signature=quality.profile_signature,
    )
    changed_signature = embedding_service.embedding_profile_signature(
        model_name=quality.model_name,
        model_revision="new-revision",
        backend=quality.backend,
        dimension=quality.dimension,
        query_prefix=quality.query_prefix,
        document_prefix=quality.document_prefix,
        max_sequence_length=quality.max_sequence_length,
        onnx_file="",
        artifact_sha256="",
    )
    assert not embedding_service.chunk_embedding_is_reusable(
        chunk=chunk,
        stored_text=chunk,
        stored_hash=None,
        stored_model=None,
        stored_backend=None,
        stored_profile_signature=None,
        model_name=quality.model_name,
        backend=quality.backend,
        profile_signature=changed_signature,
    )


def test_fast_dimension_must_match_physical_pgvector_table(monkeypatch):
    monkeypatch.setattr(embedding_service, "EMBEDDING_FAST_DIM", 768)
    with pytest.raises(RuntimeError, match=r"vector\(384\)"):
        embedding_service.embedding_profile("fast")


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _install_http_client(monkeypatch, handler) -> list[dict]:
    calls: list[dict] = []

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            calls.append({"timeout": timeout})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, *, json: dict) -> _Response:
            calls.append({"url": url, "json": json})
            return handler(url, json)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


@pytest.mark.asyncio
async def test_http_routing_is_profile_specific(monkeypatch):
    quality = embedding_service.embedding_profile("quality")
    fast = embedding_service.embedding_profile("fast")

    def _handler(url: str, payload: dict) -> _Response:
        if url.startswith(fast.server_url):
            assert payload["model"] == fast.model_name
            assert payload["dimensions"] == 384
            assert payload["purpose"] == "document"
            return _Response(
                200,
                {
                    "embeddings": [[0.1] * 384],
                    "profile_signature": fast.profile_signature,
                },
            )
        assert url.startswith(quality.server_url)
        assert payload["model"] == quality.model_name
        assert payload["dimensions"] == 1024
        return _Response(
            200,
            {
                "embeddings": [[0.2] * 1024],
                "profile_signature": quality.profile_signature,
            },
        )

    calls = _install_http_client(monkeypatch, _handler)

    fast_vectors = await embedding_service._call_embedding_server(
        ["fast-query"],
        timeout=30,
        profile=fast,
    )
    quality_vectors = await embedding_service._call_embedding_server(
        ["quality-query"],
        timeout=30,
        profile=quality,
    )

    assert fast_vectors == [[0.1] * 384]
    assert quality_vectors == [[0.2] * 1024]
    urls = [call["url"] for call in calls if "url" in call]
    assert urls == [
        f"{fast.server_url}/embed",
        f"{quality.server_url}/embed",
    ]


@pytest.mark.asyncio
async def test_server_availability_is_isolated_per_url(monkeypatch):
    quality = embedding_service.embedding_profile("quality")
    fast = embedding_service.embedding_profile("fast")

    def _handler(url: str, _payload: dict) -> _Response:
        if url.startswith(fast.server_url):
            raise ConnectionError("fast down")
        return _Response(
            200,
            {
                "embeddings": [[0.0] * 1024],
                "profile_signature": quality.profile_signature,
            },
        )

    _install_http_client(monkeypatch, _handler)

    assert (
        await embedding_service._call_embedding_server(
            ["q"], timeout=5, profile=fast
        )
        is None
    )
    assert (
        await embedding_service._call_embedding_server(
            ["q"], timeout=5, profile=quality
        )
        == [[0.0] * 1024]
    )


@pytest.mark.asyncio
async def test_generate_uses_fast_table_and_one_changed_batch(monkeypatch):
    profile = embedding_service.embedding_profile("fast")
    doc = SimpleNamespace(
        id=uuid4(),
        embedding_status="pending",
        embedding_attempts=0,
        embedding_content_hash=None,
        embedding_tier="fast",
        content_hash="a" * 64,
        content_type="text/plain",
        content="durable conversation text " * 20,
        category="conversation",
        relative_path="sessions/test.jsonl",
        tool_id="codex",
    )
    http_calls: list[dict] = []

    class _Rows:
        def all(self):
            return []

        def scalar_one_or_none(self):
            return doc.id

    class _DB:
        def __init__(self) -> None:
            self.statements = []
            self.rowcounts = [1, 1]

        async def execute(self, statement):
            self.statements.append(statement)
            compiled = str(statement.compile())
            if "document_embeddings_fast" in compiled and "SELECT" in compiled.upper():
                return _Rows()
            if "FOR UPDATE" in compiled.upper() or "with_for_update" in compiled.lower():
                return _Rows()
            rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
            return SimpleNamespace(rowcount=rowcount, all=lambda: [])

        async def commit(self) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    async def _embed(texts, **kwargs):
        http_calls.append({"texts": texts, "profile": kwargs.get("profile")})
        assert kwargs.get("profile") is profile or kwargs.get("profile") is None
        # Force profile path through generate_document_embeddings
        return [[0.5] * profile.dimension for _ in texts]

    # generate resolves its own profile; stub the HTTP layer by profile.
    async def _call(texts, timeout=None, raise_on_busy=False, profile=None):
        http_calls.append(
            {
                "texts": texts,
                "profile_tier": (profile or embedding_service.embedding_profile("quality")).tier,
                "dimension": (profile or embedding_service.embedding_profile("quality")).dimension,
            }
        )
        assert len(texts) >= 1
        dim = (profile or embedding_service.embedding_profile("quality")).dimension
        return [[0.5] * dim for _ in texts]

    monkeypatch.setattr(embedding_service, "_call_embedding_server", _call)
    db = _DB()
    count = await embedding_service.generate_document_embeddings(db, doc)
    assert count >= 1
    assert http_calls
    assert http_calls[0]["profile_tier"] == "fast"
    assert http_calls[0]["dimension"] == 384
    # Single admission request for all changed chunks.
    assert len(http_calls) == 1
    sql = " ".join(str(s.compile()) for s in db.statements)
    assert "document_embeddings_fast" in sql


@pytest.mark.asyncio
async def test_generate_quality_cleans_fast_rows_on_promotion(monkeypatch):
    doc = SimpleNamespace(
        id=uuid4(),
        embedding_status="pending",
        embedding_attempts=0,
        embedding_content_hash=None,
        embedding_tier="fast",
        content_hash="b" * 64,
        content_type="text/plain",
        content="important memory document text " * 20,
        category="memory",
        relative_path="memory/note.md",
        tool_id="obsidian",
    )

    class _Rows:
        def all(self):
            return []

        def scalar_one_or_none(self):
            return doc.id

    class _DB:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(rowcount=1, all=lambda: [], scalar_one_or_none=lambda: doc.id)

        async def commit(self) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    async def _call(texts, timeout=None, raise_on_busy=False, profile=None):
        assert profile is not None
        assert profile.tier == "quality"
        return [[0.1] * profile.dimension for _ in texts]

    monkeypatch.setattr(embedding_service, "_call_embedding_server", _call)
    db = _DB()
    await embedding_service.generate_document_embeddings(db, doc)
    sql = " ".join(str(s.compile()) for s in db.statements)
    assert "DELETE FROM document_embeddings_fast" in sql
    assert doc.embedding_tier == "quality"


def test_rrf_merges_tiers_without_comparing_raw_distances():
    quality_id = uuid4()
    fast_id = uuid4()
    quality_list = [
        {
            "_document_id": quality_id,
            "_tier": "quality",
            "score": 0.4,
            "snippet": "q",
            "category": "memory",
            "id": str(quality_id),
            "tool_id": "obsidian",
            "title": "Q",
            "relative_path": "m.md",
            "synced_at": None,
            "_metadata": {},
            "_source_modified_at": None,
            "_synced_at_value": None,
            "_file_size_bytes": 1,
        }
    ]
    fast_list = [
        {
            "_document_id": fast_id,
            "_tier": "fast",
            "score": 0.99,
            "snippet": "f",
            "category": "conversation",
            "id": str(fast_id),
            "tool_id": "codex",
            "title": "F",
            "relative_path": "s.jsonl",
            "synced_at": None,
            "_metadata": {},
            "_source_modified_at": None,
            "_synced_at_value": None,
            "_file_size_bytes": 1,
        }
    ]
    fused = embedding_service.reciprocal_rank_fusion([quality_list, fast_list])
    assert [item["_document_id"] for item in fused] == [quality_id, fast_id]
    # RRF score replaces incompatible cosine similarity.
    assert fused[0]["score"] != 0.4


@pytest.mark.asyncio
async def test_mixed_tier_semantic_search_fuses_and_survives_fast_outage(
    monkeypatch,
):
    quality_id = uuid4()
    fast_id = uuid4()
    now = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )

    quality_row = (
        "quality chunk",
        quality_id,
        "obsidian",
        "Memory note",
        "notes/memory.md",
        "memory",
        now,
        now,
        {},
        100,
        0.2,
    )
    fast_row = (
        "fast chunk",
        fast_id,
        "codex",
        "Chat",
        "sessions/a.jsonl",
        "conversation",
        now,
        now,
        {"session_id": str(fast_id), "thread_source": "root"},
        100,
        0.1,
    )

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    class _DB:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            sql = str(statement.compile()).lower()
            # Ranking queries (best chunk per document), not eligibility probes.
            if "row_number" in sql or "chunk_rank" in sql:
                if "document_embeddings_fast" in sql:
                    return _Rows([fast_row])
                return _Rows([quality_row])
            if "document_embeddings_fast" in sql:
                return _Rows([("fast",)])
            if "document_embeddings" in sql:
                return _Rows([("quality",)])
            return _Rows([])

    async def _machines(*_a, **_k):
        return None

    async def _embed(
        texts,
        timeout=None,
        raise_on_busy=False,
        profile=None,
        purpose="document",
    ):
        assert purpose == "query"
        if profile and profile.tier == "fast":
            return None  # fast server down
        dim = (profile or embedding_service.embedding_profile("quality")).dimension
        return [[0.0] * dim]

    monkeypatch.setattr(memory, "user_machine_ids", _machines)
    monkeypatch.setattr(embedding_service, "_call_embedding_server", _embed)
    monkeypatch.setattr(embedding_service, "EMBEDDING_TIERING_ENABLED", True)

    result = await memory.semantic_search(
        q="query",
        limit=5,
        tool_filter=None,
        days=None,
        db=_DB(),
        _user=SimpleNamespace(id=uuid4()),
    )
    assert [item["id"] for item in result["results"]] == [str(quality_id)]
    assert "embedding-server-unavailable" not in result.get("note", "")


@pytest.mark.asyncio
async def test_tiering_disabled_stops_new_assignment_but_keeps_existing_fast(
    monkeypatch,
):
    monkeypatch.setattr(embedding_service, "EMBEDDING_TIERING_ENABLED", False)
    assert embedding_service.desired_embedding_tier("conversation") == "quality"
    assert embedding_service.resolve_embedding_tier(
        SimpleNamespace(category="conversation", embedding_tier=None)
    ) == "quality"
    assert embedding_service.resolve_embedding_tier(
        SimpleNamespace(category="conversation", embedding_tier="fast")
    ) == "fast"


@pytest.mark.asyncio
async def test_tiering_disabled_still_discovers_searchable_fast_rows(monkeypatch):
    class _Rows:
        def __init__(self, present: bool):
            self._present = present

        def first(self):
            return ("row",) if self._present else None

    class _DB:
        async def execute(self, statement):
            sql = str(statement.compile())
            return _Rows("document_embeddings_fast" in sql)

    monkeypatch.setattr(embedding_service, "EMBEDDING_TIERING_ENABLED", False)
    assert await embedding_service.tiers_with_searchable_rows(
        _DB(),
        machine_ids=None,
    ) == ["fast"]
