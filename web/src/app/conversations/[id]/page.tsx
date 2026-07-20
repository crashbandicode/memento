"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { api, ConversationMeta, ExportDiagnostics } from "@/lib/api-client";
import { fmt, useI18n } from "@/lib/i18n";
import ConversationViewer from "@/components/viewers/ConversationViewer";
import { Icon, ToolGlyph } from "@/components/aurora/Icon";
import { Btn, Chip } from "@/components/aurora/primitives";
import SubagentBadge from "@/components/conversations/SubagentBadge";
import { useConversationPrompts } from "@/lib/use-conversation-prompts";
import { MarkdownExportDialog } from "@/components/conversations/MarkdownExportForm";

interface RelatedPlan {
  id: string;
  title: string;
  relative_path: string;
  category: string;
  content_type: string;
  content: string | null;
  file_size_bytes: number;
  synced_at: string;
}

interface ConversationMetaWithPlans extends ConversationMeta {
  related_plans?: RelatedPlan[];
}

export default function ConversationPage() {
  const params = useParams();
  const docId = params.id as string;
  const [meta, setMeta] = useState<ConversationMetaWithPlans | null>(null);
  const [showExport, setShowExport] = useState(false);
  const metaRequestRef = useRef(0);
  const metaRefreshTimerRef = useRef<number | null>(null);
  const { t, locale } = useI18n();
  const { prompts, syncVersion } = useConversationPrompts(docId);

  const refreshMeta = useCallback(() => {
    const request = ++metaRequestRef.current;
    return api.getConversation(docId)
      .then((nextMeta) => {
        if (request === metaRequestRef.current) setMeta(nextMeta);
      })
      .catch((error: unknown) => {
        if (request === metaRequestRef.current) console.error(error);
      });
  }, [docId]);

  useEffect(() => {
    void refreshMeta();
    return () => { metaRequestRef.current += 1; };
  }, [docId, refreshMeta]);

  useEffect(() => {
    if (syncVersion === 0 || metaRefreshTimerRef.current !== null) return;
    metaRefreshTimerRef.current = window.setTimeout(() => {
      metaRefreshTimerRef.current = null;
      void refreshMeta();
    }, 500);
  }, [refreshMeta, syncVersion]);

  const pendingSubagentCount = meta?.id === docId
    ? (meta.subagents || []).filter((subagent) => subagent.document_ready === false).length
    : 0;
  useEffect(() => {
    if (pendingSubagentCount === 0) return;
    const timer = window.setInterval(() => void refreshMeta(), 3_000);
    return () => window.clearInterval(timer);
  }, [pendingSubagentCount, refreshMeta]);

  useEffect(() => () => {
    if (metaRefreshTimerRef.current !== null) {
      window.clearTimeout(metaRefreshTimerRef.current);
    }
  }, [docId]);

  const currentMeta = meta?.id === docId ? meta : null;
  const plans = currentMeta?.related_plans || [];
  const diagnostics = (currentMeta?.metadata?.export_diagnostics as ExportDiagnostics | undefined) || null;
  const currentAgentPath = typeof currentMeta?.metadata?.agent_path === "string"
    ? currentMeta.metadata.agent_path
    : "";
  const currentAgentNickname = typeof currentMeta?.metadata?.agent_nickname === "string"
    ? currentMeta.metadata.agent_nickname
    : "";
  const currentAgentLabel = currentAgentPath
    ? humanizeAgentPath(currentAgentPath)
    : "";
  const hasDiagnostics = currentMeta?.tool_id === "antigravity" && diagnostics && Object.keys(diagnostics).length > 0;
  const activityTimestamp = currentMeta ? new Date(currentMeta.activity_at || currentMeta.synced_at).toLocaleString(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }) : "";

  return (
    <div className="max-w-4xl mx-auto">
      <div style={{ marginBottom: 18 }}>
        {!currentMeta ? (
          <div className="text-gray-400 text-center">{t.loading}</div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 6 }}>
              <ToolGlyph id={currentMeta.tool_id} size={32} />
              <h2 style={{ margin: 0, minWidth: 0, flex: "1 1 280px", fontSize: "clamp(20px, 3vw, 26px)", fontWeight: 600, color: "var(--aurora-fg1)", letterSpacing: "-0.02em" }}>
                {currentMeta.title || currentMeta.relative_path}
              </h2>
              <Btn size="sm" variant="glass" icon="arrow_down" onClick={() => setShowExport(true)}>
                Export
              </Btn>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, fontSize: 12, color: "var(--aurora-fg3)" }}>
              <Chip>{currentMeta.tool_id}</Chip>
              <span>{currentMeta.message_count} {t.conversation.messages}</span>
              {currentAgentLabel && (
                <span
                  title={currentAgentPath}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                    padding: "4px 9px",
                    border: "1px solid color-mix(in srgb, var(--aurora-accent) 24%, var(--aurora-border))",
                    borderRadius: 999,
                    background: "var(--aurora-accent-soft)",
                    color: "var(--aurora-accent)",
                    fontSize: 10.5,
                    fontWeight: 700,
                  }}
                >
                  <Icon name="layers" size={11} />
                  Subagent · {currentAgentLabel}
                  {currentAgentNickname && currentAgentNickname.toLocaleLowerCase() !== currentAgentLabel.toLocaleLowerCase() && (
                    <span style={{ opacity: 0.68, fontWeight: 600 }}>· codename {currentAgentNickname}</span>
                  )}
                </span>
              )}
              <SubagentBadge
                count={currentMeta.subagent_count}
                orphan={currentMeta.is_subagent_orphan}
                subagents={currentMeta.subagents}
              />
              {plans.length > 0 && <Chip tone="warn">{plans.length} artifacts</Chip>}
              {hasDiagnostics && diagnostics.step_fetch_failed && <Chip tone="danger">{t.conversation.stepFetchFailed}</Chip>}
              <span className="basis-full pt-0.5 sm:basis-auto sm:pt-0">
                {t.conversation.lastActivity}: {activityTimestamp}
              </span>
            </div>
          </>
        )}
      </div>
      {hasDiagnostics && diagnostics && (
        <div style={{ marginBottom: 18, borderRadius: 16, border: "1px solid rgba(251,191,36,0.25)", background: "rgba(251,191,36,0.08)", padding: 14 }}>
          <div style={{ marginBottom: 4, fontSize: 13, fontWeight: 600, color: "#92400E" }}>{t.conversation.diagnostics}</div>
          <div style={{ marginBottom: 10, fontSize: 11, color: "#B45309" }}>{t.conversation.diagnosticsHelp}</div>
          <div className="flex flex-wrap gap-2 text-xs">
            <DiagChip
              label={t.conversation.plannerResponses}
              value={diagnostics.assistant_response_count ?? 0}
            />
            <DiagChip
              label={t.conversation.thinkingOnly}
              value={diagnostics.assistant_thinking_only_count ?? 0}
            />
            <DiagChip
              label={t.conversation.metadataRecovered}
              value={diagnostics.assistant_fallback_count ?? 0}
            />
            <DiagChip
              label={t.conversation.transcriptRecovered}
              value={(diagnostics.transcript_messages ?? 0) + (diagnostics.offline_pb_transcript_messages ?? 0)}
            />
            <DiagChip
              label={t.conversation.offlineVscdbRecovered}
              value={diagnostics.offline_vscdb_messages ?? 0}
            />
            <DiagChip
              label={t.conversation.chatExportRecovered}
              value={diagnostics.chat_export_messages ?? 0}
            />
            <DiagChip
              label={t.conversation.offlinePbRecovered}
              value={diagnostics.offline_pb_total_messages ?? 0}
            />
            <DiagChip
              label={t.conversation.brainArtifacts}
              value={diagnostics.brain_file_count ?? 0}
            />
            <DiagChip
              label={t.conversation.browserFrames}
              value={diagnostics.browser_recording_frame_count ?? 0}
            />
            <DiagChip
              label={t.conversation.browserHighlights}
              value={diagnostics.browser_recording_highlight_count ?? 0}
            />
            <DiagFlag
              label={t.conversation.stepFetchFailed}
              enabled={Boolean(diagnostics.step_fetch_failed)}
            />
            <DiagFlag
              label={t.conversation.pbShellOnly}
              enabled={Boolean(diagnostics.pb_shell_only)}
            />
            <DiagFlag
              label={t.conversation.truncated}
              enabled={Boolean(diagnostics.messages_truncated)}
            />
            {typeof diagnostics.endpoint_count === "number" && (
              <DiagChip label="endpoints" value={diagnostics.endpoint_count} />
            )}
            {typeof diagnostics.step_count === "number" && (
              <DiagChip label="steps" value={diagnostics.step_count} />
            )}
          </div>
        </div>
      )}
      <ConversationViewer
        documentId={docId}
        prompts={prompts}
        syncVersion={syncVersion}
        toolId={currentMeta?.tool_id}
        totalMessages={currentMeta?.message_count}
        activeTaskState={currentMeta?.active_task_state}
        artifacts={plans}
      />
      {showExport && (
        <MarkdownExportDialog documentId={docId} onClose={() => setShowExport(false)} />
      )}
    </div>
  );
}

