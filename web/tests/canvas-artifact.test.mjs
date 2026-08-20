import assert from "node:assert/strict";
import test from "node:test";

import {
  CANVAS_EMBED_CSP,
  CANVAS_IFRAME_SANDBOX,
  MAX_CANVAS_HTML_LENGTH,
  MAX_CANVAS_SOURCE_LENGTH,
  buildCanvasSrcDoc,
  canvasArtifactFromLink,
  canvasDisplayName,
  isSafeCanvasEmbedUrl,
  isSafeCanvasPath,
  looksLikeCanvasArtifact,
  normalizeCanvasTarget,
  resolveCanvasView,
  sanitizeCanvasName,
} from "../src/lib/canvas-artifact.mjs";

test("detects .canvas.tsx artifacts and ignores plain tsx", () => {
  assert.equal(looksLikeCanvasArtifact("canvases/billing.canvas.tsx"), true);
  assert.equal(looksLikeCanvasArtifact("/abs/x.canvas.tsx?v=1"), true);
  assert.equal(looksLikeCanvasArtifact("src/App.tsx"), false);
  assert.equal(looksLikeCanvasArtifact("notes.md"), false);
  assert.equal(looksLikeCanvasArtifact(""), false);
});

test("display name strips the .canvas.tsx suffix and the directory", () => {
  assert.equal(canvasDisplayName("/a/b/billing-review.canvas.tsx"), "billing-review");
  assert.equal(canvasDisplayName("report.canvas.tsx?x=1#y"), "report");
});

test("normalizes JSON-escaped absolute Windows canvas paths", () => {
  assert.equal(
    normalizeCanvasTarget(String.raw`C:\\Users\\intpa\\.cursor\\projects\\work\\canvases\\report.canvas.tsx`),
    String.raw`C:\Users\intpa\.cursor\projects\work\canvases\report.canvas.tsx`,
  );
});

test("names are sanitized against control characters", () => {
  assert.equal(sanitizeCanvasName("a\u0000b\u001fc"), "abc");
  assert.equal(sanitizeCanvasName("   "), "canvas");
});

test("embed URL allowlist blocks dangerous schemes", () => {
  assert.equal(isSafeCanvasEmbedUrl("https://example.com/a.html"), true);
  assert.equal(isSafeCanvasEmbedUrl("http://example.com/a.html"), true);
  assert.equal(isSafeCanvasEmbedUrl("javascript:alert(1)"), false);
  assert.equal(isSafeCanvasEmbedUrl("data:text/html,<script>"), false);
  assert.equal(isSafeCanvasEmbedUrl("file:///etc/passwd"), false);
  assert.equal(isSafeCanvasEmbedUrl("  https://example.com  "), true);
  assert.equal(isSafeCanvasEmbedUrl("https://exa mple.com"), false);
  assert.equal(isSafeCanvasEmbedUrl(`https://example.com/${"x".repeat(600)}`), false);
});

test("resolveCanvasView prefers captured output, then legacy views", () => {
  assert.deepEqual(
    resolveCanvasView({
      render_url: "/api/canvas-artifacts/11111111-1111-4111-8111-111111111111/render",
      html: "<b>legacy</b>",
    }),
    {
      mode: "interactive",
      renderUrl: "/api/canvas-artifacts/11111111-1111-4111-8111-111111111111/render",
    },
  );
  assert.deepEqual(resolveCanvasView({ html: "<b>hi</b>" }), {
    mode: "embed",
    variant: "srcdoc",
    html: "<b>hi</b>",
  });
  assert.deepEqual(resolveCanvasView({ url: "https://example.com/a.html" }), {
    mode: "embed",
    variant: "src",
    url: "https://example.com/a.html",
  });
  assert.deepEqual(resolveCanvasView({ source: "export default 1", source_language: "tsx" }), {
    mode: "source",
    source: "export default 1",
    language: "tsx",
  });
  assert.deepEqual(resolveCanvasView({ name: "x", path: "x.canvas.tsx", href: "x.canvas.tsx" }), {
    mode: "unsupported",
  });
});

