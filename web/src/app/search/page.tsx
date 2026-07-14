"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import {
  GlobalMessageSearchResponse,
  SearchResult,
  api,
  authFetch,
  getApiBase,
} from "@/lib/api-client";
import { useI18n, fmt } from "@/lib/i18n";
import { useDevice } from "@/lib/device-context";
import { Icon, ToolGlyph } from "@/components/aurora/Icon";
import { Btn, Chip, Glass, GhostInput, TopBar } from "@/components/aurora/primitives";
import SubagentBadge from "@/components/conversations/SubagentBadge";

type SearchScope = "messages" | "files";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [toolFilter, setToolFilter] = useState("");
  const [scope, setScope] = useState<SearchScope>("messages");
  const [fileResult, setFileResult] = useState<SearchResult | null>(null);
  const [messageResult, setMessageResult] = useState<GlobalMessageSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const { t, locale } = useI18n();
  const { selectedDeviceId } = useDevice();

  const runSearch = async ({ append = false }: { append?: boolean } = {}) => {
    const cleanQuery = query.trim();
    if (!cleanQuery) return;
    setLoading(true);
    try {
      if (scope === "messages") {
        const next = await api.searchMessages(cleanQuery, {
          tool: toolFilter || undefined,
          deviceId: selectedDeviceId,
          cursor: append ? messageResult?.next_cursor : null,
          limit: 20,
        });
        setMessageResult((previous) => {
          if (!append || !previous || previous.query !== next.query) return next;
          const groups = new Map(previous.results.map((group) => [group.id, group]));
          next.results.forEach((group) => {
            const existing = groups.get(group.id);
            if (!existing) {
              groups.set(group.id, group);
              return;
            }
            const hitIds = new Set(existing.hits.map((hit) => `${hit.matched_document_id}:${hit.id}`));
            groups.set(group.id, {
              ...existing,
              hits: [
                ...existing.hits,
                ...group.hits.filter((hit) => !hitIds.has(`${hit.matched_document_id}:${hit.id}`)),
              ].slice(0, 3),
            });
          });
          return { ...next, results: Array.from(groups.values()) };
        });
      } else {
        const params = new URLSearchParams({ q: cleanQuery, offset: "0", limit: "20" });
        if (toolFilter) params.set("tool", toolFilter);
        if (selectedDeviceId) params.set("device_id", selectedDeviceId);
        const response = await authFetch(`${getApiBase()}/api/search?${params}`);
        setFileResult(await response.json());
      }
    } catch (error) {
      console.error(error);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = async (event: FormEvent) => {
    event.preventDefault();
    await runSearch();
  };

  const resultQuery = scope === "messages" ? messageResult?.query : fileResult?.query;
  const resultCount = scope === "messages" ? messageResult?.results.length : fileResult?.total;

  return (
    <div className="max-w-5xl mx-auto">
      <TopBar
        title={t.searchPage.title}
        subtitle={resultQuery
          ? fmt(t.searchPage.results, { total: resultCount ?? 0, query: resultQuery })
          : t.searchPage.subtitle}
      />

      <div
        role="tablist"
        aria-label={t.searchPage.scopeLabel}
        style={{ display: "flex", gap: 6, marginBottom: 12 }}
      >
        <ScopeButton
          active={scope === "messages"}
          onClick={() => setScope("messages")}
          label={t.searchPage.conversations}
        />
        <ScopeButton
          active={scope === "files"}
          onClick={() => setScope("files")}
          label={t.searchPage.files}
        />
      </div>

      <form onSubmit={handleSearch} style={{ display: "flex", gap: 10, marginBottom: 22, flexWrap: "wrap" }}>
        <GhostInput
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={scope === "messages" ? t.searchPage.messagePlaceholder : t.searchPage.placeholder}
          icon="search"
          wrapStyle={{ flex: 1, minWidth: 240 }}
        />
        <label className="aurora-input" style={{ minWidth: 160 }}>
          <Icon name="grid" size={15} style={{ color: "var(--aurora-fg3)" }} />
          <select value={toolFilter} onChange={(event) => setToolFilter(event.target.value)}>
            <option value="">{t.searchPage.allTools}</option>
            <option value="claude_code">Claude Code</option>
            <option value="openclaw">OpenClaw</option>
            <option value="codex">Codex</option>
            <option value="antigravity">Antigravity</option>
            <option value="obsidian">Obsidian</option>
            <option value="cursor">Cursor</option>
          </select>
        </label>
        <Btn type="submit" disabled={loading} icon={loading ? undefined : "search"}>
          {loading ? "…" : t.search}
        </Btn>
      </form>

      {scope === "messages" && messageResult && (
        <div data-global-message-results style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {messageResult.results.map((group) => (
            <Glass key={group.id} padding={18} radius={18}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
                <ToolGlyph id={group.tool_id} size={26} />
                <Link
                  href={`/conversations/${group.id}`}
                  prefetch={false}
                  style={{ color: "var(--aurora-fg1)", fontWeight: 650, textDecoration: "none", flex: 1, minWidth: 180 }}
                >
                  {group.title || group.relative_path}
                </Link>
                {group.activity_at && (
                  <span style={{ color: "var(--aurora-fg4)", fontSize: 11 }}>
                    {new Date(group.activity_at).toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" })}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {group.hits.map((hit) => (
                  <Link
                    key={`${hit.matched_document_id}:${hit.id}`}
                    href={`/conversations/${hit.matched_document_id}?line=${hit.line_number}&q=${encodeURIComponent(messageResult.query)}`}
                    prefetch={false}
                    data-global-message-hit={hit.line_number}
                    style={{
                      display: "block",
                      borderRadius: 13,
                      border: "1px solid var(--aurora-border)",
                      background: "var(--aurora-surface-solid)",
                      padding: "10px 12px",
                      textDecoration: "none",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 5 }}>
                      <Chip tone={hit.role === "user" ? "accent" : "neutral"}>
                        {hit.role === "user" ? t.searchPage.you : t.searchPage.assistant}
                      </Chip>
                      {hit.match_type === "fuzzy" && <Chip>{t.searchPage.fuzzyMatch}</Chip>}
                      {hit.is_subagent_hit && <Chip>{t.searchPage.subagentMatch}</Chip>}
                      <span style={{ marginLeft: "auto", color: "var(--aurora-fg4)", fontSize: 10 }}>
                        #{hit.line_number}
                      </span>
                    </div>
                    <div style={{ color: "var(--aurora-fg2)", fontSize: 13, lineHeight: 1.5 }}>
                      <HighlightedText
                        text={hit.snippet}
                        query={
                          hit.match_type === "fuzzy"
                            ? (messageResult.corrected_query ?? messageResult.query)
                            : messageResult.query
                        }
                      />
                    </div>
                  </Link>
                ))}
              </div>
              {Boolean(group.subagent_count) && (
                <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--aurora-border)" }}>
                  <SubagentBadge
                    count={group.subagent_count}
                    orphan={group.is_subagent_orphan}
                    subagents={group.subagents}
                  />
                </div>
              )}
            </Glass>
          ))}
          {messageResult.results.length === 0 && <EmptyResults />}
          {messageResult.has_more && messageResult.next_cursor && (
            <div style={{ display: "flex", justifyContent: "center" }}>
              <Btn onClick={() => void runSearch({ append: true })} disabled={loading}>
                {loading ? "…" : t.searchPage.loadMore}
              </Btn>
            </div>
          )}
        </div>
      )}

      {scope === "files" && fileResult && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {fileResult.results.map((result) => (
            <Glass key={result.id} hover padding={18} radius={18}>
              <Link
                href={result.category === "conversation" ? `/conversations/${result.id}` : `/documents/${result.id}`}
                prefetch={false}
                style={{ display: "block", textDecoration: "none" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
                  <ToolGlyph id={result.tool_id} size={26} />
                  <Chip>{result.category}</Chip>
                  <span style={{ fontSize: 11, color: "var(--aurora-fg4)", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 320 }}>
                    {result.relative_path}
                  </span>
                </div>
                <div style={{ fontSize: 15, fontWeight: 500, color: "var(--aurora-fg1)", marginBottom: 6, letterSpacing: "-0.01em" }}>
                  {result.title || result.relative_path}
                </div>
                {result.snippet && (
                  <div style={{ fontSize: 13, color: "var(--aurora-fg3)", lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 80, overflow: "hidden" }}>
                    <HighlightedText text={result.snippet} query={fileResult.query} />
                  </div>
                )}
              </Link>
              {Boolean(result.subagent_count) && (
                <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--aurora-border)" }}>
                  <SubagentBadge
                    count={result.subagent_count}
                    orphan={result.is_subagent_orphan}
                    subagents={result.subagents}
                    matchedSubagentId={result.matched_subagent_id}
                  />
                </div>
              )}
            </Glass>
          ))}
          {fileResult.results.length === 0 && <EmptyResults />}
        </div>
      )}
    </div>
  );
}

