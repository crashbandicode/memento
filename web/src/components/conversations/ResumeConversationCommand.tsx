"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ConversationLocation } from "@/lib/api-client";
import { Icon } from "@/components/aurora/Icon";
import { copyText } from "@/lib/copy-text";
import { useI18n } from "@/lib/i18n";

type Shell = "powershell" | "bash";
type CopyStatus = "idle" | "copied" | "error";

const RESUME_TOOL: Record<string, "codex" | "claude" | "cursor-agent"> = {
  codex: "codex",
  claude: "claude",
  claude_code: "claude",
  cursor: "cursor-agent",
};

function isWindowsPath(path: string): boolean {
  return /^[a-z]:[\\/]/i.test(path) || path.startsWith("\\\\");
}

function quoteBash(value: string): string {
  return `'${value.replaceAll("'", `'\"'\"'`)}'`;
}

function quotePowerShell(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

function bashPath(path: string): string {
  return /^[a-z]:[\\/]/i.test(path) ? path.replaceAll("\\", "/") : path;
}

export function buildResumeCommand({
  location,
  resumeId,
  shell,
  toolId,
}: {
  location: ConversationLocation;
  resumeId: string;
  shell: Shell;
  toolId: string;
}): string | null {
  const executable = RESUME_TOOL[toolId];
  if (!executable) return null;

  const quote = shell === "bash" ? quoteBash : quotePowerShell;
  const path = shell === "bash" ? bashPath(location.path) : location.path;
  const changeDirectory = shell === "bash"
    ? `cd ${quote(path)}`
    : `Set-Location -LiteralPath ${quote(path)}`;
  const resume = executable === "codex"
    ? `${executable} resume ${quote(resumeId)}`
    : executable === "claude"
      ? `${executable} --resume ${quote(resumeId)}`
      : `${executable} --resume=${quote(resumeId)}`;

  return `${changeDirectory} && ${resume}`;
}

export default function ResumeConversationCommand({
  location,
  resumeId,
  toolId,
}: {
  location: ConversationLocation;
  resumeId: string;
  toolId: string;
}) {
  const { t } = useI18n();
  const [shell, setShell] = useState<Shell>(() => isWindowsPath(location.path) ? "powershell" : "bash");
  const [copyStatus, setCopyStatus] = useState<CopyStatus>("idle");
  const resetTimer = useRef<number | null>(null);
  const command = useMemo(() => buildResumeCommand({
    location,
    resumeId,
    shell,
    toolId,
  }), [location, resumeId, shell, toolId]);

  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  if (!command) return null;

  const selectShell = (nextShell: Shell) => {
    setShell(nextShell);
    setCopyStatus("idle");
  };
  const onCopy = async () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    try {
      await copyText(command);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("error");
    }
    resetTimer.current = window.setTimeout(() => setCopyStatus("idle"), 1800);
  };
  const copyLabel = copyStatus === "copied"
    ? t.conversation.resumeCommandCopied
    : copyStatus === "error"
      ? t.conversation.resumeCommandCopyFailed
      : t.conversation.copyResumeCommand;

  return (
    <section className="conversation-resume" data-resume-command data-tool-id={toolId}>
      <div className="conversation-resume-heading">
        <span className="conversation-resume-title">
          <Icon name="terminal" size={13} strokeWidth={1.8} aria-hidden />
          {t.conversation.resumeWith}
        </span>
        <div className="conversation-resume-tabs" role="tablist" aria-label={t.conversation.resumeWith}>
          {(["powershell", "bash"] as const).map((candidate) => (
            <button
              key={candidate}
              type="button"
              role="tab"
              aria-selected={shell === candidate}
              className="conversation-resume-tab"
              onClick={() => selectShell(candidate)}
              data-resume-shell={candidate}
            >
              {candidate === "powershell" ? "PowerShell" : "Bash"}
            </button>
          ))}
        </div>
      </div>
      <div className="conversation-resume-command" data-resume-command-value={command}>
        <code>{command}</code>
        <button
          type="button"
          className="conversation-resume-copy"
          onClick={onCopy}
          aria-label={copyLabel}
          title={copyLabel}
          data-copy-status={copyStatus}
        >
          <Icon name={copyStatus === "copied" ? "check" : "copy"} size={14} aria-hidden />
        </button>
        <span className="sr-only" aria-live="polite">
          {copyStatus === "idle" ? "" : copyLabel}
        </span>
      </div>
    </section>
  );
}
