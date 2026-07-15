"use client";

import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export type ClipboardFormat = "rich" | "markdown";

function clipboardMarkdown(markdown: string): string {
  return markdown
    .replace(/<details>\s*<summary>([\s\S]*?)<\/summary>/gi, (_match, summary: string) => {
      const clean = summary.replace(/<strong>([\s\S]*?)<\/strong>/gi, "**$1**");
      return `\n### ${clean}\n`;
    })
    .replace(/<\/details>/gi, "")
    .replace(/<sub>([\s\S]*?)<\/sub>/gi, "$1");
}

async function richClipboardHtml(markdown: string): Promise<string> {
  const container = document.createElement("div");
  const root = createRoot(container);
  root.render(<ReactMarkdown remarkPlugins={[remarkGfm]}>{clipboardMarkdown(markdown)}</ReactMarkdown>);
  await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));

  const styles: Record<string, Partial<CSSStyleDeclaration>> = {
    h1: { fontSize: "26px", lineHeight: "1.2", margin: "22px 0 12px", color: "#111827" },
    h2: { fontSize: "21px", lineHeight: "1.25", margin: "20px 0 10px", color: "#111827" },
    h3: { fontSize: "17px", lineHeight: "1.3", margin: "16px 0 8px", color: "#1f2937" },
    p: { margin: "8px 0", lineHeight: "1.55" },
    blockquote: { margin: "12px 0", padding: "8px 12px", borderLeft: "3px solid #8b5cf6", background: "#f5f3ff", color: "#374151" },
    pre: { margin: "10px 0", padding: "12px", borderRadius: "8px", background: "#111827", color: "#f9fafb", whiteSpace: "pre-wrap", wordBreak: "break-word" },
    code: { fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace", fontSize: "0.9em" },
    table: { width: "100%", borderCollapse: "collapse", margin: "12px 0" },
    th: { padding: "7px 9px", border: "1px solid #d1d5db", background: "#f3f4f6", textAlign: "left" },
    td: { padding: "7px 9px", border: "1px solid #d1d5db", verticalAlign: "top" },
    hr: { margin: "20px 0", border: "0", borderTop: "1px solid #e5e7eb" },
    a: { color: "#7c3aed" },
  };
  for (const [selector, properties] of Object.entries(styles)) {
    container.querySelectorAll<HTMLElement>(selector).forEach((element) => Object.assign(element.style, properties));
  }
  container.querySelectorAll<HTMLElement>("pre code").forEach((element) => {
    element.style.color = "inherit";
    element.style.background = "transparent";
  });
  const html = `<article style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#1f2937;max-width:760px">${container.innerHTML}</article>`;
  root.unmount();
  return html;
}

/** Copy Markdown directly or as styled HTML with Markdown as its plain fallback. */
export async function copyMarkdownToClipboard(
  markdown: string,
  format: ClipboardFormat,
): Promise<ClipboardFormat> {
  if (format === "markdown") {
    await navigator.clipboard.writeText(markdown);
    return "markdown";
  }

  if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    try {
      const html = await richClipboardHtml(markdown);
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": new Blob([markdown], { type: "text/plain" }),
        "text/html": new Blob([html], { type: "text/html" }),
      })]);
      return "rich";
    } catch {
      // Some WebViews expose ClipboardItem but reject custom MIME writes.
    }
  }

  await navigator.clipboard.writeText(markdown);
  return "markdown";
}
