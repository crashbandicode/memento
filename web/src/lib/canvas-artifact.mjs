/**
 * Shared, framework-agnostic Cursor Canvas artifact model.
 *
 * A Cursor Canvas is a `*.canvas.tsx` file the Cursor IDE compiles (with its
 * proprietary `cursor/canvas` SDK, no network) and shows beside the chat.
 * Memento never possesses that compiled canvas: the collector deliberately
 * excludes the `canvases/` directory and `skills-cursor/` templates, and there
 * is no compiler/SDK on the server or web. What conversation transcripts DO
 * carry is a reference to the artifact (a Markdown link or inline path ending in
 * `.canvas.tsx`) and — sometimes — the TSX source or a self-contained HTML
 * export embedded in the same message.
 *
 * This module is the single source of truth for:
 *   - detecting canvas references (used by the smart-link classifier),
 *   - deriving a safe display name,
 *   - deciding how the viewer may present an artifact (its "view mode"),
 *   - the security rules that gate embedding (scheme allowlist + size caps).
 *
 * It is intentionally dependency-free so it can be unit-tested under
 * `node --test` and imported by both the classifier and the React viewer. The
 * server mirrors these exact rules in
 * `server/server/services/canvas_artifacts.py`, which is the authoritative
 * detector: descriptors reach the client already validated (path-traversal
 * rejection, scheme allow-list, size/type caps, name sanitization). Memento
 * never reads a canvas file from disk and never adds a canvas-serving endpoint,
 * so there is no code path on which untrusted bytes are fetched by path — that
 * is the primary path-traversal mitigation. The sandbox + CSP helpers below are
 * the second layer for any embedded HTML the transcript itself carried.
 */

