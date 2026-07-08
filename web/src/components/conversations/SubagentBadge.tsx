import Link from "next/link";
import { Icon } from "@/components/aurora/Icon";
import type { ConversationSubagentSummary } from "@/lib/api-client";

export default function SubagentBadge({
  count,
  orphan = false,
  subagents = [],
  matchedSubagentId,
}: {
  count?: number;
  orphan?: boolean;
  subagents?: ConversationSubagentSummary[];
  matchedSubagentId?: string | null;
}) {
  if (!count) return null;

  const agents = `${count} ${count === 1 ? "subagent" : "subagents"}`;
  const matchedSubagent = subagents.find(
    (subagent) => subagent.id === matchedSubagentId,
  );
  const remainingSubagents = subagents.filter(
    (subagent) => subagent.id !== matchedSubagentId,
  );
  const pill = (
    <span
      title={orphan ? `${agents}; root thread has not synced yet` : agents}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 7px",
        borderRadius: 9999,
        background: "var(--aurora-accent-soft)",
        color: "var(--aurora-accent)",
        fontSize: 10,
        fontWeight: 600,
        lineHeight: 1.4,
        whiteSpace: "nowrap",
      }}
    >
      <Icon name="layers" size={10} />
      {agents}
      {orphan && <span style={{ opacity: 0.72 }}>· root pending</span>}
      {matchedSubagentId && <span style={{ opacity: 0.78 }}>· child match</span>}
      {subagents.length > 0 && <Icon name="chevron_down" size={9} />}
    </span>
  );

  if (subagents.length === 0) return pill;

  return (
    <details style={{ display: "inline-block", maxWidth: "100%" }}>
      <summary
        aria-label={`Show ${agents}`}
        style={{ cursor: "pointer", listStyle: "none", width: "fit-content" }}
      >
        {pill}
      </summary>
      <div
        style={{
          display: "grid",
          gap: 5,
          minWidth: 220,
          width: "min(420px, calc(100vw - 48px))",
          maxHeight: 360,
          overflowY: "auto",
          overscrollBehavior: "contain",
          marginTop: 6,
          padding: 7,
          border: "1px solid var(--aurora-border)",
          borderRadius: 12,
          background: "var(--aurora-surface-solid)",
          boxShadow: "var(--aurora-card-shadow)",
        }}
      >
        <div
          style={{
            position: "sticky",
            top: -7,
            zIndex: 1,
            padding: "5px 8px",
            background: "var(--aurora-surface-solid)",
            color: "var(--aurora-fg4)",
            fontSize: 9,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          {agents}
        </div>
        {matchedSubagentId && (
          <Link
            href={`/conversations/${matchedSubagentId}`}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "8px",
              borderRadius: 9,
              color: "var(--aurora-accent)",
              textDecoration: "none",
              background: "var(--aurora-accent-soft)",
              fontSize: 11,
              fontWeight: 650,
            }}
          >
            <Icon name="target" size={11} />
            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              Matched subagent{matchedSubagent ? ` · ${matchedSubagent.title}` : ""}
            </span>
            <Icon name="chevron_right" size={10} />
          </Link>
        )}
        {remainingSubagents.map((subagent) => (
          <Link
            key={subagent.session_id || subagent.id}
            href={`/conversations/${subagent.id}`}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "7px 8px",
              borderRadius: 9,
              color: "var(--aurora-fg2)",
              textDecoration: "none",
              background: "var(--aurora-chip)",
            }}
          >
            <Icon name="arrow_right" size={11} style={{ color: "var(--aurora-accent)" }} />
            <span style={{ flex: 1, minWidth: 0 }}>
              <span
                style={{
                  display: "block",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  fontSize: 11,
                  fontWeight: 600,
                }}
              >
                {subagent.title}
              </span>
              {subagent.agent_depth != null && (
                <span style={{ display: "block", fontSize: 9, color: "var(--aurora-fg4)" }}>
                  depth {subagent.agent_depth}
                </span>
              )}
            </span>
            <Icon name="chevron_right" size={10} style={{ color: "var(--aurora-fg4)" }} />
          </Link>
        ))}
      </div>
    </details>
  );
}
