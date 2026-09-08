"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { Icon, PlatformGlyph, ToolGlyph } from "@/components/aurora/Icon";
import { Chip, Glass, SectionLabel } from "@/components/aurora/primitives";
import { authFetch, getApiBase, type ConversationLocation } from "@/lib/api-client";
import { copyText } from "@/lib/copy-text";
import { timeAgo } from "@/lib/constants";
import { fmt, useI18n } from "@/lib/i18n";

export interface OpenThreadHealth {
  available: boolean;
  stale?: boolean;
  source: string;
  ratio: number | null;
  status: string;
  hop_line: number;
  hard_line: number;
}

export interface OpenThread {
  document_id: string;
  tool_id: string;
  title: string;
  canonical_url: string;
  resume_id: string | null;
  resume_command: string | null;
  location: ConversationLocation | null;
  activity_at: string | null;
  is_open?: boolean;
  pinned: boolean;
  health: OpenThreadHealth | null;
}

export interface OpenThreadGroup {
  machine: {
    id: string;
    name: string;
    host: string;
    platform: string;
  };
  total_threads?: number;
  truncated?: boolean;
  threads: OpenThread[];
}

type CopyState = { documentId: string; status: "copied" | "error" } | null;

function healthTone(health: OpenThreadHealth | null): "success" | "warn" | "danger" | "neutral" {
  if (!health?.available) return "neutral";
  const status = health.status.toLowerCase();
  if (status === "crit" || status === "critical" || status === "hard") return "danger";
  if (status === "warn" || status === "warning") return "warn";
  if (status === "ok" || status === "healthy") return "success";
  return "neutral";
}

function sortThreads(threads: OpenThread[]): OpenThread[] {
  return [...threads].sort((left, right) => {
    if (left.pinned !== right.pinned) return left.pinned ? -1 : 1;
    return String(right.activity_at || "").localeCompare(String(left.activity_at || ""));
  });
}

