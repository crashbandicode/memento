from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from server.api.dashboard import (
    _conversation_resume_command,
    _fold_open_thread_hierarchy_by_machine,
    _handoff_predecessor_document_ids,
    _select_open_thread_rows,
    _thread_health_by_document,
)
from server.services.conversation_hierarchy import ConversationRef


def _row(document_id: str, session_id: str, **metadata):
    return SimpleNamespace(
        id=document_id,
        hierarchy_metadata={"session_id": session_id, **metadata},
        session_id=session_id,
        root_thread_id=session_id,
        parent_thread_id=None,
        is_subagent=False,
        tool_id="codex",
    )


def test_handoff_predecessor_suppression_uses_explicit_lineage_only() -> None:
    predecessor = _row("predecessor-document", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    successor = _row(
        "successor-document",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        briefing_kind="handoff",
        briefing_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    ordinary = _row("ordinary-document", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")

    assert _handoff_predecessor_document_ids(
        [predecessor, successor, ordinary]
    ) == {"predecessor-document"}


def test_handoff_predecessor_suppression_fails_closed_on_ambiguous_uuid() -> None:
    duplicate_one = _row("first-copy", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    duplicate_two = _row("second-copy", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    successor = _row(
        "successor-document",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        briefing_kind="handoff",
        briefing_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    assert _handoff_predecessor_document_ids(
        [duplicate_one, duplicate_two, successor]
    ) == set()


def test_open_thread_selection_uses_active_window_plus_explicit_pins() -> None:
    now = datetime(2026, 9, 7, 20, tzinfo=UTC)
    recent = _row("recent", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    stale = _row("stale", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    pinned_stale = _row("pinned-stale", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    predecessor = _row("predecessor", "dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    pinned_predecessor = _row(
        "pinned-predecessor", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )
    for row in (recent, stale, pinned_stale, predecessor, pinned_predecessor):
        row.is_archived = row is pinned_stale
        row.activity_at = now - (
            timedelta(minutes=5) if row is recent else timedelta(days=2)
        )
        row.source_modified_at = None
        row.synced_at = row.activity_at

    selected = _select_open_thread_rows(
        [recent, stale, pinned_stale, predecessor, pinned_predecessor],
        visible_document_ids={
            "recent",
            "stale",
            "pinned-stale",
            "predecessor",
            "pinned-predecessor",
        },
        pinned_root_ids={"pinned-stale", "pinned-predecessor"},
        handoff_predecessor_ids={"predecessor", "pinned-predecessor"},
        logical_activity_by_document={},
        now=now,
        active_minutes=20,
    )

    assert {row.id for row in selected} == {
        "recent",
        "pinned-stale",
        "pinned-predecessor",
    }


def test_open_thread_selection_keeps_explicitly_running_idle_threads() -> None:
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    running = _row("running", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    ended = _row("ended", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    for row in (running, ended):
        row.is_archived = False
        row.activity_at = now - timedelta(days=2)
        row.source_modified_at = None
        row.synced_at = row.activity_at

    selected = _select_open_thread_rows(
        [running, ended],
        visible_document_ids={"running", "ended"},
        pinned_root_ids=set(),
        handoff_predecessor_ids=set(),
        logical_activity_by_document={},
        running_document_ids={"running"},
        now=now,
        active_minutes=20,
    )

    assert [row.id for row in selected] == ["running"]


def test_open_thread_selection_does_not_cross_fold_machine_copies() -> None:
    """Each physical collector copy remains eligible for its own machine group."""
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    butterbridge = _row("butterbridge-copy", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    yoga = _row("yoga-copy", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    for row in (butterbridge, yoga):
        row.is_archived = False
        row.activity_at = now - timedelta(minutes=3)
        row.source_modified_at = None
        row.synced_at = row.activity_at

    selected = _select_open_thread_rows(
        [butterbridge, yoga],
        visible_document_ids={"butterbridge-copy", "yoga-copy"},
        pinned_root_ids=set(),
        handoff_predecessor_ids=set(),
        logical_activity_by_document={},
        running_document_ids=set(),
        now=now,
        active_minutes=20,
    )

    assert [row.id for row in selected] == ["butterbridge-copy", "yoga-copy"]


def test_open_thread_hierarchy_folds_each_machine_independently() -> None:
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    butterbridge = _row("butterbridge-copy", session_id)
    yoga = _row("yoga-copy", session_id)
    butterbridge.machine_id = "machine-butterbridge"
    yoga.machine_id = "machine-yoga"
    refs = [
        ConversationRef(
            document_id=row.id,
            tool_id=row.tool_id,
            relative_path=f"sessions/{session_id}.jsonl",
            metadata=row.hierarchy_metadata,
        )
        for row in (butterbridge, yoga)
    ]

    visible, canonical, _activity = _fold_open_thread_hierarchy_by_machine(
        [butterbridge, yoga],
        refs,
    )

    assert visible == {"butterbridge-copy", "yoga-copy"}
    assert canonical == {
        "butterbridge-copy": "butterbridge-copy",
        "yoga-copy": "yoga-copy",
    }


def test_resume_command_uses_verified_location_platform() -> None:
    thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    assert _conversation_resume_command(
        "codex",
        thread_id,
        {"host": "yoga", "path": r"C:\work\O'Brien", "platform": "Windows"},
    ) == (
        "Set-Location -LiteralPath 'C:\\work\\O''Brien' && "
        f"codex resume '{thread_id}'"
    )
    assert _conversation_resume_command(
        "cursor",
        thread_id,
        {"host": "yoga", "path": "/work/o'brien", "platform": "WSL2"},
    ) == f"cd -- '/work/o'\"'\"'brien' && cursor-agent --resume='{thread_id}'"


def test_health_join_exposes_only_observed_per_thread_measurements() -> None:
    response = {
        "available": True,
        "stale": False,
        "snapshot": {
            "hygiene": {
                "hopThreads": {
                    "items": [
                        {
                            "documentId": "document-one",
                            "source": "codex",
                            "ratio": 412.5,
                            "status": "hard",
                            "hopLine": 200,
                            "hardLine": 400,
                        },
                        {"documentId": "missing-ratio", "source": "cursor"},
                    ]
                }
            }
        },
    }

    assert _thread_health_by_document(response) == {
        "document-one": {
            "available": True,
            "stale": False,
            "source": "codex",
            "ratio": 412.5,
            "status": "hard",
            "hop_line": 200.0,
            "hard_line": 400.0,
        }
    }
    assert _thread_health_by_document({"available": False}) == {}
