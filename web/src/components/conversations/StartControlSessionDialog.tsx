"use client";

/**
 * Start a managed Codex session on a collector machine.
 *
 * Submits one durable `agent.session.start` command; the dialog then waits
 * for the session's transcript to be ingested and bound so it can navigate
 * to the real conversation. If binding takes longer than the wait window,
 * the session keeps running — it simply appears once the transcript syncs.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api-client";

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.5)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 80,
  padding: 16,
};

const dialogStyle: React.CSSProperties = {
  width: "min(480px, 100%)",
  borderRadius: 14,
  border: "1px solid var(--aurora-border)",
  background: "var(--aurora-bg1, var(--aurora-bg))",
  padding: 18,
  display: "flex",
  flexDirection: "column",
  gap: 10,
};

const inputStyle: React.CSSProperties = {
  borderRadius: 8,
  border: "1px solid var(--aurora-border)",
  background: "var(--aurora-bg2, transparent)",
  color: "var(--aurora-fg)",
  padding: "8px 10px",
  fontSize: 13,
};

export default function StartControlSessionDialog({
  machineId,
  machineName,
  defaultCwd,
  onClose,
}: {
  machineId: string;
  machineName: string;
  defaultCwd?: string;
  onClose: () => void;
}) {
  const router = useRouter();
  const [cwd, setCwd] = useState(defaultCwd ?? "");
  const [model, setModel] = useState("");
  const [initialMessage, setInitialMessage] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const cancelled = useRef(false);

  useEffect(() => () => {
    cancelled.current = true;
  }, []);

  const submit = useCallback(async () => {
    setSubmitting(true);
    setStatus("Starting managed session…");
    try {
      const { session } = await api.startControlSession({
        machine_id: machineId,
        tool_id: "codex",
        cwd: cwd.trim() || undefined,
        model: model.trim() || undefined,
        initial_message: initialMessage.trim() || undefined,
      });
      setStatus("Waiting for the agent to come up…");
      const deadline = Date.now() + 45_000;
      while (Date.now() < deadline && !cancelled.current) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const { session: latest } = await api.getControlSession(session.id);
        if (latest.state === "failed") {
          setStatus(`Session failed: ${latest.state_reason ?? "unknown reason"}`);
          setSubmitting(false);
          return;
        }
        if (latest.document_id) {
          router.push(`/conversations/${latest.document_id}`);
          onClose();
          return;
        }
        if (latest.state === "active") {
          setStatus("Agent running — waiting for its transcript to sync…");
        }
      }
      setStatus(
        "Session is starting in the background; the conversation will appear in the list once its transcript syncs.",
      );
      setSubmitting(false);
    } catch (caught) {
      setStatus(caught instanceof Error ? caught.message : "Failed to start session");
      setSubmitting(false);
    }
  }, [machineId, cwd, model, initialMessage, router, onClose]);

  return (
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-label={`Start Codex session on ${machineName}`}>
      <div data-start-control-session style={dialogStyle}>
        <strong style={{ fontSize: 14 }}>New managed Codex session · {machineName}</strong>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
          Working directory (optional)
          <input
            data-start-session-cwd
            style={inputStyle}
            value={cwd}
            onChange={(event) => setCwd(event.target.value)}
            placeholder="C:\\path\\to\\project"
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
          Model (optional)
          <input
            data-start-session-model
            style={inputStyle}
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="Use the machine's default"
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
          First message (optional)
          <textarea
            data-start-session-message
            style={{ ...inputStyle, resize: "vertical" }}
            rows={3}
            value={initialMessage}
            onChange={(event) => setInitialMessage(event.target.value)}
            placeholder="What should the agent do first?"
          />
        </label>
        {status && (
          <div data-start-session-status style={{ fontSize: 12, color: "var(--aurora-fg2)" }}>
            {status}
          </div>
        )}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button
            type="button"
            style={{
              padding: "7px 14px",
              borderRadius: 8,
              border: "1px solid var(--aurora-border)",
              background: "transparent",
              color: "var(--aurora-fg2)",
              fontSize: 12.5,
              cursor: "pointer",
            }}
            onClick={onClose}
          >
            Close
          </button>
          <button
            type="button"
            data-start-session-submit
            style={{
              padding: "7px 14px",
              borderRadius: 8,
              border: "1px solid var(--aurora-accent)",
              background: "var(--aurora-accent)",
              color: "var(--aurora-bg)",
              fontSize: 12.5,
              fontWeight: 650,
              cursor: "pointer",
              opacity: submitting ? 0.6 : 1,
            }}
            disabled={submitting}
            onClick={() => void submit()}
          >
            Start session
          </button>
        </div>
      </div>
    </div>
  );
}
