"""Bounded capture and deterministic compilation of referenced Cursor Canvases.

The collector is the only component allowed to resolve a transcript's local
``*.canvas.tsx`` path.  Resolution is deliberately narrower than ordinary file
watching: a candidate must be an existing regular file directly below
``~/.cursor/projects/<workspace>/canvases/`` and may not be a symlink.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


MAX_REFERENCES_PER_PAYLOAD = 64
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 200_000
MAX_COMPILED_BYTES = 500_000
MAX_RUNTIME_BYTES = 2 * 1024 * 1024
COMPILER_VERSION = "memento-typescript-v1"

_CANVAS_PATH = re.compile(
    r"(?P<path>(?:file://)?(?:[A-Za-z]:[\\/]|/)[^\s<>'\"\]\)]+?\.canvas\.tsx)",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IMPORT_FROM = re.compile(
    r"""(?:import|export)\s+(?:type\s+)?(?:[\s\S]*?\s+from\s+)?["']([^"']+)["']""",
    re.MULTILINE,
)
_DYNAMIC_IMPORT = re.compile(r"\bimport\s*\(")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|
        password|private[_-]?key)\b\s*[:=]\s*["'][^"'\r\n]{12,}["']
        """
    ),
)
_FORBIDDEN_SOURCE = re.compile(
    r"""(?x)
    \b(?:eval|Function|fetch|XMLHttpRequest|WebSocket|EventSource|Worker|
    SharedWorker|importScripts|require)\s*\(
    |<\s*(?:script|iframe|object|embed|form)\b
    |dangerouslySetInnerHTML
    """
)
_FORBIDDEN_COMPILED = re.compile(
    r"""(?x)
    \b(?:eval|Function|fetch|XMLHttpRequest|WebSocket|EventSource|Worker|
    SharedWorker|importScripts|require|setInterval|setTimeout|
    requestAnimationFrame|queueMicrotask)\s*\(
    """
)


