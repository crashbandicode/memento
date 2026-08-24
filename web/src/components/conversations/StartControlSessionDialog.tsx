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
import { useI18n } from "@/lib/i18n";

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

// A native <select> renders its closed value with `inputStyle`, but the popup
// option list falls back to the browser's default (light) palette unless the
// control itself carries a solid surface + color, which the options inherit.
// A transparent background makes selected text invisible in dark mode.
const selectStyle: React.CSSProperties = {
  ...inputStyle,
  background: "var(--aurora-bg2, var(--aurora-bg1, #1c1c22))",
  colorScheme: "dark light",
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
  const { t } = useI18n();
  const [cwd, setCwd] = useState(defaultCwd ?? "");
  const [model, setModel] = useState("");
  const [approvalPolicy, setApprovalPolicy] = useState("");
  const [sandbox, setSandbox] = useState("");
  const [initialMessage, setInitialMessage] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const cancelled = useRef(false);

  useEffect(() => () => {
    cancelled.current = true;
  }, []);

  const submit = useCallback(async () => {
    setSubmitting(true);
    setStatus(t.control.starting);
    try {
      const { session } = await api.startControlSession({
        machine_id: machineId,
        tool_id: "codex",
        cwd: cwd.trim() || undefined,
        model: model.trim() || undefined,
        approval_policy: approvalPolicy || undefined,
        sandbox: (sandbox || undefined) as "read-only" | "workspace-write" | "danger-full-access" | undefined,
        initial_message: initialMessage.trim() || undefined,
      });
      setStatus(t.control.waitingAgent);
      const deadline = Date.now() + 45_000;
      while (Date.now() < deadline && !cancelled.current) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const { session: latest } = await api.getControlSession(session.id);
        if (latest.state === "failed") {
          setStatus(`${t.control.sessionFailedPrefix} ${latest.state_reason ?? ""}`.trim());
          setSubmitting(false);
          return;
        }
        if (latest.document_id) {
          router.push(`/conversations/${latest.document_id}`);
          onClose();
          return;
        }
        if (latest.state === "active") {
          setStatus(t.control.waitingTranscript);
        }
      }
      setStatus(t.control.backgroundStart);
      setSubmitting(false);
    } catch (caught) {
      setStatus(caught instanceof Error ? caught.message : t.control.startFailed);
      setSubmitting(false);
    }
  }, [machineId, cwd, model, approvalPolicy, sandbox, initialMessage, router, onClose, t]);

  return (
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-label={`${t.control.dialogTitle} · ${machineName}`}>
      <div data-start-control-session style={dialogStyle}>
        <strong style={{ fontSize: 14 }}>{t.control.dialogTitle} · {machineName}</strong>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
          {t.control.cwdLabel}
          <input
            data-start-session-cwd
            style={inputStyle}
            value={cwd}
            onChange={(event) => setCwd(event.target.value)}
            placeholder="C:\\path\\to\\project"
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
          {t.control.modelLabel}
          <input
            data-start-session-model
            style={inputStyle}
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder={t.control.modelPlaceholder}
          />
        </label>
        <div style={{ display: "flex", gap: 8 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, flex: 1 }}>
            {t.control.approvalPolicyLabel}
            <select
              data-start-session-approval-policy
              style={selectStyle}
              value={approvalPolicy}
              onChange={(event) => setApprovalPolicy(event.target.value)}
            >
              <option value="">{t.control.agentDefaultOption}</option>
              <option value="untrusted">untrusted</option>
              <option value="on-request">on-request</option>
              <option value="granular">granular</option>
              <option value="never">never</option>
            </select>
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, flex: 1 }}>
            {t.control.sandboxLabel}
            <select
              data-start-session-sandbox
              style={selectStyle}
              value={sandbox}
              onChange={(event) => setSandbox(event.target.value)}
            >
              <option value="">{t.control.agentDefaultOption}</option>
              <option value="read-only">read-only</option>
              <option value="workspace-write">workspace-write</option>
              <option value="danger-full-access">danger-full-access</option>
            </select>
          </label>
        </div>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
          {t.control.firstMessageLabel}
          <textarea
            data-start-session-message
            style={{ ...inputStyle, resize: "vertical" }}
            rows={3}
            value={initialMessage}
            onChange={(event) => setInitialMessage(event.target.value)}
            placeholder={t.control.firstMessagePlaceholder}
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
            {t.control.close}
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
            {t.control.start}
          </button>
        </div>
      </div>
    </div>
  );
}