function humanizeAgentPath(agentPath: string): string {
  const tail = agentPath.replace(/\/$/, "").split("/").pop() || "Subagent";
  const acronyms = new Set(["ai", "api", "cli", "cpu", "db", "etl", "gpu", "rca", "rss", "slo", "ui"]);
  return tail
    .split(/[_-]+/)
    .filter(Boolean)
    .map((word) => acronyms.has(word.toLocaleLowerCase())
      ? word.toLocaleUpperCase()
      : `${word.charAt(0).toLocaleUpperCase()}${word.slice(1)}`)
    .join(" ");
}

function DiagChip({ label, value }: { label: string; value: number }) {
  return (
    <span
      style={{
        borderRadius: 9999,
        border: "1px solid rgba(251,191,36,0.25)",
        background: "var(--aurora-surface-solid)",
        padding: "3px 10px",
        color: "#92400E",
      }}
    >
      {fmt("{label}: {value}", { label, value })}
    </span>
  );
}

function DiagFlag({ label, enabled }: { label: string; enabled: boolean }) {
  return (
    <span
      style={{
        borderRadius: 9999,
        padding: "3px 10px",
        border: enabled ? "1px solid rgba(244,63,94,0.4)" : "1px solid var(--aurora-border)",
        background: enabled ? "rgba(244,63,94,0.12)" : "var(--aurora-surface-solid)",
        color: enabled ? "#BE123C" : "var(--aurora-fg4)",
      }}
    >
      {label}
    </span>
  );
}
