"use client";

import { useEffect, useRef, useState } from "react";
import type { ConversationLocation as ConversationLocationValue } from "@/lib/api-client";
import { fmt, useI18n } from "@/lib/i18n";
import { Icon } from "@/components/aurora/Icon";

type CopyStatus = "idle" | "copied" | "error";

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const input = document.createElement("textarea");
  input.value = value;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("Clipboard unavailable");
}

export default function ConversationLocation({
  location,
}: {
  location: ConversationLocationValue;
}) {
  const { t } = useI18n();
  const [status, setStatus] = useState<CopyStatus>("idle");
  const resetTimer = useRef<number | null>(null);
  const copyValue = `${location.host}:${location.path}`;
  const fullLabel = `${t.conversation.projectLocation}: ${location.host} · ${location.path}`;

  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  const onCopy = async () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    try {
      await copyText(copyValue);
      setStatus("copied");
    } catch {
      setStatus("error");
    }
    resetTimer.current = window.setTimeout(() => setStatus("idle"), 1800);
  };

  const statusLabel = status === "copied"
    ? t.conversation.projectLocationCopied
    : status === "error"
      ? t.conversation.projectLocationCopyFailed
      : "";

  return (
    <div className="conversation-location-row">
      <button
        type="button"
        className="conversation-location"
        onClick={onCopy}
        title={fullLabel}
        aria-label={fmt(t.conversation.copyProjectLocation, {
          host: location.host,
          path: location.path,
        })}
        data-copy-status={status}
      >
        <span className="conversation-location-glyph" aria-hidden>
          <Icon name="terminal" size={12} strokeWidth={1.8} />
        </span>
        <span className="conversation-location-host">{location.host}</span>
        <span className="conversation-location-separator" aria-hidden>·</span>
        <code className="conversation-location-path">{location.path}</code>
        <Icon
          name={status === "copied" ? "check" : "copy"}
          size={11}
          aria-hidden
          style={{ color: status === "copied" ? "#10B981" : "var(--aurora-fg4)" }}
        />
        <span className="sr-only" aria-live="polite">{statusLabel}</span>
      </button>
    </div>
  );
}
