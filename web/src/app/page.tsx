"use client";

import { useI18n } from "@/lib/i18n";

export default function HomePage() {
  const { t } = useI18n();

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      role="status"
      aria-live="polite"
      style={{ color: "var(--aurora-fg3)" }}
    >
      {t.loading}
    </div>
  );
}
