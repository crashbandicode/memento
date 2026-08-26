from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from mcp_server.content_store import document_content


class _ScalarResult:
    def scalar_one_or_none(self):
        return "postgres compatibility text"


class _FallbackDb:
    async def execute(self, _statement, _parameters):
        return _ScalarResult()


class _UndefinedColumn(Exception):
    sqlstate = "42703"


class _DroppedColumnDb:
    async def execute(self, statement, parameters):
        raise DBAPIError(statement, parameters, _UndefinedColumn(), False)


@pytest.mark.asyncio
async def test_old_server_without_s3_configuration_uses_legacy_content(monkeypatch) -> None:
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


@pytest.mark.asyncio
async def test_dropped_legacy_column_is_treated_as_no_fallback() -> None:
    document = SimpleNamespace(id=uuid.uuid4())

    assert await document_content(_DroppedColumnDb(), document) is None
