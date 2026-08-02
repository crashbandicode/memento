"use client";

import type { CSSProperties, ReactNode } from "react";
import { Icon } from "@/components/aurora/Icon";
import MarkdownViewer from "./MarkdownViewer";
import {
  contextUsageSummary,
  looksLikeMarkdownContext,
  parseContextUsageReport,
  type ContextMcpTool,
  type ContextUsageCategory,
  type ContextUsageReport,
} from "./sessionContextParse";

export {
  contextUsageSummary,
  looksLikeMarkdownContext,
  parseContextUsageReport,
} from "./sessionContextParse";

type CategoryTone = {
  color: string;
  soft: string;
  icon: "terminal" | "settings" | "folder" | "sparkles" | "message" | "grid" | "inbox" | "cube";
};

const CATEGORY_TONES: Record<string, CategoryTone> = {
  "system prompt": { color: "#64748b", soft: "rgba(100,116,139,0.14)", icon: "terminal" },
  "system tools": { color: "#3b82f6", soft: "rgba(59,130,246,0.14)", icon: "settings" },
  "mcp tools": { color: "#0ea5e9", soft: "rgba(14,165,233,0.14)", icon: "cube" },
  "mcp tools (deferred)": { color: "#0ea5e9", soft: "rgba(14,165,233,0.14)", icon: "cube" },
  "memory files": { color: "#e11d48", soft: "rgba(225,29,72,0.12)", icon: "folder" },
  skills: { color: "#ca8a04", soft: "rgba(202,138,4,0.14)", icon: "sparkles" },
  messages: { color: "#7c3aed", soft: "rgba(124,58,237,0.12)", icon: "message" },
  "free space": { color: "#94a3b8", soft: "rgba(148,163,184,0.12)", icon: "grid" },
};

function categoryTone(name: string): CategoryTone {
  const key = name.toLowerCase().replace(/\s+/g, " ").trim();
  if (CATEGORY_TONES[key]) return CATEGORY_TONES[key];
  if (key.includes("mcp")) return CATEGORY_TONES["mcp tools"];
  if (key.includes("memory")) return CATEGORY_TONES["memory files"];
  if (key.includes("skill")) return CATEGORY_TONES.skills;
  if (key.includes("message")) return CATEGORY_TONES.messages;
  if (key.includes("free")) return CATEGORY_TONES["free space"];
  if (key.includes("tool")) return CATEGORY_TONES["system tools"];
  if (key.includes("prompt")) return CATEGORY_TONES["system prompt"];
  return { color: "var(--aurora-accent)", soft: "var(--aurora-accent-soft)", icon: "inbox" };
}

function shortToolName(tool: string): { primary: string; secondary?: string } {
  const cleaned = tool.replace(/^mcp__/, "");
  const parts = cleaned.split("__");
  if (parts.length >= 2) {
    return { primary: parts.slice(1).join("__"), secondary: parts[0] };
  }
  if (tool.length > 42) return { primary: `${tool.slice(0, 20)}…${tool.slice(-18)}` };
  return { primary: tool };
}

function UsageMosaic({ categories }: { categories: ContextUsageCategory[] }) {
  const cells: Array<{ color: string; free?: boolean }> = [];
  const usable = categories.filter((c) => c.percentage > 0 || /free/i.test(c.name));
  const totalPct = usable.reduce((sum, c) => sum + c.percentage, 0) || 100;
  const TARGET = 84;

  for (const category of usable) {
    const tone = categoryTone(category.name);
    const count = Math.max(
      /free/i.test(category.name) ? 0 : 1,
      Math.round((category.percentage / totalPct) * TARGET),
    );
    const free = /free space/i.test(category.name);
    for (let i = 0; i < count; i += 1) {
      cells.push({ color: tone.color, free });
    }
  }

  while (cells.length < TARGET) cells.push({ color: "#94a3b8", free: true });
  if (cells.length > TARGET) cells.length = TARGET;

  return (
    <div
      aria-hidden="true"
      data-context-mosaic
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(28, minmax(0, 1fr))",
        gap: 2.5,
        flex: "1 1 auto",
        minWidth: 0,
      }}
    >
      {cells.map((cell, idx) => (
        <span
          key={idx}
          style={{
            aspectRatio: "1",
            borderRadius: 2.5,
            background: cell.free ? "transparent" : cell.color,
            border: cell.free
              ? `1.5px solid color-mix(in srgb, ${cell.color} 55%, transparent)`
              : "none",
            opacity: cell.free ? 0.7 : 0.92,
          }}
        />
      ))}
    </div>
  );
}

