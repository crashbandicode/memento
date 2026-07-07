"use client";

import { memo, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ConversationMessage, ConversationPrompt } from "@/lib/api-client";
import { useI18n, fmt } from "@/lib/i18n";
import MarkdownViewer from "./MarkdownViewer";
import { Icon } from "@/components/aurora/Icon";

interface Artifact {
  id: string;
  title: string;
  relative_path: string;
  content: string | null;
  file_size_bytes: number;
}

const ANSI_ESCAPE_RE = /\u001B(?:\][^\u0007]*(?:\u0007|\u001B\\)|\[[0-?]*[ -/]*[@-~]|[@-_])|\u009B[0-?]*[ -/]*[@-~]/g;

function cleanTerminalText(value: string): string {
  return value.replace(ANSI_ESCAPE_RE, "");
}

function cleanToolOutput(value: string): string {
  return cleanTerminalText(value).replace(/^\[Result\]\s*/, "").trim();
}

function formatToolText(value: string): string {
  const clean = cleanTerminalText(value).trim();
  if (!clean || !/^[\[{]/.test(clean)) return clean;
  try {
    return JSON.stringify(JSON.parse(clean), null, 2);
  } catch {
    return clean;
  }
}

export default function ConversationViewer({
  documentId,
  totalMessages,
  artifacts,
}: {
  documentId: string;
  totalMessages: number;
  artifacts?: Artifact[];
}) {
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [prompts, setPrompts] = useState<ConversationPrompt[]>([]);
  const [activePromptLine, setActivePromptLine] = useState<number | null>(null);
  const [pendingPromptLine, setPendingPromptLine] = useState<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const offsetRef = useRef(0);
  const loadingRef = useRef(false);
  const { t, locale } = useI18n();

  const loadMore = async () => {
    if (loadingRef.current || !hasMore) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      const res = await api.getMessages(documentId, offsetRef.current, 50);
      if (res.messages.length > 0) {
        setMessages((prev) => {
          const existingIds = new Set(prev.map((m) => m.id));
          const newMsgs = res.messages.filter((m) => !existingIds.has(m.id));
          return [...prev, ...newMsgs];
        });
        offsetRef.current += res.messages.length;
      }
      setHasMore(offsetRef.current < res.total);
    } catch (e) {
      console.error("Failed to load messages:", e);
    } finally {
      setLoading(false);
      loadingRef.current = false;
    }
  };

  useEffect(() => {
    setMessages([]);
    offsetRef.current = 0;
    loadingRef.current = false;
    setHasMore(true);
    setPrompts([]);
    setActivePromptLine(null);
    setPendingPromptLine(null);
    loadMore();
    api.getPrompts(documentId)
      .then((response) => {
        setPrompts(response.prompts);
        setActivePromptLine(response.prompts[0]?.line_number ?? null);
      })
      .catch((error) => console.error("Failed to load prompt outline:", error));
  }, [documentId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (pendingPromptLine === null) return;
    const target = document.getElementById(`conversation-line-${pendingPromptLine}`);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    setPendingPromptLine(null);
  }, [messages, pendingPromptLine]);

  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const promptElements = el.querySelectorAll<HTMLElement>("[data-prompt-line]");
    const containerTop = el.getBoundingClientRect().top;
    let currentLine: number | null = null;
    for (const promptElement of promptElements) {
      if (promptElement.getBoundingClientRect().top - containerTop > 120) break;
      currentLine = Number(promptElement.dataset.promptLine);
    }
    if (currentLine !== null) {
      setActivePromptLine((previous) => previous === currentLine ? previous : currentLine);
    }
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) {
      loadMore();
    }
  };

  const navigateToPrompt = async (prompt: ConversationPrompt) => {
    const anchorId = `conversation-line-${prompt.line_number}`;
    if (!document.getElementById(anchorId) && !loadingRef.current) {
      loadingRef.current = true;
      setLoading(true);
      try {
        const collected: ConversationMessage[] = [];
        let total = totalMessages;
        while (offsetRef.current < prompt.line_number) {
          const remaining = prompt.line_number - offsetRef.current;
          const response = await api.getMessages(
            documentId,
            offsetRef.current,
            Math.min(200, Math.max(50, remaining)),
          );
          total = response.total;
          if (response.messages.length === 0) break;
          collected.push(...response.messages);
          offsetRef.current += response.messages.length;
        }
        if (collected.length > 0) {
          setMessages((previous) => {
            const existingIds = new Set(previous.map((message) => message.id));
            return [
              ...previous,
              ...collected.filter((message) => !existingIds.has(message.id)),
            ];
          });
        }
        setHasMore(offsetRef.current < total);
      } catch (error) {
        console.error("Failed to load prompt target:", error);
      } finally {
        setLoading(false);
        loadingRef.current = false;
      }
    }

    setActivePromptLine(prompt.line_number);
    setPendingPromptLine(prompt.line_number);
  };

  return (
    <div style={{ position: "relative" }}>
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="h-[calc(100vh-8rem)] sm:h-[calc(100vh-10rem)] md:h-[calc(100vh-12rem)] overflow-y-auto"
      >
        <div style={{ fontSize: 11, color: "var(--aurora-fg4)", marginBottom: 16, textAlign: "center" }}>
          {fmt(t.conversation.messagesTotal, { total: totalMessages, loaded: messages.length })}
        </div>

        <div className="space-y-3 max-w-4xl mx-auto pb-8">
          {messages.map((msg, idx) => {
            const isHumanPrompt = (msg.role || msg.message_type) === "user"
              && !msg.content.includes("[Subagent Context]");
            return (
              <div
                key={`${msg.id}-${idx}`}
                id={`conversation-line-${msg.line_number}`}
                data-prompt-line={isHumanPrompt ? msg.line_number : undefined}
                style={{ scrollMarginTop: 16 }}
              >
                <ChatBubble msg={msg} locale={locale} t={t} />
              </div>
            );
          })}

          {artifacts && artifacts.length > 0 && !hasMore && (
            <>
              <div style={{ display: "flex", justifyContent: "center" }}>
                <div
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "4px 12px",
                    borderRadius: 9999,
                    border: "1px solid var(--aurora-border)",
                    background: "rgba(251,191,36,0.12)",
                    color: "#A16207",
                    fontSize: 11,
                    fontWeight: 600,
                  }}
                >
                  <Icon name="file_text" size={12} /> Brain Artifacts ({artifacts.length})
                </div>
              </div>
              {artifacts.map((art) => (
                <ArtifactBubble key={art.id} artifact={art} />
              ))}
            </>
          )}
        </div>

        {loading && (
          <div style={{ textAlign: "center", padding: 12, color: "var(--aurora-fg4)", fontSize: 13 }}>{t.conversation.loadingMore}</div>
        )}
        {!hasMore && messages.length > 0 && (
          <div style={{ textAlign: "center", padding: 12, color: "var(--aurora-fg4)", fontSize: 13 }}>{t.conversation.allLoaded}</div>
        )}
      </div>

      <PromptNavigator
        prompts={prompts}
        activeLine={activePromptLine}
        label={t.conversation.promptNavigator}
        onSelect={navigateToPrompt}
      />
    </div>
  );
}

