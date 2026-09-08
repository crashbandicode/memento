from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from server.api.conversation_exports import _all_documents, _load_document


class _EmptyResult:
    def mappings(self) -> "_EmptyResult":
        return self

    def one_or_none(self):
        return None

    def all(self):
        return []


class _CapturingSession:
    selected_keys: list[str]

    async def execute(self, statement):
        self.selected_keys = list(statement.selected_columns.keys())
        return _EmptyResult()


@pytest.mark.asyncio
async def test_export_document_query_labels_delivery_projection_columns() -> None:
    session = _CapturingSession()

    with pytest.raises(HTTPException) as caught:
        await _load_document(session, uuid.uuid4(), None)  # type: ignore[arg-type]

    assert caught.value.status_code == 404
    assert {
        "metadata_",
        "activity_at",
        "source_modified_at",
        "synced_at",
    }.issubset(session.selected_keys)


@pytest.mark.asyncio
async def test_bulk_export_query_labels_delivery_projection_columns() -> None:
    session = _CapturingSession()

    assert await _all_documents(session, None, [], []) == []  # type: ignore[arg-type]
    assert {
        "metadata_",
        "activity_at",
        "source_modified_at",
        "synced_at",
    }.issubset(session.selected_keys)
