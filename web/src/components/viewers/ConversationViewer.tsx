"use client";

import {
  memo,
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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

type AssistantContentSegment =
  | { type: "text"; content: string }
  | { type: "tool"; name: string; input: string };

const STANDALONE_REDACTED_LINE_RE = /(^|\r?\n)[ \t]*\[REDACTED\][ \t]*(?=\r?\n|$)/gi;
const FLATTENED_TOOL_MARKER_RE = /(^|\r?\n)\[Tool:\s*([^\]\r\n]{1,120})\][ \t]*(?:\r?\n|$)/g;

function stripCursorTransportRedaction(value: string): string {
  const beginsWithTransportLine = /^[ \t]*\[REDACTED\][ \t]*(?:\r?\n|$)/i.test(value);
  const cleaned = value.replace(STANDALONE_REDACTED_LINE_RE, "$1");
  return beginsWithTransportLine ? cleaned.replace(/^\r?\n/, "") : cleaned;
}

/**
 * Cursor serializes structured assistant tool calls into the stored display
 * text. Recover that structure for presentation without altering the source
 * transcript or hiding ordinary mentions of `[Tool: ...]` in prose.
 */
export function splitAssistantContent(value: string): AssistantContentSegment[] {
  const withoutTransportRedaction = stripCursorTransportRedaction(value);
  const originalMarker = new RegExp(FLATTENED_TOOL_MARKER_RE.source, "g");
  if (!originalMarker.test(withoutTransportRedaction)) {
    return withoutTransportRedaction.trim()
      ? [{ type: "text", content: withoutTransportRedaction }]
      : [];
  }

  const content = withoutTransportRedaction.trimEnd();
  const marker = new RegExp(FLATTENED_TOOL_MARKER_RE.source, "g");
  const matches = Array.from(content.matchAll(marker));

  if (matches.length === 0) {
    return [{ type: "text", content: value }];
  }

  const segments: AssistantContentSegment[] = [];
  const leadingText = content
    .slice(0, matches[0].index ?? 0)
    .replace(/\r?\n$/, "");
  if (leadingText.trim()) segments.push({ type: "text", content: leadingText });

  for (const [index, match] of matches.entries()) {
    const start = (match.index ?? 0) + match[0].length;
    const end = matches[index + 1]?.index ?? content.length;
    const input = content.slice(start, end).trim();
    // Parser-generated Cursor tool inputs are serialized JSON. If a legacy
    // payload is ambiguous (for example prose follows a raw string input),
    // leave the complete assistant message untouched instead of hiding or
    // reordering user-visible text inside a collapsed row.
    if (input) {
      try {
        JSON.parse(input);
      } catch {
        return [{ type: "text", content: value }];
      }
    }
    segments.push({
      type: "tool",
      name: match[2].trim(),
      input,
    });
  }
  return segments;
}

function toolPreview(toolName: string, input: string, output: string): string {
  const fallback = (input || output).replace(/\s+/g, " ").trim().slice(0, 240);
  if (!input.trim().startsWith("{")) return fallback;

  try {
    const parsed = JSON.parse(input) as Record<string, unknown>;
    const normalizedName = toolName.toLowerCase();
    if (normalizedName === "todowrite" && Array.isArray(parsed.todos)) {
      const counts = new Map<string, number>();
      parsed.todos.forEach((todo) => {
        if (!todo || typeof todo !== "object") return;
        const status = String((todo as Record<string, unknown>).status || "task");
        counts.set(status, (counts.get(status) || 0) + 1);
      });
      const statusSummary = Array.from(counts)
        .map(([status, count]) => `${count} ${status.replaceAll("_", " ")}`)
        .join(" · ");
      return `${parsed.todos.length} tasks${statusSummary ? ` · ${statusSummary}` : ""}`.slice(0, 240);
    }

    const preferredKeys = normalizedName === "shell"
      ? ["description", "command"]
      : ["path", "file_path", "query", "pattern", "description", "command", "url"];
    for (const key of preferredKeys) {
      const candidate = parsed[key];
      if (typeof candidate === "string" && candidate.trim()) return candidate.trim().slice(0, 240);
    }
  } catch {
    // Keep the compact raw fallback when a legacy payload is not valid JSON.
  }
  return fallback;
}