function promptSnippet(value: string): string {
  return cleanTerminalText(value)
    .replace(/```[\s\S]*?```/g, " code ")
    .replace(/<[^>]+>/g, " ")
    .replace(/[#>*_`~\[\]()]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function PromptNavigator({
  prompts,
  activeLine,
  label,
  onSelect,
}: {
  prompts: ConversationPrompt[];
  activeLine: number | null;
  label: string;
  onSelect: (prompt: ConversationPrompt) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  if (prompts.length === 0) return null;

  return (
    <aside
      data-prompt-navigator
      aria-label={label}
      className="hidden xl:flex"
      onMouseEnter={() => setExpanded(true)}
      onMouseLeave={() => setExpanded(false)}
      onFocus={() => setExpanded(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setExpanded(false);
      }}
      style={{
        position: "absolute",
        top: 34,
        right: -38,
        bottom: 44,
        zIndex: 40,
        width: expanded ? 300 : 28,
        flexDirection: "column",
        border: "1px solid var(--aurora-border)",
        borderRadius: 14,
        background: "color-mix(in srgb, var(--aurora-surface-solid) 92%, transparent)",
        boxShadow: expanded
          ? "0 20px 48px -18px rgba(15,23,42,0.28)"
          : "0 6px 18px -10px rgba(15,23,42,0.3)",
        backdropFilter: "blur(18px)",
        overflow: "hidden",
        transition: "width .18s ease, box-shadow .18s ease",
      }}
    >
      {expanded && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 11px 8px",
            borderBottom: "1px solid var(--aurora-border)",
            color: "var(--aurora-fg3)",
            flex: "0 0 auto",
          }}
        >
          <Icon name="message" size={13} style={{ color: "var(--aurora-accent)" }} />
          <span style={{ fontSize: 11, fontWeight: 650 }}>{label}</span>
          <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--aurora-fg4)" }}>
            {prompts.length}
          </span>
        </div>
      )}

      <div
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          padding: expanded ? "6px" : "8px 7px",
        }}
      >
        {prompts.map((prompt, index) => {
          const snippet = promptSnippet(prompt.content) || `Prompt ${index + 1}`;
          const active = prompt.line_number === activeLine;
          return (
            <button
              key={`${prompt.id}-${prompt.line_number}`}
              type="button"
              data-prompt-item={prompt.line_number}
              title={snippet}
              onClick={() => onSelect(prompt)}
              style={{
                width: "100%",
                minHeight: expanded ? 34 : 10,
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginBottom: expanded ? 2 : 5,
                padding: expanded ? "6px 7px" : 0,
                border: 0,
                borderRadius: expanded ? 8 : 999,
                background: expanded
                  ? active
                    ? "color-mix(in srgb, var(--aurora-accent) 10%, transparent)"
                    : "transparent"
                  : active
                    ? "var(--aurora-accent)"
                    : "var(--aurora-border)",
                color: active ? "var(--aurora-accent)" : "var(--aurora-fg3)",
                textAlign: "left",
                cursor: "pointer",
              }}
            >
              {expanded ? (
                <>
                  <span
                    style={{
                      width: 18,
                      height: 18,
                      borderRadius: 999,
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flex: "0 0 auto",
                      background: active
                        ? "color-mix(in srgb, var(--aurora-accent) 14%, transparent)"
                        : "var(--aurora-chip)",
                      fontSize: 8.5,
                      fontWeight: 700,
                    }}
                  >
                    {index + 1}
                  </span>
                  <span
                    style={{
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      fontSize: 10.5,
                      lineHeight: 1.3,
                    }}
                  >
                    {snippet}
                  </span>
                </>
              ) : (
                <span className="sr-only">{snippet}</span>
              )}
            </button>
          );
        })}
      </div>
    </aside>
  );
}

