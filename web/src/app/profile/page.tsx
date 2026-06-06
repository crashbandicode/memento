"use client";

import { useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useI18n } from "@/lib/i18n";
import { api } from "@/lib/api-client";
import { Btn, Chip, Glass, TopBar, SectionLabel } from "@/components/aurora/primitives";

type ImportSummary = {
  machine_id: string;
  counts: Record<string, number>;
  warnings: string[];
};

export default function ProfilePage() {
  const { user, token, logout } = useAuth();
  const { t } = useI18n();
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [includeLogs, setIncludeLogs] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importSummary, setImportSummary] = useState<ImportSummary | null>(null);
  const [errMsg, setErrMsg] = useState<string>("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!user) {
    return (
      <div style={{ textAlign: "center", color: "var(--aurora-fg4)", marginTop: 80 }}>
        {t.loading}
      </div>
    );
  }

  const handleExport = async () => {
    if (!token) return;
    setErrMsg("");
    setExporting(true);
    try {
      const { blob, filename } = await api.exportData(token, includeLogs);
      // Trigger a download via an off-DOM <a download>. Works in
      // Chromium/Firefox/Safari; ObjectURL is revoked after a tick to
      // free the blob.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e: unknown) {
      setErrMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
    }
  };

  const handleImport = async () => {
    if (!token || !importFile) return;
    setErrMsg("");
    setImportSummary(null);
    setImporting(true);
    try {
      const result = await api.importData(token, importFile);
      setImportSummary(result);
      // Clear the file input so the user can pick another later without
      // confusing residual state.
      setImportFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (e: unknown) {
      setErrMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto">
      <TopBar title={t.profile.title} subtitle={t.profile.subtitle} />

      <SectionLabel>{t.admin.users}</SectionLabel>
      <Glass padding={22} radius={20} style={{ marginBottom: 20 }}>
        <Row label={t.profile.email} value={user.email} />
        <Row label={t.profile.name} value={user.name || "—"} />
        <Row label={t.profile.role} valueNode={<Chip>{user.role}</Chip>} />
        <Row
          label={t.profile.status}
          valueNode={<Chip tone={user.status === "active" ? "success" : "warn"}>{user.status}</Chip>}
        />
      </Glass>

      <SectionLabel>{t.profile.backup}</SectionLabel>
      <Glass padding={22} radius={20} style={{ marginBottom: 20 }}>
        <p style={{ fontSize: 13, color: "var(--aurora-fg3)", margin: "0 0 14px" }}>
          {t.profile.backupDesc}
        </p>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--aurora-fg3)", marginBottom: 12 }}>
          <input
            type="checkbox"
            checked={includeLogs}
            onChange={(e) => setIncludeLogs(e.target.checked)}
          />
          {t.profile.includeLogs}
        </label>
        <Btn size="sm" icon="arrow_down" onClick={handleExport} disabled={exporting}>
          {exporting ? t.profile.exporting : t.profile.exportBtn}
        </Btn>

        <hr style={{ border: 0, borderTop: "1px solid var(--aurora-border)", margin: "18px 0" }} />

        <p style={{ fontSize: 13, color: "var(--aurora-fg3)", margin: "0 0 12px" }}>
          {t.profile.restoreDesc}
        </p>
        <input
          ref={fileInputRef}
          type="file"
          accept=".zip,application/zip"
          onChange={(e) => setImportFile(e.target.files?.[0] ?? null)}
          style={{ fontSize: 12, marginBottom: 12, display: "block" }}
        />
        <Btn size="sm" icon="arrow_up" onClick={handleImport} disabled={!importFile || importing}>
          {importing ? t.profile.importing : t.profile.importBtn}
        </Btn>

        {importSummary && (
          <div
            style={{
              marginTop: 14,
              padding: "10px 12px",
              borderRadius: 10,
              background: "rgba(16,185,129,0.10)",
              color: "var(--aurora-fg2)",
              fontSize: 12,
            }}
          >
            <strong>{t.profile.importSuccess}</strong>
            <ul style={{ margin: "6px 0 0 18px", padding: 0, lineHeight: 1.7 }}>
              {Object.entries(importSummary.counts).map(([k, v]) => (
                <li key={k}>
                  <code>{k}</code>: {v}
                </li>
              ))}
            </ul>
            {importSummary.warnings.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary style={{ cursor: "pointer", color: "var(--aurora-fg3)" }}>
                  {t.profile.importWarnings} ({importSummary.warnings.length})
                </summary>
                <ul style={{ margin: "6px 0 0 18px" }}>
                  {importSummary.warnings.map((w, i) => (
                    <li key={i} style={{ color: "var(--aurora-fg4)" }}>{w}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}

        {errMsg && (
          <div
            style={{
              marginTop: 14,
              padding: "10px 12px",
              borderRadius: 10,
              background: "rgba(239,68,68,0.10)",
              color: "#B91C1C",
              fontSize: 12,
            }}
          >
            {errMsg}
          </div>
        )}
      </Glass>

      <div style={{ textAlign: "right" }}>
        <Btn variant="ghost" size="sm" icon="log_out" onClick={logout}>
          {t.profile.logout}
        </Btn>
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  valueNode,
}: {
  label: string;
  value?: string;
  valueNode?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        padding: "10px 0",
        borderBottom: "1px solid var(--aurora-border)",
        gap: 12,
      }}
    >
      <span style={{ fontSize: 13, color: "var(--aurora-fg3)" }}>{label}</span>
      {valueNode ?? (
        <span style={{ fontSize: 13, color: "var(--aurora-fg1)", fontWeight: 500 }}>{value}</span>
      )}
    </div>
  );
}
