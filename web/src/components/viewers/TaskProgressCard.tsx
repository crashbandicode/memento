"use client";

import { useState, type ReactNode } from "react";
import type { ConversationTaskState } from "@/lib/api-client";

export default function TaskProgressCard({
  state,
  current = false,
  headerAction,
  footerAction,
}: {
  state: ConversationTaskState;
  current?: boolean;
  headerAction?: ReactNode;
  footerAction?: ReactNode;
}) {
  const [open, setOpen] = useState(current);
  const tasks = Array.isArray(state.tasks) ? state.tasks : [];
  const total = state.total_count || tasks.length;
  const completed = state.completed_count
    || tasks.filter((task) => task.status === "completed").length;
  const progress = total > 0
    ? Math.min(100, Math.max(0, completed / total * 100))
    : 0;
  const active = tasks.find((task) => task.id === state.active_task_id)
    || tasks.find((task) => task.status === "in_progress")
    || tasks.find((task) => task.status === "pending");
  const sourceLabel = state.source === "claude_code"
    ? "Claude"
    : state.source === "codex"
      ? "Codex"
      : state.source === "cursor"
        ? "Cursor"
        : state.source;

  return (
    <section
      data-task-state
      data-task-current={current ? "true" : "false"}
      style={{
        overflow: "hidden",
        borderRadius: 16,
        border: "1px solid color-mix(in srgb, var(--aurora-accent) 20%, var(--aurora-border))",
        background: current
          ? "linear-gradient(135deg, color-mix(in srgb, var(--aurora-accent) 8%, var(--aurora-surface-solid)), color-mix(in srgb, #10B981 4%, var(--aurora-surface-solid)))"
          : "var(--aurora-surface-solid)",
        boxShadow: current
          ? "0 8px 24px rgba(76,29,149,0.07)"
          : "0 2px 10px rgba(15,23,42,0.04)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", minWidth: 0 }}>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          style={{
            flex: "1 1 auto",
            width: "100%",
            minWidth: 0,
            display: "grid",
            gridTemplateColumns: "auto minmax(0, 1fr) auto",
            alignItems: "center",
            gap: 10,
            padding: headerAction ? "11px 6px 11px 13px" : "11px 13px",
            border: 0,
            background: "transparent",
            color: "var(--aurora-fg1)",
            cursor: "pointer",
            textAlign: "left",
          }}
        >
        <span
          aria-hidden="true"
          style={{
            width: 30,
            height: 30,
            borderRadius: 10,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            background: "color-mix(in srgb, var(--aurora-accent) 13%, transparent)",
            color: "var(--aurora-accent)",
            fontSize: 16,
            fontWeight: 800,
          }}
        >
          ✓
        </span>
        <span style={{ minWidth: 0 }}>
          <span style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 7 }}>
            <span style={{ fontSize: 12.5, fontWeight: 750 }}>
              {current ? "Active task list" : "Task update"}
            </span>
            {sourceLabel && (
              <span style={{ padding: "2px 7px", borderRadius: 999, background: "var(--aurora-chip)", color: "var(--aurora-fg3)", fontSize: 9.5, fontWeight: 700 }}>
                {sourceLabel}
              </span>
            )}
          </span>
          <span style={{ display: "block", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--aurora-fg3)", fontSize: 11.5 }}>
            {active
              ? `${active.status === "in_progress" ? "Working on" : "Next"}: ${active.active_form || active.content}`
              : total > 0 && completed === total
                ? "All tasks complete"
                : "No active task"}
          </span>
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <span style={{ padding: "4px 8px", borderRadius: 999, background: "color-mix(in srgb, #10B981 11%, var(--aurora-chip))", color: completed === total && total > 0 ? "#059669" : "var(--aurora-fg3)", fontSize: 10.5, fontWeight: 750, whiteSpace: "nowrap" }}>
            {completed}/{total}
          </span>
          <span
            aria-hidden="true"
            style={{
              color: "var(--aurora-fg4)",
              fontSize: 14,
              transform: open ? "rotate(180deg)" : "none",
              transition: "transform 160ms ease",
            }}
          >
            ⌄
          </span>
        </span>
        </button>
        {headerAction && (
          <div
            data-task-header-action
            style={{
              flex: "0 0 auto",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              paddingRight: 9,
            }}
          >
            {headerAction}
          </div>
        )}
      </div>
      <div style={{ height: 3, margin: "0 13px", borderRadius: 999, background: "color-mix(in srgb, var(--aurora-border) 75%, transparent)", overflow: "hidden" }}>
        <div style={{ width: `${progress}%`, height: "100%", borderRadius: 999, background: "linear-gradient(90deg, var(--aurora-accent), #10B981)", transition: "width 220ms ease" }} />
      </div>
      {open && (
        <ol style={{ display: "grid", gap: 5, maxHeight: current ? 300 : 420, overflowY: "auto", margin: 0, padding: "10px 13px 13px", listStyle: "none" }}>
          {tasks.map((task) => {
            const done = task.status === "completed";
            const inProgress = task.status === "in_progress";
            return (
              <li
                key={task.id}
                data-task-status={task.status}
                style={{
                  minWidth: 0,
                  display: "grid",
                  gridTemplateColumns: "20px minmax(0, 1fr)",
                  gap: 8,
                  alignItems: "start",
                  padding: "7px 8px",
                  borderRadius: 10,
                  background: inProgress
                    ? "color-mix(in srgb, var(--aurora-accent) 7%, transparent)"
                    : "color-mix(in srgb, var(--aurora-chip) 55%, transparent)",
                  color: done ? "var(--aurora-fg4)" : "var(--aurora-fg2)",
                }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    width: 18,
                    height: 18,
                    marginTop: 1,
                    borderRadius: 999,
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    border: done || inProgress
                      ? 0
                      : "1.5px solid var(--aurora-border)",
                    background: done
                      ? "#10B981"
                      : inProgress
                        ? "var(--aurora-accent)"
                        : "transparent",
                    color: "white",
                    fontSize: 10,
                    fontWeight: 800,
                  }}
                >
                  {done ? "✓" : inProgress ? "•" : ""}
                </span>
                <span style={{ minWidth: 0, fontSize: 11.5, lineHeight: 1.45, overflowWrap: "anywhere", textDecoration: done ? "line-through" : "none" }}>
                  {inProgress && task.active_form ? task.active_form : task.content}
                </span>
              </li>
            );
          })}
        </ol>
      )}
      {footerAction && (
        <div
          data-task-footer-action
          style={{ display: "flex", justifyContent: "flex-end", padding: "0 10px 10px" }}
        >
          {footerAction}
        </div>
      )}
    </section>
  );
}