export const ChatBubble = memo(function ChatBubble({
  msg,
  locale,
  t,
}: {
  msg: ConversationMessage;
  locale: string;
  t: ReturnType<typeof useI18n>["t"];
}) {
  const role = msg.role || msg.message_type || "unknown";
  const toolName = msg.tool_name ?? "";
  const content = cleanTerminalText(msg.content);
  const toolInput = cleanTerminalText(msg.tool_input ?? "");
  const thinking = cleanTerminalText(msg.thinking?.trim() || "");
  const [expanded, setExpanded] = useState(false);
  const [showThinking, setShowThinking] = useState(false);

  // User — right aligned, violet gradient.
  // OpenClaw subagent sessions inject a synthetic "user" message at the top
  // that starts with `[Subagent Context]` — it's the parent agent's task
  // dispatch, not a human chat. Render these as a gray "子任务派发" card
  // (centered, muted) so users don't mistake them for their own chat input.
  if (role === "user") {
    const isSubagentDispatch = content.startsWith("[Subagent Context]")
      || content.includes("\n[Subagent Context]");
    if (isSubagentDispatch) {
      const isLong = content.length > 300;
      const displayContent = isLong && !expanded ? content.slice(0, 300) + "..." : content;
      return (
        <div style={{ display: "flex", justifyContent: "center", margin: "6px 0" }}>
          <div
            style={{
              maxWidth: "92%",
              minWidth: 0,
              padding: "10px 14px",
              borderRadius: 12,
              background: "var(--aurora-chip)",
              border: "1px dashed var(--aurora-border)",
              fontSize: 12,
              lineHeight: 1.5,
              color: "var(--aurora-fg3)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              overflowWrap: "anywhere",
            }}
          >
            <div style={{
              fontSize: 10,
              fontWeight: 600,
              color: "var(--aurora-fg4)",
              textTransform: "uppercase",
              letterSpacing: "0.08em",
              marginBottom: 6,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}>
              <span>{t.conversation.subagentDispatch}</span>
              {msg.timestamp && (
                <span style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0, color: "var(--aurora-fg4)" }}>
                  · {new Date(msg.timestamp).toLocaleString(locale)}
                </span>
              )}
            </div>
            {displayContent}
            {isLong && (
              <button
                onClick={() => setExpanded(!expanded)}
                style={{
                  display: "block",
                  marginTop: 6,
                  fontSize: 11,
                  color: "var(--aurora-accent)",
                  background: "transparent",
                  border: 0,
                  cursor: "pointer",
                  padding: 0,
                }}
              >
                {expanded ? t.conversation.collapse : t.conversation.expandAll}
              </button>
            )}
          </div>
        </div>
      );
    }

    const isLong = content.length > 500;
    const displayContent = isLong && !expanded ? content.slice(0, 500) + "..." : content;
    return (
      <div style={{ display: "flex", justifyContent: "flex-start" }}>
        <div style={{ width: "100%", minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, marginBottom: 5, alignItems: "center", padding: "0 4px" }}>
            <span
              aria-hidden="true"
              style={{
                width: 18,
                height: 18,
                borderRadius: 999,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                background: "color-mix(in srgb, var(--aurora-accent) 14%, transparent)",
                color: "var(--aurora-accent)",
                fontSize: 9,
                fontWeight: 700,
              }}
            >
              Y
            </span>
            <span style={{ fontSize: 10.5, fontWeight: 600, color: "var(--aurora-accent)" }}>You</span>
            {msg.timestamp && (
              <span style={{ fontSize: 10.5, color: "var(--aurora-fg4)" }}>
                {new Date(msg.timestamp).toLocaleString(locale)}
              </span>
            )}
          </div>
          <div
            style={{
              padding: "12px 16px",
              borderRadius: 12,
              background: "color-mix(in srgb, var(--aurora-accent) 5%, var(--aurora-surface-solid))",
              color: "var(--aurora-fg1)",
              fontSize: 13.5,
              lineHeight: 1.55,
              letterSpacing: "-0.005em",
              wordBreak: "break-word",
              overflowWrap: "anywhere",
              border: "1px solid color-mix(in srgb, var(--aurora-accent) 20%, var(--aurora-border))",
              boxShadow: "0 1px 2px rgba(15,23,42,0.025)",
            }}
          >
            <div className="prose prose-sm max-w-none">
              <MarkdownViewer content={displayContent} />
            </div>
            {isLong && (
              <button
                onClick={() => setExpanded(!expanded)}
                style={{
                  display: "block",
                  marginTop: 6,
                  fontSize: 11,
                  color: "var(--aurora-accent)",
                  background: "transparent",
                  border: 0,
                  cursor: "pointer",
                }}
              >
                {expanded ? t.conversation.collapse : t.conversation.expandAll}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  // Assistant — a quiet neutral surface keeps each response visually bounded
  // without competing with the stronger accent used for human prompts.
  if (role === "assistant") {
    const isLong = content.length > 500;
    const displayContent = isLong && !expanded ? content.slice(0, 500) + "..." : content;
    const hasSeparateThinking = Boolean(thinking && thinking !== content.trim());

    return (
      <div style={{ display: "flex", justifyContent: "flex-start" }}>
        <div style={{ width: "100%", minWidth: 0, padding: "3px 4px 8px" }}>
          <div style={{ display: "flex", gap: 8, marginBottom: 5, alignItems: "center", padding: "0 4px" }}>
            <span
              aria-hidden="true"
              style={{
                width: 18,
                height: 18,
                borderRadius: 999,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                background: "color-mix(in srgb, #10B981 13%, transparent)",
                color: "#059669",
                fontSize: 9,
                fontWeight: 700,
              }}
            >
              A
            </span>
            <span style={{ fontSize: 10.5, fontWeight: 600, color: "#10B981" }}>Assistant</span>
            {msg.timestamp && (
              <span style={{ fontSize: 10.5, color: "var(--aurora-fg4)" }}>
                {new Date(msg.timestamp).toLocaleString(locale)}
              </span>
            )}
          </div>
          <div
            style={{
              padding: "12px 16px",
              color: "var(--aurora-fg1)",
              fontSize: 13.5,
              lineHeight: 1.55,
              letterSpacing: "-0.005em",
              background: "color-mix(in srgb, var(--aurora-chip) 34%, var(--aurora-surface-solid))",
              border: "1px solid var(--aurora-border)",
              borderRadius: 12,
              boxShadow: "0 1px 2px rgba(15,23,42,0.025)",
            }}
          >
            <div className="prose prose-sm max-w-none">
              <MarkdownViewer content={displayContent} />
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 8 }}>
              {isLong && (
                <button
                  onClick={() => setExpanded(!expanded)}
                  style={{ fontSize: 11, color: "var(--aurora-accent)", background: "transparent", border: 0, cursor: "pointer", textDecoration: "underline" }}
                >
                  {expanded ? t.conversation.collapse : t.conversation.expandAll}
                </button>
              )}
              {hasSeparateThinking && (
                <button
                  onClick={() => setShowThinking((v) => !v)}
                  style={{ fontSize: 11, color: "#D97706", background: "transparent", border: 0, cursor: "pointer", textDecoration: "underline" }}
                >
                  {showThinking ? t.conversation.hideThinking : t.conversation.showThinking}
                </button>
              )}
            </div>
            {showThinking && hasSeparateThinking && (
              <div
                style={{
                  marginTop: 12,
                  borderRadius: 12,
                  border: "1px solid var(--aurora-border)",
                  background: "rgba(251,191,36,0.08)",
                  padding: "10px 12px",
                }}
              >
                <div style={{ marginBottom: 6, fontSize: 10.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em", color: "#D97706" }}>
                  {t.conversation.thinking}
                </div>
                <div className="prose prose-sm max-w-none" style={{ color: "#78350F" }}>
                  <MarkdownViewer content={thinking} />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // Tool use — compact SpecStory-style accordion. Tool input and terminal
  // output stay collapsed until requested, keeping long agent sessions easy
  // to scan while retaining every detail.
  if (role === "tool") {
    const toolLabel = toolName || "Tool result";
    const cleanedOutput = cleanToolOutput(content);
    // Parser placeholders identify a standalone invocation but contain no
    // actual output. Keep those as quiet one-line context rather than opening
    // an accordion whose only content repeats the tool name.
    const output = cleanedOutput === `[${toolLabel}]` ? "" : cleanedOutput;
    const formattedInput = formatToolText(toolInput);
    const formattedOutput = formatToolText(output);
    const previewSource = formattedInput || formattedOutput;
    const preview = previewSource.replace(/\s+/g, " ").trim();
    const hasDetails = Boolean(formattedInput || formattedOutput);

    return (
      <div style={{ display: "flex", justifyContent: "flex-start", margin: "2px 4px" }}>
        <div
          style={{
            width: expanded ? "100%" : "fit-content",
            minWidth: expanded ? 0 : 240,
            background: "var(--aurora-surface-solid)",
            border: "1px solid var(--aurora-border)",
            borderRadius: 10,
            color: "var(--aurora-fg1)",
            maxWidth: "100%",
            overflow: "hidden",
            boxShadow: "0 1px 2px rgba(15,23,42,0.03)",
          }}
        >
          <button
            type="button"
            data-conversation-tool={toolLabel}
            aria-expanded={expanded}
            onClick={() => hasDetails && setExpanded(!expanded)}
            style={{
              width: "100%",
              minHeight: 38,
              display: "flex",
              alignItems: "center",
              gap: 9,
              padding: "8px 12px",
              border: 0,
              background: "transparent",
              color: "inherit",
              cursor: hasDetails ? "pointer" : "default",
              textAlign: "left",
            }}
          >
            <Icon name="terminal" size={13} style={{ color: "#F97316", flex: "0 0 auto" }} />
            <span style={{ fontWeight: 600, fontSize: 12, whiteSpace: "nowrap" }}>{toolLabel}</span>
            {!expanded && preview && (
              <span
                title={preview}
                style={{
                  minWidth: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  color: "var(--aurora-fg4)",
                  fontFamily: "ui-monospace,SFMono-Regular,Consolas,monospace",
                  fontSize: 10.5,
                }}
              >
                {preview}
              </span>
            )}
            {hasDetails && (
              <span style={{ marginLeft: "auto", display: "inline-flex", color: "var(--aurora-fg4)" }}>
                <Icon
                  name="chevron_down"
                  size={13}
                  style={{
                    transform: expanded ? "rotate(180deg)" : "none",
                    transition: "transform .15s ease",
                  }}
                />
              </span>
            )}
          </button>

          {expanded && hasDetails && (
            <div style={{ borderTop: "1px solid var(--aurora-border)", padding: "11px 12px 12px" }}>
              {formattedInput && (
                <ToolCodeBlock label="Input" value={formattedInput} />
              )}
              {formattedOutput && (
                <ToolCodeBlock label="Output" value={formattedOutput} topSpacing={Boolean(formattedInput)} />
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // System — centered amber
  return (
    <div style={{ display: "flex", justifyContent: "center" }}>
      <div
        style={{
          background: "rgba(251,191,36,0.08)",
          border: "1px solid var(--aurora-border)",
          borderRadius: 12,
          padding: "6px 12px",
          fontSize: 12,
          color: "#A16207",
          maxWidth: "80%",
        }}
      >
        <span style={{ fontWeight: 600 }}>System: </span>
        {content.length > 200 ? content.slice(0, 200) + "..." : content}
      </div>
    </div>
  );
});

function ToolCodeBlock({
  label,
  value,
  topSpacing = false,
}: {
  label: string;
  value: string;
  topSpacing?: boolean;
}) {
  return (
    <div style={{ marginTop: topSpacing ? 12 : 0 }}>
      <div
        style={{
          marginBottom: 5,
          color: "var(--aurora-fg4)",
          fontSize: 9.5,
          fontWeight: 700,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
        }}
      >
        {label}
      </div>
      <pre
        style={{
          margin: 0,
          padding: "10px 11px",
          maxHeight: 320,
          overflow: "auto",
          whiteSpace: "pre",
          background: "color-mix(in srgb, var(--aurora-chip) 58%, var(--aurora-surface-solid))",
          border: "1px solid var(--aurora-border)",
          borderRadius: 8,
          color: "var(--aurora-fg2)",
          fontFamily: "ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace",
          fontSize: 11.5,
          lineHeight: 1.55,
        }}
      >
        {value}
      </pre>
    </div>
  );
}

function ArtifactBubble({ artifact }: { artifact: Artifact }) {
  const [expanded, setExpanded] = useState(false);
  const { t } = useI18n();
  return (
    <div style={{ display: "flex", justifyContent: "center" }}>
      <div style={{ maxWidth: "90%", width: "100%" }}>
        <div
          style={{
            border: "1px solid var(--aurora-border)",
            borderRadius: 14,
            overflow: "hidden",
            background: "var(--aurora-accent-soft)",
          }}
        >
          <button
            onClick={() => setExpanded(!expanded)}
            style={{
              width: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "10px 14px",
              textAlign: "left",
              background: "transparent",
              border: 0,
              cursor: "pointer",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div
                style={{
                  width: 28, height: 28, borderRadius: 8,
                  background: "var(--aurora-accent)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}
              >
                <Icon name="file_text" size={13} style={{ color: "#fff" }} />
              </div>
              <span style={{ fontSize: 13, fontWeight: 500, color: "var(--aurora-fg1)", letterSpacing: "-0.01em" }}>
                {artifact.title}
              </span>
              <span style={{ fontSize: 11, color: "var(--aurora-fg4)" }}>
                {(artifact.file_size_bytes / 1024).toFixed(1)}KB
              </span>
            </div>
            <Icon
              name="chevron_down"
              size={14}
              style={{
                color: "var(--aurora-fg4)",
                transform: expanded ? "rotate(180deg)" : "none",
                transition: "transform .15s",
              }}
            />
          </button>
          {expanded && artifact.content && (
            <div
              style={{
                padding: "12px 16px",
                borderTop: "1px solid var(--aurora-border)",
                maxHeight: 400,
                overflowY: "auto",
                background: "var(--aurora-surface-solid)",
              }}
            >
              <MarkdownViewer content={cleanTerminalText(artifact.content)} />
            </div>
          )}
          {expanded && !artifact.content && (
            <div
              style={{
                padding: "12px 16px",
                borderTop: "1px solid var(--aurora-border)",
                background: "var(--aurora-surface-solid)",
              }}
            >
              <Link
                href={`/documents/${artifact.id}`}
                style={{ fontSize: 13, color: "var(--aurora-accent)", fontWeight: 500, textDecoration: "none" }}
              >
                {t.common.viewFullDocument}
              </Link>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