export default function OpenThreadsPanel({
  groups,
  truncated = false,
  activeMinutes,
}: {
  groups: OpenThreadGroup[];
  truncated?: boolean;
  activeMinutes?: number;
}) {
  const { t } = useI18n();
  const [threadGroups, setThreadGroups] = useState(groups);
  const [pendingPins, setPendingPins] = useState<Set<string>>(() => new Set());
  const [pinError, setPinError] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<CopyState>(null);
  const copyTimer = useRef<number | null>(null);

  useEffect(() => setThreadGroups(groups), [groups]);
  useEffect(() => () => {
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
  }, []);

  const threadCount = useMemo(
    () => threadGroups.reduce((total, group) => total + group.threads.length, 0),
    [threadGroups],
  );

  const updatePinned = (documentId: string, pinned: boolean) => {
    setThreadGroups((current) => current.map((group) => ({
      ...group,
      threads: sortThreads(group.threads.map((thread) => (
        thread.document_id === documentId ? { ...thread, pinned } : thread
      ))),
    })));
  };

  const togglePin = async (thread: OpenThread) => {
    const nextPinned = !thread.pinned;
    setPinError(null);
    setPendingPins((current) => new Set(current).add(thread.document_id));
    updatePinned(thread.document_id, nextPinned);
    try {
      const response = await authFetch(
        `${getApiBase()}/api/conversations/${encodeURIComponent(thread.document_id)}/pin`,
        { method: nextPinned ? "POST" : "DELETE" },
      );
      if (!response.ok) throw new Error(`Pin request failed (${response.status})`);
    } catch (error) {
      console.error(error);
      updatePinned(thread.document_id, thread.pinned);
      setPinError(t.dashboard.threadPinFailed);
    } finally {
      setPendingPins((current) => {
        const next = new Set(current);
        next.delete(thread.document_id);
        return next;
      });
    }
  };

  const copyCommand = async (thread: OpenThread) => {
    if (!thread.resume_command) return;
    try {
      await copyText(thread.resume_command);
      setCopyState({ documentId: thread.document_id, status: "copied" });
    } catch (error) {
      console.error(error);
      setCopyState({ documentId: thread.document_id, status: "error" });
    }
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopyState(null), 1800);
  };

  const healthLabel = (health: OpenThreadHealth | null) => {
    if (!health?.available) return t.dashboard.healthNotReported;
    const status = health.status.toLowerCase();
    const state = status === "crit" || status === "critical" || status === "hard"
      ? t.dashboard.healthCritical
      : status === "warn" || status === "warning"
        ? t.dashboard.healthHandoffSoon
        : status === "ok" || status === "healthy"
          ? t.dashboard.healthHealthy
          : status === "thin"
            ? t.dashboard.healthWarmingUp
            : t.dashboard.healthNotReported;
    const label = health.ratio == null ? state : `${Math.round(health.ratio)}:1 · ${state}`;
    return health.stale ? `${label} · ${t.dashboard.healthStale}` : label;
  };

  return (
    <Glass padding={22} radius={22}>
      <div className="open-threads-heading">
        <div>
          <SectionLabel style={{ margin: 0 }}>{t.dashboard.openThreads}</SectionLabel>
          {activeMinutes != null && (
            <p>{fmt(t.dashboard.openThreadsScope, { minutes: activeMinutes })}</p>
          )}
        </div>
        <Chip tone="accent">{threadCount}</Chip>
      </div>

      {threadCount === 0 ? (
        <p className="open-threads-empty">{t.dashboard.noOpenThreads}</p>
      ) : (
        <div className="open-thread-groups">
          {threadGroups.map((group) => (
            <section className="open-thread-group" key={group.machine.id} data-open-thread-machine={group.machine.id}>
              <div className="open-thread-machine">
                <PlatformGlyph name={group.machine.platform} size={30} />
                <div>
                  <h3>{group.machine.name}</h3>
                  <p>{[group.machine.host, group.machine.platform].filter(Boolean).join(" · ")}</p>
                </div>
                <span>
                  {group.truncated && (group.total_threads ?? 0) > group.threads.length
                    ? `${group.threads.length}/${group.total_threads}`
                    : group.threads.length}
                </span>
              </div>
              <div className="open-thread-list">
                {sortThreads(group.threads).map((thread) => {
                  const pending = pendingPins.has(thread.document_id);
                  const copied = copyState?.documentId === thread.document_id;
                  const pinLabel = thread.pinned ? t.dashboard.unpinThread : t.dashboard.pinThread;
                  return (
                    <article className="open-thread" data-open-thread={thread.document_id} key={thread.document_id}>
                      <ToolGlyph id={thread.tool_id} size={30} />
                      <div className="open-thread-body">
                        <div className="open-thread-title-line">
                          <Link href={thread.canonical_url || `/conversations/${encodeURIComponent(thread.document_id)}`} prefetch={false}>
                            {thread.title || t.dashboard.untitledThread}
                          </Link>
                          <Chip tone={healthTone(thread.health)}>{healthLabel(thread.health)}</Chip>
                        </div>
                        <div className="open-thread-meta">
                          {thread.activity_at && <span>{timeAgo(thread.activity_at)}</span>}
                          {thread.location?.path && <span title={thread.location.path}>{thread.location.path}</span>}
                        </div>
                        {thread.resume_command && (
                          <div className="open-thread-command">
                            <Icon name="terminal" size={13} aria-hidden />
                            <code title={thread.resume_command}>{thread.resume_command}</code>
                            <button
                              type="button"
                              aria-label={t.conversation.copyResumeCommand}
                              title={t.conversation.copyResumeCommand}
                              onClick={() => copyCommand(thread)}
                            >
                              <Icon name={copied && copyState.status === "copied" ? "check" : "copy"} size={14} />
                            </button>
                          </div>
                        )}
                        {copied && (
                          <span className="open-thread-copy-status" role="status">
                            {copyState.status === "copied"
                              ? t.conversation.resumeCommandCopied
                              : t.conversation.resumeCommandCopyFailed}
                          </span>
                        )}
                      </div>
                      <button
                        className="open-thread-pin"
                        type="button"
                        aria-label={pinLabel}
                        title={pinLabel}
                        aria-pressed={thread.pinned}
                        disabled={pending}
                        onClick={() => togglePin(thread)}
                      >
                        <Icon name="pin" size={16} />
                      </button>
                    </article>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}

      {truncated && <p className="open-threads-note">{t.dashboard.openThreadsTruncated}</p>}
      {pinError && <p className="open-threads-error" role="alert">{pinError}</p>}

      <style jsx>{`
        .open-threads-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
        .open-threads-heading p{margin:3px 0 0;color:var(--aurora-fg4);font-size:10px}
        .open-thread-groups{display:grid;gap:12px}
        .open-thread-group{overflow:hidden;border:1px solid var(--aurora-border);border-radius:16px;background:color-mix(in srgb,var(--aurora-chip) 24%,transparent)}
        .open-thread-machine{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:10px;padding:11px 12px;border-bottom:1px solid var(--aurora-border)}
        .open-thread-machine h3{margin:0;color:var(--aurora-fg1);font-size:13px;font-weight:650;letter-spacing:-.01em}
        .open-thread-machine p{margin:2px 0 0;color:var(--aurora-fg4);font-size:10px}
        .open-thread-machine>span{display:grid;place-items:center;min-width:23px;height:23px;padding:0 6px;border-radius:999px;background:var(--aurora-chip);color:var(--aurora-fg3);font-size:10px;font-weight:700}
        .open-thread-list{display:grid}
        .open-thread{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:start;gap:10px;padding:12px}
        .open-thread+.open-thread{border-top:1px solid var(--aurora-border)}
        .open-thread-body{min-width:0}
        .open-thread-title-line{display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:0}
        .open-thread-title-line>a{min-width:0;overflow:hidden;color:var(--aurora-fg1);font-size:12px;font-weight:650;text-decoration:none;text-overflow:ellipsis;white-space:nowrap}
        .open-thread-title-line>a:hover{text-decoration:underline}
        .open-thread-meta{display:flex;gap:6px;margin-top:3px;overflow:hidden;color:var(--aurora-fg4);font-size:9px}
        .open-thread-meta span+span:before{content:"·";margin-right:6px}
        .open-thread-meta span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .open-thread-command{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:7px;margin-top:8px;padding:6px 7px;border:1px solid var(--aurora-border);border-radius:8px;background:color-mix(in srgb,var(--aurora-bg) 75%,transparent);color:var(--aurora-fg4)}
        .open-thread-command code{overflow:hidden;color:var(--aurora-fg3);font-size:9px;text-overflow:ellipsis;white-space:nowrap}
        .open-thread-command button,.open-thread-pin{display:grid;place-items:center;border:0;background:transparent;color:var(--aurora-fg4);cursor:pointer}
        .open-thread-command button{width:25px;height:25px;border-radius:7px}
        .open-thread-command button:hover,.open-thread-pin:hover{background:var(--aurora-chip);color:var(--aurora-fg1)}
        .open-thread-pin{width:30px;height:30px;border-radius:9px;transition:transform .14s ease,color .14s ease,background .14s ease}
        .open-thread-pin[aria-pressed="true"]{color:#a78bfa;transform:rotate(-18deg)}
        .open-thread-pin:disabled{cursor:wait;opacity:.45}
        .open-thread-copy-status{display:block;margin-top:4px;color:var(--aurora-fg4);font-size:9px}
        .open-threads-empty,.open-threads-note,.open-threads-error{margin:0;padding:12px;color:var(--aurora-fg4);font-size:12px}
        .open-threads-note{padding:10px 0 0}
        .open-threads-error{padding:10px 0 0;color:#f87171}
        @media(max-width:560px){
          .open-thread-title-line{align-items:flex-start;flex-direction:column;gap:5px}
          .open-thread-title-line>a{max-width:100%}
          .open-thread-meta span:last-child{display:none}
        }
      `}</style>
    </Glass>
  );
}
