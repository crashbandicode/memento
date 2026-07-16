"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useI18n } from "@/lib/i18n";
import { getApiBase, authFetch } from "@/lib/api-client";
import { Icon, ToolGlyph, CategoryIcon } from "@/components/aurora/Icon";
import { Btn, Glass, TopBar, SectionLabel } from "@/components/aurora/primitives";
import BrowseFileRow from "@/components/conversations/BrowseFileRow";
import LowActivitySection from "@/components/conversations/LowActivitySection";

interface ProjectDetail {
  id: string;
  slug: string;
  title: string;
  tool_id: string;
  source_path: string;
  visibility: string;
  documents: {
    id: string;
    relative_path: string;
    category: string;
    title: string;
    file_size_bytes: number;
    activity_at?: string | null;
    synced_at: string;
    message_count?: number;
    is_low_activity?: boolean;
    subagent_count?: number;
    is_subagent_orphan?: boolean;
  }[];
}

export default function ProjectDetailPage() {
  const params = useParams();
  const projectId = params.id as string;
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const { t, locale } = useI18n();
  const dateFmt = locale === "zh-CN" ? "zh-CN" : "en-US";

  useEffect(() => {
    const controller = new AbortController();
    authFetch(`${getApiBase()}/api/projects/${projectId}`, {
      signal: controller.signal,
    })
      .then((r) => r.json())
      .then(setProject)
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error(error);
        }
      });
    return () => controller.abort();
  }, [projectId]);

  // Hit the server's per-project markdown export endpoint and trigger
  // a browser download. authFetch attaches the JWT; we read the body
  // as a Blob and click an off-DOM <a download> — same pattern the
  // account-level export uses on the Profile page.
  const handleExportMarkdown = async (pid: string, slug: string) => {
    try {
      const res = await authFetch(`${getApiBase()}/api/projects/${pid}/export.md`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `memento-context-${slug || "project"}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e: unknown) {
      alert(t.projects_page.exportFailed + ": " + (e instanceof Error ? e.message : String(e)));
    }
  };

  if (!project) return <div style={{ color: "var(--aurora-fg4)", textAlign: "center", marginTop: 80 }}>{t.loading}</div>;

  const documents = [
    ...new Map(project.documents.map((document) => [document.id, document])).values(),
  ];
  const byCategory: Record<string, typeof project.documents> = {};
  for (const d of documents) (byCategory[d.category] ??= []).push(d);

  return (
    <div className="max-w-5xl mx-auto">
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--aurora-fg4)", marginBottom: 8 }}>
        <Link href="/projects" style={{ color: "var(--aurora-fg4)" }}>{t.projects}</Link>
        <Icon name="chevron_right" size={12} />
        <Link href={`/tools/${project.tool_id}`} style={{ color: "var(--aurora-fg4)", textTransform: "capitalize" }}>
          {project.tool_id.replace("_", " ")}
        </Link>
      </div>

      <TopBar
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 12 }}>
            <ToolGlyph id={project.tool_id} size={34} />
            {project.title}
          </span>
        }
        subtitle={<span style={{ fontFamily: "ui-monospace,monospace" }}>{project.source_path}</span>}
        right={
          <>
            <Btn
              variant="ghost"
              size="sm"
              icon="arrow_down"
              onClick={() => handleExportMarkdown(projectId, project.slug || project.title || "project")}
              title={t.projects_page.exportMdHint}
            >
              {t.projects_page.exportMd}
            </Btn>
            <Link href={`/projects/${projectId}/conversations`} prefetch={false} style={{ textDecoration: "none" }}>
              <Btn variant="glass" size="sm" icon="message">{t.conversations}</Btn>
            </Link>
            <Link href={`/projects/${projectId}/timeline`} prefetch={false} style={{ textDecoration: "none" }}>
              <Btn size="sm" icon="target">{t.timeline.title}</Btn>
            </Link>
          </>
        }
      />

      {Object.entries(byCategory).map(([cat, docs]) => {
        const visibleDocs = cat === "conversation"
          ? docs.filter((doc) => !doc.is_low_activity)
          : docs;
        const lowActivityDocs = cat === "conversation"
          ? docs.filter((doc) => doc.is_low_activity)
          : [];
        const renderDoc = (d: (typeof docs)[number]) => {
          const href = cat === "conversation" ? `/conversations/${d.id}` : `/documents/${d.id}`;
          return (
            <BrowseFileRow
              key={d.id}
              href={href}
              category={cat}
              title={d.title || d.relative_path.split("/").pop() || ""}
              path={d.relative_path}
              size={cat === "conversation" && typeof d.message_count === "number"
                ? `${d.message_count} msgs`
                : `${(d.file_size_bytes / 1024).toFixed(1)}KB`}
              date={new Date(
                cat === "conversation" && d.activity_at ? d.activity_at : d.synced_at,
              ).toLocaleString(dateFmt, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
              subagentCount={d.subagent_count}
              isSubagentOrphan={d.is_subagent_orphan}
            />
          );
        };

        return (
          <div key={cat} style={{ marginBottom: 24 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "8px 4px 12px" }}>
              <CategoryIcon category={cat} size={16} />
              <SectionLabel style={{ margin: 0 }}>
                {(t.category as Record<string, string>)[cat] || cat}{" "}
                <span style={{ textTransform: "none", color: "var(--aurora-fg4)", fontWeight: 400 }}>({docs.length})</span>
              </SectionLabel>
            </div>
            {visibleDocs.length > 0 && (
              <Glass padding={6} radius={18}>
                {visibleDocs.map(renderDoc)}
              </Glass>
            )}
            <LowActivitySection
              count={lowActivityDocs.length}
              title={t.conversation.lowActivity}
              description={t.conversation.lowActivityHint}
            >
              {lowActivityDocs.map(renderDoc)}
            </LowActivitySection>
          </div>
        );
      })}

      {documents.length === 0 && (
        <Glass padding={40} radius={20} style={{ textAlign: "center" }}>
          <p style={{ color: "var(--aurora-fg4)", fontSize: 13 }}>{t.noData}</p>
        </Glass>
      )}
    </div>
  );
}
