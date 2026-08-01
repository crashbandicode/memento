"""Detect Cursor Canvas artifacts referenced in conversation messages.

A Cursor Canvas is a ``*.canvas.tsx`` file that the Cursor IDE compiles (with
its proprietary ``cursor/canvas`` SDK and no network access) and shows beside
the chat. Memento never possesses that compiled canvas: the collector
deliberately excludes the ``canvases/`` directory and ``skills-cursor/``
templates, and there is no Cursor compiler/SDK on the server. What conversation
transcripts *do* carry is a reference to the artifact — a Markdown link or
inline path ending in ``.canvas.tsx`` — and, sometimes, the TSX source or a
self-contained HTML export embedded in the same message.

This module is the authoritative, security-bounded detector. It mirrors the
front-end rules in ``web/src/lib/canvas-artifact.mjs`` and returns descriptors
matching the ``CanvasArtifact`` shape consumed by the viewer. It NEVER reads
files from disk and NEVER executes transcript content: it only classifies text
that was already ingested and sanitized. The descriptors it emits are the only
canvas data the web client trusts, so all validation (path-traversal rejection,
scheme allowlist, size/type/count caps, name sanitization) lives here.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

# --- Security limits (kept in sync with web/src/lib/canvas-artifact.mjs) ---

MAX_CANVAS_TARGET_LENGTH = 512
MAX_CANVAS_SOURCE_LENGTH = 200_000
MAX_CANVAS_HTML_LENGTH = 500_000
MAX_CANVASES_PER_MESSAGE = 12
CANVAS_SOURCE_LANGUAGE = "tsx"

# How far after a canvas reference we will look for an associated fenced block.
_SOURCE_ASSOCIATION_WINDOW = 2_000

_CANVAS_EXTENSION = re.compile(r"\.canvas\.tsx(?:[?#].*)?$", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BARE_CANVAS = re.compile(
    rf"(?<![\w./\\@~:-])[\w./\\@~:-]{{1,{MAX_CANVAS_TARGET_LENGTH}}}"
    r"\.canvas\.tsx(?![\w])",
    re.IGNORECASE,
)
_HTTP = re.compile(r"^https?://", re.IGNORECASE)
_CONTROL_CHARS = re.compile(r"[\u0000-\u001f\u007f]")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_FENCED_BLOCK = re.compile(
    r"```([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```",
    re.DOTALL,
)
_SOURCE_LANGS = {"tsx", "typescript", "ts", "jsx"}
_HTML_LANGS = {"html", "htm"}


def _strip_target(value: str | None) -> str:
    value = unquote(str(value or "")).strip()
    value = re.sub(r"^file://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[),.;:]+$", "", value)
    return value


def _path_part(value: str) -> str:
    value = _strip_target(value)
    value = value.split("#", 1)[0]
    value = value.split("?", 1)[0]
    return value


def looks_like_canvas_artifact(value: str | None) -> bool:
    """True when ``value`` points at a ``*.canvas.tsx`` Cursor Canvas artifact."""
    candidate = _path_part(value or "")
    if not candidate or len(candidate) > MAX_CANVAS_TARGET_LENGTH:
        return False
    if "\n" in candidate or "\r" in candidate:
        return False
    return bool(_CANVAS_EXTENSION.search(candidate))


def sanitize_canvas_name(value: str | None) -> str:
    cleaned = _CONTROL_CHARS.sub("", str(value or "")).strip()[:120]
    return cleaned or "canvas"


def canvas_display_name(value: str | None) -> str:
    candidate = _path_part(value or "").rstrip("/\\")
    base = re.split(r"[\\/]", candidate)[-1] if candidate else candidate
    base = re.sub(r"\.canvas\.tsx$", "", base, flags=re.IGNORECASE)
    return sanitize_canvas_name(base or "canvas")


def is_safe_canvas_path(value: str | None) -> bool:
    """Reject path traversal / control chars / over-long paths.

    We never open these paths, but a hostile ``..`` path must never be echoed
    back as a trusted descriptor the client might act on.
    """
    candidate = _path_part(value or "")
    if not candidate or len(candidate) > MAX_CANVAS_TARGET_LENGTH:
        return False
    if _CONTROL_CHARS.search(candidate):
        return False
    segments = re.split(r"[\\/]", candidate)
    if any(segment == ".." for segment in segments):
        return False
    scheme = _SCHEME.match(candidate)
    if scheme and len(scheme.group(1)) > 1:
        # A single letter followed by ":" is a Windows drive, not a URL scheme.
        if scheme.group(1).lower() not in {"http", "https", "file"}:
            return False
    return True


def is_safe_canvas_embed_url(value: str | None) -> bool:
    """Only ``http(s)`` URLs may ever be loaded into the sandboxed iframe."""
    raw = str(value or "").strip()
    if not raw or len(raw) > MAX_CANVAS_TARGET_LENGTH or re.search(r"\s", raw):
        return False
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        # Accessing ``port`` validates malformed values such as ``:not-a-port``.
        _ = parsed.port
    except ValueError:
        return False
    return True


def _iter_references(content: str):
    """Yield ``(position, label, href)`` for each canvas reference, deduped."""
    seen: set[str] = set()
    markdown_ranges: list[tuple[int, int]] = []

    for match in _MARKDOWN_LINK.finditer(content):
        label, href = match.group(1), match.group(2)
        if looks_like_canvas_artifact(href) or looks_like_canvas_artifact(label):
            markdown_ranges.append(match.span())
            target = href if looks_like_canvas_artifact(href) else label
            key = _path_part(target).lower()
            if key and key not in seen:
                seen.add(key)
                yield match.start(), label, href

    for match in _BARE_CANVAS.finditer(content):
        if any(start <= match.start() < end for start, end in markdown_ranges):
            continue
        href = match.group(0)
        key = _path_part(href).lower()
        if key and key not in seen:
            seen.add(key)
            yield match.start(), "", href


def _fenced_blocks(content: str):
    """Yield ``(position, language, code)`` for fenced code blocks."""
    for match in _FENCED_BLOCK.finditer(content):
        yield match.start(), (match.group(1) or "").lower(), match.group(2)


def _associate_content(
    position: int,
    blocks: list[tuple[int, str, str]],
    single_reference: bool,
) -> tuple[str, str] | None:
    """Find source/html for a reference; returns ``(kind, payload)`` or None."""

    def _pick(langs: set[str]) -> tuple[int, str] | None:
        following = [
            (start, code)
            for start, lang, code in blocks
            if lang in langs and start >= position and start - position <= _SOURCE_ASSOCIATION_WINDOW
        ]
        if following:
            following.sort(key=lambda item: item[0])
            return following[0]
        if single_reference:
            candidates = [(start, code) for start, lang, code in blocks if lang in langs]
            if candidates:
                candidates.sort(key=lambda item: item[0])
                return candidates[0]
        return None

    source = _pick(_SOURCE_LANGS)
    if source is not None:
        code = source[1].rstrip("\n")
        if 0 < len(code) <= MAX_CANVAS_SOURCE_LENGTH:
            return "source", code

    html = _pick(_HTML_LANGS)
    if html is not None:
        markup = html[1].strip()
        if 0 < len(markup) <= MAX_CANVAS_HTML_LENGTH:
            return "html", markup

    return None


def detect_message_canvases(content: str | None) -> list[dict[str, Any]]:
    """Return validated canvas descriptors referenced in ``content``.

    Descriptors match the front-end ``CanvasArtifact`` shape. The list is empty
    for the overwhelming majority of messages (a cheap early-out on the
    ``.canvas.tsx`` marker keeps this off the hot path).
    """
    if not isinstance(content, str) or not content or ".canvas.tsx" not in content.lower():
        return []

    references = list(_iter_references(content))
    if not references:
        return []

    blocks = list(_fenced_blocks(content))
    single_reference = len(references) == 1
    descriptors: list[dict[str, Any]] = []

    for position, label, href in references:
        if len(descriptors) >= MAX_CANVASES_PER_MESSAGE:
            break

        target = href if looks_like_canvas_artifact(href) else label
        path = _path_part(target)
        if not is_safe_canvas_path(path):
            continue

        name = canvas_display_name(
            label if looks_like_canvas_artifact(label) else target
        )

        descriptor: dict[str, Any] = {
            "name": name,
            "path": path,
            "href": _strip_target(href) or _strip_target(label),
            "source_kind": "unsupported",
        }

        host = None
        if _HTTP.match(descriptor["href"]):
            match = re.match(r"^https?://([^/]+)", descriptor["href"], re.IGNORECASE)
            if match:
                host = match.group(1)
                descriptor["host"] = host

        # Only source/HTML that the transcript already carried is ever attached.
        # A raw `.canvas.tsx` path is never a renderable URL, so we never emit an
        # `embed`-by-URL descriptor from server-side detection.
        associated = _associate_content(position, blocks, single_reference)
        if associated is not None:
            kind, payload = associated
            if kind == "source":
                descriptor["source"] = payload
                descriptor["source_language"] = CANVAS_SOURCE_LANGUAGE
                descriptor["source_kind"] = "source"
            elif kind == "html":
                descriptor["html"] = payload
                descriptor["source_kind"] = "embed"

        descriptors.append(descriptor)

    return descriptors


def conversation_has_canvas(content: str | None) -> bool:
    """Cheap predicate for metadata/discovery use."""
    return bool(detect_message_canvases(content))
