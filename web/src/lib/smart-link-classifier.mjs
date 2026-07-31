import {
  canvasDisplayName,
  isSafeCanvasPath,
  looksLikeCanvasArtifact,
} from "./canvas-artifact.mjs";

const FILE_EXTENSION = /\.(?:c|cc|cpp|cs|css|csv|dockerfile|go|h|hpp|html?|ini|java|js|jsx|json|jsonl|kt|kts|lock|log|lua|md|mdx|mjs|mts|php|plist|properties|proto|ps1|py|rb|rs|scss|sh|sql|svg|swift|toml|ts|tsx|txt|vue|xml|ya?ml)(?:[?#].*)?$/i;
const SPECIAL_FILE = /(?:^|[/\\])(?:AGENTS\.md|Dockerfile|Gemfile|HANDOFF\.md|LICENSE|Makefile|Procfile|README|Tiltfile)(?:[?#].*)?$/i;
const SHA = /^[0-9a-f]{7,40}$/i;
const HTTP = /^https?:\/\//i;

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function normalizedCandidate(value) {
  return safeDecode(value)
    .trim()
    .replace(/^file:\/\//i, "")
    .split("#", 1)[0]
    .split("?", 1)[0]
    .replace(/[),.;:]+$/, "");
}

export function looksLikeFilePath(value) {
  const candidate = normalizedCandidate(value);
  if (!candidate || candidate.length > 320 || /[\r\n]/.test(candidate) || HTTP.test(candidate)) {
    return false;
  }
  if (SPECIAL_FILE.test(candidate)) return true;
  if (!FILE_EXTENSION.test(candidate)) return false;
  return (
    /[/\\]/.test(candidate)
    || /^[.~]/.test(candidate)
    || /^[A-Za-z]:[\\/]/.test(candidate)
    || /^[\w@-]+\.[\w.-]+$/.test(candidate)
  );
}

export function displayFileName(value) {
  const candidate = normalizedCandidate(value).replace(/[\\/]+$/, "");
  const pieces = candidate.split(/[\\/]/);
  return pieces.at(-1) || candidate;
}

function shortRef(value) {
  const decoded = safeDecode(value).replace(/^refs\/(?:heads|tags)\//, "");
  return SHA.test(decoded) ? decoded.slice(0, 10) : decoded;
}

function rawLinkLabel(label, href) {
  const trimmed = label.trim();
  return !trimmed || trimmed === href || HTTP.test(trimmed);
}

function providerFor(url) {
  const host = url.hostname.toLowerCase();
  if (host === "github.com" || host.endsWith(".github.com")) return "github";
  if (host === "gitlab.com" || host.endsWith(".gitlab.com") || url.pathname.includes("/-/")) {
    return "gitlab";
  }
  return null;
}

function compareRefs(url, provider) {
  const marker = provider === "gitlab" ? "/-/compare/" : "/compare/";
  const start = url.pathname.indexOf(marker);
  if (start < 0) return null;
  const comparison = url.pathname.slice(start + marker.length);
  const separator = comparison.indexOf("...");
  if (separator < 1 || separator >= comparison.length - 3) return null;
  return [
    shortRef(comparison.slice(0, separator)),
    shortRef(comparison.slice(separator + 3)),
  ];
}

function commitRef(url) {
  const match = url.pathname.match(/\/(?:-\/)?commit\/([0-9a-f]{7,40})(?:\/|$)/i);
  return match ? shortRef(match[1]) : null;
}

export function classifySmartLink(href, label = "") {
  const normalizedHref = href.trim();
  const normalizedLabel = label.trim();

  if (!HTTP.test(normalizedHref)) {
    const path = normalizedCandidate(normalizedHref || normalizedLabel);
    const canvasTarget = looksLikeCanvasArtifact(path) ? path : normalizedLabel;
    if (
      (looksLikeCanvasArtifact(path) || looksLikeCanvasArtifact(normalizedLabel))
      && isSafeCanvasPath(canvasTarget)
    ) {
      const labelIsCanvas = !normalizedLabel || looksLikeCanvasArtifact(normalizedLabel);
      return {
        kind: "canvas",
        href: normalizedHref,
        label: labelIsCanvas ? canvasDisplayName(canvasTarget) : normalizedLabel,
        name: canvasDisplayName(canvasTarget),
        path: canvasTarget || normalizedLabel,
      };
    }
    if (looksLikeFilePath(path) || looksLikeFilePath(normalizedLabel)) {
      const labelIsPath = !normalizedLabel || looksLikeFilePath(normalizedLabel);
      return {
        kind: "file",
        href: normalizedHref,
        label: labelIsPath ? displayFileName(normalizedLabel || path) : normalizedLabel,
        path: path || normalizedLabel,
      };
    }
    return { kind: "plain", href: normalizedHref, label: normalizedLabel || normalizedHref };
  }

  let url;
  try {
    url = new URL(normalizedHref);
  } catch {
    return { kind: "plain", href: normalizedHref, label: normalizedLabel || normalizedHref };
  }

  const provider = providerFor(url);
  if (provider) {
    const refs = compareRefs(url, provider);
    if (refs) {
      return {
        kind: "git-compare",
        href: normalizedHref,
        label: rawLinkLabel(normalizedLabel, normalizedHref)
          ? `${provider === "gitlab" ? "GitLab" : "GitHub"} compare`
          : normalizedLabel,
        provider,
        refs,
        domain: url.hostname,
      };
    }
    const ref = commitRef(url);
    if (ref) {
      return {
        kind: "git-commit",
        href: normalizedHref,
        label: rawLinkLabel(normalizedLabel, normalizedHref)
          ? `${provider === "gitlab" ? "GitLab" : "GitHub"} commit`
          : normalizedLabel,
        provider,
        ref,
        domain: url.hostname,
      };
    }
  }

  const decodedPath = safeDecode(url.pathname);
  if (looksLikeCanvasArtifact(decodedPath) && isSafeCanvasPath(decodedPath)) {
    return {
      kind: "canvas",
      href: normalizedHref,
      label: rawLinkLabel(normalizedLabel, normalizedHref)
        ? canvasDisplayName(decodedPath)
        : normalizedLabel,
      name: canvasDisplayName(decodedPath),
      path: `${url.hostname}${decodedPath}`,
      domain: url.hostname,
    };
  }
  if (looksLikeFilePath(decodedPath)) {
    return {
      kind: "file",
      href: normalizedHref,
      label: rawLinkLabel(normalizedLabel, normalizedHref)
        ? displayFileName(decodedPath)
        : normalizedLabel,
      path: `${url.hostname}${decodedPath}`,
      domain: url.hostname,
    };
  }

  return {
    kind: "web",
    href: normalizedHref,
    label: rawLinkLabel(normalizedLabel, normalizedHref) ? url.hostname : normalizedLabel,
    domain: url.hostname.replace(/^www\./i, ""),
  };
}

export function classifyInlineCode(value) {
  const normalized = value.trim();
  if (!normalized || /[\r\n]/.test(normalized)) return { kind: "plain", value };
  if (SHA.test(normalized)) {
    return { kind: "sha", value: normalized, display: normalized.slice(0, 10) };
  }
  if (looksLikeCanvasArtifact(normalized) && isSafeCanvasPath(normalized)) {
    return {
      kind: "canvas",
      value: normalized,
      display: canvasDisplayName(normalized),
      path: normalized,
    };
  }
  if (looksLikeFilePath(normalized)) {
    return { kind: "file", value: normalized, display: displayFileName(normalized) };
  }
  return { kind: "plain", value };
}