test("resolveCanvasView rejects unsafe embed URLs (falls back)", () => {
  assert.deepEqual(resolveCanvasView({ url: "javascript:alert(1)" }), { mode: "unsupported" });
  assert.deepEqual(resolveCanvasView({ render_url: "https://evil.test/render" }), {
    mode: "unsupported",
  });
});

test("resolveCanvasView enforces size caps", () => {
  const bigHtml = "x".repeat(MAX_CANVAS_HTML_LENGTH + 1);
  assert.deepEqual(resolveCanvasView({ html: bigHtml }), { mode: "unsupported" });
  const bigSource = "y".repeat(MAX_CANVAS_SOURCE_LENGTH + 1);
  assert.deepEqual(resolveCanvasView({ source: bigSource }), { mode: "unsupported" });
});

test("iframe sandbox never grants same-origin", () => {
  assert.ok(!/allow-same-origin/.test(CANVAS_IFRAME_SANDBOX));
  assert.ok(/allow-scripts/.test(CANVAS_IFRAME_SANDBOX));
  assert.ok(!/allow-popups/.test(CANVAS_IFRAME_SANDBOX));
});

test("canvasArtifactFromLink builds a link-only descriptor", () => {
  const artifact = canvasArtifactFromLink("/a/b/audit.canvas.tsx", "audit");
  assert.equal(artifact.name, "audit");
  assert.equal(artifact.path, "/a/b/audit.canvas.tsx");
  assert.equal(resolveCanvasView(artifact).mode, "unsupported");
});

test("isSafeCanvasPath mirrors the server path-traversal rejection", () => {
  assert.equal(isSafeCanvasPath("/Users/me/canvases/billing.canvas.tsx"), true);
  assert.equal(isSafeCanvasPath("canvases/report.canvas.tsx"), true);
  assert.equal(isSafeCanvasPath("C:\\Users\\me\\x.canvas.tsx"), true);
  assert.equal(isSafeCanvasPath("../../etc/passwd.canvas.tsx"), false);
  assert.equal(isSafeCanvasPath("a/../../b.canvas.tsx"), false);
  assert.equal(isSafeCanvasPath("a\\..\\b.canvas.tsx"), false);
  assert.equal(isSafeCanvasPath("javascript:alert(1)"), false);
  assert.equal(isSafeCanvasPath("data:text/html,x"), false);
  assert.equal(isSafeCanvasPath("bad\u0000path.canvas.tsx"), false);
  assert.equal(isSafeCanvasPath(""), false);
  assert.equal(isSafeCanvasPath("x".repeat(600)), false);
});

test("buildCanvasSrcDoc puts a restrictive CSP before all untrusted markup", () => {
  const fullDoc = buildCanvasSrcDoc(
    "<html><head><title>x</title></head><body><b>hi</b></body></html>",
  );
  assert.ok(fullDoc.startsWith("<!doctype html><html><head><meta charset=\"utf-8\">"));
  assert.ok(fullDoc.includes(CANVAS_EMBED_CSP));
  assert.ok(fullDoc.includes("<b>hi</b>"));
  assert.ok(fullDoc.indexOf(CANVAS_EMBED_CSP) < fullDoc.indexOf("<title>x</title>"));

  // A fake <head> inside attacker-controlled script text cannot precede CSP.
  const hostile = buildCanvasSrcDoc(
    "<html><script>const fake = '<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src *\">';</script></html>",
  );
  assert.ok(hostile.indexOf(CANVAS_EMBED_CSP) < hostile.indexOf("default-src *"));

  const fragment = buildCanvasSrcDoc("<b>fragment</b>");
  assert.ok(fragment.startsWith("<!doctype html>"));
  assert.ok(fragment.includes(CANVAS_EMBED_CSP));
  assert.ok(fragment.includes("<b>fragment</b>"));

  // The policy blocks network egress and never grants same-origin escape.
  assert.ok(/default-src 'none'/.test(CANVAS_EMBED_CSP));
  assert.ok(/connect-src 'none'/.test(CANVAS_EMBED_CSP));
  assert.ok(/frame-src 'none'/.test(CANVAS_EMBED_CSP));
  assert.ok(/object-src 'none'/.test(CANVAS_EMBED_CSP));
  assert.ok(/worker-src 'none'/.test(CANVAS_EMBED_CSP));
});
