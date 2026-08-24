"use client";

/**
 * Controls for a conversation bound to a managed agent-control session.
 *
 * View-only conversations render nothing here — file observation is not an
 * input channel, so controls appear only when this document is attached to a
 * session Memento itself started or resumed. Every action admits a durable,
 * idempotent command server-side; this component never talks to agents.
 *
 * While a turn is active the composer steers (appends to the exact expected
 * turn); when idle it sends a new turn. Permission requests grant a
 * user-selected subset — anything unchecked is denied.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type ControlPendingInteraction,
  type ControlSession,
} from "@/lib/api-client";
import { useI18n } from "@/lib/i18n";

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

export default function ManagedSessionControls({
  documentId,
  refreshSignal = 0,
}: {
  documentId: string;
  /** Bumped by the page when a control_session SSE event targets this doc. */
  refreshSignal?: number;
}) {
  const { t } = useI18n();
  const [session, setSession] = useState<ControlSession | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A pending interaction card unmounts the instant the interaction resolves,
  // so without a transient acknowledgement a decline looks like it did nothing.
  const [notice, setNotice] = useState<string | null>(null);
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
  }, [refresh, refreshSignal]);

  useEffect(() => {
    if (!session || !ACTIVE_STATES.has(session.state)) return;
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [session, refresh]);

  const act = useCallback(
    async (action: () => Promise<unknown>, successNote?: string) => {
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        await action();
        if (successNote) setNotice(successNote);
        await refresh();
        setTimeout(() => void refresh(), 1500);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : t.control.commandFailed);
      } finally {
        setBusy(false);
      }
    },
    [refresh, t.control.commandFailed],
  );

  // Auto-clear the resolution acknowledgement so it stays transient.
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  const activeTurnId = session?.active_native_turn_id ?? null;

  const submitComposer = useCallback(() => {
    const current = sessionRef.current;
    const text = message.trim();
    if (!current || !text) return;
    void act(async () => {
      if (current.active_native_turn_id) {
        await api.steerControlSession(current.id, {
          text,
          expected_turn_id: current.active_native_turn_id,
        });
      } else {
        await api.sendControlMessage(current.id, { text });
      }
      setMessage("");
    });
  }, [act, message]);

  const chip = useMemo(() => {
    if (!session) return null;
    if (session.state === "failed") {
      return {
        label: `${t.control.managedFailed}${session.state_reason ? ` (${session.state_reason})` : ""}`,
        color: "var(--aurora-danger, #e5484d)",
      };
    }
    if (session.state === "starting") {
      return { label: t.control.managedStarting, color: "var(--aurora-fg2)" };
    }
    return { label: t.control.managed, color: "var(--aurora-accent)" };
  }, [session, t]);

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
        {activeTurnId && (
          <span data-control-active-turn style={{ fontSize: 11, color: "var(--aurora-fg2)" }}>
            {t.control.agentWorking}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {activeTurnId && controllable && (
          <button
            type="button"
            data-control-interrupt
            style={subtleButtonStyle}
            disabled={busy}
            onClick={() => void act(() => api.interruptControlSession(session.id))}
          >
            {t.control.interrupt}
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
            {t.control.endControl}
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
                submitComposer();
              }
            }}
            placeholder={
              activeTurnId ? t.control.steerPlaceholder : t.control.composerPlaceholder
            }
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
            data-control-send-mode={activeTurnId ? "steer" : "send"}
            style={buttonStyle}
            disabled={busy || !message.trim()}
            onClick={submitComposer}
          >
            {activeTurnId ? t.control.steer : t.control.send}
          </button>
        </div>
      )}

      {notice && (
        <div data-control-notice role="status" style={{ fontSize: 12, color: "var(--aurora-fg2)" }}>
          {notice}
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

interface GrantChoice {
  key: string;
  label: string;
  granted: boolean;
}

function grantChoicesFromRequest(
  interaction: ControlPendingInteraction,
  labels: { write: string; read: string; network: string },
): GrantChoice[] {
  const permissions = interaction.request.permissions;
  if (!permissions) return [];
  const choices: GrantChoice[] = [];
  for (const path of permissions.fileSystem?.write ?? []) {
    choices.push({ key: `write:${path}`, label: `${labels.write}: ${path}`, granted: true });
  }
  for (const path of permissions.fileSystem?.read ?? []) {
    choices.push({ key: `read:${path}`, label: `${labels.read}: ${path}`, granted: true });
  }
  if (permissions.network?.enabled) {
    choices.push({ key: "network", label: labels.network, granted: true });
  }
  return choices;
}

function grantedSubset(choices: GrantChoice[]): Record<string, unknown> {
  const write = choices
    .filter((choice) => choice.granted && choice.key.startsWith("write:"))
    .map((choice) => choice.key.slice("write:".length));
  const read = choices
    .filter((choice) => choice.granted && choice.key.startsWith("read:"))
    .map((choice) => choice.key.slice("read:".length));
  const network = choices.some((choice) => choice.granted && choice.key === "network");
  const fileSystem: Record<string, string[]> = {};
  if (write.length) fileSystem.write = write;
  if (read.length) fileSystem.read = read;
  const subset: Record<string, unknown> = {};
  if (Object.keys(fileSystem).length) subset.fileSystem = fileSystem;
  if (network) subset.network = { enabled: true };
  return subset;
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
  act: (action: () => Promise<unknown>, successNote?: string) => Promise<void> | void;
}) {
  const { t } = useI18n();
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [grants, setGrants] = useState<GrantChoice[]>(() =>
    grantChoicesFromRequest(interaction, {
      write: t.control.writeAccess,
      read: t.control.readAccess,
      network: t.control.networkAccess,
    }),
  );
  const questions = interaction.request.questions ?? [];
  const isPermissionRequest = interaction.method.includes("permissions");

  if (interaction.kind === "approval") {
    const request = interaction.request;
    const title = interaction.method.includes("fileChange")
      ? t.control.fileChangeApproval
      : isPermissionRequest
        ? t.control.permissionRequest
        : t.control.commandApproval;
    const approve = (decision: "accept" | "acceptForSession") => {
      const subset = isPermissionRequest ? grantedSubset(grants) : undefined;
      void act(
        () =>
          api.respondControlApproval(sessionId, interaction.interaction_id, decision, subset),
        t.control.approvedNotice,
      );
    };
    return (
      <div data-control-pending data-control-pending-kind="approval" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <strong style={{ fontSize: 12.5 }}>{title}</strong>
        {typeof request.command === "string" && (
          <code style={{ fontSize: 12, wordBreak: "break-all" }}>{request.command}</code>
        )}
        {typeof request.cwd === "string" && (
          <span style={{ fontSize: 11, color: "var(--aurora-fg2)" }}>in {request.cwd}</span>
        )}
        {typeof request.reason === "string" && request.reason && (
          <span style={{ fontSize: 11.5 }}>{request.reason}</span>
        )}
        {isPermissionRequest && grants.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {grants.map((choice, index) => (
              <label
                key={choice.key}
                data-control-grant-option
                style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}
              >
                <input
                  type="checkbox"
                  checked={choice.granted}
                  disabled={busy}
                  onChange={(event) =>
                    setGrants((prev) =>
                      prev.map((item, itemIndex) =>
                        itemIndex === index
                          ? { ...item, granted: event.target.checked }
                          : item,
                      ),
                    )
                  }
                />
                <code style={{ wordBreak: "break-all" }}>{choice.label}</code>
              </label>
            ))}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            data-control-approval-accept
            style={buttonStyle}
            disabled={busy}
            onClick={() => approve("accept")}
          >
            {t.control.approve}
          </button>
          {isPermissionRequest && (
            <button
              type="button"
              data-control-approval-accept-session
              style={subtleButtonStyle}
              disabled={busy}
              onClick={() => approve("acceptForSession")}
            >
              {t.control.approveForSession}
            </button>
          )}
          <button
            type="button"
            data-control-approval-decline
            style={subtleButtonStyle}
            disabled={busy}
            onClick={() =>
              void act(
                () =>
                  api.respondControlApproval(sessionId, interaction.interaction_id, "decline"),
                t.control.declinedNotice,
              )
            }
          >
            {t.control.decline}
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
    void act(
      () => api.answerControlInteraction(sessionId, interaction.interaction_id, answers),
      t.control.answeredNotice,
    );
  };

  return (
    <div data-control-pending data-control-pending-kind="question" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {questions.map((question) => (
        <div key={question.id} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          <strong style={{ fontSize: 12.5 }}>
            {question.header ? `${question.header}: ` : ""}
            {question.question ?? t.control.needsInput}
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
              placeholder={t.control.customAnswerPlaceholder}
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
          {t.control.answer}
        </button>
      </div>
    </div>
  );
}
