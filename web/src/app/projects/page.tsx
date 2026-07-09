"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useI18n } from "@/lib/i18n";
import { getApiBase, authFetch } from "@/lib/api-client";
import { useDevice } from "@/lib/device-context";
import { Icon, ToolGlyph } from "@/components/aurora/Icon";
import { Btn, Glass, TopBar, SectionLabel, Chip } from "@/components/aurora/primitives";

interface ProjectItem {
  id: string;
  slug: string;
  title: string;
  tool_id: string;
  source_path: string;
  document_count: number;
  created_at: string;
  updated_at: string | null;
}

type LoadState = "loading" | "success" | "error";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [filterTool, setFilterTool] = useState("");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadedRequestKey, setLoadedRequestKey] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const { t } = useI18n();
  const { selectedDeviceId } = useDevice();
  const requestKey = `${selectedDeviceId ?? "all"}:${filterTool || "all"}`;

  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams();
    if (filterTool) query.set("tool_id", filterTool);
    if (selectedDeviceId) query.set("device_id", selectedDeviceId);
    const url = `${getApiBase()}/api/projects${query.size ? `?${query}` : ""}`;

    setProjects([]);
    setLoadState("loading");
    setLoadError(null);

    void (async () => {
      try {
        const response = await authFetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!Array.isArray(data)) throw new Error("Invalid projects response");
        if (controller.signal.aborted) return;
        setProjects(data as ProjectItem[]);
        setLoadedRequestKey(requestKey);
        setLoadState("success");
      } catch (error) {
        if (controller.signal.aborted) return;
        setLoadError(error instanceof Error ? error.message : "Unknown error");
        setLoadedRequestKey(requestKey);
        setLoadState("error");
      }
    })();

    return () => controller.abort();
  }, [filterTool, requestKey, retryToken, selectedDeviceId]);

  const visibleState: LoadState = loadedRequestKey === requestKey ? loadState : "loading";
  const visibleProjects = visibleState === "success" ? projects : [];

  const byTool: Record<string, ProjectItem[]> = {};
  for (const p of visibleProjects) (byTool[p.tool_id] ??= []).push(p);

  return (
    <div className="max-w-6xl mx-auto">
      <TopBar
        title={t.projectPage.title}
        subtitle={visibleState === "loading" ? t.loading : `${visibleProjects.length} ${t.projects}`}
        right={
          <label className="aurora-input" style={{ padding: "8px 14px", minWidth: 180 }}>
            <Icon name="grid" size={14} style={{ color: "var(--aurora-fg3)" }} />
            <select value={filterTool} onChange={(e) => setFilterTool(e.target.value)}>
              <option value="">{t.all}</option>
              <option value="claude_code">Claude Code</option>
              <option value="openclaw">OpenClaw</option>
              <option value="codex">Codex</option>
              <option value="obsidian">Obsidian</option>
              <option value="cursor">Cursor</option>
            </select>
          </label>
        }
      />

      {visibleState === "loading" ? (
        <Glass padding={40} radius={22} style={{ textAlign: "center" }}>
          <p role="status" aria-live="polite" style={{ color: "var(--aurora-fg3)", fontSize: 13 }}>{t.loading}</p>
        </Glass>
      ) : visibleState === "error" ? (
        <Glass padding={40} radius={22} style={{ textAlign: "center" }}>
          <div role="alert">
            <p style={{ color: "var(--aurora-fg2)", fontSize: 13, margin: "0 0 4px" }}>{t.projectPage.listLoadFailed}</p>
            {loadError && <p style={{ color: "var(--aurora-fg4)", fontSize: 11, margin: "0 0 16px" }}>{loadError}</p>}
          </div>
          <Btn size="sm" variant="glass" icon="refresh" onClick={() => setRetryToken((token) => token + 1)}>
            {t.projectPage.retry}
          </Btn>
        </Glass>
      ) : visibleProjects.length === 0 ? (
        <Glass padding={40} radius={22} style={{ textAlign: "center" }}>
          <p style={{ color: "var(--aurora-fg4)", fontSize: 13 }}>{t.projectPage.noProjects}</p>
        </Glass>
      ) : (
        Object.entries(byTool).map(([toolId, toolProjects]) => (
          <div key={toolId} style={{ marginBottom: 24 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "8px 4px 12px" }}>
              <ToolGlyph id={toolId} size={26} />
              <SectionLabel style={{ margin: 0 }}>
                {toolId.replace("_", " ")} <span style={{ textTransform: "none", color: "var(--aurora-fg4)", fontWeight: 400 }}>({toolProjects.length})</span>
              </SectionLabel>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {toolProjects.map((p) => (
                <Glass key={p.id} hover padding={16} radius={18}>
                  <Link
                    href={selectedDeviceId
                      ? `/devices/${selectedDeviceId}/tools/${p.tool_id}/projects/${p.id}`
                      : `/projects/${p.id}`}
                    prefetch={false}
                    style={{ textDecoration: "none" }}
                  >
                    <div
                      style={{
                        fontSize: 14,
                        fontWeight: 600,
                        color: "var(--aurora-fg1)",
                        letterSpacing: "-0.01em",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {p.title}
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--aurora-fg4)",
                        fontFamily: "ui-monospace,monospace",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        marginBottom: 12,
                      }}
                    >
                      {p.source_path}
                    </div>
                  </Link>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                    <Chip>{p.document_count} {t.files}</Chip>
                    <Link
                      href={`/projects/${p.id}/timeline`}
                      prefetch={false}
                      style={{
                        fontSize: 11,
                        color: "var(--aurora-accent)",
                        fontWeight: 500,
                        letterSpacing: "-0.005em",
                        textDecoration: "none",
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      <Icon name="target" size={12} />
                      {t.timeline.title}
                    </Link>
                  </div>
                </Glass>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
