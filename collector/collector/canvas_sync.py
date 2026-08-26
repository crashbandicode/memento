"""Bounded, idempotent Canvas artifact exchange with the Memento server."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .canvas_artifacts import (
    CanvasCaptureError,
    capture_canvas,
    locate_canvas_toolchain,
    probe_canvas_source,
)
from .config import CollectorConfig
from .tls import SSL_CONTEXT

MAX_BATCH = 16
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _headers(config: CollectorConfig) -> dict[str, str]:
    return {
        "X-Collector-Token": config.server.token,
        "X-Device-Id": config.device_id,
        "X-Device-Name": config.device_name,
        "X-Device-Platform": config.platform,
    }


def _valid_request(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and len(value["path"]) <= 2048
        and isinstance(value.get("path_hash"), str)
        and bool(_HASH.fullmatch(value["path_hash"]))
        and isinstance(value.get("reference_ids"), list)
        and 0 < len(value["reference_ids"]) <= 128
        and all(isinstance(item, str) and len(item) <= 64 for item in value["reference_ids"])
        and (
            value.get("current_source_hash") is None
            or (
                isinstance(value.get("current_source_hash"), str)
                and bool(_HASH.fullmatch(value["current_source_hash"]))
            )
        )
        and value.get("current_render_mode") in {None, "interactive", "static"}
    )


def _submit_outcome(
    client: httpx.Client,
    request: dict[str, Any],
    *,
    status: str,
    reason: str,
) -> None:
    response = client.post(
        "/api/canvas-artifacts/outcome",
        json={
            "reference_ids": request["reference_ids"],
            "path_hash": request["path_hash"],
            "status": status,
            "reason": reason[:128],
        },
    )
    response.raise_for_status()


def sync_pending_canvases(
    config: CollectorConfig,
    logger: logging.Logger,
) -> dict[str, int]:
    """Resolve one server-owned batch; never crawls or mutates sync offsets."""
    counts = {
        "requested": 0,
        "renderable": 0,
        "static_only": 0,
        "missing": 0,
        "rejected": 0,
        "unchanged": 0,
        "updated": 0,
        "failed": 0,
    }
    with httpx.Client(
        base_url=config.server.url,
        http2=True,
        headers=_headers(config),
        timeout=httpx.Timeout(30.0, connect=10.0),
        verify=SSL_CONTEXT,
    ) as client:
        response = client.get("/api/canvas-artifacts/pending")
        if response.status_code == 404:
            # Server-first rolling deploy compatibility.
            return counts
        response.raise_for_status()
        payload = response.json()
        requests = payload.get("artifacts", []) if isinstance(payload, dict) else []
        if not isinstance(requests, list):
            return counts

        toolchain = None
        toolchain_loaded = False
        for request in requests[:MAX_BATCH]:
            if not _valid_request(request):
                counts["failed"] += 1
                continue
            counts["requested"] += 1
            try:
                probe = probe_canvas_source(request["path"])
                if (
                    request.get("current_source_hash") == probe.source_hash
                    and request.get("current_render_mode") != "static"
                ):
                    _submit_outcome(
                        client,
                        request,
                        status="unchanged",
                        reason="source_hash_match",
                    )
                    counts["unchanged"] += 1
                    continue
                if not toolchain_loaded:
                    toolchain = locate_canvas_toolchain()
                    toolchain_loaded = True
                captured = capture_canvas(
                    request["path"],
                    toolchain=toolchain,
                )
            except CanvasCaptureError as exc:
                status = "missing" if exc.reason == "missing" else "rejected"
                try:
                    _submit_outcome(
                        client,
                        request,
                        status=status,
                        reason=exc.reason,
                    )
                    counts[status] += 1
                except httpx.HTTPError:
                    counts["failed"] += 1
                    logger.exception("Canvas outcome upload failed")
                continue

            metadata = {
                "reference_ids": request["reference_ids"],
                "path_hash": request["path_hash"],
                "name": captured.name,
                "source_hash": captured.source_hash,
                "compiled_hash": captured.compiled_hash,
                "runtime_hash": captured.runtime_hash,
                "render_mode": captured.render_mode,
                "compiler_version": captured.compiler_version,
                "runtime_sdk_version": captured.runtime_sdk_version,
                "static_reason": captured.static_reason,
            }
            files: dict[str, tuple[str, bytes, str]] = {
                "source": (
                    f"{captured.name}.canvas.tsx",
                    captured.source,
                    "text/typescript",
                ),
            }
            if (
                captured.compiled_javascript is not None
                and captured.runtime_javascript is not None
            ):
                files["compiled"] = (
                    f"{captured.name}.mjs",
                    captured.compiled_javascript,
                    "text/javascript",
                )
                files["runtime"] = (
                    "canvas-runtime.mjs",
                    captured.runtime_javascript,
                    "text/javascript",
                )
            try:
                upload = client.post(
                    "/api/canvas-artifacts/upload",
                    data={"metadata": json.dumps(metadata, separators=(",", ":"))},
                    files=files,
                )
                upload.raise_for_status()
                outcome = upload.json().get("status")
                if outcome in {"renderable", "already_current"}:
                    counts["renderable"] += 1
                else:
                    counts["static_only"] += 1
                if request.get("current_source_hash"):
                    counts["updated"] += 1
            except httpx.HTTPError:
                counts["failed"] += 1
                logger.exception(
                    "Canvas artifact upload failed for path hash %s",
                    request["path_hash"],
                )
    return counts
