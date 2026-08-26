from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from mcp_server.content_store import document_content


class _ScalarResult:
    def scalar_one_or_none(self):
        return "postgres compatibility text"


class _FallbackDb:
    async def execute(self, _statement):
        return _ScalarResult()


@pytest.mark.asyncio
async def test_no_s3_configuration_preserves_postgres_content(monkeypatch) -> None:
    for name in (
        "MEMENTO_S3_ENDPOINT",
        "MEMENTO_S3_ACCESS_KEY",
        "MEMENTO_S3_SECRET_KEY",
        "MEMENTO_S3_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    document = SimpleNamespace(
        id=uuid.uuid4(),
        content_s3_key="document-content/v1/document/sha",
        content_object_sha256="0" * 64,
        content_object_size_bytes=1,
        content_object_verified_at=object(),
    )

    content = await document_content(_FallbackDb(), document)

    assert content == "postgres compatibility text"