function CategoryRow({ category }: { category: ContextUsageCategory }) {
  const tone = categoryTone(category.name);
  const isFree = /free space/i.test(category.name);
  return (
    <div
      data-context-category={category.name}
      style={{
        display: "grid",
        gridTemplateColumns: "auto minmax(0, 1fr) auto",
        alignItems: "center",
        gap: 10,
        padding: "7px 0",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 26,
          height: 26,
          borderRadius: 8,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          background: tone.soft,
          color: tone.color,
        }}
      >
        <Icon name={tone.icon} size={13} />
      </span>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontSize: 12, fontWeight: 600, color: "var(--aurora-fg2)" }}>
          {category.name}
        </span>
        <span
          style={{
            display: "block",
            marginTop: 4,
            height: 4,
            borderRadius: 999,
            background: "color-mix(in srgb, var(--aurora-fg4) 18%, transparent)",
            overflow: "hidden",
          }}
        >
          <span
            style={{
              display: "block",
              width: `${Math.min(100, category.percentage)}%`,
              height: "100%",
              borderRadius: 999,
              background: isFree
                ? `repeating-linear-gradient(90deg, ${tone.color} 0 2px, transparent 2px 5px)`
                : tone.color,
              opacity: isFree ? 0.55 : 0.9,
            }}
          />
        </span>
      </span>
      <span
        style={{
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
          whiteSpace: "nowrap",
        }}
      >
        <span style={{ display: "block", fontSize: 11.5, fontWeight: 650, color: "var(--aurora-fg2)" }}>
          {category.tokens}
        </span>
        <span style={{ display: "block", marginTop: 1, fontSize: 10.5, color: "var(--aurora-fg4)" }}>
          {category.percentage.toFixed(1)}%
        </span>
      </span>
    </div>
  );
}

function McpToolRow({ tool }: { tool: ContextMcpTool }) {
  const names = shortToolName(tool.tool);
  return (
    <div
      data-context-mcp-tool={tool.tool}
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) auto",
        gap: 10,
        alignItems: "start",
        padding: "8px 10px",
        borderRadius: 10,
        background: "color-mix(in srgb, var(--aurora-chip) 55%, transparent)",
        border: "1px solid color-mix(in srgb, var(--aurora-border) 80%, transparent)",
      }}
    >
      <span style={{ minWidth: 0 }}>
        <span
          style={{
            display: "block",
            fontSize: 11.5,
            fontWeight: 650,
            color: "var(--aurora-fg2)",
            overflowWrap: "anywhere",
            fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
          }}
        >
          {names.primary}
        </span>
        <span style={{ display: "block", marginTop: 2, fontSize: 10.5, color: "var(--aurora-fg4)" }}>
          {tool.server || names.secondary || "MCP"}
        </span>
      </span>
      <span
        style={{
          fontSize: 10.5,
          fontWeight: 700,
          color: "var(--aurora-fg3)",
          fontVariantNumeric: "tabular-nums",
          whiteSpace: "nowrap",
          padding: "2px 7px",
          borderRadius: 999,
          background: "color-mix(in srgb, var(--aurora-surface-solid) 80%, transparent)",
        }}
      >
        {tool.tokens}
      </span>
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10.5,
        fontWeight: 700,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        color: "var(--aurora-fg4)",
        marginBottom: 4,
      }}
    >
      {children}
    </div>
  );
}

