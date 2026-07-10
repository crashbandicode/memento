"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { fmt, useI18n } from "@/lib/i18n";
import { getApiBase, authFetch } from "@/lib/api-client";
import { mergeProjectFiles } from "@/lib/project-files";
import { Icon, CategoryIcon } from "@/components/aurora/Icon";
import { Btn, Glass, SectionLabel, TopBar } from "@/components/aurora/primitives";
import SubagentBadge from "@/components/conversations/SubagentBadge";

interface FileItem { id: string; title: string; relative_path: string; category: string; content_type: string; file_size_bytes: number; activity_at?: string | null; synced_at: string; subagent_count?: number; is_subagent_orphan?: boolean; }
interface ProjectInfo { id: string; slug: string; title: string; tool_id: string; source_path: string | null; }
interface HierarchyFilesResponse { total: number; files: FileItem[]; project: ProjectInfo | null; }

type LoadState = "loading" | "success" | "error";

const PAGE_SIZE = 100;

async function fetchProjectFiles(
  deviceId: string,
  toolId: string,
  projectId: string,
  offset: number,
  signal: AbortSignal,
): Promise<HierarchyFilesResponse> {
  const query = new URLSearchParams({
    project_id: projectId,
    offset: String(offset),
    limit: String(PAGE_SIZE),
  });
  const response = await authFetch(
    `${getApiBase()}/api/hierarchy/devices/${deviceId}/tools/${toolId}/files?${query}`,
    { signal },
  );

  if (!response.ok) {
    const status = response.statusText
      ? `${response.status} ${response.statusText}`
      : String(response.status);
    throw new Error(`HTTP ${status}`);
  }

  const data = (await response.json()) as Partial<HierarchyFilesResponse>;
  if (!Number.isFinite(data.total) || (data.total ?? -1) < 0 || !Array.isArray(data.files)) {
    throw new Error("Invalid project files response");
  }

  return {
    total: data.total as number,
    files: data.files,
    project: data.project ?? null,
  };
}

