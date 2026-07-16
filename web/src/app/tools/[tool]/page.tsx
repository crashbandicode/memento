"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ToolDetail, DocumentSummary, getApiBase, authFetch } from "@/lib/api-client";
import { useI18n, fmt } from "@/lib/i18n";
import { useDevice } from "@/lib/device-context";
import { ToolGlyph, CategoryIcon } from "@/components/aurora/Icon";
import { Btn, Chip, Glass, TopBar, SectionLabel } from "@/components/aurora/primitives";
import BrowseFileRow from "@/components/conversations/BrowseFileRow";
import LowActivitySection from "@/components/conversations/LowActivitySection";

type LoadState = "loading" | "success" | "error";

export default function ToolDetailPage() {
  const params = useParams();
  const toolId = params.tool as string;
  const [tool, setTool] = useState<ToolDetail | null>(null);
  const [files, setFiles] = useState<DocumentSummary[]>([]);
  const [projects, setProjects] = useState<{ id: string; title: string; document_count: number }[]>([]);
  const [categorySelection, setCategorySelection] = useState<{ scope: string; value?: string }>({ scope: "" });
  const [toolLoadState, setToolLoadState] = useState<LoadState>("loading");
  const [toolLoadError, setToolLoadError] = useState<string | null>(null);
  const [loadedToolKey, setLoadedToolKey] = useState<string | null>(null);
  const [toolRetryToken, setToolRetryToken] = useState(0);
  const [projectLoadState, setProjectLoadState] = useState<LoadState>("loading");
  const [projectLoadError, setProjectLoadError] = useState<string | null>(null);
  const [loadedProjectKey, setLoadedProjectKey] = useState<string | null>(null);
  const [projectRetryToken, setProjectRetryToken] = useState(0);
  const [fileLoadState, setFileLoadState] = useState<LoadState>("loading");
  const [fileLoadError, setFileLoadError] = useState<string | null>(null);
  const [loadedFileKey, setLoadedFileKey] = useState<string | null>(null);
  const [fileRetryToken, setFileRetryToken] = useState(0);
  const { t, locale } = useI18n();
  const { selectedDeviceId } = useDevice();
  const dateFmt = locale === "zh-CN" ? "zh-CN" : "en-US";
  const dq = selectedDeviceId ? `&device_id=${selectedDeviceId}` : "";
  const projectRequestKey = `${toolId}:${selectedDeviceId ?? "all"}`;
  const activeCategory = categorySelection.scope === projectRequestKey ? categorySelection.value : undefined;
  const fileRequestKey = `${projectRequestKey}:${activeCategory ?? "all"}`;

  useEffect(() => {
    const controller = new AbortController();
    setTool(null);
    setToolLoadState("loading");
    setToolLoadError(null);

    void (async () => {
      try {
        const response = await authFetch(`${getApiBase()}/api/tools/${toolId}?_=1${dq}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (controller.signal.aborted) return;
        setTool(data as ToolDetail);
        setLoadedToolKey(projectRequestKey);
        setToolLoadState("success");
      } catch (error) {
        if (controller.signal.aborted) return;
        setToolLoadError(error instanceof Error ? error.message : "Unknown error");
        setLoadedToolKey(projectRequestKey);
        setToolLoadState("error");
      }
    })();

    return () => controller.abort();
  }, [dq, projectRequestKey, toolId, toolRetryToken]);

  useEffect(() => {
    const controller = new AbortController();
    const projectQuery = new URLSearchParams({ tool_id: toolId });
    if (selectedDeviceId) projectQuery.set("device_id", selectedDeviceId);

    setProjects([]);
    setProjectLoadState("loading");
    setProjectLoadError(null);

    void (async () => {
      try {
        const response = await authFetch(`${getApiBase()}/api/projects?${projectQuery}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!Array.isArray(data)) throw new Error("Invalid projects response");
        if (controller.signal.aborted) return;
        setProjects(data);
        setLoadedProjectKey(projectRequestKey);
        setProjectLoadState("success");
      } catch (error) {
        if (controller.signal.aborted) return;
        setProjectLoadError(error instanceof Error ? error.message : "Unknown error");
        setLoadedProjectKey(projectRequestKey);
        setProjectLoadState("error");
      }
    })();

    return () => controller.abort();
  }, [projectRequestKey, projectRetryToken, selectedDeviceId, toolId]);

  useEffect(() => {
    const controller = new AbortController();
    const catParam = activeCategory ? `&category=${activeCategory}` : "";
    setFiles([]);
    setFileLoadState("loading");
    setFileLoadError(null);

    void (async () => {
      try {
        const response = await authFetch(
          `${getApiBase()}/api/tools/${toolId}/files?offset=0&limit=50${catParam}${dq}`,
          { signal: controller.signal },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!Array.isArray(data)) throw new Error("Invalid files response");
        if (controller.signal.aborted) return;
        setFiles(data);
        setLoadedFileKey(fileRequestKey);
        setFileLoadState("success");
      } catch (error) {
        if (controller.signal.aborted) return;
        setFileLoadError(error instanceof Error ? error.message : "Unknown error");
        setLoadedFileKey(fileRequestKey);
        setFileLoadState("error");
      }
    })();

    return () => controller.abort();
  }, [activeCategory, dq, fileRequestKey, fileRetryToken, toolId]);

  const visibleProjectState: LoadState = loadedProjectKey === projectRequestKey ? projectLoadState : "loading";
  const visibleProjects = visibleProjectState === "success" ? projects : [];
  const visibleFileState: LoadState = loadedFileKey === fileRequestKey ? fileLoadState : "loading";
  const visibleFiles = visibleFileState === "success" ? files : [];
  const primaryFiles = visibleFiles.filter((file) => file.category !== "conversation" || !file.is_low_activity);
  const lowActivityFiles = visibleFiles.filter((file) => file.category === "conversation" && file.is_low_activity);
  const visibleToolState: LoadState = loadedToolKey === projectRequestKey ? toolLoadState : "loading";

  if (visibleToolState === "loading") {
    return <div role="status" aria-live="polite" style={{ color: "var(--aurora-fg4)", marginTop: 80, textAlign: "center" }}>{t.loading}</div>;
  }
  if (visibleToolState === "error" || !tool) {
    return (
      <Glass padding={40} radius={20} style={{ maxWidth: 560, margin: "80px auto 0", textAlign: "center" }}>
        <div role="alert" style={{ marginBottom: 16 }}>
          <p style={{ color: "var(--aurora-fg2)", fontSize: 13, margin: "0 0 4px" }}>{t.tools.toolLoadFailed}</p>
          {toolLoadError && <p style={{ color: "var(--aurora-fg4)", fontSize: 11, margin: 0 }}>{toolLoadError}</p>}
        </div>
        <Btn size="sm" variant="glass" icon="refresh" onClick={() => setToolRetryToken((token) => token + 1)}>
          {t.projectPage.retry}
        </Btn>
      </Glass>
    );
  }

  const categories = Object.entries(tool.categories);
  const renderFile = (file: DocumentSummary) => (
    <BrowseFileRow
      key={file.id}
      href={file.category === "conversation" ? `/conversations/${file.id}` : `/documents/${file.id}`}
      category={file.category}
      title={file.title || file.relative_path}
      path={file.relative_path}
      size={file.category === "conversation" && typeof file.message_count === "number"
        ? `${file.message_count} msgs`
        : `${(file.file_size_bytes / 1024).toFixed(1)}KB`}
      date={new Date(
        file.category === "conversation" && file.activity_at ? file.activity_at : file.synced_at,
      ).toLocaleString(dateFmt, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
      subagentCount={file.subagent_count}
      isSubagentOrphan={file.is_subagent_orphan}
    />
  );

  return (
    <div className="max-w-6xl mx-auto">
      <TopBar
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 12 }}>
            <ToolGlyph id={toolId} size={44} />
            <span style={{ textTransform: "capitalize" }}>{tool.display_name}</span>
          </span>
        }
        subtitle={`${fmt(t.tools.filesCount, { count: tool.total_files })} · ${t.tools.lastSync}: ${tool.last_sync_at ? new Date(tool.last_sync_at).toLocaleString(dateFmt) : t.never}`}
      />

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 sm:gap-6">
        <div className="lg:col-span-1 space-y-4">
          <Glass padding={6} radius={18}>
            <div style={{ padding: "8px 12px" }}>
              <SectionLabel style={{ margin: 0 }}>{t.tools.categories}</SectionLabel>
            </div>
            <CatRow label={t.all} count={tool.total_files} active={!activeCategory} onClick={() => setCategorySelection({ scope: projectRequestKey })} />
            {categories.map(([cat, count]) => (
              <CatRow
                key={cat}
                icon={cat}
                label={(t.category as Record<string, string>)[cat] || cat}
                count={count}
                active={activeCategory === cat}
                onClick={() => setCategorySelection({ scope: projectRequestKey, value: cat })}
              />
            ))}
          </Glass>

          {visibleProjectState === "loading" && (
            <Glass padding={18} radius={18}>
              <div role="status" aria-live="polite" style={{ color: "var(--aurora-fg3)", fontSize: 12 }}>
                {t.loading}
              </div>
            </Glass>
          )}

          {visibleProjectState === "error" && (
            <Glass padding={18} radius={18}>
              <div role="alert" style={{ marginBottom: 10 }}>
                <p style={{ color: "var(--aurora-fg2)", fontSize: 12, margin: "0 0 2px" }}>{t.projectPage.listLoadFailed}</p>
                {projectLoadError && <p style={{ color: "var(--aurora-fg4)", fontSize: 10, margin: 0 }}>{projectLoadError}</p>}
              </div>
              <Btn size="sm" variant="glass" icon="refresh" onClick={() => setProjectRetryToken((token) => token + 1)}>
                {t.projectPage.retry}
              </Btn>
            </Glass>
          )}

          {visibleProjectState === "success" && visibleProjects.length > 0 && (
            <Glass padding={6} radius={18}>
              <div style={{ padding: "8px 12px" }}>
                <SectionLabel style={{ margin: 0 }}>{t.tools.projectsInTool}</SectionLabel>
              </div>
              {visibleProjects.map((p) => (
                <Link
                  key={p.id}
                  href={selectedDeviceId
                    ? `/devices/${selectedDeviceId}/tools/${toolId}/projects/${p.id}`
                    : `/projects/${p.id}`}
                  prefetch={false}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "8px 12px",
                    borderRadius: 12,
                    fontSize: 13,
                    color: "var(--aurora-fg2)",
                    textDecoration: "none",
                  }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.title}</span>
                  <span style={{ fontSize: 11, color: "var(--aurora-fg4)", marginLeft: 8 }}>{p.document_count}</span>
                </Link>
              ))}
            </Glass>
          )}
        </div>

        <div className="lg:col-span-3">
          {(visibleFileState !== "success" || primaryFiles.length > 0 || visibleFiles.length === 0) && (
            <Glass padding={6} radius={18}>
              <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--aurora-border)" }}>
                <SectionLabel style={{ margin: 0 }}>
                  {t.tools.fileList} ({visibleFiles.length})
                </SectionLabel>
              </div>
              {visibleFileState === "loading" ? (
                <div role="status" aria-live="polite" style={{ textAlign: "center", color: "var(--aurora-fg3)", padding: 48, fontSize: 13 }}>
                  {t.loading}
                </div>
              ) : visibleFileState === "error" ? (
                <div style={{ textAlign: "center", padding: 40 }}>
                  <div role="alert" style={{ marginBottom: 12 }}>
                    <p style={{ color: "var(--aurora-fg2)", fontSize: 13, margin: "0 0 3px" }}>{t.tools.fileLoadFailed}</p>
                    {fileLoadError && <p style={{ color: "var(--aurora-fg4)", fontSize: 11, margin: 0 }}>{fileLoadError}</p>}
                  </div>
                  <Btn size="sm" variant="glass" icon="refresh" onClick={() => setFileRetryToken((token) => token + 1)}>
                    {t.projectPage.retry}
                  </Btn>
                </div>
              ) : visibleFiles.length === 0 ? (
                <div style={{ textAlign: "center", color: "var(--aurora-fg4)", padding: 48, fontSize: 13 }}>
                  {t.tools.noFiles}
                </div>
              ) : primaryFiles.map(renderFile)}
            </Glass>
          )}
          {visibleFileState === "success" && (
            <LowActivitySection
              count={lowActivityFiles.length}
              title={t.conversation.lowActivity}
              description={t.conversation.lowActivityHint}
            >
              {lowActivityFiles.map(renderFile)}
            </LowActivitySection>
          )}
        </div>
      </div>
    </div>
  );
}

function CatRow({
  icon, label, count, active, onClick,
}: { icon?: string; label: string; count: number; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        width: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "8px 12px",
        borderRadius: 12,
        fontSize: 13,
        cursor: "pointer",
        border: 0,
        background: active ? "var(--aurora-accent-soft)" : "transparent",
        color: active ? "var(--aurora-accent)" : "var(--aurora-fg2)",
        textAlign: "left",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
        {icon && <CategoryIcon category={icon} size={13} />}
        {label}
      </span>
      <Chip tone={active ? "accent" : "neutral"} style={{ padding: "2px 8px" }}>{count}</Chip>
    </button>
  );
}
