"use client";

import { ReactNode, useState } from "react";
import { Icon } from "@/components/aurora/Icon";

export default function LowActivitySection({
  count,
  title,
  description,
  children,
  compact = false,
}: {
  count: number;
  title: string;
  description: string;
  children: ReactNode;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);

  if (count === 0) return null;

  return (
    <div
      style={{
        marginTop: compact ? 4 : 18,
        border: "1px solid var(--aurora-border)",
        borderRadius: compact ? 12 : 16,
        background: "color-mix(in srgb, var(--aurora-chip) 42%, transparent)",
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 9,
          border: 0,
          padding: compact ? "9px 11px" : "11px 14px",
          background: "transparent",
          color: "var(--aurora-fg3)",
          textAlign: "left",
          cursor: "pointer",
        }}
      >
        <Icon name="eye" size={13} style={{ color: "var(--aurora-fg4)" }} />
        <span style={{ minWidth: 0, flex: 1 }}>
          <span style={{ display: "block", fontSize: 11.5, fontWeight: 650, color: "var(--aurora-fg2)" }}>
            {title} · {count}
          </span>
          {!compact && (
            <span style={{ display: "block", marginTop: 2, fontSize: 10.5, color: "var(--aurora-fg4)" }}>
              {description}
            </span>
          )}
        </span>
        <Icon
          name="chevron_down"
          size={13}
          style={{
            color: "var(--aurora-fg4)",
            transform: open ? "rotate(180deg)" : "none",
            transition: "transform .15s ease",
          }}
        />
      </button>
      {open && (
        <div style={{ borderTop: "1px solid var(--aurora-border)", padding: compact ? 4 : "2px 10px 10px" }}>
          {children}
        </div>
      )}
    </div>
  );
}
