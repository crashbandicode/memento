"use client";

import { useEffect, useMemo, useState } from "react";
import { api, ConversationMarkdownExportSettings, ProjectSummary, ToolSummary } from "@/lib/api-client";
import { copyMarkdownToClipboard } from "@/lib/rich-clipboard";
import {
  DEFAULT_CONVERSATION_VISIBILITY,
  readConversationVisibility,
  type ConversationVisibility,
} from "@/lib/conversation-visibility";
import { Btn, Chip, GhostInput, Glass, SectionLabel } from "@/components/aurora/primitives";
import { Icon, ToolGlyph } from "@/components/aurora/Icon";

interface MarkdownExportFormProps {
  documentId?: string;
  onClose?: () => void;
}

function localDayBoundary(date: string, end: boolean): string | null {
  if (!date) return null;
  return new Date(`${date}T${end ? "23:59:59.999" : "00:00:00"}`).toISOString();
}

function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export function MarkdownExportForm({ documentId, onClose }: MarkdownExportFormProps) {
  const global = !documentId;
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [promptRange, setPromptRange] = useState("");
  const [query, setQuery] = useState("");
  const [tools, setTools] = useState<ToolSummary[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [projectId, setProjectId] = useState("");
  const [includeSubagents, setIncludeSubagents] = useState(false);
  const [includeLowActivity, setIncludeLowActivity] = useState(false);
  const [contentFilters, setContentFilters] = useState<ConversationVisibility>(
    DEFAULT_CONVERSATION_VISIBILITY,
  );
  const [includeTimestamps, setIncludeTimestamps] = useState(true);
  const [output, setOutput] = useState<"zip" | "combined">("zip");
  const [maxThreads, setMaxThreads] = useState(250);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!documentId) return;
    setContentFilters(readConversationVisibility(documentId));
  }, [documentId]);

  useEffect(() => {
    if (!global) return;
    let cancelled = false;
    Promise.all([api.getTools(), api.getProjects()])
      .then(([toolItems, projectItems]) => {
        if (!cancelled) {
          setTools(toolItems);
          setProjects(projectItems);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [global]);

  const dateError = useMemo(
    () => startDate && endDate && startDate > endDate
      ? "Start date must be before the end date."
      : "",
    [startDate, endDate],
  );

  const toggleTool = (id: string) => {
    setSelectedTools((current) => current.includes(id)
      ? current.filter((item) => item !== id)
      : [...current, id]);
  };

  const toggleContent = (key: keyof ConversationVisibility) => {
    setContentFilters((current) => ({ ...current, [key]: !current[key] }));
  };

  const settings = (): ConversationMarkdownExportSettings => ({
    start_at: localDayBoundary(startDate, false),
    end_at: localDayBoundary(endDate, true),
    prompt_range: promptRange.trim(),
    query: query.trim(),
    tool_ids: selectedTools,
    project_ids: projectId ? [projectId] : [],
    include_subagents: includeSubagents,
    include_low_activity: includeLowActivity,
    include_user: contentFilters.user,
    include_assistant: contentFilters.assistant,
    include_tools: contentFilters.tools,
    include_tasks: contentFilters.tasks,
    include_agents: contentFilters.agents,
    include_thinking: contentFilters.thinking,
    include_session_context: contentFilters.context,
    include_timestamps: includeTimestamps,
    output,
    max_threads: maxThreads,
  });

  const runExport = async () => {
    if (dateError) {
      setError(dateError);
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const download = documentId
        ? await api.exportConversationMarkdown(documentId, settings())
        : await api.exportConversationsMarkdown(settings());
      saveDownload(download.blob, download.filename);
      if (download.exportedThreads !== null) {
        const suffix = download.truncated
          ? ` The result was capped at ${download.exportedThreads} of ${download.matchingThreads} matching threads.`
          : "";
        setNotice(`Exported ${download.exportedThreads} thread${download.exportedThreads === 1 ? "" : "s"}.${suffix}`);
      } else {
        setNotice("Markdown export is ready.");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Export failed.");
    } finally {
      setBusy(false);
    }
  };

  const copyRichText = async () => {
    if (!documentId) return;
    if (dateError) {
      setError(dateError);
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const markdownPromise = api.exportConversationMarkdown(documentId, settings())
        .then((download) => download.blob.text());
      const copiedFormat = await copyMarkdownToClipboard(await markdownPromise, "rich");
      setNotice(copiedFormat === "rich"
        ? "Copied as rich text, with Markdown as the plain-text fallback."
        : "Rich clipboard formats are unavailable; copied Markdown instead.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not copy the thread.");
    } finally {
      setBusy(false);
    }
  };

  const contentOptions: Array<{ key: keyof ConversationVisibility; label: string }> = [
    { key: "user", label: "User prompts" },
    { key: "assistant", label: "Agent messages" },
    { key: "tools", label: "Tool calls and results" },
    { key: "tasks", label: "Tasks" },
    { key: "agents", label: "Agent activity" },
    { key: "thinking", label: "Agent thinking" },
    { key: "context", label: "Session context" },
  ];

  return (
    <div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="From date" hint="Uses your local timezone">
          <GhostInput
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
            aria-label="Export from date"
            wrapStyle={{ width: "100%" }}
          />
        </Field>
        <Field label="Through date" hint="Includes the entire day">
          <GhostInput
            type="date"
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
            aria-label="Export through date"
            wrapStyle={{ width: "100%" }}
          />
        </Field>
      </div>

      <div className="grid gap-4 mt-4 sm:grid-cols-2">
        <Field label="Prompt numbers" hint="Examples: 1-3,7 or 10-">
          <GhostInput
            value={promptRange}
            onChange={(event) => setPromptRange(event.target.value)}
            placeholder="All prompts"
            aria-label="Prompt number range"
            wrapStyle={{ width: "100%" }}
          />
        </Field>
        {global ? (
          <Field label="Message query" hint="Selects threads using the search index">
            <GhostInput
              icon="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Any message text"
              aria-label="Export message query"
              wrapStyle={{ width: "100%" }}
            />
          </Field>
        ) : (
          <div className="hidden sm:block" />
        )}
      </div>

      {global && tools.length > 0 && (
        <div className="mt-5">
          <SectionLabel style={{ margin: "0 0 8px" }}>Tools</SectionLabel>
          <div className="flex flex-wrap gap-2">
            {tools.map((tool) => {
              const active = selectedTools.includes(tool.id);
              return (
                <button
                  key={tool.id}
                  type="button"
                  onClick={() => toggleTool(tool.id)}
                  aria-pressed={active}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 7,
                    padding: "6px 10px",
                    borderRadius: 10,
                    border: active ? "1px solid var(--aurora-accent)" : "1px solid var(--aurora-border)",
                    background: active ? "var(--aurora-accent-soft)" : "var(--aurora-surface-solid)",
                    color: active ? "var(--aurora-accent)" : "var(--aurora-fg2)",
                    fontSize: 12,
                    cursor: "pointer",
                  }}
                >
                  <ToolGlyph id={tool.id} size={18} />
                  {tool.display_name}
                </button>
              );
            })}
          </div>
          <p style={{ margin: "7px 0 0", fontSize: 11, color: "var(--aurora-fg4)" }}>
            No selection exports every tool.
          </p>
        </div>
      )}

      {global && projects.length > 0 && (
        <div className="mt-5">
          <Field label="Project" hint="Optional; export conversations from one project">
            <select
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              aria-label="Export project"
              style={selectStyle}
            >
              <option value="">All projects</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.title} · {project.tool_id.replace("_", " ")}
                </option>
              ))}
            </select>
          </Field>
        </div>
      )}

      <div className="mt-5">
        <SectionLabel style={{ margin: "0 0 8px" }}>
          Content filters
          {documentId ? " · matched to Display" : ""}
        </SectionLabel>
        <div className="grid gap-2 sm:grid-cols-2">
          {contentOptions.map((option) => (
            <Toggle
              key={option.key}
              label={option.label}
              checked={contentFilters[option.key]}
              onChange={() => toggleContent(option.key)}
            />
          ))}
          <Toggle label="Message timestamps" checked={includeTimestamps} onChange={setIncludeTimestamps} />
          {global && (
            <Toggle label="Include subagent threads" checked={includeSubagents} onChange={setIncludeSubagents} />
          )}
          {global && (
            <Toggle label="Include virtually empty threads" checked={includeLowActivity} onChange={setIncludeLowActivity} />
          )}
        </div>
      </div>

      {global && (
        <div className="grid gap-4 mt-5 sm:grid-cols-2">
          <Field label="Archive layout" hint="ZIP keeps each thread in its own file">
            <select
              value={output}
              onChange={(event) => setOutput(event.target.value as "zip" | "combined")}
              aria-label="Markdown archive layout"
              style={selectStyle}
            >
              <option value="zip">ZIP · one Markdown file per thread</option>
              <option value="combined">One combined Markdown file</option>
            </select>
          </Field>
          <Field label="Maximum threads" hint="A safety cap; newest matching threads win">
            <GhostInput
              type="number"
              min={1}
              max={1000}
              value={maxThreads}
              onChange={(event) => setMaxThreads(Math.max(1, Math.min(1000, Number(event.target.value) || 1)))}
              aria-label="Maximum exported threads"
              wrapStyle={{ width: "100%" }}
            />
          </Field>
        </div>
      )}

      <div
        style={{
          marginTop: 18,
          padding: "10px 12px",
          borderRadius: 12,
          background: "var(--aurora-chip)",
          color: "var(--aurora-fg3)",
          fontSize: 12,
          lineHeight: 1.5,
        }}
      >
        <Icon name="message" size={13} style={{ display: "inline", marginRight: 7, verticalAlign: -2 }} />
        Prompt and date filters keep the full response turn through the next human prompt. Content filters match the Display options on the thread.
      </div>

      {(error || notice) && (
        <div className="mt-3" role={error ? "alert" : "status"}>
          <Chip tone={error ? "danger" : "success"}>{error || notice}</Chip>
        </div>
      )}

      <div
        data-export-actions
        className="sticky bottom-0 z-20 -mx-4 mt-5 grid grid-cols-2 gap-2 border-t px-4 pb-1 pt-3 sm:static sm:mx-0 sm:flex sm:flex-wrap sm:justify-end sm:border-0 sm:p-0"
        style={{
          borderColor: "var(--aurora-border)",
          background: "var(--aurora-surface-solid)",
        }}
      >
        {onClose && (
          <div className="hidden sm:block">
            <Btn variant="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          </div>
        )}
        {documentId && (
          <Btn className="w-full sm:w-auto" variant="glass" icon="copy" onClick={() => void copyRichText()} disabled={busy || Boolean(dateError)}>
            {busy ? "Working…" : "Copy rich text"}
          </Btn>
        )}
        <Btn className="w-full sm:w-auto" icon="arrow_down" onClick={() => void runExport()} disabled={busy || Boolean(dateError)}>
          {busy ? "Preparing export…" : global ? "Export conversations" : "Export thread"}
        </Btn>
      </div>
    </div>
  );
}

