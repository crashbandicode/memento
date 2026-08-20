from __future__ import annotations

from server.services.canvas_artifacts import (
    MAX_CANVAS_HTML_LENGTH,
    MAX_CANVAS_SOURCE_LENGTH,
    conversation_has_canvas,
    detect_message_canvases,
    is_safe_canvas_embed_url,
    is_safe_canvas_path,
    looks_like_canvas_artifact,
)

CANVAS_PATH = "/Users/p/.cursor/projects/ws/canvases/billing-review.canvas.tsx"


def test_no_canvas_returns_empty():
    assert detect_message_canvases("Just some prose with src/App.tsx and a link.") == []
    assert detect_message_canvases(None) == []
    assert detect_message_canvases("") == []
    assert detect_message_canvases(123) == []  # type: ignore[arg-type]


def test_detects_markdown_link_without_source_is_unsupported():
    content = f"Built it at [billing-review]({CANVAS_PATH})."
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    c = canvases[0]
    assert c["name"] == "billing-review"
    assert c["path"] == CANVAS_PATH
    assert c["source_kind"] == "unsupported"
    assert "source" not in c
    assert conversation_has_canvas(content) is True


def test_canvas_filename_label_does_not_create_duplicate_relative_reference():
    content = f"Built [{CANVAS_PATH.rsplit('/', 1)[-1]}]({CANVAS_PATH})."
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    assert canvases[0]["path"] == CANVAS_PATH


def test_detects_bare_inline_canvas_path():
    content = f"See `{CANVAS_PATH}` for the report."
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    assert canvases[0]["path"] == CANVAS_PATH


def test_normalizes_json_escaped_windows_canvas_path():
    escaped = (
        r"C:\\Users\\intpa\\.cursor\\projects\\workspace\\canvases"
        r"\\incident.canvas.tsx"
    )
    content = '{"result":"Opened canvas: ' + escaped + '"}'
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    assert canvases[0]["path"] == (
        r"C:\Users\intpa\.cursor\projects\workspace\canvases"
        r"\incident.canvas.tsx"
    )


def test_associates_adjacent_tsx_source():
    content = (
        f"Built the canvas at [billing-review]({CANVAS_PATH}).\n\n"
        "```tsx\nexport default function Canvas() { return null; }\n```\n"
    )
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    c = canvases[0]
    assert c["source_kind"] == "source"
    assert c["source_language"] == "tsx"
    assert "export default function Canvas()" in c["source"]


def test_associates_self_contained_html_as_embed():
    content = (
        f"Exported to [report]({CANVAS_PATH}).\n\n"
        "```html\n<!doctype html><main>hi</main>\n```\n"
    )
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    c = canvases[0]
    assert c["source_kind"] == "embed"
    assert "<main>hi</main>" in c["html"]


def test_path_traversal_is_rejected():
    evil = "[x](../../../../etc/passwd.canvas.tsx)"
    assert detect_message_canvases(evil) == []
    assert is_safe_canvas_path("../secret.canvas.tsx") is False
    assert is_safe_canvas_path("%2e%2e/secret.canvas.tsx") is False
    assert is_safe_canvas_path("javascript:evil.canvas.tsx") is False
    assert is_safe_canvas_path("data:text/html,evil.canvas.tsx") is False
    assert is_safe_canvas_path(CANVAS_PATH) is True
    assert is_safe_canvas_path(r"C:\Users\me\safe.canvas.tsx") is True


def test_control_characters_in_path_are_rejected():
    assert is_safe_canvas_path("a\u0000b.canvas.tsx") is False


def test_oversized_source_is_not_attached():
    big = "z" * (MAX_CANVAS_SOURCE_LENGTH + 10)
    content = f"[x]({CANVAS_PATH})\n\n```tsx\n{big}\n```\n"
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    assert canvases[0]["source_kind"] == "unsupported"


def test_oversized_html_is_not_attached():
    big = "z" * (MAX_CANVAS_HTML_LENGTH + 10)
    content = f"[x]({CANVAS_PATH})\n\n```html\n{big}\n```\n"
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    assert canvases[0]["source_kind"] == "unsupported"


def test_count_cap_is_enforced():
    links = "\n".join(
        f"[c{i}](/ws/canvases/c{i}.canvas.tsx)" for i in range(0, 40)
    )
    canvases = detect_message_canvases(links)
    assert len(canvases) <= 12


def test_http_canvas_link_exposes_host():
    content = "[report](https://canvas.example.com/artifacts/report.canvas.tsx)"
    canvases = detect_message_canvases(content)
    assert len(canvases) == 1
    assert canvases[0]["host"] == "canvas.example.com"


def test_embed_url_scheme_allowlist():
    assert is_safe_canvas_embed_url("https://example.com/a.html") is True
    assert is_safe_canvas_embed_url("javascript:alert(1)") is False
    assert is_safe_canvas_embed_url("data:text/html,x") is False
    assert is_safe_canvas_embed_url("file:///etc/passwd") is False
    assert is_safe_canvas_embed_url("https://") is False
    assert is_safe_canvas_embed_url("https://example.com:not-a-port/a.html") is False
    assert (
        is_safe_canvas_embed_url(f"https://example.com/{'x' * 600}") is False
    )


def test_plain_tsx_is_not_a_canvas():
    assert looks_like_canvas_artifact("src/App.tsx") is False
    assert looks_like_canvas_artifact(CANVAS_PATH) is True
