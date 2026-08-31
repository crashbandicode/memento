"use client";

import {
  Children,
  isValidElement,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import type { Components, UrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import MermaidDiagram from "./MermaidDiagram";
import { SmartCode, SmartLink } from "./SmartLink";
import { Icon } from "@/components/aurora/Icon";
import { CanvasArtifactProvider } from "@/lib/canvas-context";
import {
  isSafeCanvasPath,
  looksLikeCanvasArtifact,
  type CanvasArtifact,
} from "@/lib/canvas-artifact.mjs";
import { copyText } from "@/lib/copy-text";
import { useI18n } from "@/lib/i18n";
import "highlight.js/styles/github-dark.min.css";
import styles from "./MarkdownViewer.module.css";

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

function mermaidSource(children: ReactNode): string | null {
  const childNodes = Children.toArray(children);
  if (childNodes.length !== 1) return null;
  const child = childNodes[0];
  if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) return null;
  if (!/(?:^|\s)language-mermaid(?:\s|$)/i.test(child.props.className ?? "")) return null;
  return nodeText(child.props.children).replace(/\n$/, "");
}

function CodeCopyButton({ source }: { source: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const feedbackTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (feedbackTimer.current !== null) clearTimeout(feedbackTimer.current);
  }, []);

  const handleCopy = async () => {
    try {
      await copyText(source);
      setCopied(true);
      if (feedbackTimer.current !== null) clearTimeout(feedbackTimer.current);
      feedbackTimer.current = setTimeout(() => {
        feedbackTimer.current = null;
        setCopied(false);
      }, 1600);
    } catch {
      // Do not claim success when the Clipboard API and its fallback both fail.
      if (feedbackTimer.current !== null) clearTimeout(feedbackTimer.current);
      feedbackTimer.current = null;
      setCopied(false);
    }
  };

  const label = copied ? t.common.copied : t.common.copy;

  return (
    <button
      type="button"
      className={styles.copyButton}
      data-testid="code-block-copy"
      data-copy-status={copied ? "copied" : "idle"}
      aria-label={label}
      title={label}
      onClick={() => { void handleCopy(); }}
    >
      <Icon name={copied ? "check" : "copy"} size={15} aria-hidden="true" />
      <span className={styles.copyLabel} aria-live="polite" aria-atomic="true">{label}</span>
    </button>
  );
}

function CodeBlock({ children, ...props }: ComponentPropsWithoutRef<"pre">) {
  const source = mermaidSource(children);
  if (source !== null) return <MermaidDiagram source={source} />;

  // react-markdown appends one terminal newline to fenced blocks. Strip only
  // that parser artifact so blank lines deliberately included in the source
  // remain part of the copied value.
  const copySource = nodeText(children).replace(/\n$/, "");

  return (
    <div className={styles.codeBlock} data-testid="markdown-code-block">
      <pre {...props}>{children}</pre>
      <CodeCopyButton source={copySource} />
    </div>
  );
}

const canvasAwareUrlTransform: UrlTransform = (url) => {
  if (looksLikeCanvasArtifact(url) && isSafeCanvasPath(url)) return url;
  return defaultUrlTransform(url);
};