class CanvasCaptureError(ValueError):
    """A stable, non-sensitive rejection reason for one referenced artifact."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CapturedCanvas:
    recorded_path: str
    canonical_path: str
    name: str
    source: bytes
    source_hash: str
    compiled_javascript: bytes | None
    compiled_hash: str | None
    runtime_javascript: bytes | None
    runtime_hash: str | None
    runtime_sdk_version: str | None
    render_mode: str
    compiler_version: str | None
    static_reason: str | None


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _walk_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_strings(nested)


def extract_canvas_references(payload: str | bytes) -> list[str]:
    """Extract distinct absolute Canvas paths without crawling the filesystem."""
    if isinstance(payload, bytes):
        if len(payload) > MAX_SCAN_BYTES:
            payload = payload[:MAX_SCAN_BYTES]
        text = payload.decode("utf-8", errors="replace")
    else:
        text = payload[:MAX_SCAN_BYTES]
    if ".canvas.tsx" not in text.casefold():
        return []

    strings: list[str] = []
    for line in text.splitlines():
        if ".canvas.tsx" not in line.casefold():
            continue
        try:
            parsed = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            strings.append(line)
        else:
            strings.extend(_walk_strings(parsed))

    found: list[str] = []
    seen: set[str] = set()
    for value in strings or [text]:
        for match in _CANVAS_PATH.finditer(value):
            candidate = unquote(match.group("path")).rstrip(".,;:")
            key = candidate.replace("\\", "/").casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
            if len(found) >= MAX_REFERENCES_PER_PAYLOAD:
                return found
    return found


def _windows_path_on_windows(raw: str) -> Path:
    parsed = urlparse(raw)
    candidate = parsed.path if parsed.scheme.casefold() == "file" else raw
    candidate = unquote(candidate)
    if re.match(r"^/[A-Za-z]:/", candidate):
        candidate = candidate[1:]
    return Path(candidate.replace("/", os.sep))


def canonicalize_canvas_path(
    recorded_path: str,
    *,
    home: Path | None = None,
) -> Path:
    """Resolve an exact reference under the canonical Cursor Canvas root."""
    if not isinstance(recorded_path, str) or not recorded_path:
        raise CanvasCaptureError("invalid_path")
    if len(recorded_path) > 2048 or _CONTROL.search(recorded_path):
        raise CanvasCaptureError("invalid_path")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", recorded_path) and not re.match(
        r"^(?:file:|[A-Za-z]:[\\/])", recorded_path, re.IGNORECASE
    ):
        raise CanvasCaptureError("unsupported_scheme")

    home = (home or Path.home()).resolve()
    root = (home / ".cursor" / "projects").resolve()
    if os.name == "nt":
        candidate = _windows_path_on_windows(recorded_path)
    else:
        parsed = urlparse(recorded_path)
        raw = unquote(parsed.path if parsed.scheme.casefold() == "file" else recorded_path)
        candidate = Path(raw)
    if not candidate.is_absolute():
        raise CanvasCaptureError("path_not_absolute")

    try:
        unresolved = candidate.absolute()
        # Reject every symlink component. This is stricter than merely checking
        # the final target and removes TOCTOU ambiguity during capture.
        cursor = Path(unresolved.anchor)
        for part in unresolved.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise CanvasCaptureError("symlink_rejected")
        resolved = candidate.resolve(strict=True)
    except CanvasCaptureError:
        raise
    except FileNotFoundError as exc:
        raise CanvasCaptureError("missing") from exc
    except OSError as exc:
        raise CanvasCaptureError("unreadable") from exc

    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise CanvasCaptureError("outside_allowlisted_root") from exc
    parts = relative.parts
    if (
        len(parts) != 3
        or parts[1].casefold() != "canvases"
        or not parts[2].casefold().endswith(".canvas.tsx")
    ):
        raise CanvasCaptureError("outside_allowlisted_root")
    if not resolved.is_file():
        raise CanvasCaptureError("not_regular_file")
    return resolved


def validate_canvas_source(source: bytes) -> str:
    if not source or len(source) > MAX_SOURCE_BYTES:
        raise CanvasCaptureError("source_size")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanvasCaptureError("source_encoding") from exc
    if "\x00" in text:
        raise CanvasCaptureError("source_encoding")
    imports = _IMPORT_FROM.findall(text)
    if not imports or any(module != "cursor/canvas" for module in imports):
        raise CanvasCaptureError("unsupported_import")
    if _DYNAMIC_IMPORT.search(text):
        raise CanvasCaptureError("dynamic_import")
    if _FORBIDDEN_SOURCE.search(text):
        raise CanvasCaptureError("unsafe_source_api")
    if not re.search(r"\bexport\s+default\b", text):
        raise CanvasCaptureError("missing_default_export")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise CanvasCaptureError("possible_secret")
    return text


def read_canvas_source(path: Path) -> bytes:
    """Read a bounded regular file while detecting symlink/swap races."""
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise CanvasCaptureError("missing") from exc
    except OSError as exc:
        raise CanvasCaptureError("unreadable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise CanvasCaptureError("symlink_rejected")
    if not stat.S_ISREG(before.st_mode):
        raise CanvasCaptureError("not_regular_file")
    if before.st_size <= 0 or before.st_size > MAX_SOURCE_BYTES:
        raise CanvasCaptureError("source_size")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CanvasCaptureError("unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise CanvasCaptureError("source_changed")
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        source = b"".join(chunks)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        after_path = path.lstat()
        still_resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CanvasCaptureError("source_changed") from exc
    if (
        stat.S_ISLNK(after_path.st_mode)
        or after_open.st_dev != opened.st_dev
        or after_open.st_ino != opened.st_ino
        or after_open.st_size != opened.st_size
        or after_path.st_dev != opened.st_dev
        or after_path.st_ino != opened.st_ino
        or still_resolved != path
    ):
        raise CanvasCaptureError("source_changed")
    if len(source) != opened.st_size or len(source) > MAX_SOURCE_BYTES:
        raise CanvasCaptureError("source_size")
    return source


def _cursor_install_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roots.extend(
            (
                local / "Programs" / "cursor" / "resources" / "app",
                local / "Programs" / "Cursor" / "resources" / "app",
            )
        )
    elif sys.platform == "darwin":
        roots.append(Path("/Applications/Cursor.app/Contents/Resources/app"))
    else:
        roots.extend(
            (
                Path("/usr/share/cursor/resources/app"),
                Path("/opt/Cursor/resources/app"),
                Path("/opt/cursor/resources/app"),
            )
        )
    return [root for root in roots if root.is_dir()]


def locate_canvas_toolchain() -> tuple[Path, Path, Path | None, str | None] | None:
    """Return ``(node, typescript.js, runtime.js, sdk_version)`` when available."""
    node = shutil.which("node")
    if not node:
        return None
    for root in _cursor_install_roots():
        typescript = root / "extensions" / "node_modules" / "typescript" / "lib" / "typescript.js"
        extension_roots = (
            root / "extensions" / "cursor-local-agent-runtime",
            root / "extensions" / "cursor-agent-exec",
        )
        for extension in extension_roots:
            runtime = extension / "dist" / "canvas-runtime" / "canvas-runtime.esm.js"
            version_file = extension / "dist" / "agent-sdk" / "canvas-sdk-version"
            if typescript.is_file() and runtime.is_file():
                sdk_version = None
                try:
                    sdk_version = version_file.read_text(encoding="utf-8").strip()[:128]
                except OSError:
                    pass
                return Path(node), typescript, runtime, sdk_version
    return None


def _subprocess_limits():
    if os.name == "nt":
        return None

    def apply() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_COMPILED_BYTES,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

    return apply


def compile_canvas_source(
    source_path: Path,
    *,
    node: Path,
    typescript: Path,
) -> bytes:
    """Transpile TSX without evaluating it or resolving any source imports."""
    compiler = Path(__file__).with_name("canvas_compile.cjs")
    environment = {
        "PATH": str(node.parent),
        "HOME": str(Path.home()),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
    }
    if os.name == "nt":
        # Node's Windows crypto initialization needs the OS root available even
        # though the compiler subprocess otherwise receives a minimal environment.
        environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        environment["WINDIR"] = os.environ.get("WINDIR", environment["SYSTEMROOT"])
    try:
        result = subprocess.run(
            [str(node), str(compiler), str(typescript), str(source_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=8,
            env=environment,
            cwd=str(compiler.parent),
            preexec_fn=_subprocess_limits(),
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
    except subprocess.TimeoutExpired as exc:
        raise CanvasCaptureError("compile_timeout") from exc
    if result.returncode != 0:
        reason = result.stderr.decode("utf-8", errors="replace").strip()
        allowed = {
            "compile_diagnostic",
            "compile_output",
            "dynamic_import",
            "forbidden_syntax",
            "missing_default_export",
            "unsupported_import",
        }
        raise CanvasCaptureError(reason if reason in allowed else "compile_failed")
    output = result.stdout
    if not output or len(output) > MAX_COMPILED_BYTES:
        raise CanvasCaptureError("compile_output")
    text = output.decode("utf-8", errors="strict")
    if re.search(r"^\s*(?:import|export\s+\{)", text, re.MULTILINE):
        raise CanvasCaptureError("compile_output")
    if _FORBIDDEN_COMPILED.search(text):
        raise CanvasCaptureError("unsafe_compiled_api")
    return output


def capture_canvas(
    recorded_path: str,
    *,
    home: Path | None = None,
    toolchain: tuple[Path, Path, Path | None, str | None] | None = None,
) -> CapturedCanvas:
    path = canonicalize_canvas_path(recorded_path, home=home)
    source = read_canvas_source(path)
    validate_canvas_source(source)
    source_hash = hashlib.sha256(source).hexdigest()

    toolchain = toolchain if toolchain is not None else locate_canvas_toolchain()
    if toolchain is None:
        return CapturedCanvas(
            recorded_path=recorded_path,
            canonical_path=str(path),
            name=path.name.removesuffix(".canvas.tsx"),
            source=source,
            source_hash=source_hash,
            compiled_javascript=None,
            compiled_hash=None,
            runtime_javascript=None,
            runtime_hash=None,
            runtime_sdk_version=None,
            render_mode="source_only",
            compiler_version=None,
            static_reason="toolchain_unavailable",
        )

    node, typescript, runtime_path, sdk_version = toolchain
    if runtime_path is None:
        return CapturedCanvas(
            recorded_path=recorded_path,
            canonical_path=str(path),
            name=path.name.removesuffix(".canvas.tsx"),
            source=source,
            source_hash=source_hash,
            compiled_javascript=None,
            compiled_hash=None,
            runtime_javascript=None,
            runtime_hash=None,
            runtime_sdk_version=sdk_version,
            render_mode="source_only",
            compiler_version=None,
            static_reason="runtime_missing",
        )
    try:
        runtime = runtime_path.read_bytes()
        if not runtime or len(runtime) > MAX_RUNTIME_BYTES:
            raise CanvasCaptureError("runtime_size")
        runtime_text = runtime.decode("utf-8", errors="strict")
        if "mountCanvas" not in runtime_text or "cursor/canvas" in runtime_text[:256]:
            raise CanvasCaptureError("runtime_invalid")
        compiled = compile_canvas_source(path, node=node, typescript=typescript)
    except (OSError, UnicodeDecodeError):
        static_reason = "runtime_unreadable"
    except CanvasCaptureError as exc:
        static_reason = exc.reason
    else:
        return CapturedCanvas(
            recorded_path=recorded_path,
            canonical_path=str(path),
            name=path.name.removesuffix(".canvas.tsx"),
            source=source,
            source_hash=source_hash,
            compiled_javascript=compiled,
            compiled_hash=hashlib.sha256(compiled).hexdigest(),
            runtime_javascript=runtime,
            runtime_hash=hashlib.sha256(runtime).hexdigest(),
            runtime_sdk_version=sdk_version,
            render_mode="interactive",
            compiler_version=COMPILER_VERSION,
            static_reason=None,
        )
    return CapturedCanvas(
        recorded_path=recorded_path,
        canonical_path=str(path),
        name=path.name.removesuffix(".canvas.tsx"),
        source=source,
        source_hash=source_hash,
        compiled_javascript=None,
        compiled_hash=None,
        runtime_javascript=None,
        runtime_hash=None,
        runtime_sdk_version=sdk_version,
        render_mode="source_only",
        compiler_version=None,
        static_reason=static_reason,
    )


def capture_outcome(recorded_path: str, **kwargs) -> tuple[CapturedCanvas | None, str]:
    """Capture one artifact and return a report-safe explicit outcome."""
    try:
        artifact = capture_canvas(recorded_path, **kwargs)
    except CanvasCaptureError as exc:
        return None, exc.reason
    return artifact, artifact.render_mode