function ContextUsageView({ report }: { report: ContextUsageReport }) {
  const used = report.categories
    .filter((c) => !/free space/i.test(c.name))
    .reduce((sum, c) => sum + c.percentage, 0);
  const totalLabel = report.totalLabel
    || `${used.toFixed(used >= 10 ? 0 : 1)}% used`;

  return (
    <div data-context-usage style={{ display: "grid", gap: 16 }}>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 14,
          alignItems: "center",
          padding: "12px 12px 10px",
          borderRadius: 12,
          background:
            "linear-gradient(135deg, color-mix(in srgb, var(--aurora-accent) 7%, var(--aurora-surface-solid)), color-mix(in srgb, var(--aurora-chip) 70%, transparent))",
          border: "1px solid color-mix(in srgb, var(--aurora-accent) 12%, var(--aurora-border))",
        }}
      >
        <UsageMosaic categories={report.categories} />
        <div style={{ flex: "0 1 150px", minWidth: 120 }}>
          {report.modelLabel && (
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--aurora-fg1)" }}>
              {report.modelLabel}
            </div>
          )}
          {report.modelId && (
            <div
              style={{
                marginTop: 2,
                fontSize: 10.5,
                color: "var(--aurora-fg4)",
                fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
              }}
            >
              {report.modelId}
            </div>
          )}
          <div
            style={{
              marginTop: report.modelLabel || report.modelId ? 6 : 0,
              fontSize: 12,
              fontWeight: 650,
              color: "var(--aurora-fg2)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {totalLabel}
          </div>
        </div>
      </div>

      <div>
        <SectionLabel>Estimated usage by category</SectionLabel>
        <div style={{ display: "grid" }}>
          {report.categories.map((category) => (
            <CategoryRow key={category.name} category={category} />
          ))}
        </div>
      </div>

      {report.mcpTools.length > 0 && (
        <div>
          <SectionLabel>
            MCP tools
            <span style={{ marginLeft: 6, fontWeight: 600, textTransform: "none", letterSpacing: 0 }}>
              · {report.mcpTools.length}
            </span>
          </SectionLabel>
          <div style={{ display: "grid", gap: 7, marginTop: 8 }}>
            {report.mcpTools.map((tool) => (
              <McpToolRow key={`${tool.server}:${tool.tool}`} tool={tool} />
            ))}
          </div>
        </div>
      )}

      {report.suggestion && (
        <div
          data-context-suggestion
          style={{
            display: "grid",
            gridTemplateColumns: "auto minmax(0, 1fr)",
            gap: 10,
            padding: "10px 12px",
            borderRadius: 12,
            background: "color-mix(in srgb, #0ea5e9 8%, var(--aurora-surface-solid))",
            border: "1px solid color-mix(in srgb, #0ea5e9 22%, var(--aurora-border))",
          }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 22,
              height: 22,
              borderRadius: 999,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              background: "color-mix(in srgb, #0ea5e9 16%, transparent)",
              color: "#0284c7",
              fontSize: 12,
              fontWeight: 800,
            }}
          >
            i
          </span>
          <span>
            <span style={{ display: "block", fontSize: 12, fontWeight: 650, color: "var(--aurora-fg2)" }}>
              {report.suggestion.title}
            </span>
            {report.suggestion.detail && (
              <span style={{ display: "block", marginTop: 3, fontSize: 11, color: "var(--aurora-fg4)", lineHeight: 1.45 }}>
                {report.suggestion.detail}
              </span>
            )}
          </span>
        </div>
      )}
    </div>
  );
}

const bodyShellStyle: CSSProperties = {
  maxHeight: "min(50vh, 520px)",
  overflow: "auto",
  padding: "12px 14px",
  borderTop: "1px solid var(--aurora-border)",
  color: "var(--aurora-fg3)",
  background: "color-mix(in srgb, var(--aurora-surface-solid) 88%, transparent)",
  fontSize: 12,
  lineHeight: 1.55,
  overflowWrap: "anywhere",
};

export default function SessionContextBody({
  content,
  preferMarkdown = false,
}: {
  content: string;
  preferMarkdown?: boolean;
}) {
  const report = parseContextUsageReport(content);
  if (report) {
    return (
      <div style={bodyShellStyle} data-context-body="usage">
        <ContextUsageView report={report} />
      </div>
    );
  }

  if (preferMarkdown || looksLikeMarkdownContext(content)) {
    return (
      <div className="prose prose-sm max-w-none" style={bodyShellStyle} data-context-body="markdown">
        <MarkdownViewer content={content} />
      </div>
    );
  }

  return (
    <pre
      data-context-body="raw"
      style={{
        ...bodyShellStyle,
        margin: 0,
        fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
        fontSize: 11,
        lineHeight: 1.5,
        whiteSpace: "pre-wrap",
      }}
    >
      {content}
    </pre>
  );
}
