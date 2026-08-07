"""Device service — registers and updates collector devices."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Machine


class DeviceOwnershipError(PermissionError):
    """Raised when a collector device identifier belongs to another user."""


async def ensure_device(
    db: AsyncSession,
    device_id: str,
    device_name: str,
    device_platform: str,
    user_id: uuid.UUID | None = None,
    *,
    touch_heartbeat: bool = False,
) -> Machine:
    """Find or create a device without serializing its ordinary ingest work.

    The advisory lock protects only the one-time missing-row race.  Taking a
    transaction-scoped device lock before every ingest made all uploads from a
    collector wait for the slowest transcript transaction.  Heartbeats already
    have dedicated endpoints, so normal content and metadata requests only
    validate ownership.
    """
    result = await db.execute(
        select(Machine).where(Machine.collector_token_hash == device_id)
    )
    machine = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if machine is None:
        # The collector drains its initial queue concurrently. Serialize only
        # first registration, then re-check after the winner commits.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:device_id))"),
            {"device_id": device_id},
        )
        machine = (
            await db.execute(
                select(Machine).where(Machine.collector_token_hash == device_id)
            )
        ).scalar_one_or_none()
        if machine is None:
            machine = Machine(
                name=device_name,
                collector_token_hash=device_id,
                user_id=user_id,
                last_heartbeat=now,
            )
            db.add(machine)
            await db.flush()
            return machine

    if user_id and machine.user_id and machine.user_id != user_id:
        raise DeviceOwnershipError("collector device belongs to another user")
    machine.name = device_name
    if touch_heartbeat:
        machine.last_heartbeat = now
    # Bind to user if not already bound
    if user_id and not machine.user_id:
        machine.user_id = user_id

    return machine


async def list_devices(db: AsyncSession) -> list[Machine]:
    """List all registered devices."""
    result = await db.execute(
        select(Machine).order_by(Machine.last_heartbeat.desc())
    )
    return list(result.scalars().all())
