"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { AuthProvider, useAuth } from "@/lib/auth-context";
import { DeviceProvider } from "@/lib/device-context";
import { ThemeProvider } from "@/lib/theme-context";
import { I18nContext, locales, type Locale } from "@/lib/i18n";
import Sidebar from "@/components/layout/Sidebar";
import Header from "@/components/layout/Header";
import { AuroraBackdrop } from "@/components/aurora/AuroraBackdrop";

/** Detect the best initial locale: user's saved choice → browser preference → zh-CN default. */
function detectInitialLocale(): Locale {
  if (typeof window === "undefined") return "zh-CN";
  const saved = localStorage.getItem("dr_locale") as Locale | null;
  if (saved && saved in locales) return saved;
  // Browser preference. navigator.languages is ordered by priority.
  const prefs = (navigator.languages && navigator.languages.length
    ? navigator.languages
    : [navigator.language || ""]);
  for (const p of prefs) {
    const lower = p.toLowerCase();
    if (lower.startsWith("zh")) return "zh-CN";
    if (lower.startsWith("en")) return "en-US";
  }
  return "zh-CN";
}

export default function ClientLayout({ children }: { children: React.ReactNode }) {
  // The server and the browser's first render must use the same locale or
  // translated text cannot hydrate. Restore the saved/browser preference only
  // after hydration; requestAnimationFrame keeps the update out of the effect
  // body and avoids a cascading synchronous render.
  const [locale, setLocale] = useState<Locale>("zh-CN");
  const t = locales[locale].translations;
  const pathname = usePathname();

  useEffect(() => {
    const initialLocale = detectInitialLocale();
    if (initialLocale === "zh-CN") return;
    const frame = requestAnimationFrame(() => setLocale(initialLocale));
    return () => cancelAnimationFrame(frame);
  }, []);

  const handleSetLocale = (l: Locale) => {
    setLocale(l);
    localStorage.setItem("dr_locale", l);
  };

  // Server metadata (layout.tsx) hard-codes English tab title since it can't
  // read locale. On every route change, Next.js App Router re-applies the
  // layout metadata — including the English title — which clobbers whatever
  // we set on mount. Re-assert on every locale AND pathname change.
  useEffect(() => {
    document.title = `${t.app.title} — ${t.app.subtitle}`;
    document.documentElement.lang = locale === "zh-CN" ? "zh" : "en";
  }, [locale, t.app.title, t.app.subtitle, pathname]);

  return (
    <ThemeProvider>
      <I18nContext.Provider value={{ locale, t, setLocale: handleSetLocale }}>
        <AuthProvider>
          <AuroraBackdrop />
          <AppShell>{children}</AppShell>
        </AuthProvider>
      </I18nContext.Provider>
    </ThemeProvider>
  );
}

/** Renders Sidebar+Header only inside the authenticated app. Public entry,
 *  splash, auth, and share pages are always plain; protected children are not
 *  mounted until auth has resolved, which avoids duplicate route effects. */
function AppShell({ children }: { children: React.ReactNode }) {
  const { token, loading } = useAuth();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const pathname = usePathname();

  // Root is an auth-aware redirect and /splash owns its marketing nav.
  const isPublicEntry = pathname === "/" || pathname === "/splash";
  // Public share pages are read-only and shouldn't show the app sidebar
  // (visitors have no account; the sidebar wouldn't work anyway).
  const isSharePage = pathname.startsWith("/s/") || pathname === "/s";
  const isAuthPage = pathname.startsWith("/auth/");

  if (isPublicEntry || isSharePage || isAuthPage) {
    return <main className="min-h-screen">{children}</main>;
  }

  if (loading || !token) {
    return (
      <main className="min-h-screen">
        <div style={{ color: "var(--aurora-fg4)", textAlign: "center", marginTop: 80 }}>
          Loading...
        </div>
      </main>
    );
  }

  return (
    <DeviceProvider>
      <div className="min-h-screen">
        <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
        <div className="lg:ml-60 relative z-0">
          <Header onMenuToggle={() => setSidebarOpen((v) => !v)} />
          <main className="pt-20 px-4 pb-4 md:px-6 md:pb-6">{children}</main>
        </div>
      </div>
    </DeviceProvider>
  );
}
