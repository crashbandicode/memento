from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from server.services.canvas_artifact_store import (
    normalized_path_hash,
    render_shell,
    validate_compiled,
    validate_runtime,
    validate_source,
)


SOURCE = b"""
import { Card, Text } from "cursor/canvas";
export default function Report() { return <Card><Text>Safe</Text></Card>; }
"""
COMPILED = b"const Report=()=>React.createElement(Card,null);export default Report;"
RUNTIME = b"function mountCanvas(value){return value}export{mountCanvas};"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_validates_hash_addressed_canvas_parts() -> None:
    assert "export default" in validate_source(SOURCE, _sha(SOURCE))
    validate_compiled(COMPILED, _sha(COMPILED))
    validate_runtime(RUNTIME, _sha(RUNTIME))


def test_rejects_source_network_api_and_hash_mismatch() -> None:
    unsafe = SOURCE + b'\nfetch("https://example.test");'
    with pytest.raises(HTTPException, match="security policy"):
        validate_source(unsafe, _sha(unsafe))
    with pytest.raises(HTTPException, match="invalid source payload"):
        validate_source(SOURCE, "0" * 64)


def test_static_source_may_contain_loop_but_compiled_output_may_not() -> None:
    static_source = SOURCE + b"\nfor (const item of []) { console.log(item); }"
    assert validate_source(static_source, _sha(static_source))
    loop = b"for(;;){};export default function Report(){}"
    with pytest.raises(HTTPException, match="compiled payload failed policy"):
        validate_compiled(loop, _sha(loop))


def test_render_shell_has_opaque_iframe_csp_and_embedded_modules() -> None:
    shell = render_shell(RUNTIME, COMPILED)
    assert "default-src 'none'" in shell
    assert "connect-src 'none'" in shell
    assert "frame-src 'none'" in shell
    assert "script-src 'nonce-" in shell
    assert "blob:" in shell
    assert "await runtime.mountCanvas(artifactUrl)" in shell
    assert SOURCE.decode("utf-8").strip() not in shell


def test_path_identity_is_separator_and_case_stable() -> None:
    left = r"C:\Users\Me\.cursor\projects\Work\canvases\Report.canvas.tsx"
    right = "c:/users/me/.cursor/projects/work/canvases/report.canvas.tsx"
    assert normalized_path_hash(left) == normalized_path_hash(right)
