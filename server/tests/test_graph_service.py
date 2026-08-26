from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from server.services import graph_service


class _Document:
    def __init__(self, content: str) -> None:
        self.id = uuid4()
        self.tool_id = "codex"
        self.relative_path = "memory.md"
        self.category = "memory"
        self._test_content = content
        self.content_hash = "raw-revision-1"
        self.machine_id = None
        self.metadata_: dict = {}
        self.knowledge_status = "pending"
        self.knowledge_attempts = 0
        self.knowledge_retry_at = None
        self.knowledge_failure_kind = None


class _Session:
    def __init__(self) -> None:
        self.commit_count = 0
        self.flush_count = 0
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commit_count += 1

    async def flush(self) -> None:
        self.flush_count += 1


@pytest.fixture(autouse=True)
def _configured_provider(monkeypatch) -> None:
    monkeypatch.setenv("MEMENTO_AI_API_KEY", "test-key")
    monkeypatch.setenv("MEMENTO_AI_MODEL", "graph-model-v1")
    monkeypatch.setenv("MEMENTO_AI_BASE_URL", "https://provider.test/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MEMENTO_ANTHROPIC_API_KEY", raising=False)

    async def content_prefix(_db, document, *, max_chars: int) -> str:
        return document._test_content[:max_chars]

    monkeypatch.setattr(graph_service, "document_content_prefix", content_prefix)


@pytest.mark.asyncio
async def test_late_append_skips_same_bounded_successful_input(monkeypatch) -> None:
    doc = _Document("a" * 4_200)
    db = _Session()
    prompts: list[str] = []

    async def _empty_success(prompt: str) -> dict:
        prompts.append(prompt)
        return {}

    monkeypatch.setattr(graph_service, "_call_llm", _empty_success)

    assert await graph_service.extract_knowledge_from_document(db, doc) == 0
    first_hash = doc.metadata_["_graph_hash"]
    assert doc.knowledge_status == "ok"
    assert doc.knowledge_failure_kind is None

    doc._test_content += "late append outside the model window"
    doc.content_hash = "raw-revision-2"
    assert await graph_service.extract_knowledge_from_document(db, doc) == 0

    assert len(prompts) == 1
    assert db.commit_count == 1
    assert doc.metadata_["_graph_hash"] == first_hash
    assert doc.knowledge_status == "ok"


@pytest.mark.asyncio
async def test_model_version_change_is_a_new_graph_input(monkeypatch) -> None:
    doc = _Document("model-versioned input " * 30)
    db = _Session()
    calls = 0

    async def _empty_success(_prompt: str) -> dict:
        nonlocal calls
        calls += 1
        return {"entities": [], "relations": [], "observations": []}

    monkeypatch.setattr(graph_service, "_call_llm", _empty_success)

    await graph_service.extract_knowledge_from_document(db, doc)
    first_hash = doc.metadata_["_graph_hash"]
    monkeypatch.setenv("MEMENTO_AI_MODEL", "graph-model-v2")
    await graph_service.extract_knowledge_from_document(db, doc)

    assert calls == 2
    assert doc.metadata_["_graph_hash"] != first_hash
    assert doc.knowledge_status == "ok"


@pytest.mark.asyncio
async def test_permanent_provider_failure_is_not_retryable(monkeypatch) -> None:
    doc = _Document("permanent provider failure " * 20)
    db = _Session()

    async def _fail(_prompt: str):
        raise graph_service._ProviderFailure("authentication", permanent=True)

    monkeypatch.setattr(graph_service, "_call_llm", _fail)

    assert await graph_service.extract_knowledge_from_document(db, doc) == 0
    assert doc.knowledge_status == "permanent_failed"
    assert doc.knowledge_failure_kind == "authentication"
    assert doc.knowledge_retry_at is None


@pytest.mark.asyncio
async def test_transient_failure_persists_retry_timing(monkeypatch) -> None:
    doc = _Document("rate limited provider " * 20)
    db = _Session()
    before = datetime.now(timezone.utc)

    async def _fail(_prompt: str):
        raise graph_service._ProviderFailure(
            "rate_limit",
            permanent=False,
            retry_after_seconds=120,
        )

    monkeypatch.setattr(graph_service, "_call_llm", _fail)

    assert await graph_service.extract_knowledge_from_document(db, doc) == 0
    assert doc.knowledge_status == "failed"
    assert doc.knowledge_failure_kind == "rate_limit"
    assert doc.knowledge_retry_at is not None
    assert doc.knowledge_retry_at >= before
    assert doc.metadata_["_graph_attempt_hash"] not in (None, "")
