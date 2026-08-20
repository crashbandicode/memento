from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from server.api.devices import _is_visible_device
from server.db.models import Machine


NOW = datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc)


def machine(*, heartbeat_age: timedelta, version: str | None = None) -> Machine:
    return Machine(
        id=uuid.uuid4(),
        name="butterbridge (Windows)",
        collector_token_hash=uuid.uuid4().hex,
        collector_version=version,
        created_at=NOW - heartbeat_age,
        last_heartbeat=NOW - heartbeat_age,
    )


def test_abandoned_unversioned_empty_registration_is_hidden() -> None:
    assert not _is_visible_device(
        machine(heartbeat_age=timedelta(days=19)),
        0,
        now=NOW,
    )


def test_fresh_empty_registration_keeps_its_setup_window() -> None:
    assert _is_visible_device(
        machine(heartbeat_age=timedelta(minutes=5)),
        0,
        now=NOW,
    )


def test_versioned_or_document_bearing_device_never_disappears() -> None:
    stale = machine(heartbeat_age=timedelta(days=19), version="0.0.38")
    assert _is_visible_device(stale, 0, now=NOW)

    unversioned = machine(heartbeat_age=timedelta(days=19))
    assert _is_visible_device(unversioned, 1, now=NOW)
