"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useI18n } from "@/lib/i18n";
import { useAuth } from "@/lib/auth-context";
import { getApiBase, authFetch } from "@/lib/api-client";
import type { DeviceGroupSummary, DeviceIdentitySummary } from "@/lib/api-client";
import { getStoredAuthToken } from "@/lib/auth-storage";
import { Icon, ToolGlyph, PlatformGlyph } from "@/components/aurora/Icon";
// Read version from package.json so the sidebar footer tracks releases
// automatically — no more "v0.1.0 forever" when actual builds are 0.2.x.
import pkg from "../../../package.json";
const WEB_VERSION = `v${(pkg as { version: string }).version}`;

type IconName = Parameters<typeof Icon>[0]["name"];

export default function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const pathname = usePathname();
  const { t } = useI18n();
  const { user } = useAuth();
  const [devices, setDevices] = useState<DeviceGroupSummary[]>([]);

  useEffect(() => {
    const token = getStoredAuthToken();
    if (!token) return;
    authFetch(`${getApiBase()}/api/hierarchy/devices`)
      .then((r) => r.json())
      .then(setDevices)
      .catch(() => {});
  }, []);

  const handleNavClick = () => {
    if (typeof window !== "undefined" && window.innerWidth < 1024) onClose();
  };

  const pathParts = pathname.split("/");
  const currentDeviceId = pathParts[2] || "";
  const currentToolId = pathParts[4] || "";

  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [identityCollapsed, setIdentityCollapsed] = useState<Record<string, boolean>>({});
  const toggleDevice = (id: string, initiallyCollapsed: boolean) => setCollapsed((previous) => {
    const current = id in previous ? previous[id] : initiallyCollapsed;
    return { ...previous, [id]: !current };
  });
  const isCollapsed = (id: string, initiallyCollapsed: boolean) => {
    if (id in collapsed) return collapsed[id];
    return initiallyCollapsed;
  };
  const toggleIdentity = (id: string, initiallyCollapsed: boolean) =>
    setIdentityCollapsed((previous) => {
      const current = id in previous ? previous[id] : initiallyCollapsed;
      return { ...previous, [id]: !current };
    });
  const isIdentityCollapsed = (id: string, initiallyCollapsed: boolean) =>
    id in identityCollapsed ? identityCollapsed[id] : initiallyCollapsed;

  const isAdmin = user?.role === "admin" || user?.role === "owner";
  const STATIC_NAV: { href: string; label: string; icon: IconName }[] = [
    { href: "/projects", label: t.nav.projects, icon: "folder" },
    { href: "/memory", label: t.nav.memory || "Memory", icon: "brain" },
    { href: "/daily", label: t.nav.daily, icon: "calendar" },
    { href: "/search", label: t.nav.search, icon: "search" },
    { href: "/export", label: t.nav.export, icon: "arrow_down" },
    { href: "/devices", label: t.nav.devices, icon: "devices" },
    { href: "/inbox", label: t.nav.inbox, icon: "inbox" },
    ...(isAdmin ? [{ href: "/admin", label: t.nav.admin, icon: "lock" as IconName }] : []),
  ];

  const OVERVIEW_HREF = "/app";

  return (
    <>
      {open && (
        <div
          data-testid="sidebar-overlay"
          className="fixed inset-0 bg-black/50 z-30 lg:hidden"
          onClick={onClose}
        />
      )}

      <aside
        aria-label="Memento sidebar"
        data-testid="app-sidebar"
        className={[
          "fixed left-0 top-0 z-40 w-60 flex flex-col h-screen",
          "transition-transform duration-200 ease-in-out",
          open ? "translate-x-0" : "-translate-x-full",
          "lg:!translate-x-0",
        ].join(" ")}
        style={{
          background: "var(--aurora-sidebar)",
          backdropFilter: "blur(24px) saturate(180%)",
          WebkitBackdropFilter: "blur(24px) saturate(180%)",
          borderRight: "1px solid var(--aurora-border)",
          color: "var(--aurora-fg2)",
        }}
      >
        {/* Brand */}
        <div className="px-4 pt-5 pb-3 flex items-center gap-3">
          <Link href="/app" onClick={handleNavClick} className="flex items-center gap-3 flex-1 min-w-0">
            <div
              style={{
                width: 34,
                height: 34,
                borderRadius: 11,
                background: "var(--aurora-brand-grad)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                boxShadow: "0 4px 14px -4px rgba(124,58,237,0.5)",
                flexShrink: 0,
              }}
            >
              <Icon name="sparkles" size={17} style={{ color: "#fff" }} strokeWidth={2} />
            </div>
            <div className="min-w-0">
              <div
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: "var(--aurora-fg1)",
                  letterSpacing: "-0.02em",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {t.app.title}
              </div>
              <div style={{ fontSize: 11, color: "var(--aurora-fg4)", marginTop: 1 }}>{t.app.subtitle}</div>
            </div>
          </Link>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="lg:hidden"
            style={{ color: "var(--aurora-fg3)", padding: 4 }}
          >
            <Icon name="close" size={18} />
          </button>
        </div>

        <div style={{ height: 1, background: "var(--aurora-border)", margin: "0 16px" }} />

        <nav aria-label="Primary navigation" className="flex-1 overflow-y-auto py-2">
          {/* Overview link (dashboard) */}
          <NavRow
            href={OVERVIEW_HREF}
            label={t.nav.dashboard || "Overview"}
            icon="home"
            active={pathname === OVERVIEW_HREF}
            onClick={handleNavClick}
          />

          {/* Static nav */}
          {STATIC_NAV.map((item) => {
            const active = pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href));
            return <NavRow key={item.href} {...item} active={active} onClick={handleNavClick} />;
          })}

          {/* Device tree */}
          {devices.length > 0 && (
            <div style={{ height: 1, background: "var(--aurora-border)", margin: "10px 20px 4px" }} />
          )}

          {devices.map((device) => {
            const isCurrentGroupScope = device.device_id === currentDeviceId;
            const isCurrentDevice = isCurrentGroupScope
              || device.identities.some((identity) => identity.device_id === currentDeviceId);
            const initiallyCollapsed = devices.length > 1 && !isCurrentDevice;
            const deviceCollapsed = isCollapsed(device.group_id, initiallyCollapsed);
            const groupPanelId = `sidebar-host-${device.group_id}`;

            return (
              <div
                key={device.group_id}
                data-testid="sidebar-host"
                data-host-name={device.name}
                style={{ marginTop: 6 }}
              >
                <button
                  type="button"
                  onClick={() => toggleDevice(device.group_id, initiallyCollapsed)}
                  aria-expanded={!deviceCollapsed}
                  aria-controls={groupPanelId}
                  aria-label={`${device.name}, ${device.total_files} files`}
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "6px 18px 6px 14px",
                    color: isCurrentDevice ? "var(--aurora-accent)" : "var(--aurora-fg3)",
                    fontSize: 11,
                    fontWeight: 500,
                    letterSpacing: "-0.005em",
                    background: isCurrentDevice ? "var(--aurora-accent-soft)" : "transparent",
                    border: 0,
                    cursor: "pointer",
                    borderRadius: 10,
                  }}
                >
                  <Icon name="devices" size={18} />
                  <span
                    style={{
                      flex: 1,
                      textAlign: "left",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      color: "var(--aurora-fg2)",
                    }}
                  >
                    {device.name}
                  </span>
                  <span style={{ color: "var(--aurora-fg4)", fontSize: 11 }}>{device.total_files}</span>
                  <Icon
                    name="chevron_right"
                    size={11}
                    style={{
                      color: "var(--aurora-fg4)",
                      transform: deviceCollapsed ? "rotate(0)" : "rotate(90deg)",
                      transition: "transform .15s",
                    }}
                  />
                </button>

                {!deviceCollapsed && (
                  <div id={groupPanelId} style={{ padding: "2px 0" }}>
                    {device.tools.map((tool) => {
                      const href = `/devices/${device.device_id}/tools/${tool.id}`;
                      const active = isCurrentGroupScope && tool.id === currentToolId;
                      return (
                        <ToolRow
                          key={tool.id}
                          href={href}
                          toolId={tool.id}
                          fileCount={tool.file_count}
                          active={active}
                          aggregate={device.identities.length > 1}
                          deviceScope={device.device_id}
                          onClick={handleNavClick}
                        />
                      );
                    })}
                    {device.identities.length > 1 && (
                      <div
                        data-testid="sidebar-identities"
                        style={{
                          margin: "3px 10px 0",
                          paddingTop: 3,
                          borderTop: "1px solid var(--aurora-border)",
                        }}
                      >
                        {device.identities.map((identity) => (
                          <IdentityRows
                            key={identity.device_id}
                            identity={identity}
                            currentDeviceId={currentDeviceId}
                            currentToolId={currentToolId}
                            collapsed={isIdentityCollapsed(
                              identity.device_id,
                              identity.device_id !== currentDeviceId,
                            )}
                            onToggle={() => toggleIdentity(
                              identity.device_id,
                              identity.device_id !== currentDeviceId,
                            )}
                            onNavClick={handleNavClick}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {devices.length === 0 && (
            <div
              style={{
                padding: "32px 16px",
                textAlign: "center",
                fontSize: 12,
                color: "var(--aurora-fg4)",
              }}
            >
              {t.devices.noDevices}
            </div>
          )}
        </nav>

        <div className="p-3">
          <div
            data-testid="sidebar-version"
            style={{
              fontSize: 10.5,
              color: "var(--aurora-fg4)",
              textAlign: "center",
              padding: "6px 0",
            }}
          >
            {WEB_VERSION}
          </div>
        </div>
      </aside>
    </>
  );
}

function NavRow({
  href, label, icon, active, onClick,
}: {
  href: string; label: string; icon: IconName; active: boolean; onClick?: () => void;
}) {
  const [hover, setHover] = useState(false);
  const color = active ? "var(--aurora-accent)" : hover ? "var(--aurora-fg1)" : "var(--aurora-fg2)";
  const bg = active ? "var(--aurora-accent-soft)" : hover ? "var(--aurora-chip)" : "transparent";
  return (
    <Link
      href={href}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "8px 14px",
        margin: "1px 10px",
        borderRadius: 12,
        color,
        background: bg,
        fontSize: 13.5,
        fontWeight: active ? 500 : 400,
        letterSpacing: "-0.01em",
        transition: "all .15s",
      }}
    >
      <Icon name={icon} size={16} />
      <span style={{ flex: 1 }}>{label}</span>
      {active && <span style={{ width: 5, height: 5, borderRadius: 9999, background: "var(--aurora-accent)" }} />}
    </Link>
  );
}

function IdentityRows({
  identity,
  currentDeviceId,
  currentToolId,
  collapsed,
  onToggle,
  onNavClick,
}: {
  identity: DeviceIdentitySummary;
  currentDeviceId: string;
  currentToolId: string;
  collapsed: boolean;
  onToggle: () => void;
  onNavClick: () => void;
}) {
  const panelId = `sidebar-identity-${identity.device_id}`;
  const current = identity.device_id === currentDeviceId;
  return (
    <div data-testid="sidebar-identity" data-device-id={identity.device_id}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        aria-controls={panelId}
        aria-label={`${identity.label}, ${identity.total_files} files`}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "5px 6px",
          color: current ? "var(--aurora-accent)" : "var(--aurora-fg3)",
          fontSize: 10.5,
          background: current ? "var(--aurora-accent-soft)" : "transparent",
          border: 0,
          borderRadius: 9,
          cursor: "pointer",
        }}
      >
        <PlatformGlyph name={identity.name} size={16} />
        <span
          style={{
            flex: 1,
            textAlign: "left",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {identity.label}
        </span>
        <span style={{ color: "var(--aurora-fg4)" }}>{identity.total_files}</span>
        <Icon
          name="chevron_right"
          size={10}
          style={{
            color: "var(--aurora-fg4)",
            transform: collapsed ? "rotate(0)" : "rotate(90deg)",
          }}
        />
      </button>
      {!collapsed && (
        <div id={panelId}>
          {identity.tools.map((tool) => (
            <ToolRow
              key={tool.id}
              href={`/devices/${identity.device_id}/tools/${tool.id}`}
              toolId={tool.id}
              fileCount={tool.file_count}
              active={current && tool.id === currentToolId}
              deviceScope={identity.device_id}
              indent
              onClick={onNavClick}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ToolRow({
  href, toolId, fileCount, active, aggregate = false, deviceScope, indent = false, onClick,
}: {
  href: string;
  toolId: string;
  fileCount: number;
  active: boolean;
  aggregate?: boolean;
  deviceScope: string;
  indent?: boolean;
  onClick?: () => void;
}) {
  const [hover, setHover] = useState(false);
  const bg = active ? "var(--aurora-chip)" : hover ? "var(--aurora-chip)" : "transparent";
  const color = active || hover ? "var(--aurora-fg1)" : "var(--aurora-fg2)";
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      data-testid="sidebar-tool-link"
      data-device-scope={deviceScope}
      data-tool-id={toolId}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: indent ? "5px 6px 5px 22px" : "6px 14px",
        margin: indent ? "1px 0" : "1px 10px",
        borderRadius: 12,
        color,
        background: bg,
        transition: "all .15s",
      }}
    >
      <ToolGlyph id={toolId} size={20} />
      <span
        style={{
          flex: 1,
          fontSize: 13,
          textTransform: "capitalize",
          letterSpacing: "-0.01em",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {aggregate ? "All " : ""}
        {toolId.replace("_", " ")}
      </span>
      <span style={{ fontSize: 11, color: "var(--aurora-fg4)" }}>{fileCount}</span>
    </Link>
  );
}
