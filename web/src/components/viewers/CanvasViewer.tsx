"use client";

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { Icon } from "@/components/aurora/Icon";
import { authFetch, getApiBase } from "@/lib/api-client";
import { useI18n } from "@/lib/i18n";
import {
  buildCanvasSrcDoc,
  CANVAS_IFRAME_SANDBOX,
  isSafeCanvasEmbedUrl,
  resolveCanvasView,
  type CanvasArtifact,
} from "@/lib/canvas-artifact.mjs";
import styles from "./CanvasViewer.module.css";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input, select, iframe, [tabindex]:not([tabindex="-1"])';

const MOBILE_QUERY = "(max-width: 640px)";

/** Reactively track whether the viewport is in the mobile (full-screen sheet) range. */
function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const query = window.matchMedia(MOBILE_QUERY);
    const sync = () => setMobile(query.matches);
    sync();
    query.addEventListener?.("change", sync);
    return () => query.removeEventListener?.("change", sync);
  }, []);
  return mobile;
}

/**
 * The secure Cursor Canvas viewer. It renders as a centered modal on desktop and
 * a full-screen sheet on mobile (the module stylesheet's media query handles the
 * geometry; `data-canvas-layout` exposes which one is active).
 *
 * Security model:
 *   - Read-only TSX/source is shown as inert, escaped text via `<pre>` — it is
 *     NEVER compiled or executed on the host page.
 *   - A self-contained HTML export is isolated in a `sandbox` iframe with no
 *     `allow-same-origin` (opaque origin) and a restrictive CSP injected by
 *     {@link buildCanvasSrcDoc}. A validated remote URL uses the same sandbox;
 *     its remote document cannot be rewritten with our CSP.
 *   - Anything else degrades to an honest "preview unavailable" fallback that
 *     still surfaces the recorded canvas path.
 */
