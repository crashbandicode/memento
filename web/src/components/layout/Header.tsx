"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import {
  createAppRouteHistory,
  getBackFallback,
  recordAppRoute,
  type AppRouteNavigation,
} from "@/lib/app-navigation";
import { useDevice } from "@/lib/device-context";
import { useI18n, locales, type Locale } from "@/lib/i18n";
import { Icon, PlatformGlyph } from "@/components/aurora/Icon";
import { SkinPicker, ThemeToggle } from "@/components/aurora/primitives";
import { UserMenu } from "@/components/UserMenu";

const HISTORY_POSITION_KEY = "__memento_app_position";

export default function Header({ onMenuToggle }: { onMenuToggle: () => void }) {
  const { user } = useAuth();
  const { t, locale, setLocale } = useI18n();
  const { devices, selectedDeviceId, setSelectedDeviceId } = useDevice();
  const pathname = usePathname();
  const router = useRouter();
  const [routeHistory, setRouteHistory] = useState(() => createAppRouteHistory(pathname));
  const [navigationPending, setNavigationPending] = useState(false);
  const pendingNavigation = useRef<AppRouteNavigation | null>(null);
  const observedPathname = useRef(pathname);
  const browserPosition = useRef(0);
  const navigationLock = useRef(false);
  const selectedDevice = devices.find((d) => d.device_id === selectedDeviceId);
  const backFallback = getBackFallback(pathname);
  const canGoBack = routeHistory.index > 0;
  const canGoForward = routeHistory.index >= 0 && routeHistory.index < routeHistory.entries.length - 1;
  const showHistoryControls = Boolean(backFallback || canGoBack || canGoForward);

  useEffect(() => {
    if (observedPathname.current === pathname) return;
    const navigation = pendingNavigation.current || "push";
    pendingNavigation.current = null;
    observedPathname.current = pathname;

    if (navigation === "push") {
      browserPosition.current += 1;
      stampBrowserPosition(browserPosition.current);
    }

    setRouteHistory((current) => recordAppRoute(current, pathname, navigation));
    navigationLock.current = false;
    setNavigationPending(false);
  }, [pathname]);

  useEffect(() => {
    const initialPosition = readBrowserPosition(window.history.state);
    browserPosition.current = initialPosition ?? 0;
    if (initialPosition === null) stampBrowserPosition(browserPosition.current);

    const handlePopState = (event: PopStateEvent) => {
      const targetPosition = readBrowserPosition(event.state);
      let direction: AppRouteNavigation = "pop";
      if (targetPosition !== null) {
        if (targetPosition < browserPosition.current) direction = "back";
        if (targetPosition > browserPosition.current) direction = "forward";
        browserPosition.current = targetPosition;
      }

      // Query/hash-only history does not change usePathname. Consume the
      // pending direction here so it cannot poison the next real route.
      if (window.location.pathname === observedPathname.current) {
        pendingNavigation.current = null;
        navigationLock.current = false;
        setNavigationPending(false);
        return;
      }

      pendingNavigation.current ||= direction;
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const handleBack = () => {
    if ((!canGoBack && !backFallback) || navigationLock.current) return;
    navigationLock.current = true;
    setNavigationPending(true);

    // Only trust browser history after this mounted app shell has observed a
    // prior in-app route. window.history.length alone may point off-site.
    if (canGoBack && window.history.length > 1) {
      pendingNavigation.current = "back";
      router.back();
      return;
    }

    if (!backFallback) {
      navigationLock.current = false;
      setNavigationPending(false);
      return;
    }

    // A direct link or refreshed detail page has no known in-app predecessor.
    // Replace it with the logical parent so repeated Back clicks cannot loop.
    pendingNavigation.current = "replace";
    router.replace(backFallback);
  };

  const handleForward = () => {
    if (!canGoForward || navigationLock.current) return;
    navigationLock.current = true;
    setNavigationPending(true);
    pendingNavigation.current = "forward";
    router.forward();
  };

  return (
    <header
      className="h-14 flex items-center justify-between px-3 sm:px-4 md:px-6 fixed top-0 left-0 lg:left-60 right-0 z-20"
      style={{
        background: "var(--aurora-surface)",
        backdropFilter: "blur(20px) saturate(180%)",
        WebkitBackdropFilter: "blur(20px) saturate(180%)",
        borderBottom: "1px solid var(--aurora-border)",
      }}
    >
      <div className="flex items-center gap-2 sm:gap-3 min-w-0 flex-1">
        {/* Mobile menu */}
        <button
          onClick={onMenuToggle}
          aria-label="Menu"
          className="lg:hidden p-1"
          style={{ color: "var(--aurora-fg2)" }}
        >
          <Icon name="menu" size={22} />
        </button>

        {showHistoryControls && (
          <div
            role="group"
            data-testid="app-history-controls"
            className="inline-flex h-9 shrink-0 items-center overflow-hidden rounded-xl"
            style={{
              color: "var(--aurora-fg2)",
              background: "var(--aurora-surface-solid)",
              border: "1px solid var(--aurora-border)",
              boxShadow: "0 1px 0 rgba(255,255,255,0.45) inset, 0 3px 10px rgba(15,23,42,0.05)",
            }}
          >
            <HistoryButton
              direction="back"
              label={t.back}
              disabled={navigationPending || (!canGoBack && !backFallback)}
              onClick={handleBack}
            />
            <span aria-hidden className="h-4 w-px" style={{ background: "var(--aurora-border)" }} />
            <HistoryButton
              direction="forward"
              label={t.forward}
              disabled={navigationPending || !canGoForward}
              onClick={handleForward}
            />
          </div>
        )}

        {/* Device selector */}
        {devices.length > 0 && (
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "5px 10px 5px 6px",
              background: "var(--aurora-surface)",
              border: "1px solid var(--aurora-border)",
              borderRadius: 10,
              boxShadow: "0 1px 0 rgba(255,255,255,0.5) inset",
              maxWidth: 220,
              minWidth: 0,
            }}
          >
            {selectedDevice ? (
              <PlatformGlyph name={selectedDevice.name} size={20} />
            ) : (
              <Icon name="devices" size={14} style={{ color: "var(--aurora-fg3)" }} />
            )}
            <select
              value={selectedDeviceId || "all"}
              onChange={(e) => setSelectedDeviceId(e.target.value === "all" ? null : e.target.value)}
              className="bg-transparent text-xs outline-none min-w-0 truncate"
              style={{
                color: "var(--aurora-fg1)",
                appearance: "none",
                border: 0,
                maxWidth: 160,
                cursor: "pointer",
              }}
            >
              <option value="all">{t.all} ({devices.length})</option>
              {devices.map((d) => {
                const shortName = d.name.replace(/ \(\w+\)$/, "");
                return (
                  <option key={d.device_id} value={d.device_id}>
                    {shortName}
                  </option>
                );
              })}
            </select>
            <Icon name="chevron_down" size={12} style={{ color: "var(--aurora-fg4)" }} />
          </div>
        )}

        {/* Language switcher */}
        <div className="hidden sm:flex gap-1">
          {(Object.keys(locales) as Locale[]).map((l) => {
            const active = locale === l;
            return (
              <button
                key={l}
                onClick={() => setLocale(l)}
                style={{
                  padding: "5px 10px",
                  borderRadius: 8,
                  fontSize: 11,
                  fontWeight: 500,
                  letterSpacing: "-0.005em",
                  border: 0,
                  cursor: "pointer",
                  background: active ? "var(--aurora-accent-soft)" : "transparent",
                  color: active ? "var(--aurora-accent)" : "var(--aurora-fg3)",
                  transition: "all .15s",
                }}
              >
                {locales[l].label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex items-center gap-2 sm:gap-3">
        <div className="hidden md:block"><SkinPicker /></div>
        <ThemeToggle />
        {user ? (
          <UserMenu />
        ) : (
          <Link
            href="/auth/login"
            style={{ fontSize: 13, color: "var(--aurora-accent)", fontWeight: 500, letterSpacing: "-0.01em" }}
          >
            {t.login}
          </Link>
        )}
      </div>
    </header>
  );
}

function readBrowserPosition(state: unknown): number | null {
  if (!state || typeof state !== "object") return null;
  const value = (state as Record<string, unknown>)[HISTORY_POSITION_KEY];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stampBrowserPosition(position: number) {
  const state = window.history.state;
  const current = state && typeof state === "object" ? state : {};
  window.history.replaceState({ ...current, [HISTORY_POSITION_KEY]: position }, "");
}

function HistoryButton({
  direction,
  label,
  disabled,
  onClick,
}: {
  direction: "back" | "forward";
  label: string;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      data-testid={`app-${direction}-button`}
      className="inline-flex h-full w-8 items-center justify-center transition focus-visible:outline-2 focus-visible:outline-offset-[-3px]"
      style={{
        color: disabled ? "var(--aurora-fg4)" : "var(--aurora-fg2)",
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.38 : 1,
        outlineColor: "var(--aurora-accent)",
      }}
    >
      <Icon
        name={direction === "back" ? "chevron_left" : "chevron_right"}
        size={17}
        strokeWidth={1.9}
      />
    </button>
  );
}