export function MarkdownExportDialog({ documentId, onClose }: { documentId: string; onClose: () => void }) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-6"
      style={{ background: "rgba(15,23,42,0.48)", backdropFilter: "blur(8px)" }}
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="markdown-export-title"
    >
      <Glass
        padding="clamp(16px, 3vw, 24px)"
        radius={22}
        className="w-full max-w-2xl max-h-[92dvh] overflow-y-auto sm:max-h-[92vh]"
        style={{ background: "var(--aurora-surface-solid)" }}
      >
        <div className="flex items-start justify-between gap-4 mb-5">
          <div>
            <h2 id="markdown-export-title" style={{ margin: 0, fontSize: 20, fontWeight: 600, color: "var(--aurora-fg1)" }}>
              Export thread as Markdown
            </h2>
            <p style={{ margin: "5px 0 0", fontSize: 13, color: "var(--aurora-fg3)" }}>
              Preserve prose, code, tables, tool calls, questions, and answers.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close Markdown export"
            style={{ padding: 5, border: 0, background: "transparent", color: "var(--aurora-fg3)", cursor: "pointer" }}
          >
            <Icon name="close" size={18} />
          </button>
        </div>
        <MarkdownExportForm documentId={documentId} onClose={onClose} />
      </Glass>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "block" }}>
      <span style={{ display: "block", marginBottom: 6, fontSize: 12, fontWeight: 600, color: "var(--aurora-fg2)" }}>{label}</span>
      {children}
      {hint && <span style={{ display: "block", marginTop: 5, fontSize: 10.5, color: "var(--aurora-fg4)" }}>{hint}</span>}
    </label>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        minHeight: 42,
        padding: "9px 11px",
        borderRadius: 12,
        border: "1px solid var(--aurora-border)",
        background: checked ? "var(--aurora-accent-soft)" : "var(--aurora-surface-solid)",
        color: checked ? "var(--aurora-accent)" : "var(--aurora-fg2)",
        fontSize: 12,
        cursor: "pointer",
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        style={{ accentColor: "var(--aurora-accent)", width: 15, height: 15 }}
      />
      {label}
    </label>
  );
}

const selectStyle: React.CSSProperties = {
  width: "100%",
  height: 40,
  padding: "0 11px",
  borderRadius: 12,
  border: "1px solid var(--aurora-border)",
  background: "var(--aurora-surface-solid)",
  color: "var(--aurora-fg1)",
  fontSize: 12,
};
