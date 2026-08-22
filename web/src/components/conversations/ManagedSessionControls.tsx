"use client";

/**
 * Controls for a conversation bound to a managed agent-control session.
 *
 * View-only conversations render nothing here — file observation is not an
 * input channel, so controls appear only when this document is attached to a
 * session Memento itself started or resumed. Every action admits a durable,
 * idempotent command server-side; this component never talks to agents.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type ControlPendingInteraction,
  type ControlSession,
} from "@/lib/api-client";
import { useSSE, type SSEEvent } from "@/lib/use-sse";

const ACTIVE_STATES = new Set(["starting", "active"]);
const POLL_INTERVAL_MS = 6000;

const panelStyle: React.CSSProperties = {
  border: "1px solid color-mix(in srgb, var(--aurora-accent) 22%, var(--aurora-border))",
  borderRadius: 12,
  padding: "10px 12px",
  marginBottom: 10,
  background: "color-mix(in srgb, var(--aurora-accent-soft) 30%, transparent)",
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const buttonStyle: React.CSSProperties = {
  padding: "6px 12px",
  borderRadius: 8,
  border: "1px solid var(--aurora-border)",
  background: "var(--aurora-accent)",
  color: "var(--aurora-bg)",
  fontSize: 12,
  fontWeight: 650,
  cursor: "pointer",
};

const subtleButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  background: "transparent",
  color: "var(--aurora-fg2)",
};

function stateChip(session: ControlSession): { label: string; color: string } {
  if (session.state === "failed") {
    return {
      label: `Managed · failed${session.state_reason ? ` (${session.state_reason})` : ""}`,
      color: "var(--aurora-danger, #e5484d)",
    };
  }
  if (session.state === "starting") {
    return { label: "Managed · starting", color: "var(--aurora-fg2)" };
  }
  return { label: "Managed session", color: "var(--aurora-accent)" };
}

function approvalTitle(interaction: ControlPendingInteraction): string {
  if (interaction.method.includes("fileChange")) return "File change approval";
  if (interaction.method.includes("permissions")) return "Permission request";
  return "Command approval";
}

export default function ManagedSessionControls({ documentId }: { documentId: string }) {
  const [session, setSession] = useState<ControlSession | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef<ControlSession | null>(null);
  sessionRef.current = session;

  const refresh = useCallback(async () => {
    try {
      const sessions = await api.listControlSessions({ document_id: documentId });
      const relevant = sessions.find((item) => item.state !== "closed") ?? null;
      setSession(relevant);
    } catch {
      // Endpoint availability governs visibility; stay silent on failure.
    }
  }, [documentId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useSSE(
    useCallback(
      (event: SSEEvent) => {
        if (event.type !== "control_session") return;
        const data = (event.data ?? {}) as unknown as Record<string, unknown>;
        const current = sessionRef.current;
        if (
          data.document_id === documentId ||
          (current && data.session_id === current.id)
        ) {
          void refresh();
        }
      },
      [documentId, refresh],
    ),
  );

  useEffect(() => {
    if (!session || !ACTIVE_STATES.has(session.state)) return;
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [session, refresh]);

  const act = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await action();
        await refresh();
        setTimeout(() => void refresh(), 1500);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Command failed");
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const sendMessage = useCallback(() => {
    const current = sessionRef.current;
    const text = message.trim();
    if (!current || !text) return;
    void act(async () => {
      await api.sendControlMessage(current.id, { text });
      setMessage("");
    });
  }, [act, message]);

  const chip = useMemo(() => (session ? stateChip(session) : null), [session]);

  if (!session || !chip) return null;
  const controllable = ACTIVE_STATES.has(session.state);

  return (
    <div data-managed-session data-managed-session-state={session.state} style={panelStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "3px 10px",
            borderRadius: 999,
            border: `1px solid ${chip.color}`,
            color: chip.color,
            fontSize: 11,
            fontWeight: 700,
          }}
        >
          {chip.label}
        </span>
        {session.active_native_turn_id && (
          <span data-control-active-turn style={{ fontSize: 11, color: "var(--aurora-fg2)" }}>
            Agent is working…
          </span>
        )}
        <span style={{ flex: 1 }} />
        {session.active_native_turn_id && controllable && (
          <button
            type="button"
            data-control-interrupt
            style={subtleButtonStyle}
            disabled={busy}
            onClick={() => void act(() => api.interruptControlSession(session.id))}
          >
            Interrupt
          </button>
        )}
        {controllable && (
          <button
            type="button"
            data-control-close
            style={subtleButtonStyle}
            disabled={busy}
            onClick={() => void act(() => api.closeControlSession(session.id))}
          >
            End control
          </button>
        )}
      </div>

      {session.pending_interactions.map((interaction) => (
        <PendingInteractionCard
          key={interaction.interaction_id}
          sessionId={session.id}
          interaction={interaction}
          busy={busy}
          act={act}
        />
      ))}

      {controllable && (
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <textarea
            data-control-composer
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                sendMessage();
              }
            }}
            placeholder="Send a message to this agent…"
            rows={2}
            style={{
              flex: 1,
              resize: "vertical",
              borderRadius: 8,
              border: "1px solid var(--aurora-border)",
              background: "var(--aurora-bg2, transparent)",
              color: "var(--aurora-fg)",
              padding: "8px 10px",
              fontSize: 13,
            }}
          />
          <button
            type="button"
            data-control-send
            style={buttonStyle}
            disabled={busy || !message.trim()}
            onClick={sendMessage}
          >
            Send
          </button>
        </div>
      )}

      {error && (
        <div data-control-error role="alert" style={{ fontSize: 12, color: "var(--aurora-danger, #e5484d)" }}>
          {error}
        </div>
      )}
    </div>
  );
}

function PendingInteractionCard({
  sessionId,
  interaction,
  busy,
  act,
}: {
  sessionId: string;
  interaction: ControlPendingInteraction;
  busy: boolean;
  act: (action: () => Promise<unknown>) => Promise<void> | void;
}) {
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const questions = interaction.request.questions ?? [];

  if (interaction.kind === "approval") {
    const request = interaction.request;
    return (
      <div data-control-pending data-control-pending-kind="approval" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <strong style={{ fontSize: 12.5 }}>{approvalTitle(interaction)}</strong>
        {typeof request.command === "string" && (
          <code style={{ fontSize: 12, wordBreak: "break-all" }}>{request.command}</code>
        )}
        {typeof request.cwd === "string" && (
          <span style={{ fontSize: 11, color: "var(--aurora-fg2)" }}>in {request.cwd}</span>
        )}
        {typeof request.reason === "string" && request.reason && (
          <span style={{ fontSize: 11.5 }}>{request.reason}</span>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            data-control-approval-accept
            style={buttonStyle}
            disabled={busy}
            onClick={() =>
              void act(() =>
                api.respondControlApproval(sessionId, interaction.interaction_id, "accept"),
              )
            }
          >
            Approve
          </button>
          <button
            type="button"
            data-control-approval-decline
            style={subtleButtonStyle}
            disabled={busy}
            onClick={() =>
              void act(() =>
                api.respondControlApproval(sessionId, interaction.interaction_id, "decline"),
              )
            }
          >
            Decline
          </button>
        </div>
      </div>
    );
  }

  const ready = questions.every(
    (question) =>
      (selections[question.id] ?? "") !== "" || (custom[question.id] ?? "").trim() !== "",
  );

  const submit = () => {
    const answers: Record<string, { answers: string[] }> = {};
    for (const question of questions) {
      const customText = (custom[question.id] ?? "").trim();
      const chosen = customText || selections[question.id];
      answers[question.id] = { answers: chosen ? [chosen] : [] };
    }
    void act(() =>
      api.answerControlInteraction(sessionId, interaction.interaction_id, answers),
    );
  };

  return (
    <div data-control-pending data-control-pending-kind="question" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {questions.map((question) => (
        <div key={question.id} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          <strong style={{ fontSize: 12.5 }}>
            {question.header ? `${question.header}: ` : ""}
            {question.question ?? "The agent needs input"}
          </strong>
          <div role="radiogroup" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {(question.options ?? []).map((option) => {
              const selected = selections[question.id] === option.label;
              return (
                <button
                  key={option.label}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  data-control-question-option
                  data-selected={selected || undefined}
                  title={option.description}
                  style={{
                    ...subtleButtonStyle,
                    borderColor: selected ? "var(--aurora-accent)" : "var(--aurora-border)",
                    color: selected ? "var(--aurora-accent)" : "var(--aurora-fg2)",
                  }}
                  disabled={busy}
                  onClick={() =>
                    setSelections((prev) => ({ ...prev, [question.id]: option.label }))
                  }
                >
                  {option.label}
                </button>
              );
            })}
          </div>
          {(question.isOther ?? true) && (
            <input
              data-control-question-custom
              value={custom[question.id] ?? ""}
              onChange={(event) =>
                setCustom((prev) => ({ ...prev, [question.id]: event.target.value }))
              }
              placeholder="Custom answer…"
              style={{
                borderRadius: 8,
                border: "1px solid var(--aurora-border)",
                background: "var(--aurora-bg2, transparent)",
                color: "var(--aurora-fg)",
                padding: "6px 10px",
                fontSize: 12.5,
              }}
            />
          )}
        </div>
      ))}
      <div>
        <button
          type="button"
          data-control-answer-submit
          style={buttonStyle}
          disabled={busy || !ready}
          onClick={submit}
        >
          Answer
        </button>
      </div>
    </div>
  );
}
