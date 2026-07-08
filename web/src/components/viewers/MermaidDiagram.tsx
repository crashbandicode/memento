"use client";

import { useEffect, useId, useState } from "react";
import { useI18n } from "@/lib/i18n";
import { useTheme } from "@/lib/theme-context";
import styles from "./MermaidDiagram.module.css";

const MAX_SOURCE_LENGTH = 50_000;
const MAX_EDGES = 500;
const FONT_FAMILY = "Inter, ui-sans-serif, system-ui, sans-serif";

type MermaidApi = typeof import("mermaid")["default"];

let mermaidImportPromise: Promise<MermaidApi> | undefined;
let renderQueue: Promise<unknown> = Promise.resolve();

function loadMermaid(): Promise<MermaidApi> {
  mermaidImportPromise ??= import("mermaid").then(({ default: mermaid }) => mermaid);
  return mermaidImportPromise;
}

// Mermaid configuration is process-global. Keep initialize() and render() in
// one serialized operation so adjacent diagrams cannot race themes/config.
function enqueueRender<T>(operation: () => Promise<T>): Promise<T> {
  const result = renderQueue.then(operation, operation);
  renderQueue = result.then(() => undefined, () => undefined);
  return result;
}

function cssColor(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

type RenderState =
  | { kind: "loading" }
  | { kind: "rendered"; svg: string }
  | { kind: "error"; reason: "invalid" | "too-large" };

export default function MermaidDiagram({ source }: { source: string }) {
  const reactId = useId();
  const { theme, skin } = useTheme();
  const { t } = useI18n();
  const [state, setState] = useState<RenderState>({ kind: "loading" });
  const sourceTooLarge = source.length > MAX_SOURCE_LENGTH;

  useEffect(() => {
    let active = true;
    const renderId = `mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

    if (sourceTooLarge) {
      return () => {
        active = false;
      };
    }

    queueMicrotask(() => {
      if (active) setState({ kind: "loading" });
    });

    void enqueueRender(async () => {
      if (!active) return null;
      const mermaid = await loadMermaid();
      if (!active) return null;

      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        suppressErrorRendering: true,
        maxTextSize: MAX_SOURCE_LENGTH,
        maxEdges: MAX_EDGES,
        htmlLabels: false,
        secure: [
          "securityLevel",
          "theme",
          "themeCSS",
          "themeVariables",
          "fontFamily",
          "htmlLabels",
          "maxTextSize",
          "maxEdges",
        ],
        theme: theme === "dark" ? "dark" : "default",
        fontFamily: FONT_FAMILY,
        themeVariables: {
          background: cssColor("--aurora-surface-solid", theme === "dark" ? "#15151f" : "#ffffff"),
          primaryColor: cssColor("--aurora-chip", theme === "dark" ? "#252531" : "#f5f3ff"),
          primaryTextColor: cssColor("--aurora-fg1", theme === "dark" ? "#f4f4f8" : "#0b0b14"),
          primaryBorderColor: cssColor("--aurora-accent", theme === "dark" ? "#a78bfa" : "#6d28d9"),
          lineColor: cssColor("--aurora-fg3", theme === "dark" ? "#8e8e9e" : "#6e6e80"),
          secondaryColor: cssColor("--aurora-accent-soft", theme === "dark" ? "#29233d" : "#f3e8ff"),
          tertiaryColor: cssColor("--aurora-surface-solid", theme === "dark" ? "#15151f" : "#ffffff"),
          fontFamily: FONT_FAMILY,
        },
        flowchart: {
          htmlLabels: false,
          useMaxWidth: true,
        },
      });
      const { svg } = await mermaid.render(renderId, source);
      return svg;
    })
      .then((svg) => {
        if (active && svg) setState({ kind: "rendered", svg });
      })
      .catch(() => {
        document.getElementById(renderId)?.remove();
        if (active) setState({ kind: "error", reason: "invalid" });
      });

    return () => {
      active = false;
      document.getElementById(renderId)?.remove();
    };
  }, [reactId, skin, source, sourceTooLarge, theme]);

  const displayState: RenderState = sourceTooLarge
    ? { kind: "error", reason: "too-large" }
    : state;

  const errorMessage = displayState.kind === "error"
    ? displayState.reason === "too-large"
      ? t.conversation.diagramTooLarge
      : t.conversation.diagramError
    : null;

  return (
    <figure className={styles.card} data-mermaid-diagram>
      <figcaption className={styles.header}>
        <span className={styles.mark} aria-hidden="true">◇</span>
        {t.conversation.diagram}
      </figcaption>

      {displayState.kind === "loading" && (
        <div className={styles.status} role="status">
          <span className={styles.spinner} aria-hidden="true" />
          {t.conversation.renderingDiagram}
        </div>
      )}

      {displayState.kind === "rendered" && (
        <div
          className={styles.viewport}
          role="img"
          aria-label={t.conversation.diagram}
          // Mermaid's strict security mode sanitizes generated SVG and disables
          // unsafe HTML before it reaches this rendering boundary.
          dangerouslySetInnerHTML={{ __html: displayState.svg }}
        />
      )}

      {errorMessage && (
        <div className={styles.error} role="alert">
          {errorMessage}
        </div>
      )}

      <details className={styles.source} open={displayState.kind === "error"}>
        <summary>{t.conversation.viewDiagramSource}</summary>
        <pre><code className="language-mermaid">{source}</code></pre>
      </details>
    </figure>
  );
}
