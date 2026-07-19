"use client";

import Link from "next/link";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/aurora/Icon";
import type { ConversationSubagentSummary } from "@/lib/api-client";
import styles from "./SubagentBadge.module.css";

const DEFAULT_VISIBLE_AGENTS = 80;

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
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const total = count || subagents.length;
  const agentsLabel = `${total} ${total === 1 ? "subagent" : "subagents"}`;

  useEffect(() => {
    if (!open) return;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const orderedSubagents = useMemo(() => {
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

      {open && (
        <>
          <button
            type="button"
            className={styles.backdrop}
            aria-label="Close subagent browser"
            onClick={() => setOpen(false)}
          />
          <div id={panelId} className={styles.panel} role="dialog" aria-label={agentsLabel}>
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

            {subagents.length > 8 && (
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
            )}

            <div className={styles.list}>
              {visibleSubagents.map((subagent) => {
                const isMatched = subagent.id === matchedSubagentId;
                const hasDistinctNickname = Boolean(
                  subagent.agent_nickname
                  && subagent.agent_nickname.toLocaleLowerCase() !== subagent.title.toLocaleLowerCase(),
                );
                return (
                  <Link
                    key={subagent.session_id || subagent.id}
                    href={`/conversations/${subagent.id}`}
                    prefetch={false}
                    className={`${styles.agentLink} ${isMatched ? styles.matched : ""}`}
                    onClick={() => setOpen(false)}
                  >
                    <span className={styles.agentIcon}><Icon name="layers" size={13} /></span>
                    <span className={styles.agentText}>
                      <span className={styles.agentTitle}>{subagent.title}</span>
                      <span className={styles.agentMeta}>
                        Subagent{typeof subagent.agent_depth === "number" ? ` · depth ${subagent.agent_depth}` : ""}
                        {hasDistinctNickname ? ` · codename ${subagent.agent_nickname}` : ""}
                      </span>
                    </span>
                    {isMatched && <span className={styles.matchLabel}>Match</span>}
                    <Icon name="chevron_right" size={11} />
                  </Link>
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
        </>
      )}
    </div>
  );
}