export default function DeviceToolProjectPage() {
  const params = useParams();
  const { deviceId, toolId, projectId } = params as { deviceId: string; toolId: string; projectId: string };
  const { t, locale } = useI18n();
  const dateFmt = locale === "zh-CN" ? "zh-CN" : "en-US";

  const [files, setFiles] = useState<FileItem[]>([]);
  const [total, setTotal] = useState(0);
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState(0);
  const [retryToken, setRetryToken] = useState(0);
  const generationRef = useRef(0);
  const activeControllerRef = useRef<AbortController | null>(null);
  const loadingMoreRef = useRef(false);

  useEffect(() => {
    const generation = ++generationRef.current;
    activeControllerRef.current?.abort();
    const controller = new AbortController();
    activeControllerRef.current = controller;
    loadingMoreRef.current = false;

    setFiles([]);
    setTotal(0);
    setProject(null);
    setNextOffset(0);
    setLoadState("loading");
    setLoadError(null);
    setLoadingMore(false);
    setLoadMoreError(null);

    void (async () => {
      try {
        const data = await fetchProjectFiles(deviceId, toolId, projectId, 0, controller.signal);
        if (controller.signal.aborted || generation !== generationRef.current) return;
        if (data.total > 0 && data.files.length === 0) {
          throw new Error("Project files response was unexpectedly empty");
        }

        setFiles(mergeProjectFiles([], data.files));
        setTotal(data.total);
        setProject(data.project);
        setNextOffset(data.files.length);
        setLoadState("success");
      } catch (error) {
        if (controller.signal.aborted || generation !== generationRef.current) return;
        setLoadError(error instanceof Error ? error.message : "Unknown error");
        setLoadState("error");
      } finally {
        if (generation === generationRef.current && activeControllerRef.current === controller) {
          activeControllerRef.current = null;
        }
      }
    })();

    return () => {
      controller.abort();
      if (activeControllerRef.current === controller) activeControllerRef.current = null;
    };
  }, [deviceId, toolId, projectId, retryToken]);

  const loadMore = useCallback(async () => {
    if (loadState !== "success" || loadingMoreRef.current || nextOffset >= total) return;

    const generation = generationRef.current;
    const offset = nextOffset;
    activeControllerRef.current?.abort();
    const controller = new AbortController();
    activeControllerRef.current = controller;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    setLoadMoreError(null);

    try {
      const data = await fetchProjectFiles(deviceId, toolId, projectId, offset, controller.signal);
      if (controller.signal.aborted || generation !== generationRef.current) return;
      if (data.files.length === 0 && offset < data.total) {
        throw new Error("Project files pagination did not advance");
      }

      setFiles((current) => mergeProjectFiles(current, data.files));
      setTotal(data.total);
      setProject(data.project);
      setNextOffset(offset + data.files.length);
    } catch (error) {
      if (controller.signal.aborted || generation !== generationRef.current) return;
      setLoadMoreError(error instanceof Error ? error.message : "Unknown error");
    } finally {
      if (generation === generationRef.current) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
      if (activeControllerRef.current === controller) activeControllerRef.current = null;
    }
  }, [deviceId, loadState, nextOffset, projectId, toolId, total]);

  const byCategory: Record<string, FileItem[]> = {};
  for (const f of files) (byCategory[f.category] ??= []).push(f);

  return (
    <div className="max-w-5xl mx-auto">
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--aurora-fg4)", marginBottom: 8, flexWrap: "wrap" }}>
        <Link href="/devices" style={{ color: "var(--aurora-fg4)" }}>{t.nav.devices}</Link>
        <Icon name="chevron_right" size={12} />
        <span>{deviceId.slice(0, 8)}</span>
        <Icon name="chevron_right" size={12} />
        <Link href={`/devices/${deviceId}/tools/${toolId}`} style={{ color: "var(--aurora-fg4)", textTransform: "capitalize" }}>
          {toolId.replace("_", " ")}
        </Link>
        <Icon name="chevron_right" size={12} />
        <span style={{ color: "var(--aurora-fg2)" }}>{t.projects}</span>
      </div>

      <TopBar
        title={project?.title || (projectId === "none" ? "(No Project)" : projectId.slice(0, 8))}
        subtitle={
          loadState === "loading"
            ? t.loading
            : loadState === "error"
              ? t.projectPage.loadFailed
              : project?.source_path
                ? `${project.source_path} · ${total} ${t.files}`
                : `${total} ${t.files}`
        }
        right={
          project && (
            <Link href={`/projects/${project.id}/timeline`} prefetch={false} style={{ textDecoration: "none" }}>
              <Btn size="sm" icon="target">{t.timeline.title}</Btn>
            </Link>
          )
        }
      />

      {loadState === "loading" && (
        <Glass padding={40} radius={20} style={{ textAlign: "center" }}>
          <div role="status" aria-live="polite" style={{ color: "var(--aurora-fg3)", fontSize: 13 }}>
            {t.loading}
          </div>
        </Glass>
      )}

      {loadState === "error" && (
        <Glass padding={40} radius={20} style={{ textAlign: "center" }}>
          <div role="alert">
            <p style={{ color: "var(--aurora-fg2)", fontSize: 13, margin: "0 0 4px" }}>{t.projectPage.loadFailed}</p>
            {loadError && (
              <p style={{ color: "var(--aurora-fg4)", fontSize: 11, margin: "0 0 16px" }}>{loadError}</p>
            )}
          </div>
          <Btn size="sm" variant="glass" icon="refresh" onClick={() => setRetryToken((token) => token + 1)}>
            {t.projectPage.retry}
          </Btn>
        </Glass>
      )}

      {loadState === "success" && Object.entries(byCategory).map(([cat, catFiles]) => (
        <div key={cat} style={{ marginBottom: 24 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "8px 4px 12px" }}>
            <CategoryIcon category={cat} size={14} />
            <SectionLabel style={{ margin: 0 }}>
              {(t.category as Record<string, string>)[cat] || cat} <span style={{ textTransform: "none", color: "var(--aurora-fg4)", fontWeight: 400 }}>({catFiles.length})</span>
            </SectionLabel>
          </div>
          <Glass padding={6} radius={18}>
            {catFiles.map((f) => {
              const href = f.category === "conversation" ? `/conversations/${f.id}` : `/documents/${f.id}`;
              return (
                <Link
                  key={f.id}
                  href={href}
                  prefetch={false}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "10px 12px",
                    borderRadius: 12,
                    textDecoration: "none",
                  }}
                >
                  <div
                    style={{
                      width: 32, height: 32, borderRadius: 10, flexShrink: 0,
                      background: "var(--aurora-accent-soft)", color: "var(--aurora-accent)",
                      display: "flex", alignItems: "center", justifyContent: "center",
                    }}
                  >
                    <CategoryIcon category={cat} size={14} />
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, color: "var(--aurora-fg1)", letterSpacing: "-0.01em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {f.title || f.relative_path.split("/").pop()}
                    </div>
                    {Boolean(f.subagent_count) && (
                      <div style={{ marginTop: 4 }}>
                        <SubagentBadge count={f.subagent_count} orphan={f.is_subagent_orphan} />
                      </div>
                    )}
                    <div style={{ fontSize: 11, color: "var(--aurora-fg4)", fontFamily: "ui-monospace,monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {f.relative_path}
                    </div>
                  </div>
                  <span style={{ fontSize: 11, color: "var(--aurora-fg4)", flexShrink: 0 }}>{(f.file_size_bytes / 1024).toFixed(1)}KB</span>
                  <span style={{ fontSize: 11, color: "var(--aurora-fg4)", flexShrink: 0 }}>
                    {new Date(
                      f.category === "conversation" && f.activity_at
                        ? f.activity_at
                        : f.synced_at,
                    ).toLocaleString(dateFmt, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                  </span>
                </Link>
              );
            })}
          </Glass>
        </div>
      ))}

      {loadState === "success" && total === 0 && (
        <Glass padding={40} radius={20} style={{ textAlign: "center" }}>
          <p style={{ color: "var(--aurora-fg4)", fontSize: 13 }}>{t.noData}</p>
        </Glass>
      )}

      {loadState === "success" && (nextOffset < total || loadMoreError) && (
        <div style={{ textAlign: "center", padding: "0 0 24px" }}>
          {loadMoreError && (
            <div role="alert" style={{ marginBottom: 10 }}>
              <p style={{ color: "var(--aurora-fg2)", fontSize: 13, margin: "0 0 2px" }}>{t.projectPage.loadFailed}</p>
              <p style={{ color: "var(--aurora-fg4)", fontSize: 11, margin: 0 }}>{loadMoreError}</p>
            </div>
          )}
          <Btn
            size="sm"
            variant="glass"
            icon={loadMoreError ? "refresh" : "chevron_down"}
            onClick={() => void loadMore()}
            disabled={loadingMore}
            aria-busy={loadingMore}
          >
            {loadingMore
              ? t.projectPage.loadingMore
              : loadMoreError
                ? t.projectPage.retry
                : fmt(t.projectPage.loadMore, { loaded: files.length, total })}
          </Btn>
        </div>
      )}
    </div>
  );
}
