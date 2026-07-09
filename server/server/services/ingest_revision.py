"""Deterministic ordering helpers for snapshots of one collector source."""

from __future__ import annotations

from datetime import datetime, timezone


def normalized_source_timestamp(
    value: datetime | float | int | None,
) -> datetime | None:
    """Return one timezone-aware timestamp, or ``None`` when ordering is unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def bounded_source_timestamp(
    value: datetime | float | int | None,
    observed_at: datetime | float | int | None,
) -> datetime | None:
    """Normalize source time without allowing it beyond server observation.

    Filesystem clocks are collector-controlled and can be skewed into the
    future.  Since source time is also a FULL-snapshot revision key, persisting
    an unbounded value could make every later valid revision look stale.
    """
    source_timestamp = normalized_source_timestamp(value)
    observation_timestamp = normalized_source_timestamp(observed_at)
    if source_timestamp is None:
        return observation_timestamp
    if observation_timestamp is None:
        return source_timestamp
    return min(source_timestamp, observation_timestamp)


def full_snapshot_revision(
    *,
    timestamp: datetime | float | int | None,
    offset: int,
    file_size: int,
    content_hash: str,
) -> tuple[datetime, int, int, str] | None:
    """Build the revision persisted on ``Document`` for deterministic ordering."""
    normalized_timestamp = normalized_source_timestamp(timestamp)
    if (
        normalized_timestamp is None
        or isinstance(offset, bool)
        or isinstance(file_size, bool)
    ):
        return None
    try:
        normalized_offset = int(offset)
        normalized_size = int(file_size)
    except (TypeError, ValueError):
        return None
    if (
        normalized_offset < 0
        or normalized_size < 0
        or not isinstance(content_hash, str)
        or not content_hash
    ):
        return None
    return normalized_timestamp, normalized_offset, normalized_size, content_hash


def committed_full_supersedes(
    *,
    existing_hash: str,
    existing_timestamp: datetime | None,
    existing_offset: int,
    existing_size: int,
    incoming_hash: str,
    incoming_timestamp: datetime | float | int | None,
    incoming_offset: int,
    incoming_size: int,
) -> bool:
    """Return whether a committed FULL is at least as new as an incoming FULL."""
    if existing_hash == incoming_hash:
        return True
    existing_revision = full_snapshot_revision(
        timestamp=existing_timestamp,
        offset=existing_offset,
        file_size=existing_size,
        content_hash=existing_hash,
    )
    incoming_revision = full_snapshot_revision(
        timestamp=incoming_timestamp,
        offset=incoming_offset,
        file_size=incoming_size,
        content_hash=incoming_hash,
    )
    if existing_revision is None or incoming_revision is None:
        return False
    return existing_revision >= incoming_revision