const markdownComponents: Components = {
  a: SmartLink,
  code: SmartCode,
  pre: CodeBlock,
  table: ({ children, ...props }) => (
    <div
      style={{
        width: "100%",
        margin: "12px 0 14px",
        overflowX: "auto",
        border: "1px solid var(--aurora-border)",
        borderRadius: 10,
        background: "var(--aurora-surface-solid)",
      }}
    >
      <table
        {...props}
        style={{
          width: "100%",
          minWidth: 520,
          margin: 0,
          borderCollapse: "collapse",
          fontSize: "0.92em",
          lineHeight: 1.45,
        }}
      >
        {children}
      </table>
    </div>
  ),
  th: ({ children, ...props }) => (
    <th
      {...props}
      style={{
        padding: "8px 10px",
        borderRight: "1px solid var(--aurora-border)",
        borderBottom: "1px solid var(--aurora-border)",
        background: "color-mix(in srgb, var(--aurora-chip) 72%, var(--aurora-surface-solid))",
        color: "var(--aurora-fg2)",
        fontWeight: 650,
        textAlign: "left",
        verticalAlign: "top",
      }}
    >
      {children}
    </th>
  ),
  td: ({ children, ...props }) => (
    <td
      {...props}
      style={{
        padding: "8px 10px",
        borderRight: "1px solid var(--aurora-border)",
        borderBottom: "1px solid var(--aurora-border)",
        color: "var(--aurora-fg2)",
        verticalAlign: "top",
        overflowWrap: "anywhere",
      }}
    >
      {children}
    </td>
  ),
  p: ({ children, ...props }) => (
    <p {...props} style={{ margin: "0 0 9px", lineHeight: 1.62 }}>
      {children}
    </p>
  ),
  ul: ({ children, ...props }) => (
    <ul
      {...props}
      style={{
        // Tailwind preflight sets list-style: none; restore markers for prose.
        listStyleType: "disc",
        listStylePosition: "outside",
        margin: "7px 0 10px",
        paddingLeft: 28,
      }}
    >
      {children}
    </ul>
  ),
  ol: ({ children, ...props }) => (
    <ol
      {...props}
      style={{
        listStyleType: "decimal",
        listStylePosition: "outside",
        margin: "7px 0 10px",
        paddingLeft: 28,
      }}
    >
      {children}
    </ol>
  ),
  li: ({ children, ...props }) => (
    <li {...props} style={{ margin: "3px 0", paddingLeft: 2 }}>
      {children}
    </li>
  ),
};

export default function MarkdownViewer({
  content,
  canvases,
}: {
  content: string;
  canvases?: CanvasArtifact[] | null;
}) {
  return (
    <CanvasArtifactProvider artifacts={canvases}>
    <div
      className={[
        "prose prose-sm max-w-none",
        // Headings
        "prose-headings:font-semibold prose-headings:text-gray-900",
        // Code blocks — dark background for all pre tags
        "prose-pre:bg-[#1e1e1e] prose-pre:text-gray-100 prose-pre:rounded-lg prose-pre:border-0",
        "prose-pre:text-sm prose-pre:leading-relaxed prose-pre:shadow-md",
        "[&_pre]:bg-[#1e1e1e] [&_pre]:text-gray-100 [&_pre]:rounded-lg [&_pre]:shadow-md [&_pre]:overflow-x-auto",
        "[&_pre_code]:bg-transparent [&_pre_code]:text-inherit [&_pre_code]:p-0",
        // Inline code — light background (keep readable in white bubbles)
        "prose-code:before:content-none prose-code:after:content-none",
        "prose-code:bg-gray-100 prose-code:text-pink-600",
        "prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded",
        "prose-code:text-[0.85em] prose-code:font-mono",
        // Tables
        "prose-table:border-collapse prose-th:bg-gray-50",
        "prose-td:border prose-td:border-gray-200 prose-td:px-3 prose-td:py-1.5",
        "prose-th:border prose-th:border-gray-200 prose-th:px-3 prose-th:py-1.5",
        // Word break for long URLs and paths
        "break-words overflow-wrap-anywhere",
        // Lists
        "prose-li:my-0.5",
        // Blockquote
        "prose-blockquote:border-l-blue-400 prose-blockquote:bg-blue-50 prose-blockquote:py-1 prose-blockquote:rounded-r",
        // Images
        "prose-img:rounded-lg prose-img:shadow-sm",
      ].join(" ")}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { plainText: ["mermaid"] }]]}
        components={markdownComponents}
        urlTransform={canvasAwareUrlTransform}
      >
        {content}
      </ReactMarkdown>
    </div>
    </CanvasArtifactProvider>
  );
}
