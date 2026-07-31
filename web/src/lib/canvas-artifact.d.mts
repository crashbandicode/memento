export interface CanvasArtifact {
  /** Display name (base filename without `.canvas.tsx`). */
  name: string;
  /** Normalized artifact path (e.g. `.../canvases/billing-review.canvas.tsx`). */
  path: string;
  /** Original href/target as it appeared in the transcript. */
  href: string;
  /** How the server classified the available representation. */
  source_kind?: "embed" | "source" | "unsupported";
  /** Read-only TSX/source text embedded in the transcript, if any. */
  source?: string | null;
  /** Language hint for the source preview (defaults to `tsx`). */
  source_language?: string | null;
  /** Self-contained HTML export to sandbox via `srcdoc`, if any. */
  html?: string | null;
  /** Safe remote artifact URL to sandbox via `src`, if any. */
  url?: string | null;
  /** Host for a remote artifact URL. */
  host?: string | null;
}

export type CanvasView =
  | { mode: "embed"; variant: "srcdoc"; html: string }
  | { mode: "embed"; variant: "src"; url: string }
  | { mode: "source"; source: string; language: string }
  | { mode: "unsupported" };

export const MAX_CANVAS_TARGET_LENGTH: number;
export const CANVAS_SOURCE_LANGUAGE: string;
export const MAX_CANVAS_SOURCE_LENGTH: number;
export const MAX_CANVAS_HTML_LENGTH: number;
export const CANVAS_IFRAME_SANDBOX: string;
export const CANVAS_EMBED_CSP: string;

export function normalizeCanvasTarget(value: string): string;
export function looksLikeCanvasArtifact(value: string): boolean;
export function sanitizeCanvasName(value: string): string;
export function canvasDisplayName(value: string): string;
export function isSafeCanvasEmbedUrl(value: string): boolean;
export function isSafeCanvasPath(value: string): boolean;
export function buildCanvasSrcDoc(html: string): string;
export function canvasArtifactFromLink(href: string, label?: string): CanvasArtifact;
export function resolveCanvasView(artifact: CanvasArtifact | null | undefined): CanvasView;