const MESSAGE_PAGE_SIZE = 50;
const PROMPT_JUMP_CONTEXT_BEFORE = 12;
const PROMPT_JUMP_WINDOW_SIZE = 120;

export default function ConversationViewer({
  documentId,
  prompts,
  toolId,
  totalMessages,
  artifacts,
}: {
  documentId: string;
  prompts: ConversationPrompt[];
  toolId?: string;
  totalMessages?: number;
  artifacts?: Artifact[];
}) {
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [knownTotal, setKnownTotal] = useState(totalMessages);
  const [activePromptLine, setActivePromptLine] = useState<number | null>(null);
  const [pendingPromptLine, setPendingPromptLine] = useState<number | null>(null);
  const [navigatingPromptLine, setNavigatingPromptLine] = useState<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const startOffsetRef = useRef(0);
  const offsetRef = useRef(0);
  const loadingRef = useRef(false);
  const { t, locale } = useI18n();

  const loadMore = async ({ force = false }: { force?: boolean } = {}) => {
    if (loadingRef.current || (!force && !hasMore)) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      const res = await api.getMessages(documentId, offsetRef.current, MESSAGE_PAGE_SIZE);
      setKnownTotal(res.total);
      if (res.messages.length > 0) {
        if (messages.length === 0) startOffsetRef.current = res.offset;
        setMessages((prev) => {
          const existingIds = new Set(prev.map((m) => m.id));
          const newMsgs = res.messages.filter((m) => !existingIds.has(m.id));
          return [...prev, ...newMsgs];
        });
        offsetRef.current = Math.max(offsetRef.current, res.offset + res.messages.length);
      }
      setHasMore(offsetRef.current < res.total);
      setHasEarlier(startOffsetRef.current > 0);
    } catch (e) {
      console.error("Failed to load messages:", e);
    } finally {
      setLoading(false);
      loadingRef.current = false;
    }
  };

  const loadEarlier = async () => {
    if (loadingRef.current || !hasEarlier) return;
    const previousStart = startOffsetRef.current;
    const nextOffset = Math.max(0, previousStart - MESSAGE_PAGE_SIZE);
    const nextLimit = previousStart - nextOffset;
    if (nextLimit <= 0) return;

    const el = containerRef.current;
    const previousScrollHeight = el?.scrollHeight ?? 0;
    loadingRef.current = true;
    setLoading(true);
    try {
      const res = await api.getMessages(documentId, nextOffset, nextLimit);
      setKnownTotal(res.total);
      if (res.messages.length > 0) {
        startOffsetRef.current = res.offset;
        setMessages((prev) => {
          const existingIds = new Set(prev.map((message) => message.id));
          return [
            ...res.messages.filter((message) => !existingIds.has(message.id)),
            ...prev,
          ];
        });
        window.requestAnimationFrame(() => {
          if (!el) return;
          el.scrollTop += el.scrollHeight - previousScrollHeight;
        });
      }
      setHasEarlier(startOffsetRef.current > 0);
      setHasMore(offsetRef.current < res.total);
    } catch (error) {
      console.error("Failed to load earlier messages:", error);
    } finally {
      setLoading(false);
      loadingRef.current = false;
    }
  };

  useEffect(() => {
    setMessages([]);
    startOffsetRef.current = 0;
    offsetRef.current = 0;
    loadingRef.current = false;
    setHasMore(true);
    setHasEarlier(false);
    setKnownTotal(totalMessages);
    setActivePromptLine(null);
    setPendingPromptLine(null);
    setNavigatingPromptLine(null);
    loadMore({ force: true });
  }, [documentId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (typeof totalMessages === "number") setKnownTotal(totalMessages);
  }, [totalMessages]);

  useEffect(() => {
    setActivePromptLine((previous) => {
      if (previous !== null && prompts.some((prompt) => prompt.line_number === previous)) return previous;
      return prompts[0]?.line_number ?? null;
    });
  }, [prompts]);

  useEffect(() => {
    if (pendingPromptLine === null) return;
    const target = document.getElementById(`conversation-line-${pendingPromptLine}`);
    if (!target) {
      setPendingPromptLine(null);
      setNavigatingPromptLine(null);
      return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    const timeout = window.setTimeout(() => {
      setPendingPromptLine(null);
      setNavigatingPromptLine(null);
    }, 650);
    return () => window.clearTimeout(timeout);
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
    setNavigatingPromptLine(prompt.line_number);
    if (!document.getElementById(anchorId) && loadingRef.current) {
      await new Promise<void>((resolve) => {
        const startedAt = Date.now();
        const check = () => {
          if (!loadingRef.current || Date.now() - startedAt > 10_000) resolve();
          else window.setTimeout(check, 50);
        };
        check();
      });
      if (loadingRef.current) {
        setNavigatingPromptLine(null);
        return;
      }
    }
    if (!document.getElementById(anchorId)) {
      loadingRef.current = true;
      setLoading(true);
      try {
        const response = await api.getMessagesAround(
          documentId,
          prompt.line_number,
          PROMPT_JUMP_CONTEXT_BEFORE,
          PROMPT_JUMP_WINDOW_SIZE,
        );
        setKnownTotal(response.total);
        startOffsetRef.current = response.offset;
        offsetRef.current = response.offset + response.messages.length;
        setMessages(response.messages);
        setHasEarlier(response.offset > 0);
        setHasMore(offsetRef.current < response.total);
      } catch (error) {
        console.error("Failed to load prompt target:", error);
        setNavigatingPromptLine(null);
        return;
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
        data-conversation-viewer
        data-loaded-messages={messages.length}
        data-has-earlier={hasEarlier ? "true" : "false"}
        onScroll={handleScroll}
        className="h-[calc(100vh-8rem)] sm:h-[calc(100vh-10rem)] md:h-[calc(100vh-12rem)] overflow-y-auto"
      >
        <div style={{ fontSize: 11, color: "var(--aurora-fg4)", marginBottom: 16, textAlign: "center" }}>
          {fmt(t.conversation.messagesTotal, { total: knownTotal ?? "…", loaded: messages.length })}
        </div>

        <div className="space-y-3 max-w-4xl mx-auto pb-24 xl:pb-8">
          {hasEarlier && (
            <div style={{ display: "flex", justifyContent: "center" }}>
              <button
                type="button"
                data-load-earlier-messages
                onClick={loadEarlier}
                disabled={loading}
                style={{
                  padding: "7px 12px",
                  borderRadius: 999,
                  border: "1px solid var(--aurora-border)",
                  background: "var(--aurora-chip)",
                  color: "var(--aurora-fg3)",
                  fontSize: 12,
                  cursor: loading ? "wait" : "pointer",
                  opacity: loading ? 0.7 : 1,
                }}
              >
                {t.conversation.loadEarlier}
              </button>
            </div>
          )}

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
                <ChatBubble msg={msg} toolId={toolId} locale={locale} t={t} />
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
        {!hasMore && !hasEarlier && messages.length > 0 && (
          <div style={{ textAlign: "center", padding: 12, color: "var(--aurora-fg4)", fontSize: 13 }}>{t.conversation.allLoaded}</div>
        )}
      </div>

      <PromptNavigator
        key={documentId}
        prompts={prompts}
        activeLine={activePromptLine}
        loadingLine={navigatingPromptLine}
        label={t.conversation.promptNavigator}
        loadingLabel={t.loading}
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
  loadingLine,
  label,
  loadingLabel,
  onSelect,
}: {
  prompts: ConversationPrompt[];
  activeLine: number | null;
  loadingLine: number | null;
  label: string;
  loadingLabel: string;
  onSelect: (prompt: ConversationPrompt) => void | Promise<void>;
}) {
  const { t: translations } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [query, setQuery] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const promptItems = useMemo(
    () => prompts.map((prompt, index) => ({
      prompt,
      index,
      snippet: promptSnippet(prompt.content) || fmt(
        translations.conversation.promptFallback,
        { number: index + 1 },
      ),
    })),
    [prompts, translations.conversation.promptFallback],
  );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filteredPromptItems = normalizedQuery
    ? promptItems.filter(({ snippet }) => snippet.toLocaleLowerCase().includes(normalizedQuery))
    : promptItems;
  const activeIndex = prompts.findIndex((prompt) => prompt.line_number === activeLine);

  useEffect(() => {
    if (!mobileOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const animationFrame = window.requestAnimationFrame(() => searchRef.current?.focus());
    const desktopBreakpoint = window.matchMedia("(min-width: 1280px)");
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMobileOpen(false);
      setQuery("");
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    };
    const handleDesktopBreakpoint = (event: MediaQueryListEvent) => {
      if (!event.matches) return;
      setMobileOpen(false);
      setQuery("");
    };
    window.addEventListener("keydown", handleEscape);
    desktopBreakpoint.addEventListener("change", handleDesktopBreakpoint);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      window.removeEventListener("keydown", handleEscape);
      desktopBreakpoint.removeEventListener("change", handleDesktopBreakpoint);
      document.body.style.overflow = previousOverflow;
    };
  }, [mobileOpen]);

  const closeMobileSheet = () => {
    setMobileOpen(false);
    setQuery("");
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const handleMobileDialogKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable?.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  if (prompts.length === 0) return null;

  return (
    <>
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
        {promptItems.map(({ prompt, index, snippet }) => {
          const active = prompt.line_number === activeLine;
          const isLoading = prompt.line_number === loadingLine;
          return (
            <button
              key={`${prompt.id}-${prompt.line_number}`}
              type="button"
              data-prompt-item={prompt.line_number}
              title={snippet}
              onClick={() => onSelect(prompt)}
              disabled={loadingLine !== null}
              aria-busy={isLoading}
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
                cursor: loadingLine !== null ? "wait" : "pointer",
                opacity: loadingLine !== null && !isLoading ? 0.55 : 1,
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
                    {isLoading ? (
                      <Icon name="refresh" size={10} className="animate-spin" />
                    ) : (
                      index + 1
                    )}
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
                    {isLoading ? loadingLabel : snippet}
                  </span>
                </>
              ) : isLoading ? (
                <Icon name="refresh" size={10} className="animate-spin" style={{ color: "#fff" }} />
              ) : (
                <span className="sr-only">{snippet}</span>
              )}
            </button>
          );
        })}
        </div>
      </aside>

      <button
        ref={triggerRef}
        type="button"
        data-mobile-prompt-trigger
        className={`${mobileOpen ? "hidden" : "inline-flex"} xl:hidden`}
        aria-haspopup="dialog"
        aria-expanded={mobileOpen}
        aria-controls="mobile-prompt-navigator"
        aria-busy={loadingLine !== null}
        disabled={loadingLine !== null}
        onClick={() => setMobileOpen(true)}
        style={{
          position: "fixed",
          right: 16,
          bottom: "calc(14px + env(safe-area-inset-bottom))",
          zIndex: 24,
          minHeight: 46,
          maxWidth: "calc(100vw - 32px)",
          alignItems: "center",
          gap: 9,
          padding: "9px 12px",
          border: "1px solid color-mix(in srgb, var(--aurora-accent) 24%, var(--aurora-border))",
          borderRadius: 999,
          background: "color-mix(in srgb, var(--aurora-surface-solid) 94%, transparent)",
          color: "var(--aurora-fg1)",
          boxShadow: "0 12px 32px -12px rgba(15,23,42,0.38)",
          backdropFilter: "blur(18px)",
          cursor: loadingLine !== null ? "wait" : "pointer",
        }}
      >
        <span
          style={{
            width: 28,
            height: 28,
            borderRadius: 999,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            flex: "0 0 auto",
            background: "var(--aurora-accent)",
            color: "#fff",
          }}
        >
          <Icon
            name={loadingLine !== null ? "refresh" : "message"}
            size={13}
            className={loadingLine !== null ? "animate-spin" : undefined}
          />
        </span>
        <span
          style={{
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            fontSize: 12,
            fontWeight: 650,
          }}
        >
          {loadingLine !== null ? loadingLabel : label}
        </span>
        <span
          style={{
            flex: "0 0 auto",
            padding: "2px 7px",
            borderRadius: 999,
            background: "var(--aurora-chip)",
            color: "var(--aurora-fg3)",
            fontSize: 10,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {activeIndex >= 0 ? activeIndex + 1 : 1}/{prompts.length}
        </span>
      </button>

      {mobileOpen && (
        <div
          className="xl:hidden"
          style={{ position: "fixed", inset: 0, zIndex: 120 }}
        >
          <button
            type="button"
            data-mobile-prompt-backdrop
            tabIndex={-1}
            aria-label={translations.conversation.closePromptNavigator}
            onClick={closeMobileSheet}
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              padding: 0,
              border: 0,
              background: "rgba(15,23,42,0.42)",
              backdropFilter: "blur(4px)",
              touchAction: "none",
            }}
          />
          <section
            ref={dialogRef}
            id="mobile-prompt-navigator"
            data-mobile-prompt-sheet
            role="dialog"
            aria-modal="true"
            aria-labelledby="mobile-prompt-navigator-title"
            onKeyDown={handleMobileDialogKeyDown}
            style={{
              position: "absolute",
              left: "50%",
              bottom: 0,
              width: "100%",
              maxWidth: 640,
              maxHeight: "min(78dvh, 680px)",
              display: "flex",
              flexDirection: "column",
              transform: "translateX(-50%)",
              border: "1px solid var(--aurora-border)",
              borderBottom: 0,
              borderRadius: "22px 22px 0 0",
              background: "var(--aurora-surface-solid)",
              color: "var(--aurora-fg1)",
              boxShadow: "0 -24px 70px -30px rgba(15,23,42,0.55)",
              overflow: "hidden",
            }}
          >
            <div
              aria-hidden="true"
              style={{
                width: 42,
                height: 4,
                margin: "8px auto 2px",
                borderRadius: 999,
                background: "var(--aurora-border)",
                flex: "0 0 auto",
              }}
            />
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "8px 14px 10px 16px",
                borderBottom: "1px solid var(--aurora-border)",
                flex: "0 0 auto",
              }}
            >
              <span
                style={{
                  width: 32,
                  height: 32,
                  borderRadius: 10,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  background: "var(--aurora-accent-soft)",
                  color: "var(--aurora-accent)",
                  flex: "0 0 auto",
                }}
              >
                <Icon name="message" size={15} />
              </span>
              <div style={{ minWidth: 0 }}>
                <div id="mobile-prompt-navigator-title" style={{ fontSize: 14, fontWeight: 700 }}>
                  {label}
                </div>
                <div style={{ marginTop: 1, color: "var(--aurora-fg4)", fontSize: 10.5 }}>
                  {filteredPromptItems.length === prompts.length
                    ? fmt(translations.conversation.promptCount, { count: prompts.length })
                    : fmt(translations.conversation.filteredPromptCount, {
                        visible: filteredPromptItems.length,
                        total: prompts.length,
                      })}
                </div>
              </div>
              <button
                type="button"
                data-mobile-prompt-close
                aria-label={translations.conversation.closePromptNavigator}
                onClick={closeMobileSheet}
                style={{
                  width: 40,
                  height: 40,
                  marginLeft: "auto",
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flex: "0 0 auto",
                  border: "1px solid var(--aurora-border)",
                  borderRadius: 12,
                  background: "var(--aurora-chip)",
                  color: "var(--aurora-fg3)",
                  cursor: "pointer",
                }}
              >
                <Icon name="close" size={15} />
              </button>
            </div>

            <div style={{ padding: "10px 14px", flex: "0 0 auto" }}>
              <label
                style={{
                  minHeight: 42,
                  display: "flex",
                  alignItems: "center",
                  gap: 9,
                  padding: "8px 11px",
                  border: "1px solid var(--aurora-border)",
                  borderRadius: 12,
                  background: "var(--aurora-chip)",
                  color: "var(--aurora-fg4)",
                }}
              >
                <Icon name="search" size={14} style={{ flex: "0 0 auto" }} />
                <span className="sr-only">
                  {fmt(translations.conversation.searchPrompts, { count: prompts.length })}
                </span>
                <input
                  ref={searchRef}
                  data-mobile-prompt-search
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={fmt(translations.conversation.searchPrompts, { count: prompts.length })}
                  autoComplete="off"
                  style={{
                    width: "100%",
                    minWidth: 0,
                    padding: 0,
                    border: 0,
                    outline: 0,
                    background: "transparent",
                    color: "var(--aurora-fg1)",
                    fontSize: 16,
                  }}
                />
              </label>
            </div>

            <div
              style={{
                flex: 1,
                minHeight: 0,
                overflowY: "auto",
                overscrollBehavior: "contain",
                padding: "0 10px calc(12px + env(safe-area-inset-bottom))",
              }}
            >
              {filteredPromptItems.length === 0 ? (
                <div
                  data-mobile-prompt-empty
                  style={{ padding: "34px 18px", textAlign: "center", color: "var(--aurora-fg4)", fontSize: 12 }}
                >
                  {translations.conversation.noMatchingPrompts}
                </div>
              ) : (
                filteredPromptItems.map(({ prompt, index, snippet }) => {
                  const active = prompt.line_number === activeLine;
                  const isLoading = prompt.line_number === loadingLine;
                  return (
                    <button
                      key={`${prompt.id}-${prompt.line_number}`}
                      type="button"
                      data-mobile-prompt-item={prompt.line_number}
                      aria-current={active ? "true" : undefined}
                      aria-busy={isLoading}
                      disabled={loadingLine !== null}
                      onClick={async () => {
                        await onSelect(prompt);
                        if (dialogRef.current) closeMobileSheet();
                      }}
                      style={{
                        width: "100%",
                        minHeight: 54,
                        display: "flex",
                        alignItems: "center",
                        gap: 11,
                        marginBottom: 5,
                        padding: "9px 10px",
                        border: active
                          ? "1px solid color-mix(in srgb, var(--aurora-accent) 24%, var(--aurora-border))"
                          : "1px solid transparent",
                        borderRadius: 12,
                        background: active
                          ? "color-mix(in srgb, var(--aurora-accent) 8%, var(--aurora-chip))"
                          : "transparent",
                        color: active ? "var(--aurora-accent)" : "var(--aurora-fg2)",
                        textAlign: "left",
                        cursor: loadingLine !== null ? "wait" : "pointer",
                        opacity: loadingLine !== null && !isLoading ? 0.5 : 1,
                        contentVisibility: "auto",
                        containIntrinsicSize: "54px",
                      }}
                    >
                      <span
                        style={{
                          width: 28,
                          height: 28,
                          borderRadius: 999,
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          flex: "0 0 auto",
                          background: active ? "var(--aurora-accent)" : "var(--aurora-chip)",
                          color: active ? "#fff" : "var(--aurora-fg3)",
                          fontSize: 9.5,
                          fontWeight: 750,
                          fontVariantNumeric: "tabular-nums",
                        }}
                      >
                        {isLoading ? (
                          <Icon name="refresh" size={12} className="animate-spin" />
                        ) : (
                          index + 1
                        )}
                      </span>
                      <span
                        style={{
                          minWidth: 0,
                          display: "-webkit-box",
                          overflow: "hidden",
                          WebkitBoxOrient: "vertical",
                          WebkitLineClamp: 2,
                          fontSize: 12.5,
                          lineHeight: 1.35,
                        }}
                      >
                        {isLoading ? loadingLabel : snippet}
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          </section>
        </div>
      )}
    </>
  );
}

function ConversationToolCard({
  name,
  input = "",
  output = "",
}: {
  name: string;
  input?: string;
  output?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const toolLabel = name || "Tool result";
  const cleanedOutput = cleanToolOutput(output);
  const visibleOutput = cleanedOutput === `[${toolLabel}]` ? "" : cleanedOutput;
  const preview = toolPreview(toolLabel, input, visibleOutput);
  const hasDetails = Boolean(input.trim() || visibleOutput);
  const formattedInput = expanded ? formatToolText(input) : "";
  const formattedOutput = expanded ? formatToolText(visibleOutput) : "";

  return (
    <div className="mx-0.5 flex min-w-0 justify-start sm:mx-1">
      <div
        className={expanded ? "w-full min-w-0" : "w-full min-w-0 sm:w-fit sm:min-w-60"}
        style={{
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
          aria-label={`${toolLabel} tool call`}
          aria-expanded={expanded}
          disabled={!hasDetails}
          onClick={() => hasDetails && setExpanded((value) => !value)}
          style={{
            width: "100%",
            minHeight: 44,
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "9px 12px",
            border: 0,
            background: "transparent",
            color: "inherit",
            cursor: hasDetails ? "pointer" : "default",
            textAlign: "left",
          }}
        >
          <Icon name="terminal" size={13} style={{ color: "#F97316", flex: "0 0 auto" }} />
          <span
            title={toolLabel}
            style={{
              maxWidth: "min(42%, 220px)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              fontWeight: 600,
              fontSize: 12,
              whiteSpace: "nowrap",
              flex: "0 1 auto",
            }}
          >
            {toolLabel}
          </span>
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
            <span style={{ marginLeft: "auto", display: "inline-flex", color: "var(--aurora-fg4)", flex: "0 0 auto" }}>
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
            {formattedInput && <ToolCodeBlock label="Input" value={formattedInput} />}
            {formattedOutput && (
              <ToolCodeBlock label="Output" value={formattedOutput} topSpacing={Boolean(formattedInput)} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function sessionContextSummary(
  content: string,
  t: ReturnType<typeof useI18n>["t"],
): string {
  if (/<recommended_plugins\b/i.test(content)) return t.conversation.recommendedPlugins;
  if (/<external_links\b/i.test(content)) return t.conversation.webSearchContext;
  if (/<plugin_info\b/i.test(content)) return t.conversation.pluginContext;
  if (/<uploaded_documents\b/i.test(content)) return t.conversation.uploadedDocuments;
  if (/<codex_internal_context\b[^>]*\bsource=["']goal["']/i.test(content)) {
    return t.conversation.activeGoalContext;
  }
  if (/This session is being continued from a previous conversation/i.test(content)) {
    return t.conversation.conversationSummary;
  }
  if (/AGENTS\.md instructions|Base directory for this skill:/i.test(content)) {
    return t.conversation.workspaceInstructions;
  }
  return t.conversation.sessionContextHint;
}

function ConversationContextCard({
  content,
  t,
}: {
  content: string;
  t: ReturnType<typeof useI18n>["t"];
}) {
  const [expanded, setExpanded] = useState(false);
  const summary = sessionContextSummary(content, t);

  return (
    <div
      data-conversation-context
      style={{
        width: "100%",
        minWidth: 0,
        border: "1px dashed color-mix(in srgb, var(--aurora-fg4) 34%, var(--aurora-border))",
        borderRadius: 11,
        background: "color-mix(in srgb, var(--aurora-chip) 52%, transparent)",
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        style={{
          width: "100%",
          minHeight: 46,
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "9px 12px",
          border: 0,
          background: "transparent",
          color: "var(--aurora-fg2)",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <span
          aria-hidden="true"
          style={{
            width: 28,
            height: 28,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            flex: "0 0 auto",
            borderRadius: 9,
            background: "color-mix(in srgb, var(--aurora-accent) 10%, var(--aurora-surface-solid))",
            color: "var(--aurora-accent)",
          }}
        >
          <Icon name="layers" size={14} />
        </span>
        <span style={{ minWidth: 0, display: "grid", gap: 1 }}>
          <span style={{ fontSize: 11.5, fontWeight: 650 }}>{t.conversation.sessionContext}</span>
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              color: "var(--aurora-fg4)",
              fontSize: 10.5,
            }}
          >
            {summary}
          </span>
        </span>
        <Icon
          name="chevron_down"
          size={13}
          style={{
            marginLeft: "auto",
            color: "var(--aurora-fg4)",
            transform: expanded ? "rotate(180deg)" : "none",
            transition: "transform .15s ease",
          }}
        />
      </button>
      {expanded && (
        <pre
          style={{
            margin: 0,
            maxHeight: "min(50vh, 480px)",
            overflow: "auto",
            padding: "12px 14px",
            borderTop: "1px solid var(--aurora-border)",
            color: "var(--aurora-fg3)",
            background: "color-mix(in srgb, var(--aurora-surface-solid) 84%, transparent)",
            fontFamily: "ui-monospace,SFMono-Regular,Consolas,monospace",
            fontSize: 11,
            lineHeight: 1.5,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
          }}
        >
          {content}
        </pre>
      )}
    </div>
  );
}

export const ChatBubble = memo(function ChatBubble({
  msg,
  toolId = "",
  locale,
  t,
}: {
  msg: ConversationMessage;
  toolId?: string;
  locale: string;
  t: ReturnType<typeof useI18n>["t"];
}) {
  const role = msg.role || msg.message_type || "unknown";
  const toolName = msg.tool_name ?? "";
  const content = cleanTerminalText(msg.content);
  const toolInput = cleanTerminalText(msg.tool_input ?? "");
  const thinking = cleanTerminalText(msg.thinking?.trim() || "");
  const sessionContext = cleanTerminalText(msg.session_context?.trim() || "");
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
          {sessionContext && (
            <div style={{ marginBottom: 8 }}>
              <ConversationContextCard content={sessionContext} t={t} />
            </div>
          )}
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
    const structuredToolCalls = msg.tool_calls || [];
    const assistantContent = toolId === "cursor"
      ? stripCursorTransportRedaction(content)
      : content;
    const legacySegments: AssistantContentSegment[] = structuredToolCalls.length > 0
      ? assistantContent.trim()
        ? [{ type: "text", content: assistantContent }]
        : []
      : toolId === "cursor"
        ? splitAssistantContent(content)
        : assistantContent
          ? [{ type: "text", content: assistantContent }]
          : [];
    const narrative = legacySegments
      .filter((segment): segment is Extract<AssistantContentSegment, { type: "text" }> => segment.type === "text")
      .map((segment) => segment.content)
      .join("\n\n")
      .trim();
    const toolCalls = structuredToolCalls.length > 0
      ? structuredToolCalls.map((call) => ({
          name: call.name,
          input: typeof call.input === "string" ? call.input : JSON.stringify(call.input),
        }))
      : legacySegments
          .filter((segment): segment is Extract<AssistantContentSegment, { type: "tool" }> => segment.type === "tool")
          .map((segment) => ({ name: segment.name, input: segment.input }));
    const isLong = narrative.length > 500;
    const displayContent = isLong && !expanded ? narrative.slice(0, 500) + "..." : narrative;
    const hasSeparateThinking = Boolean(thinking && thinking !== narrative);
    const hasNarrative = Boolean(narrative || hasSeparateThinking);

    return (
      <div style={{ display: "flex", justifyContent: "flex-start" }}>
        <div style={{ width: "100%", minWidth: 0, padding: hasNarrative ? "3px 2px 8px" : "0 0 4px" }}>
          {hasNarrative && (
            <>
              <div
                style={{
                  display: "flex",
                  gap: 8,
                  marginBottom: 5,
                  alignItems: "center",
                  padding: "0 4px",
                }}
              >
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
                className="px-3 py-3 sm:px-4"
                style={{
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
                {displayContent && (
                  <div className="prose prose-sm max-w-none">
                    <MarkdownViewer content={displayContent} />
                  </div>
                )}
                <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: displayContent ? 8 : 0 }}>
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
                      onClick={() => setShowThinking((value) => !value)}
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
            </>
          )}
          {toolCalls.length > 0 && (
            <div
              role="group"
              aria-label="Assistant tool calls"
              style={{ display: "grid", gap: 6, marginTop: hasNarrative ? 6 : 0 }}
            >
              {toolCalls.map((call, index) => (
                <ConversationToolCard
                  key={`${call.name}-${index}`}
                  name={call.name}
                  input={call.input}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  // Tool use — compact SpecStory-style accordion. Tool input and terminal
  // output stay collapsed until requested, keeping long agent sessions easy
  // to scan while retaining every detail.
  if (role === "tool") {
    return <ConversationToolCard name={toolName || "Tool result"} input={toolInput} output={content} />;
  }

  const isSessionContext = /(?:^|_)(?:codex|claude|cursor)_context$/i.test(
    msg.raw_type || msg.message_type || "",
  ) || /^(?:\s*<(?:recommended_plugins|codex_internal_context)\b|\s*#\s*AGENTS\.md instructions)/i.test(content);
  if (isSessionContext) {
    return <ConversationContextCard content={content} t={t} />;
  }

  // Other system notices remain compact and centered.
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
        className="whitespace-pre-wrap break-words sm:whitespace-pre"
        style={{
          margin: 0,
          padding: "10px 11px",
          maxWidth: "100%",
          maxHeight: 320,
          overflow: "auto",
          overscrollBehaviorX: "contain",
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
