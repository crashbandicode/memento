"use client";

import { useAuth } from "@/lib/auth-context";
import { useI18n } from "@/lib/i18n";
import { Btn, Chip, Glass, TopBar, SectionLabel } from "@/components/aurora/primitives";

export default function ProfilePage() {
  const { user, logout } = useAuth();
  const { t } = useI18n();

  if (!user) {
    return (
      <div style={{ textAlign: "center", color: "var(--aurora-fg4)", marginTop: 80 }}>
        {t.loading}
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto">
      <TopBar title={t.profile.title} subtitle={t.profile.subtitle} />

      <SectionLabel>{t.admin.users}</SectionLabel>
      <Glass padding={22} radius={20} style={{ marginBottom: 20 }}>
        <Row label={t.profile.email} value={user.email} />
        <Row label={t.profile.name} value={user.name || "—"} />
        <Row
          label={t.profile.role}
          valueNode={<Chip>{user.role}</Chip>}
        />
        <Row
          label={t.profile.status}
          valueNode={<Chip tone={user.status === "active" ? "success" : "warn"}>{user.status}</Chip>}
        />
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
