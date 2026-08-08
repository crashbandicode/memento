from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from server.services.ingest_service import (
    _conversation_search_index_needs_refresh,
    _ingest_cache_scope,
    _record_tool_sync,
)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "is_new_document": False,
                "mode": "delta",
                "new_search_text": "",
                "previous_title": "Thread",
                "current_title": "Thread",
            },
            False,
        ),
        (
            {
                "is_new_document": False,
                "mode": "delta",
                "new_search_text": "[assistant] done",
                "previous_title": "Thread",
                "current_title": "Thread",
            },
            True,
        ),
        (
            {
                "is_new_document": False,
                "mode": "delta",
                "new_search_text": "",
                "previous_title": "Old",
                "current_title": "Renamed",
            },
            True,
        ),
        (
            {
                "is_new_document": False,
                "mode": "full",
                "new_search_text": "",
                "previous_title": "Thread",
                "current_title": "Thread",
            },
            True,
        ),
    ],
)
def test_conversation_index_refresh_is_change_driven(kwargs, expected) -> None:
    assert _conversation_search_index_needs_refresh(**kwargs) is expected


def test_tool_only_delta_does_not_invalidate_read_namespaces() -> None:
    assert _ingest_cache_scope(
        category="conversation",
        mode="delta",
        activity_advanced=False,
        title_changed=False,
        lifecycle_changed=False,
    ) == (False, False)


def test_human_activity_invalidates_daily_and_actual_project() -> None:
    assert _ingest_cache_scope(
        category="conversation",
        mode="delta",
        activity_advanced=True,
        title_changed=False,
        lifecycle_changed=False,
    ) == (True, True)


def test_non_conversation_change_does_not_invalidate_conversation_caches() -> None:
    assert _ingest_cache_scope(
        category="note",
        mode="full",
        activity_advanced=False,
        title_changed=True,
        lifecycle_changed=False,
    ) == (False, False)


class _SessionStub:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement) -> None:
        self.statements.append(statement)


@pytest.mark.asyncio
async def test_existing_document_does_not_recount_tool_files() -> None:
    db = _SessionStub()
    tool = SimpleNamespace(id="codex", last_sync_at=None)
    synced_at = datetime.now(timezone.utc)

    await _record_tool_sync(
        db,
        tool,
        synced_at,
        is_new_document=False,
    )

    assert tool.last_sync_at == synced_at
    assert db.statements == []


@pytest.mark.asyncio
async def test_new_document_atomically_increments_tool_count() -> None:
    db = _SessionStub()
    tool = SimpleNamespace(id="codex", last_sync_at=None)

    await _record_tool_sync(
        db,
        tool,
        datetime.now(timezone.utc),
        is_new_document=True,
    )

    assert len(db.statements) == 1
    assert "total_files" in str(db.statements[0])
