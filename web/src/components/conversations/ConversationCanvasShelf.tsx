"use client";

import { useState } from "react";
import { Icon } from "@/components/aurora/Icon";
import { CanvasViewer } from "@/components/viewers/CanvasViewer";
import { useI18n } from "@/lib/i18n";
import type { CanvasArtifact } from "@/lib/canvas-artifact.mjs";

const READY_STATES = new Set(["renderable", "static_only", "already_current"]);

export default function ConversationCanvasShelf({
  canvases,
}: {
  canvases?: CanvasArtifact[] | null;
}) {
  const { t } = useI18n();
  const [selected, setSelected] = useState<CanvasArtifact | null>(null);
  if (!canvases?.length) return null;

  const copy = t.conversation.canvas;
  return (
    <>
      <section
        data-conversation-canvases
        style={{
          marginTop: 10,
          padding: "11px 12px",
          border: "1px solid color-mix(in srgb, var(--aurora-accent) 24%, var(--aurora-border))",
          borderRadius: 14,
          background: "linear-gradient(120deg, color-mix(in srgb, var(--aurora-accent-soft) 70%, var(--aurora-surface-solid)), var(--aurora-surface-solid))",
          boxShadow: "0 8px 24px rgba(30, 20, 70, 0.06)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 8 }}>
          <span
            style={{
              display: "grid",
              placeItems: "center",
              width: 28,
              height: 28,
              borderRadius: 9,
              color: "var(--aurora-accent)",
              background: "var(--aurora-accent-soft)",
            }}
          >
            <Icon name="cube" size={15} />
          </span>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 750, color: "var(--aurora-fg1)" }}>
              {copy.shelfTitle} <span style={{ color: "var(--aurora-fg3)" }}>({canvases.length})</span>
            </div>
            <div style={{ fontSize: 10.5, color: "var(--aurora-fg3)" }}>{copy.shelfHint}</div>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 7 }}>
          {canvases.map((canvas) => {
            const ready = READY_STATES.has(canvas.capture_status || "");
            return (
              <button
                key={`${canvas.path}:${canvas.artifact_id || canvas.capture_status || "pending"}`}
                type="button"
                onClick={() => setSelected(canvas)}
                aria-label={`${copy.openCanvas}: ${canvas.name}`}
                data-conversation-canvas={canvas.name}
                style={{
                  minWidth: 0,
                  display: "flex",
                  alignItems: "center",
                  gap: 9,
                  padding: "9px 10px",
                  border: "1px solid var(--aurora-border)",
                  borderRadius: 11,
                  background: "color-mix(in srgb, var(--aurora-surface-solid) 92%, transparent)",
                  color: "var(--aurora-fg1)",
                  cursor: "pointer",
                  textAlign: "left",
                }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: 999,
                    flex: "0 0 auto",
                    background: ready ? "#10b981" : "#f59e0b",
                    boxShadow: ready ? "0 0 0 4px rgba(16,185,129,.10)" : "0 0 0 4px rgba(245,158,11,.10)",
                  }}
                />
                <span style={{ minWidth: 0, flex: 1 }}>
                  <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11.5, fontWeight: 700 }}>
                    {canvas.name}
                  </span>
                  <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 9.5, color: "var(--aurora-fg3)" }}>
                    {ready ? copy.current : copy.awaitingCapture}
                  </span>
                </span>
                <Icon name="arrow_right" size={13} style={{ flex: "0 0 auto", color: "var(--aurora-fg3)" }} />
              </button>
            );
          })}
        </div>
      </section>
      {selected && (
        <CanvasViewer
          artifact={selected}
          open
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}