export function CanvasViewer({
  artifact,
  open,
  onClose,
}: {
  artifact: CanvasArtifact;
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const copy = t.conversation.canvas;
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const isMobile = useIsMobile();
  const [renderResult, setRenderResult] = useState<{
    url: string;
    shell: string | null;
    error: boolean;
  } | null>(null);
  const [storedSource, setStoredSource] = useState<string | null>(null);
  const [sourceLoadError, setSourceLoadError] = useState(false);
  const [showSource, setShowSource] = useState(false);

  const view = useMemo(() => resolveCanvasView(artifact), [artifact]);
  const requestClose = useCallback(() => onClose(), [onClose]);

  useEffect(() => {
    if (!open || view.mode !== "interactive") return undefined;
    const controller = new AbortController();
    void authFetch(`${getApiBase()}${view.renderUrl}`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Canvas HTTP ${response.status}`);
        const shell = await response.text();
        if (!shell || shell.length > 5_000_000) throw new Error("Invalid Canvas shell");
        setRenderResult({ url: view.renderUrl, shell, error: false });
      })
      .catch((error: unknown) => {
        if ((error as { name?: string })?.name !== "AbortError") {
          setRenderResult({ url: view.renderUrl, shell: null, error: true });
        }
      });
    return () => controller.abort();
  }, [open, view]);

  useEffect(() => {
    if (!open || view.mode !== "stored_source") return undefined;
    const controller = new AbortController();
    void authFetch(`${getApiBase()}${view.sourceUrl}`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Canvas source HTTP ${response.status}`);
        const source = await response.text();
        if (!source || source.length > 200_000) throw new Error("Invalid Canvas source");
        setStoredSource(source);
      })
      .catch((error: unknown) => {
        if ((error as { name?: string })?.name !== "AbortError") {
          setSourceLoadError(true);
        }
      });
    return () => controller.abort();
  }, [open, view]);

  useEffect(() => {
    if (!open) return undefined;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.requestAnimationFrame(() => {
      const first = dialogRef.current?.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? dialogRef.current)?.focus();
    });
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        requestClose();
      }
    };
    window.addEventListener("keydown", handleKey, true);
    return () => {
      window.cancelAnimationFrame(focusTimer);
      window.removeEventListener("keydown", handleKey, true);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, [open, requestClose]);

  const trapTab = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
    if (!focusable || focusable.length === 0) {
      event.preventDefault();
      dialogRef.current?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === dialogRef.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const loadStoredSource = async (): Promise<string | null> => {
    if (storedSource) return storedSource;
    if (!artifact.source_url) return null;
    const response = await authFetch(`${getApiBase()}${artifact.source_url}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`Canvas source HTTP ${response.status}`);
    const source = await response.text();
    if (source.length > 200_000) throw new Error("Canvas source is too large");
    setStoredSource(source);
    return source;
  };

  const copySource = async () => {
    try {
      const source = view.mode === "source" ? view.source : await loadStoredSource();
      if (source) await navigator.clipboard?.writeText(source);
    } catch {
      /* clipboard denied — the source stays visible for manual selection */
    }
  };

  const toggleSource = async () => {
    if (showSource) {
      setShowSource(false);
      return;
    }
    try {
      if (view.mode !== "source") await loadStoredSource();
      setShowSource(true);
    } catch {
      setShowSource(false);
    }
  };

  if (!open || typeof document === "undefined") return null;

  const canOpenExternally =
    typeof artifact.url === "string" && isSafeCanvasEmbedUrl(artifact.url);
  const canShowStoredSource = Boolean(artifact.source_url);
  const cursorUrl = artifact.artifact_id && /^[A-Za-z]:[\\/]/.test(artifact.path || "")
    ? `cursor://file/${encodeURI((artifact.path || "").replaceAll("\\", "/"))}`
    : null;
  const currentRender = view.mode === "interactive" && renderResult?.url === view.renderUrl
    ? renderResult
    : null;

  return createPortal(
    <div
      className={styles.overlay}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) requestClose();
      }}
    >
      <div
        ref={dialogRef}
        className={styles.dialog}
        data-testid="canvas-viewer"
        data-canvas-mode={view.mode}
        data-canvas-layout={isMobile ? "sheet" : "modal"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={trapTab}
      >
        <header className={styles.header}>
          <span className={styles.badge} aria-hidden="true">
            <Icon name="layers" size={16} />
          </span>
          <div className={styles.titleWrap}>
            <span className={styles.eyebrow}>{copy.eyebrow}</span>
            <h2 id={titleId} className={styles.title} title={artifact.path || artifact.name}>
              {artifact.name || "canvas"}
            </h2>
          </div>
          <span className={styles.modeBadge}>
            {view.mode === "interactive" ? copy.interactive : copy.static}
          </span>
          <span className={styles.spacer} />
          {(view.mode === "source" || view.mode === "stored_source") && (
            <button
              type="button"
              className={styles.toolbarBtn}
              data-testid="canvas-copy"
              onClick={copySource}
            >
              <Icon name="copy" size={14} />
              <span>{copy.copySource}</span>
            </button>
          )}
          {canShowStoredSource
            && view.mode !== "source"
            && view.mode !== "stored_source"
            && (
            <button
              type="button"
              className={styles.toolbarBtn}
              data-testid="canvas-source-toggle"
              onClick={toggleSource}
            >
              <Icon name="file_text" size={14} />
              <span>{showSource ? copy.viewVisual : copy.viewSource}</span>
            </button>
            )}
          {cursorUrl && (
            <a
              className={styles.toolbarBtn}
              data-testid="canvas-open-cursor"
              href={cursorUrl}
              rel="noreferrer"
            >
              <Icon name="external_link" size={14} />
              <span>{copy.openInCursor}</span>
            </a>
          )}
          {canOpenExternally && (
            <a
              className={styles.toolbarBtn}
              data-testid="canvas-open-external"
              href={artifact.url as string}
              target="_blank"
              rel="noreferrer noopener"
            >
              <Icon name="external_link" size={14} />
              <span>{copy.openExternal}</span>
            </a>
          )}
          <button
            type="button"
            className={styles.iconBtn}
            data-testid="canvas-viewer-close"
            aria-label={copy.close}
            onClick={requestClose}
          >
            <Icon name="close" size={18} />
          </button>
        </header>

        <div className={styles.body}>
          {view.mode === "stored_source" ? (
            sourceLoadError ? (
              <div className={styles.fallback} data-testid="canvas-load-error">
                <p className={styles.fallbackTitle}>{copy.loadError}</p>
              </div>
            ) : storedSource ? (
              <pre className={styles.source} data-testid="canvas-source" tabIndex={0}>
                {storedSource}
              </pre>
            ) : (
              <div className={styles.fallback} data-testid="canvas-loading" role="status">
                <p className={styles.fallbackText}>{copy.loading}</p>
              </div>
            )
          ) : showSource && (storedSource || view.mode === "source") ? (
            <div className={styles.sourceWrap}>
              <button
                type="button"
                className={styles.copyFloating}
                data-testid="canvas-copy"
                onClick={copySource}
              >
                <Icon name="copy" size={14} />
                {copy.copySource}
              </button>
              <pre className={styles.source} data-testid="canvas-source" tabIndex={0}>
                {storedSource || (view.mode === "source" ? view.source : "")}
              </pre>
            </div>
          ) : (
            <CanvasViewerBody
              view={view}
              artifact={artifact}
              copy={copy}
              renderShell={currentRender?.shell ?? null}
              renderError={currentRender?.error ?? false}
            />
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function CanvasViewerBody({
  view,
  artifact,
  copy,
  renderShell,
  renderError,
}: {
  view: ReturnType<typeof resolveCanvasView>;
  artifact: CanvasArtifact;
  copy: ReturnType<typeof useI18n>["t"]["conversation"]["canvas"];
  renderShell: string | null;
  renderError: boolean;
}) {
  if (view.mode === "interactive") {
    if (renderError) {
      return (
        <div className={styles.fallback} data-testid="canvas-load-error">
          <p className={styles.fallbackTitle}>{copy.loadError}</p>
        </div>
      );
    }
    if (!renderShell) {
      return (
        <div className={styles.fallback} data-testid="canvas-loading" role="status">
          <p className={styles.fallbackText}>{copy.loading}</p>
        </div>
      );
    }
    return (
      <iframe
        className={styles.frame}
        data-testid="canvas-frame"
        data-canvas-embed="captured"
        title={`${copy.eyebrow}: ${artifact.name}`}
        sandbox={CANVAS_IFRAME_SANDBOX}
        referrerPolicy="no-referrer"
        allow={
          "camera 'none'; clipboard-read 'none'; clipboard-write 'none'; "
          + "display-capture 'none'; geolocation 'none'; microphone 'none'; "
          + "payment 'none'; usb 'none'"
        }
        srcDoc={renderShell}
      />
    );
  }
  if (view.mode === "embed") {
    const commonProps = {
      className: styles.frame,
      "data-testid": "canvas-frame",
      title: `${copy.eyebrow}: ${artifact.name}`,
      sandbox: CANVAS_IFRAME_SANDBOX,
      referrerPolicy: "no-referrer" as const,
      allow:
        "camera 'none'; clipboard-read 'none'; clipboard-write 'none'; "
        + "display-capture 'none'; geolocation 'none'; microphone 'none'; "
        + "payment 'none'; usb 'none'",
    };
    return view.variant === "srcdoc" ? (
      <iframe {...commonProps} data-canvas-embed="srcdoc" srcDoc={buildCanvasSrcDoc(view.html)} />
    ) : (
      <iframe {...commonProps} data-canvas-embed="src" src={view.url} />
    );
  }

  if (view.mode === "source") {
    return (
      <pre className={styles.source} data-testid="canvas-source" tabIndex={0}>
        {view.source}
      </pre>
    );
  }

  return (
    <div className={styles.fallback} data-testid="canvas-unsupported">
      <span className={styles.fallbackIcon} aria-hidden="true">
        <Icon name="layers" size={24} />
      </span>
      <p className={styles.fallbackTitle}>{copy.unavailableTitle}</p>
      <p className={styles.fallbackText}>{copy.unavailableBody}</p>
      {(artifact.path || artifact.href) && (
        <span className={styles.metaRow} data-testid="canvas-path">
          <Icon name="file_text" size={12} aria-hidden="true" />
          {artifact.path || artifact.href}
        </span>
      )}
    </div>
  );
}