/** A canvas file always ends in `.canvas.tsx` (optionally with a query/hash). */
const CANVAS_EXTENSION = /\.canvas\.tsx(?:[?#].*)?$/i;

/** Upper bound on a canvas path/href we are willing to inspect. */
export const MAX_CANVAS_TARGET_LENGTH = 512;

/** Default language label for canvas source previews. */
export const CANVAS_SOURCE_LANGUAGE = "tsx";

/** Hard cap on inline source we will render (read-only text, never executed). */
export const MAX_CANVAS_SOURCE_LENGTH = 200_000;

/** Hard cap on a self-contained HTML artifact we will sandbox in an iframe. */
export const MAX_CANVAS_HTML_LENGTH = 500_000;

function decode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** Strip a `file://` prefix, surrounding punctuation, and whitespace. */
export function normalizeCanvasTarget(value) {
  const normalized = decode(String(value ?? ""))
    .trim()
    .replace(/^file:\/\//i, "")
    .replace(/[),.;:]+$/, "");
  // Cursor tool results can carry a nested JSON string, leaving Windows path
  // separators escaped in the visible message. Match the server's bounded
  // normalization so an inline chip resolves to the captured artifact.
  return /^[a-z]:\\{2,}/i.test(normalized)
    ? normalized.replace(/\\{2,}/g, "\\")
    : normalized;
}

function pathPart(value) {
  return normalizeCanvasTarget(value).split("#", 1)[0].split("?", 1)[0];
}

/** True when `value` points at a `*.canvas.tsx` Cursor Canvas artifact. */
export function looksLikeCanvasArtifact(value) {
  const candidate = pathPart(value);
  if (!candidate || candidate.length > MAX_CANVAS_TARGET_LENGTH || /[\r\n]/.test(candidate)) {
    return false;
  }
  return CANVAS_EXTENSION.test(candidate);
}

/** Remove control characters and clamp length so a name is safe to render. */
export function sanitizeCanvasName(value) {
  const cleaned = String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .trim()
    .slice(0, 120);
  return cleaned || "canvas";
}

/** Human display name for a canvas: the base filename without `.canvas.tsx`. */
export function canvasDisplayName(value) {
  const candidate = pathPart(value).replace(/[\\/]+$/, "");
  const base = candidate.split(/[\\/]/).at(-1) || candidate;
  return sanitizeCanvasName(base.replace(/\.canvas\.tsx$/i, "") || base || "canvas");
}

/**
 * Only `http(s)` URLs may ever be loaded into the sandboxed iframe. This blocks
 * `javascript:`, `data:`, `file:`, `blob:` and any other scheme that could read
 * host state or execute in a privileged context.
 */
export function isSafeCanvasEmbedUrl(value) {
  const raw = String(value ?? "").trim();
  if (!raw || raw.length > MAX_CANVAS_TARGET_LENGTH || /[\s]/.test(raw)) return false;
  let url;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  return url.protocol === "http:" || url.protocol === "https:";
}

/**
 * Build a link-only descriptor from a classified canvas link. This is what the
 * viewer receives when the server did not attach richer content (no source, no
 * bytes) — it renders as the "unsupported / source-unavailable" fallback.
 */
export function canvasArtifactFromLink(href, label) {
  const target = normalizeCanvasTarget(href) || normalizeCanvasTarget(label);
  const name = canvasDisplayName(label && looksLikeCanvasArtifact(label) ? label : target || label);
  return {
    name,
    path: pathPart(href) || pathPart(label),
    href: String(href ?? ""),
  };
}

/**
 * Decide how the viewer may present an artifact, applying the security policy.
 * Order of preference: a self-contained HTML export (sandboxed srcdoc), a safe
 * remote artifact URL (sandboxed src), read-only source text, otherwise an
 * honest "unsupported" fallback. Arbitrary transcript code is NEVER executed in
 * the host page — HTML only ever runs inside a minimally-permissioned iframe.
 *
 * @returns {(
 *   | { mode: "embed", variant: "srcdoc", html: string }
 *   | { mode: "embed", variant: "src", url: string }
 *   | { mode: "source", source: string, language: string }
 *   | { mode: "stored_source", sourceUrl: string }
 *   | { mode: "unsupported" }
 * )}
 */
export function resolveCanvasView(artifact) {
  const a = artifact || {};
  if (
    typeof a.render_url === "string"
    && /^\/api\/canvas-artifacts\/[0-9a-f-]{36}\/render$/.test(a.render_url)
  ) {
    return { mode: "interactive", renderUrl: a.render_url };
  }
  const html = typeof a.html === "string" ? a.html : "";
  if (html && html.length <= MAX_CANVAS_HTML_LENGTH) {
    return { mode: "embed", variant: "srcdoc", html };
  }
  if (typeof a.url === "string" && isSafeCanvasEmbedUrl(a.url)) {
    return { mode: "embed", variant: "src", url: a.url };
  }
  const source = typeof a.source === "string" ? a.source : "";
  if (source && source.length <= MAX_CANVAS_SOURCE_LENGTH) {
    return {
      mode: "source",
      source,
      language: sanitizeCanvasName(a.source_language || CANVAS_SOURCE_LANGUAGE),
    };
  }
  if (
    typeof a.source_url === "string"
    && /^\/api\/canvas-artifacts\/[0-9a-f-]{36}\/source$/.test(a.source_url)
  ) {
    return { mode: "stored_source", sourceUrl: a.source_url };
  }
  return { mode: "unsupported" };
}

/** The exact `sandbox` token set used for canvas iframes (no `allow-same-origin`). */
export const CANVAS_IFRAME_SANDBOX = "allow-scripts";

/**
 * Content-Security-Policy applied to any embedded canvas HTML. It runs inside a
 * `sandbox` iframe WITHOUT `allow-same-origin`, so the document already lives in
 * an opaque origin that cannot read the host page, its cookies, or its storage.
 * This CSP is the second layer: `default-src 'none'` blocks every network egress
 * and `connect-src 'none'` explicitly denies fetch/XHR/WebSocket/beacon, while
 * inline style/script and `data:`/`blob:` media are permitted so a
 * self-contained export can still render. Raw transcript TSX is NEVER executed
 * — it is only ever shown as read-only text on the host page.
 */
export const CANVAS_EMBED_CSP = [
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
  "script-src 'unsafe-inline'",
].join("; ");

/**
 * Wrap a self-contained HTML export so it always carries the restrictive
 * {@link CANVAS_EMBED_CSP} before it is assigned to a sandboxed iframe's
 * `srcdoc`. The untrusted markup is always placed after a fresh security head;
 * regex-injecting into an attacker-controlled `<head>` would allow fake tags in
 * comments/scripts to bypass the policy. The markup is never parsed or executed
 * on the host page — only inside the `sandbox` iframe (opaque origin, no
 * `allow-same-origin`).
 */
export function buildCanvasSrcDoc(html) {
  const markup = typeof html === "string" ? html : "";
  const metas =
    `<meta http-equiv="Content-Security-Policy" content="${CANVAS_EMBED_CSP}">`
    + "<meta name=\"referrer\" content=\"no-referrer\">";
  return (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    + metas
    + "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    + "<style>html,body{margin:0;padding:16px;background:#fff;color:#111;"
    + "font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}</style>"
    + `</head><body>${markup}</body></html>`
  );
}

/**
 * Mirror of the server's `is_safe_canvas_path`. Rejects parent-directory
 * traversal (`..` in either slash style), NUL/control bytes, over-long inputs,
 * and dangerous URL schemes (`javascript:`, `data:`, `blob:`, `vbscript:`, …).
 * Plain relative/absolute POSIX paths, Windows drive paths (`C:\\…`), and
 * `http(s)`/`file` targets are allowed. Memento never opens these paths; this is
 * defense-in-depth so a hostile `..` path is never echoed as a trusted target.
 */
export function isSafeCanvasPath(value) {
  const candidate = pathPart(value || "");
  if (!candidate || candidate.length > MAX_CANVAS_TARGET_LENGTH) return false;
  if (/[\u0000-\u001f\u007f]/.test(candidate)) return false;
  if (candidate.split(/[\\/]/).some((segment) => segment === "..")) return false;
  const scheme = candidate.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/);
  if (scheme && scheme[1].length > 1) {
    // A single letter followed by `:` is a Windows drive (C:\…), not a scheme.
    if (!["http", "https", "file"].includes(scheme[1].toLowerCase())) return false;
  }
  return true;
}
