"""Stable physical-host grouping for collector machine identities.

Collectors intentionally keep separate machine rows per runtime (for example,
Windows and WSL) and per persistent collector ID.  Navigation groups those
rows only inside the owning account boundary, using the normalized hostname
from the collector's ``"<hostname> (<platform>)"`` display name.

The machine rows remain independently addressable.  A ``host_...`` scope ID
resolves to every authorized member machine so filters do not drop documents
when a collector is reinstalled or when one physical host has several runtimes.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from typing import Iterable, Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Machine, User


HOST_GROUP_PREFIX = "host_"
_PLATFORM_SUFFIX = re.compile(
    r"\s+\((windows|linux|wsl2?|darwin|macos)\)\s*$",
    re.IGNORECASE,
)
_PLATFORM_NAMES = {
    "windows": "Windows",
    "linux": "Linux",
    "wsl": "WSL",
    "wsl2": "WSL2",
    "darwin": "Darwin",
    "macos": "Darwin",
}


def split_device_name(name: str) -> tuple[str, str]:
    """Return a display hostname and runtime platform from a machine name."""
    clean_name = " ".join((name or "").strip().split()) or "Unknown device"
    match = _PLATFORM_SUFFIX.search(clean_name)
    if not match:
        return clean_name, "Unknown"
    hostname = clean_name[:match.start()].strip() or clean_name
    return hostname, _PLATFORM_NAMES[match.group(1).casefold()]


def normalized_hostname(name: str) -> str:
    """Normalize only the host portion used inside an ownership boundary."""
    hostname, _platform = split_device_name(name)
    return hostname.casefold()


def _ownership_boundary(machine: Machine) -> str:
    # Historical unowned rows are visible only to privileged users.  Keeping
    # each unowned row in its own boundary avoids joining unrelated tenants by
    # a common hostname such as "desktop".
    if machine.user_id is None:
        return f"unowned-machine:{machine.id}"
    return f"user:{machine.user_id}"


def host_group_id(machine: Machine) -> str:
    """Return the stable, URL-safe host scope for one machine."""
    material = (
        f"{_ownership_boundary(machine)}\0{normalized_hostname(machine.name)}"
    ).encode("utf-8")
    return HOST_GROUP_PREFIX + hashlib.sha256(material).hexdigest()[:24]


def build_host_groups(
    machines: Sequence[Machine],
    document_rows: Iterable[tuple],
) -> list[dict]:
    """Build deterministic host summaries and de-duplicate churned documents.

    ``document_rows`` contains ``(document_id, machine_id, tool_id,
    relative_path)``.  Group counts use ``(tool_id, relative_path)`` so the
    same collected file copied into a replacement machine registration is
    counted once at host level.  Child identity counts remain per machine.
    """
    machine_by_id = {machine.id: machine for machine in machines}
    group_doc_keys: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    child_doc_keys: dict[uuid.UUID, dict[str, set[tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(set)
    )

    for _document_id, machine_id, tool_id, relative_path in document_rows:
        machine = machine_by_id.get(machine_id)
        if machine is None or tool_id == "system":
            continue
        document_key = (str(tool_id), str(relative_path))
        group_doc_keys[host_group_id(machine)][str(tool_id)].add(document_key)
        child_doc_keys[machine_id][str(tool_id)].add(document_key)

    grouped: dict[str, list[Machine]] = defaultdict(list)
    for machine in machines:
        grouped[host_group_id(machine)].append(machine)

    items: list[dict] = []
    for group_id, members in grouped.items():
        members = sorted(
            members,
            key=lambda machine: (
                split_device_name(machine.name)[1].casefold(),
                machine.name.casefold(),
                machine.collector_token_hash,
                str(machine.id),
            ),
        )
        hostname_labels = [split_device_name(machine.name)[0] for machine in members]
        display_name = min(hostname_labels, key=lambda value: (value.casefold(), value))
        tools = [
            {"id": tool_id, "file_count": len(document_keys)}
            for tool_id, document_keys in sorted(group_doc_keys[group_id].items())
        ]

        identities = []
        for machine in members:
            _hostname, platform = split_device_name(machine.name)
            child_tools = [
                {"id": tool_id, "file_count": len(document_keys)}
                for tool_id, document_keys in sorted(
                    child_doc_keys[machine.id].items()
                )
            ]
            identities.append({
                "id": str(machine.id),
                "device_id": machine.collector_token_hash,
                "name": machine.name,
                "platform": platform,
                "label": f"{platform} · {machine.collector_token_hash[:8]}",
                "last_heartbeat": (
                    machine.last_heartbeat.isoformat()
                    if machine.last_heartbeat
                    else None
                ),
                "total_files": sum(tool["file_count"] for tool in child_tools),
                "tools": child_tools,
            })

        items.append({
            "id": group_id,
            "group_id": group_id,
            "device_id": group_id,
            "name": display_name,
            "total_files": sum(tool["file_count"] for tool in tools),
            "tools": tools,
            "machine_ids": [identity["id"] for identity in identities],
            "device_ids": [identity["device_id"] for identity in identities],
            "identities": identities,
        })

    return sorted(
        items,
        key=lambda item: (item["name"].casefold(), item["group_id"]),
    )


async def accessible_machines(db: AsyncSession, user: User) -> list[Machine]:
    """Return machines visible to a user in deterministic order."""
    query = select(Machine).order_by(
        Machine.name,
        Machine.collector_token_hash,
        Machine.id,
    )
    if user.role not in ("admin", "owner"):
        query = query.where(Machine.user_id == user.id)
    return list((await db.execute(query)).scalars().all())


def _machine_is_accessible(machine: Machine | None, user: User) -> bool:
    return bool(
        machine
        and (
            user.role in ("admin", "owner")
            or machine.user_id == user.id
        )
    )


async def resolve_device_scope_ids(
    db: AsyncSession,
    user: User,
    device_id: str,
) -> list[uuid.UUID]:
    """Resolve a host group, collector ID, or legacy database UUID."""
    if device_id.startswith(HOST_GROUP_PREFIX):
        matches = [
            machine.id
            for machine in await accessible_machines(db, user)
            if host_group_id(machine) == device_id
        ]
        if not matches:
            raise HTTPException(status_code=404, detail="Device not found")
        return matches

    machine = (
        await db.execute(
            select(Machine).where(Machine.collector_token_hash == device_id)
        )
    ).scalar_one_or_none()
    if machine is None:
        try:
            database_id = uuid.UUID(device_id)
        except (AttributeError, ValueError):
            database_id = None
        if database_id is not None:
            machine = (
                await db.execute(
                    select(Machine).where(Machine.id == database_id)
                )
            ).scalar_one_or_none()

    if not _machine_is_accessible(machine, user):
        raise HTTPException(status_code=404, detail="Device not found")
    return [machine.id]
