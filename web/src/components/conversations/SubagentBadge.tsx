"use client";

import Link from "next/link";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import { Icon } from "@/components/aurora/Icon";
import { api } from "@/lib/api-client";
import type { ConversationMessage, ConversationSubagentSummary } from "@/lib/api-client";
import styles from "./SubagentBadge.module.css";

const DEFAULT_VISIBLE_AGENTS = 80;
const PREVIEW_MESSAGE_LIMIT = 30;

type PreviewState = {
  loading: boolean;
  error: boolean;
  total: number;
  messages: ConversationMessage[];
};

export default function SubagentBadge({
  count,
  orphan = false,
  subagents = [],
  matchedSubagentId,
}: {
  count?: number;
  orphan?: boolean;
  subagents?: ConversationSubagentSummary[];
  matchedSubagentId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});
  const [panelStyle, setPanelStyle] = useState<CSSProperties>({});
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const total = count || subagents.length;
  const agentsLabel = `${total} ${total === 1 ? "subagent" : "subagents"}`;
  const showSearch = subagents.length > 8;

  useLayoutEffect(() => {
    if (!open || !rootRef.current) return;
    const place = () => {
      const rect = rootRef.current?.getBoundingClientRect();
      if (!rect) return;
      const mobile = window.matchMedia("(max-width: 640px)").matches;
      if (mobile) {
        // Definite height is required so the flex child list can scroll on iOS Safari.
        const height = Math.min(Math.round(window.innerHeight * 0.78), 640);
        setPanelStyle({
          position: "fixed",
          top: "auto",
          left: "max(10px, env(safe-area-inset-left))",
          right: "max(10px, env(safe-area-inset-right))",
          bottom: "max(10px, env(safe-area-inset-bottom))",
          width: "auto",
          height,
          maxHeight: height,
          zIndex: 200,
        });
        return;
      }
      const width = Math.min(440, Math.max(300, window.innerWidth - 48));
      const left = Math.min(
        Math.max(24, rect.right - width),
        window.innerWidth - width - 24,
      );
      const top = Math.min(rect.bottom + 8, window.innerHeight - 48);
      const maxHeight = Math.min(520, window.innerHeight - top - 24);
      setPanelStyle({
        position: "fixed",
        top,
        left,
        right: "auto",
        bottom: "auto",
        width,
        height: maxHeight,
        maxHeight,
        zIndex: 200,
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  // After open / expand, keep the active card reachable inside the scrollport.
  useLayoutEffect(() => {
    if (!open || !expandedKey || !listRef.current) return;
    const card = listRef.current.querySelector<HTMLElement>(`[data-subagent-key="${CSS.escape(expandedKey)}"]`);
    card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [expandedKey, open, previews]);

  const orderedSubagents = useMemo(() => {
    if (!matchedSubagentId) return subagents;
    const matched = matchedSubagentId
      ? subagents.find((subagent) => subagent.id === matchedSubagentId)
      : undefined;
    const remaining = subagents.filter((subagent) => subagent.id !== matchedSubagentId);
    return matched ? [matched, ...remaining] : remaining;
  }, [matchedSubagentId, subagents]);

  const filteredSubagents = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return orderedSubagents;
    return orderedSubagents.filter((subagent) => (
      `${subagent.title} ${subagent.agent_nickname || ""} ${subagent.agent_path || ""}`
        .toLocaleLowerCase()
        .includes(normalizedQuery)
    ));
  }, [orderedSubagents, query]);
  const visibleSubagents = filteredSubagents.slice(0, DEFAULT_VISIBLE_AGENTS);

  const togglePreview = async (subagent: ConversationSubagentSummary) => {
    const key = subagent.session_id || subagent.id || subagent.agent_path || subagent.title;
    if (expandedKey === key) {
      setExpandedKey(null);
      return;
    }
    setExpandedKey(key);
    if (!subagent.id || previews[key]) return;
    setPreviews((current) => ({
      ...current,
      [key]: { loading: true, error: false, total: 0, messages: [] },
    }));
    try {
      const response = await api.getLatestMessages(subagent.id, PREVIEW_MESSAGE_LIMIT);
      setPreviews((current) => ({
        ...current,
        [key]: {
          loading: false,
          error: false,
          total: response.total,
          messages: response.messages,
        },
      }));
    } catch {
      setPreviews((current) => ({
        ...current,
        [key]: { loading: false, error: true, total: 0, messages: [] },
      }));
    }
  };

  if (!total) return null;

  if (subagents.length === 0) {
    return (
      <span className={styles.staticPill} title={orphan ? `${agentsLabel}; root thread has not synced yet` : agentsLabel}>
        <Icon name="layers" size={11} />
        {agentsLabel}
        {orphan && <span className={styles.muted}>· root pending</span>}
      </span>
    );
  }

  return (
    <div ref={rootRef} className={styles.root}>
      <button
        type="button"
        className={styles.trigger}
        aria-expanded={open}
        aria-controls={panelId}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
        title={`Browse ${agentsLabel}`}
      >
        <Icon name="layers" size={11} />
        <span>{agentsLabel}</span>
        {orphan && <span className={styles.muted}>· root pending</span>}
        {matchedSubagentId && <span className={styles.muted}>· child match</span>}
        <Icon name={open ? "chevron_up" : "chevron_down"} size={10} />
      </button>

      {open && typeof document !== "undefined" && createPortal(
        <>
          <button
            type="button"
            className={styles.backdrop}
            aria-label="Close subagent browser"
            onClick={() => setOpen(false)}
          />
          <div
            ref={panelRef}
            id={panelId}
            className={styles.panel}
            role="dialog"
            aria-modal="true"
            aria-label={agentsLabel}
            style={panelStyle}
          >
            <div className={styles.header}>
              <span className={styles.headerIcon}><Icon name="layers" size={15} /></span>
              <span className={styles.headerText}>
                <strong>Subagents</strong>
                <span>Task name first; generated codename second</span>
              </span>
              <button type="button" className={styles.closeButton} aria-label="Close subagent browser" onClick={() => setOpen(false)}>
                <Icon name="close" size={15} />
              </button>
            </div>

            {showSearch ? (
              <label className={styles.searchField}>
                <Icon name="search" size={14} />
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Find a subagent task or codename"
                  autoFocus
                />
              </label>
            ) : null}

            <div ref={listRef} className={styles.list} data-subagent-list>
              {visibleSubagents.map((subagent) => {
                const isMatched = Boolean(matchedSubagentId && subagent.id === matchedSubagentId);
                const key = subagent.session_id || subagent.id || subagent.agent_path || subagent.title;
                const expanded = expandedKey === key;
                const preview = previews[key];
                const hasDistinctNickname = Boolean(
                  subagent.agent_nickname
                  && subagent.agent_nickname.toLocaleLowerCase() !== subagent.title.toLocaleLowerCase(),
                );
                return (
                  <div
                    key={key}
                    data-subagent-key={key}
                    className={`${styles.agentCard} ${isMatched ? styles.matched : ""}`}
                  >
                    <button
                      type="button"
                      className={styles.agentSummary}
                      aria-expanded={expanded}
                      onClick={() => void togglePreview(subagent)}
                    >
                      <span className={styles.agentIcon}><Icon name="layers" size={13} /></span>
                      <span className={styles.agentText}>
                        <span className={styles.agentTitle}>{subagent.title}</span>
                        <span className={styles.agentMeta}>
                          <span className={`${styles.statusDot} ${styles[`status_${subagent.status || "unknown"}`]}`} />
                          {subagent.status && subagent.status !== "unknown" ? subagent.status : "Subagent"}
                          {typeof subagent.agent_depth === "number" ? ` · depth ${subagent.agent_depth}` : ""}
                          {hasDistinctNickname ? ` · codename ${subagent.agent_nickname}` : ""}
                          {subagent.document_ready === false ? " · transcript syncing" : ""}
                        </span>
                      </span>
                      {isMatched && <span className={styles.matchLabel}>Match</span>}
                      <Icon name={expanded ? "chevron_up" : "chevron_down"} size={11} />
                    </button>
                    {expanded && (
                      <SubagentPreview
                        subagent={subagent}
                        preview={preview}
                        onOpen={() => setOpen(false)}
                      />
                    )}
                  </div>
                );
              })}
              {visibleSubagents.length === 0 && (
                <div className={styles.empty}>No subagents match “{query.trim()}”.</div>
              )}
            </div>

            <div className={styles.footer}>
              Showing {visibleSubagents.length} of {filteredSubagents.length} matching subagents
              {filteredSubagents.length < orderedSubagents.length ? ` · ${orderedSubagents.length} total` : ""}
            </div>
          </div>
        </>,
        document.body,
      )}
    </div>
  );
}

function SubagentPreview({
  subagent,
  preview,
  onOpen,
}: {
  subagent: ConversationSubagentSummary;
  preview?: PreviewState;
  onOpen: () => void;
}) {
  if (!subagent.id || subagent.document_ready === false) {
    return (
      <div className={styles.previewBody}>
        <div className={styles.previewPending}>
          <span className={styles.loadingDot} />
          The task is visible now; its child transcript is still being normalized.
        </div>
      </div>
    );
  }
  if (!preview || preview.loading) {
    return (
      <div className={styles.previewBody}>
        <div className={styles.previewPending}><span className={styles.loadingDot} /> Loading recent child activity…</div>
      </div>
    );
  }
  if (preview.error) {
    return <div className={styles.previewBody}><div className={styles.previewPending}>Could not load the child preview.</div></div>;
  }

  const rows = preview.messages
    .flatMap((message) => {
      const role = message.role || message.message_type || "activity";
      const values: Array<{ key: string; kind: string; label: string; content: string }> = [];
      if (message.thinking?.trim()) {
        values.push({ key: `${message.id}-thinking`, kind: "thinking", label: "Thought", content: message.thinking.trim() });
      }
      if (message.content?.trim()) {
        values.push({
          key: `${message.id}-content`,
          kind: role,
          label: formatPreviewLabel(
            role === "assistant" ? "Response" : role === "user" ? "Prompt" : message.tool_name || "Tool",
          ),
          content: message.content.trim(),
        });
      }
      return values;
    })
    .slice(-8);

  return (
    <div className={styles.previewBody}>
      <div className={styles.previewHeader}>
        <span>Recent activity</span>
        <span>{preview.total.toLocaleString()} messages in child thread</span>
      </div>
      <div className={styles.previewTimeline}>
        {rows.map((row) => (
          <div key={row.key} className={`${styles.previewRow} ${styles[`preview_${row.kind}`] || ""}`}>
            <span className={styles.previewLabel}>{row.label}</span>
            <span className={styles.previewText}>{compactPreviewText(row.content)}</span>
          </div>
        ))}
        {rows.length === 0 && <div className={styles.previewPending}>No renderable child activity yet.</div>}
      </div>
      <Link
        href={`/conversations/${subagent.id}`}
        prefetch={false}
        className={styles.openThread}
        onClick={onOpen}
      >
        Open full subagent thread
        <Icon name="chevron_right" size={11} />
      </Link>
    </div>
  );
}

function formatPreviewLabel(value: string): string {
  const clean = value.replace(/\s+/g, " ").trim();
  if (!clean) return "Tool";
  // Prefer the readable tail of dotted tool ids (mcp.server.ToolName → ToolName).
  const leaf = clean.includes(".") ? clean.split(".").pop() || clean : clean;
  if (leaf.length <= 18) return leaf;
  return `${leaf.slice(0, 16)}…`;
}

function compactPreviewText(value: string): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > 280 ? `${compact.slice(0, 277)}…` : compact;
}
