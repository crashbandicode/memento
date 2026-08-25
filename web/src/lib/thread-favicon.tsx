"use client";

import { useEffect } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { BrandMark, BRAND_COLORS } from "@/components/aurora/BrandMark";

// A tab favicon reads best as a filled brand tile with a white mark — the same
// shape the dashboard's aurora ToolGlyph uses — rather than a thin mark on
// transparent. Rendered at 64px so it downsamples cleanly to 16/32px.
const FAVICON_SIZE = 64;
const MARK_SIZE = 34;
const MARK_INSET = (FAVICON_SIZE - MARK_SIZE) / 2;

// Neutral Memento indigo for tools without a registered brand mark.
const FALLBACK_BG = "#6366F1";

const _cache = new Map<string, string>();

/**
 * A `data:` SVG of the tool's brand tile, reusing BrandMark (the single source
 * of truth for tool marks — Anthropic sunburst for claude_code, the Codex and
 * Cursor product marks, etc.) so the favicon always matches the dashboard.
 */
export function toolFaviconDataUri(toolId: string): string {
  const brandId = toolId in BRAND_COLORS ? toolId : "notes";
  const cached = _cache.get(brandId);
  if (cached) return cached;
  const background = BRAND_COLORS[brandId] || FALLBACK_BG;
  const markup = renderToStaticMarkup(
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={FAVICON_SIZE}
      height={FAVICON_SIZE}
      viewBox={`0 0 ${FAVICON_SIZE} ${FAVICON_SIZE}`}
    >
      <rect width={FAVICON_SIZE} height={FAVICON_SIZE} rx={14} fill={background} />
      <g transform={`translate(${MARK_INSET} ${MARK_INSET})`}>
        <BrandMark id={brandId} size={MARK_SIZE} inverted />
      </g>
    </svg>,
  );
  const uri = `data:image/svg+xml,${encodeURIComponent(markup)}`;
  _cache.set(brandId, uri);
  return uri;
}

/**
 * Swap the browser tab favicon to the thread's tool mark while the thread is
 * open, then restore the site default when it closes or the tool changes.
 */
export function useThreadFavicon(toolId: string | null | undefined): void {
  useEffect(() => {
    if (!toolId || typeof document === "undefined") return undefined;
    const head = document.head;
    // Detach whatever icon links are currently live (the root layout's default
    // svg + png) and restore them verbatim on cleanup, so navigating away never
    // leaves a stale thread favicon behind.
    const previous = Array.from(
      head.querySelectorAll<HTMLLinkElement>('link[rel~="icon"]'),
    );
    previous.forEach((link) => link.remove());
    const link = document.createElement("link");
    link.rel = "icon";
    link.type = "image/svg+xml";
    link.setAttribute("data-thread-favicon", toolId);
    link.href = toolFaviconDataUri(toolId);
    head.appendChild(link);
    return () => {
      link.remove();
      previous.forEach((old) => head.appendChild(old));
    };
  }, [toolId]);
}
