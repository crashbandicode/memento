"use client";

import { useState } from "react";
import Link from "next/link";
import { CategoryIcon } from "@/components/aurora/Icon";
import SubagentBadge from "@/components/conversations/SubagentBadge";

export default function BrowseFileRow({
  href,
  category,
  title,
  path,
  size,
  date,
  subagentCount,
  isSubagentOrphan,
}: {
  href: string;
  category: string;
  title: string;
  path: string;
  size: string;
  date: string;
  subagentCount?: number;
  isSubagentOrphan?: boolean;
}) {
  const [hovered, setHovered] = useState(false);

  return (
    <Link
      href={href}
      prefetch={false}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "10px 12px",
        borderRadius: 12,
        background: hovered ? "var(--aurora-chip)" : "transparent",
        transition: "background .15s",
        textDecoration: "none",
      }}
    >
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 10,
          flexShrink: 0,
          background: "var(--aurora-accent-soft)",
          color: "var(--aurora-accent)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <CategoryIcon category={category} size={14} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 500,
            color: "var(--aurora-fg1)",
            letterSpacing: "-0.01em",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={title}
        >
          {title}
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--aurora-fg4)",
            fontFamily: "ui-monospace,monospace",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={path}
        >
          {path}
        </div>
        <div style={{ marginTop: subagentCount ? 5 : 0 }}>
          <SubagentBadge count={subagentCount} orphan={isSubagentOrphan} />
        </div>
      </div>
      <span style={{ fontSize: 11, color: "var(--aurora-fg4)", flexShrink: 0 }}>
        {size}
      </span>
      <span style={{ fontSize: 11, color: "var(--aurora-fg4)", flexShrink: 0 }}>
        {date}
      </span>
    </Link>
  );
}
