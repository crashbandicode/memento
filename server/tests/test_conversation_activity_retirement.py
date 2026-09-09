"""Deterministic retirement of background 'Running' cards on session end.

These tests exercise the pure read-time retirement decision and its bounded
runtime lookup without the web stack, so they run under bare Python (no
fastapi/redis). A launched-and-forgotten Claude ``run_in_background`` shell
never emits a completion event; when its owning session ends the card must be
retired instead of lingering until the 24h freshness TTL.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_activity import (  # noqa: E402
    background_activity_retired_by_session_end,
    conversation_runtime_ended_at_map,
)

_SESSION_END = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
_STARTED = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)


def _activity(**overrides: object) -> dict:
    activity = {
        "activity_id": "tool-1",
        "activity_type": "shell",
        "status": "running",
        "command": "pytest -q &",
        "is_background": True,
        "started_at": _STARTED.isoformat(),
        "updated_at": _STARTED.isoformat(),
    }
    activity.update(overrides)
    return activity


class BackgroundRetirementDecisionTests(unittest.TestCase):
    def test_ended_session_retires_running_background_card(self) -> None:
        self.assertTrue(
            background_activity_retired_by_session_end(_activity(), _SESSION_END)
        )

    def test_open_session_keeps_running_background_card(self) -> None:
        # A session that is still open (no recorded end) is exactly the case
        # where a genuinely-running background shell must remain visible.
        self.assertFalse(
            background_activity_retired_by_session_end(_activity(), None)
        )

    def test_non_background_activity_is_never_retired(self) -> None:
        self.assertFalse(
            background_activity_retired_by_session_end(
                _activity(is_background=False),
                _SESSION_END,
            )
        )

    def test_terminal_background_activity_is_never_retired(self) -> None:
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status):
                self.assertFalse(
                    background_activity_retired_by_session_end(
                        _activity(status=status),
                        _SESSION_END,
                    )
                )

    def test_activity_started_after_session_end_is_kept(self) -> None:
        # Guards a stale end marker racing a newer observation: a card that
        # clearly began after the recorded end (beyond clock skew) survives.
        started_after = _SESSION_END + timedelta(hours=1)
        self.assertFalse(
            background_activity_retired_by_session_end(
                _activity(
                    started_at=started_after.isoformat(),
                    updated_at=started_after.isoformat(),
                ),
                _SESSION_END,
            )
        )

    def test_missing_start_time_retires_on_ended_session(self) -> None:
        self.assertTrue(
            background_activity_retired_by_session_end(
                _activity(started_at=None, updated_at=None),
                _SESSION_END,
            )
        )


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list:
        return self._rows


class _Db:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.statements: list = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self._rows)


def _runtime(session_id: str, *, state: str, ended_at: object) -> SimpleNamespace:
    return SimpleNamespace(
        machine_id="machine-1",
        tool_id="claude_code",
        session_id=session_id,
        state=state,
        ended_at=ended_at,
    )


class ConversationRuntimeEndedMapTests(unittest.IsolatedAsyncioTestCase):
    ended_session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    open_session = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    null_end_session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    def _identities(self) -> list[tuple]:
        return [
            ("doc-ended", "machine-1", "claude_code", {"session_id": self.ended_session}),
            ("doc-open", "machine-1", "claude_code", {"session_id": self.open_session}),
            ("doc-null", "machine-1", "claude_code", {"session_id": self.null_end_session}),
            # No native session id -> contributes no key and no query row.
            ("doc-anon", "machine-1", "claude_code", {}),
        ]

    async def test_only_ended_sessions_map_to_end_time(self) -> None:
        rows = [
            _runtime(self.ended_session, state="ended", ended_at=_SESSION_END),
            _runtime(self.open_session, state="running", ended_at=None),
            _runtime(self.null_end_session, state="ended", ended_at=None),
        ]
        db = _Db(rows)

        mapping = await conversation_runtime_ended_at_map(db, self._identities())

        self.assertEqual(mapping, {"doc-ended": _SESSION_END})
        self.assertEqual(len(db.statements), 1)

    async def test_no_identities_avoids_query(self) -> None:
        db = _Db([])

        mapping = await conversation_runtime_ended_at_map(db, [])

        self.assertEqual(mapping, {})
        self.assertEqual(db.statements, [])


if __name__ == "__main__":
    unittest.main()
