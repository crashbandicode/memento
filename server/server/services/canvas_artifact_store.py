"""Persistence and device-owned backfill for captured Cursor Canvas artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    CanvasArtifact,
    CanvasArtifactBlob,
    CanvasArtifactInventoryState,
    CanvasArtifactReference,
    ConversationMessage,
    Document,
    Machine,
    User,
)
from .canvas_artifacts import detect_message_canvases

MAX_SOURCE_BYTES = 200_000
MAX_COMPILED_BYTES = 500_000
MAX_RUNTIME_BYTES = 2 * 1024 * 1024
MAX_INVENTORY_MESSAGES = 500
MAX_PENDING_GROUPS = 16
MAX_REFERENCE_IDS = 128
ALLOWED_TOOLS = ("cursor", "claude_code", "codex")
TERMINAL_OUTCOMES = {
    "renderable",
    "static_only",
    "missing",
    "rejected",
    "unsupported",
    "already_current",
}

_LOCAL_ABSOLUTE_CANVAS = re.compile(
    r"^(?:[A-Za-z]:[\\/]|/).+\.cursor[\\/]projects[\\/]"
    r"[^\\/]+[\\/]canvases[\\/][^\\/]+\.canvas\.tsx$",
    re.IGNORECASE,
)
_IMPORT_FROM = re.compile(
    r"""(?:import|export)\s+(?:type\s+)?(?:[\s\S]*?\s+from\s+)?["']([^"']+)["']""",
    re.MULTILINE,
)
_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\(")
_FORBIDDEN_SOURCE = re.compile(
    r"""(?x)
    \b(?:eval|Function|fetch|XMLHttpRequest|WebSocket|EventSource|Worker|
    SharedWorker|importScripts|require)\s*\(
    |\b(?:window|document|globalThis|localStorage|sessionStorage|indexedDB|
    parent|top|opener)\b
    """
)
_FORBIDDEN_COMPILED = re.compile(
    r"""(?x)
    \b(?:eval|Function|fetch|XMLHttpRequest|WebSocket|EventSource|Worker|
    SharedWorker|importScripts|require|setInterval|setTimeout|
    requestAnimationFrame|queueMicrotask)\s*\(
    |\b(?:while|for)\s*\(
    |\b(?:window|document|globalThis|localStorage|sessionStorage|indexedDB|
    parent|top|opener)\b
    """
)
_SECRETS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|
        password|private[_-]?key)\b\s*[:=]\s*["'][^"'\r\n]{12,}["']
        """
    ),
)
_SAFE_REASON = re.compile(r"^[a-z0-9_]{1,128}$")


def normalized_path_hash(path: str) -> str:
    normalized = path.strip().replace("\\", "/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_collector_eligible(path: str) -> bool:
    return bool(_LOCAL_ABSOLUTE_CANVAS.fullmatch(path)) and ".." not in re.split(
        r"[\\/]", path
    )


async def inventory_machine_canvases(
    db: AsyncSession,
    machine_id: uuid.UUID,
) -> dict[str, int]:
    """Discover a bounded set of exact references without reading source paths."""
    await db.execute(
        pg_insert(CanvasArtifactInventoryState)
        .values(machine_id=machine_id, last_message_id=0)
        .on_conflict_do_nothing(
            index_elements=[CanvasArtifactInventoryState.machine_id]
        )
    )
    state = (
        await db.execute(
            select(CanvasArtifactInventoryState)
            .where(CanvasArtifactInventoryState.machine_id == machine_id)
            .with_for_update()
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(ConversationMessage, Document.tool_id)
            .join(Document, Document.id == ConversationMessage.document_id)
            .where(
                Document.machine_id == machine_id,
                Document.tool_id.in_(ALLOWED_TOOLS),
                ConversationMessage.id > state.last_message_id,
                ConversationMessage.content.ilike("%.canvas.tsx%"),
            )
            .order_by(ConversationMessage.id)
            .limit(MAX_INVENTORY_MESSAGES)
            # Ingest replaces a document's normalized rows transactionally.
            # Keep selected messages alive until their FK-backed references
            # and the inventory high-water mark are flushed.
            .with_for_update(of=ConversationMessage, read=True, key_share=True)
        )
    ).all()
    message_ids = [message.id for message, _tool_id in rows]
    existing = set(
        (
            await db.execute(
                select(
                    CanvasArtifactReference.message_id,
                    CanvasArtifactReference.path_hash,
                ).where(
                    CanvasArtifactReference.machine_id == machine_id,
                    CanvasArtifactReference.message_id.in_(message_ids),
                )
            )
        ).all()
    ) if message_ids else set()
    discovered = 0
    unsupported = 0
    for message, _tool_id in rows:
        for descriptor in detect_message_canvases(message.content):
            path = str(descriptor.get("path") or "")
            if not path:
                continue
            path_hash = normalized_path_hash(path)
            if (message.id, path_hash) in existing:
                continue
            eligible = _is_collector_eligible(path)
            db.add(
                CanvasArtifactReference(
                    document_id=message.document_id,
                    message_id=message.id,
                    machine_id=machine_id,
                    recorded_path=path,
                    path_hash=path_hash,
                    name=str(descriptor.get("name") or "canvas")[:120],
                    status="discovered" if eligible else "unsupported",
                    reason=None if eligible else "non_local_or_unsupported_path",
                )
            )
            existing.add((message.id, path_hash))
            if eligible:
                discovered += 1
            else:
                unsupported += 1
    if rows:
        state.last_message_id = max(message.id for message, _tool_id in rows)
    await db.flush()
    return {"discovered": discovered, "unsupported": unsupported}


async def pending_machine_canvases(
    db: AsyncSession,
    machine_id: uuid.UUID,
) -> list[dict[str, Any]]:
    await inventory_machine_canvases(db, machine_id)
    rows = (
        await db.execute(
            select(CanvasArtifactReference)
            .where(
                CanvasArtifactReference.machine_id == machine_id,
                CanvasArtifactReference.status == "discovered",
            )
            .order_by(
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.created_at,
            )
            .limit(MAX_PENDING_GROUPS * MAX_REFERENCE_IDS)
        )
    ).scalars().all()
    groups: dict[str, dict[str, Any]] = {}
    for reference in rows:
        group = groups.get(reference.path_hash)
        if group is None:
            if len(groups) >= MAX_PENDING_GROUPS:
                break
            group = {
                "path": reference.recorded_path,
                "path_hash": reference.path_hash,
                "name": reference.name,
                "reference_ids": [],
            }
            groups[reference.path_hash] = group
        if len(group["reference_ids"]) < MAX_REFERENCE_IDS:
            group["reference_ids"].append(str(reference.id))
    return list(groups.values())


async def _owned_references(
    db: AsyncSession,
    machine_id: uuid.UUID,
    reference_ids: list[uuid.UUID],
    path_hash: str,
) -> list[CanvasArtifactReference]:
    if not reference_ids or len(reference_ids) > MAX_REFERENCE_IDS:
        raise HTTPException(status_code=400, detail="invalid reference set")
    references = (
        (
            await db.execute(
                select(CanvasArtifactReference).where(
                    CanvasArtifactReference.id.in_(reference_ids),
                    CanvasArtifactReference.machine_id == machine_id,
                    CanvasArtifactReference.path_hash == path_hash,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(references) != len(set(reference_ids)):
        raise HTTPException(status_code=404, detail="canvas reference not found")
    return references


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_source(payload: bytes, expected_hash: str) -> str:
    if not payload or len(payload) > MAX_SOURCE_BYTES or _hash(payload) != expected_hash:
        raise HTTPException(status_code=400, detail="invalid source payload")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid source encoding") from exc
    imports = _IMPORT_FROM.findall(text)
    if (
        not imports
        or any(module != "cursor/canvas" for module in imports)
        or _DYNAMIC_IMPORT.search(text)
        or _FORBIDDEN_SOURCE.search(text)
        or not re.search(r"\bexport\s+default\b", text)
        or any(pattern.search(text) for pattern in _SECRETS)
    ):
        raise HTTPException(status_code=400, detail="source failed security policy")
    return text


def validate_compiled(payload: bytes, expected_hash: str) -> None:
    if (
        not payload
        or len(payload) > MAX_COMPILED_BYTES
        or _hash(payload) != expected_hash
    ):
        raise HTTPException(status_code=400, detail="invalid compiled payload")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid compiled encoding") from exc
    if (
        re.search(r"^\s*(?:import|export\s+\{)", text, re.MULTILINE)
        or _DYNAMIC_IMPORT.search(text)
        or _FORBIDDEN_COMPILED.search(text)
        or "export default" not in text
    ):
        raise HTTPException(status_code=400, detail="compiled payload failed policy")


def validate_runtime(payload: bytes, expected_hash: str) -> None:
    if (
        not payload
        or len(payload) > MAX_RUNTIME_BYTES
        or _hash(payload) != expected_hash
    ):
        raise HTTPException(status_code=400, detail="invalid runtime payload")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid runtime encoding") from exc
    if "mountCanvas" not in text or not re.search(r"\bexport\s*\{", text):
        raise HTTPException(status_code=400, detail="invalid Canvas runtime")


async def _store_blob(
    db: AsyncSession,
    *,
    content_hash: str,
    kind: str,
    media_type: str,
    content: bytes,
) -> None:
    existing = await db.get(CanvasArtifactBlob, content_hash)
    if existing is not None:
        if existing.size_bytes != len(content) or existing.content != content:
            raise HTTPException(status_code=409, detail="artifact hash collision")
        return
    db.add(
        CanvasArtifactBlob(
            content_hash=content_hash,
            kind=kind,
            media_type=media_type,
            size_bytes=len(content),
            content=content,
        )
    )
    await db.flush()


async def store_captured_canvas(
    db: AsyncSession,
    *,
    user: User,
    machine: Machine,
    metadata: dict[str, Any],
    source: bytes,
    compiled: bytes | None,
    runtime: bytes | None,
) -> tuple[CanvasArtifact, str, int]:
    try:
        reference_ids = [uuid.UUID(value) for value in metadata["reference_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid reference ids") from exc
    path_hash = str(metadata.get("path_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", path_hash):
        raise HTTPException(status_code=400, detail="invalid path hash")
    references = await _owned_references(db, machine.id, reference_ids, path_hash)

    source_hash = str(metadata.get("source_hash") or "")
    validate_source(source, source_hash)
    requested_mode = str(metadata.get("render_mode") or "")
    if requested_mode not in {"interactive", "source_only"}:
        raise HTTPException(status_code=400, detail="invalid render mode")

    compiled_hash: str | None = None
    runtime_hash: str | None = None
    if requested_mode == "interactive":
        compiled_hash = str(metadata.get("compiled_hash") or "")
        runtime_hash = str(metadata.get("runtime_hash") or "")
        if compiled is None or runtime is None:
            raise HTTPException(status_code=400, detail="render payload is incomplete")
        validate_compiled(compiled, compiled_hash)
        validate_runtime(runtime, runtime_hash)
    elif compiled is not None or runtime is not None:
        raise HTTPException(status_code=400, detail="unexpected render payload")

    await _store_blob(
        db,
        content_hash=source_hash,
        kind="source",
        media_type="text/typescript; charset=utf-8",
        content=source,
    )
    if compiled is not None and compiled_hash is not None:
        await _store_blob(
            db,
            content_hash=compiled_hash,
            kind="compiled",
            media_type="text/javascript; charset=utf-8",
            content=compiled,
        )
    if runtime is not None and runtime_hash is not None:
        await _store_blob(
            db,
            content_hash=runtime_hash,
            kind="runtime",
            media_type="text/javascript; charset=utf-8",
            content=runtime,
        )

    artifact = (
        await db.execute(
            select(CanvasArtifact).where(
                CanvasArtifact.user_id == user.id,
                CanvasArtifact.source_hash == source_hash,
            )
        )
    ).scalar_one_or_none()
    already_current = artifact is not None
    if artifact is None:
        artifact = CanvasArtifact(
            user_id=user.id,
            origin_machine_id=machine.id,
            source_hash=source_hash,
            compiled_hash=compiled_hash,
            runtime_hash=runtime_hash,
            name=str(metadata.get("name") or references[0].name)[:120],
            render_mode=(
                "interactive" if compiled_hash and runtime_hash else "static"
            ),
            compiler_version=str(metadata.get("compiler_version") or "")[:128] or None,
            runtime_sdk_version=(
                str(metadata.get("runtime_sdk_version") or "")[:128] or None
            ),
            origin={
                "device_id": machine.collector_token_hash,
                "device_name": machine.name,
                "recorded_path_hash": path_hash,
            },
        )
        db.add(artifact)
        await db.flush()
    elif (
        artifact.render_mode != "interactive"
        and compiled_hash is not None
        and runtime_hash is not None
    ):
        artifact.compiled_hash = compiled_hash
        artifact.runtime_hash = runtime_hash
        artifact.render_mode = "interactive"
        artifact.compiler_version = str(metadata.get("compiler_version") or "")[:128] or None
        artifact.runtime_sdk_version = (
            str(metadata.get("runtime_sdk_version") or "")[:128] or None
        )
        already_current = False

    status = (
        "already_current"
        if already_current
        else ("renderable" if artifact.render_mode == "interactive" else "static_only")
    )
    now = datetime.now(timezone.utc)
    for reference in references:
        reference.artifact_id = artifact.id
        reference.status = status
        reference.reason = None
        reference.attempt_count += 1
        reference.last_attempt_at = now
    await db.flush()
    return artifact, status, len(references)


async def record_canvas_outcome(
    db: AsyncSession,
    *,
    machine_id: uuid.UUID,
    reference_ids: list[uuid.UUID],
    path_hash: str,
    status: str,
    reason: str,
) -> int:
    if status not in {"missing", "rejected", "unsupported"}:
        raise HTTPException(status_code=400, detail="invalid Canvas outcome")
    if reason and not _SAFE_REASON.fullmatch(reason):
        reason = "collector_rejected"
    references = await _owned_references(db, machine_id, reference_ids, path_hash)
    now = datetime.now(timezone.utc)
    for reference in references:
        reference.status = status
        reference.reason = reason or status
        reference.attempt_count += 1
        reference.last_attempt_at = now
    await db.flush()
    return len(references)


async def artifact_for_user(
    db: AsyncSession,
    artifact_id: uuid.UUID,
    user: User,
) -> CanvasArtifact:
    query = select(CanvasArtifact).where(CanvasArtifact.id == artifact_id)
    if user.role not in ("admin", "owner"):
        query = query.where(CanvasArtifact.user_id == user.id)
    artifact = (await db.execute(query)).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Canvas artifact not found")
    return artifact


async def blob_content(
    db: AsyncSession,
    content_hash: str | None,
) -> bytes:
    if not content_hash:
        raise HTTPException(status_code=404, detail="Canvas content unavailable")
    blob = await db.get(CanvasArtifactBlob, content_hash)
    if blob is None:
        raise HTTPException(status_code=404, detail="Canvas content unavailable")
    return bytes(blob.content)


def render_shell(runtime: bytes, compiled: bytes) -> str:
    """Build an isolated, no-network document from validated module bytes."""
    runtime_b64 = base64.b64encode(runtime).decode("ascii")
    compiled_b64 = base64.b64encode(compiled).decode("ascii")
    nonce = uuid.uuid4().hex
    csp = "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "connect-src 'none'",
            "form-action 'none'",
            "frame-src 'none'",
            "object-src 'none'",
            "worker-src 'none'",
            "img-src data: blob:",
            "media-src data: blob:",
            "font-src data:",
            "style-src 'unsafe-inline'",
            f"script-src 'nonce-{nonce}' blob:",
        )
    )
    bootstrap = f"""
const bytes = (value) => Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
const moduleUrl = (value) => URL.createObjectURL(
  new Blob([bytes(value)], {{ type: "text/javascript" }})
);
try {{
  const runtimeUrl = moduleUrl("{runtime_b64}");
  const artifactUrl = moduleUrl("{compiled_b64}");
  const runtime = await import(runtimeUrl);
  await runtime.mountCanvas(artifactUrl);
}} catch (error) {{
  document.getElementById("root").textContent = "Canvas preview failed.";
  console.error("[MementoCanvas] isolated render failed", error);
}}
"""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{csp}\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>html,body,#root{margin:0;min-height:100%;background:transparent}</style>"
        f"</head><body><div id=\"root\"></div><script type=\"module\" nonce=\"{nonce}\">"
        f"{bootstrap}</script></body></html>"
    )


async def inventory_summary(db: AsyncSession, user: User) -> dict[str, Any]:
    machine_query = select(Machine)
    if user.role not in ("admin", "owner"):
        machine_query = machine_query.where(Machine.user_id == user.id)
    machines = (await db.execute(machine_query)).scalars().all()
    for machine in machines:
        await inventory_machine_canvases(db, machine.id)

    machine_ids = [machine.id for machine in machines]
    if not machine_ids:
        return {"total": 0, "outcomes": {}, "groups": []}
    rows = (
        await db.execute(
            select(
                Machine.name,
                Document.tool_id,
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.recorded_path,
                CanvasArtifactReference.status,
                func.count(),
            )
            .join(Machine, Machine.id == CanvasArtifactReference.machine_id)
            .join(Document, Document.id == CanvasArtifactReference.document_id)
            .where(CanvasArtifactReference.machine_id.in_(machine_ids))
            .group_by(
                Machine.name,
                Document.tool_id,
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.recorded_path,
                CanvasArtifactReference.status,
            )
            .order_by(Machine.name, Document.tool_id, CanvasArtifactReference.path_hash)
        )
    ).all()
    outcomes: Counter[str] = Counter()
    groups: list[dict[str, Any]] = []
    for machine_name, tool_id, path_hash, path, status, count in rows:
        outcomes[status] += count
        groups.append(
            {
                "device": machine_name,
                "tool": tool_id,
                "path": path,
                "path_hash": path_hash,
                "outcome": status,
                "references": count,
            }
        )
    return {
        "total": sum(outcomes.values()),
        "outcomes": dict(sorted(outcomes.items())),
        "groups": groups,
    }