function ScopeButton({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      style={{
        border: `1px solid ${active ? "var(--aurora-accent)" : "var(--aurora-border)"}`,
        borderRadius: 999,
        background: active ? "var(--aurora-accent-soft)" : "var(--aurora-surface-solid)",
        color: active ? "var(--aurora-accent)" : "var(--aurora-fg3)",
        padding: "7px 13px",
        fontSize: 12,
        fontWeight: 650,
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const cleanQuery = query.trim();
  if (!cleanQuery) return <>{text}</>;
  const lowerText = text.toLocaleLowerCase();
  const lowerQuery = cleanQuery.toLocaleLowerCase();
  const parts = [];
  let cursor = 0;
  let index = lowerText.indexOf(lowerQuery);
  while (index >= 0) {
    if (index > cursor) parts.push(<span key={`t-${cursor}`}>{text.slice(cursor, index)}</span>);
    parts.push(
      <mark key={`m-${index}`} style={{ background: "var(--aurora-accent-soft)", color: "var(--aurora-accent)", padding: "0 3px", borderRadius: 4, fontWeight: 600 }}>
        {text.slice(index, index + cleanQuery.length)}
      </mark>,
    );
    cursor = index + cleanQuery.length;
    index = lowerText.indexOf(lowerQuery, cursor);
  }
  if (cursor === 0) return <>{text}</>;
  if (cursor < text.length) parts.push(<span key={`t-${cursor}`}>{text.slice(cursor)}</span>);
  return <>{parts}</>;
}

function EmptyResults() {
  const { t } = useI18n();
  return (
    <Glass padding={36} radius={20} style={{ textAlign: "center" }}>
      <p style={{ color: "var(--aurora-fg4)", fontSize: 13 }}>{t.searchPage.noResults}</p>
    </Glass>
  );
}
