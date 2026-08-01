from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from collector.canvas_artifacts import (
    MAX_SOURCE_BYTES,
    CanvasCaptureError,
    capture_canvas,
    canonicalize_canvas_path,
    extract_canvas_references,
    validate_canvas_source,
)


SAFE_SOURCE = b"""
import { Card, Stack, Text } from "cursor/canvas";
export default function Report() {
  return <Card><Stack><Text>Hello</Text></Stack></Card>;
}
"""


def _canvas(tmp_path: Path, source: bytes = SAFE_SOURCE) -> tuple[Path, Path]:
    home = tmp_path / "home"
    path = home / ".cursor" / "projects" / "workspace" / "canvases" / "report.canvas.tsx"
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    return home, path


def test_extract_references_is_deduped_and_bounded() -> None:
    path = "C:/Users/test/.cursor/projects/work/canvases/report.canvas.tsx"
    payload = f'{{"content":"[{path}]({path})"}}\n{{"content":"{path}"}}'
    assert extract_canvas_references(payload) == [path]


def test_capture_source_only_under_exact_allowlisted_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, path = _canvas(tmp_path)
    monkeypatch.setattr(
        "collector.canvas_artifacts.locate_canvas_toolchain",
        lambda: None,
    )

    captured = capture_canvas(str(path), home=home)

    assert captured.render_mode == "source_only"
    assert captured.static_reason == "toolchain_unavailable"
    assert captured.source_hash == hashlib.sha256(SAFE_SOURCE).hexdigest()
    assert captured.canonical_path == str(path.resolve())


def test_rejects_outside_root_and_nested_canvas_path(tmp_path: Path) -> None:
    home, path = _canvas(tmp_path)
    outside = tmp_path / "report.canvas.tsx"
    outside.write_bytes(SAFE_SOURCE)
    nested = path.parent / "nested" / "report.canvas.tsx"
    nested.parent.mkdir()
    nested.write_bytes(SAFE_SOURCE)

    with pytest.raises(CanvasCaptureError, match="outside_allowlisted_root"):
        canonicalize_canvas_path(str(outside), home=home)
    with pytest.raises(CanvasCaptureError, match="outside_allowlisted_root"):
        canonicalize_canvas_path(str(nested), home=home)


def test_rejects_symlinked_canvas(tmp_path: Path) -> None:
    home, path = _canvas(tmp_path)
    linked = path.with_name("linked.canvas.tsx")
    try:
        linked.symlink_to(path)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(CanvasCaptureError, match="symlink_rejected"):
        canonicalize_canvas_path(str(linked), home=home)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            b'import value from "react"; export default function X() { return null; }',
            "unsupported_import",
        ),
        (
            b'import { Card } from "cursor/canvas"; const x = import("bad"); '
            b"export default function X() { return <Card/>; }",
            "dynamic_import",
        ),
        (
            b'import { Card } from "cursor/canvas"; const token = '
            b'"AKIAABCDEFGHIJKLMNOP"; export default function X() { return <Card/>; }',
            "possible_secret",
        ),
        (
            b'import { Card } from "cursor/canvas"; fetch("/leak"); '
            b"export default function X() { return <Card/>; }",
            "unsafe_source_api",
        ),
    ],
)
def test_source_security_rejections(source: bytes, reason: str) -> None:
    with pytest.raises(CanvasCaptureError, match=reason):
        validate_canvas_source(source)


def test_valid_source_is_accepted() -> None:
    assert "export default" in validate_canvas_source(SAFE_SOURCE)


def test_security_words_in_text_are_not_global_api_usage() -> None:
    prose = (
        b'import { Card, Text } from "cursor/canvas"; '
        b'const note = "document parent top"; '
        b"export default function X() { return <Card><Text>{note}</Text></Card>; }"
    )
    assert "document parent top" in validate_canvas_source(prose)


def test_oversized_source_is_rejected_before_unbounded_read(tmp_path: Path) -> None:
    home, path = _canvas(tmp_path, b"x" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(CanvasCaptureError, match="source_size"):
        capture_canvas(str(path), home=home, toolchain=None)
