import styles from "./AssistantIdentityBadge.module.css";

interface AssistantIdentityBadgeProps {
  model?: string | null;
  reasoningEffort?: string | null;
  thinkingLabel?: string;
}

function cleanIdentityValue(value?: string | null): string {
  return (value || "").replace(/[\u0000-\u001F\u007F]/g, "").trim();
}

function titleCaseToken(value: string): string {
  return value ? value[0].toUpperCase() + value.slice(1).toLowerCase() : "";
}

export function formatAssistantModelLabel(value?: string | null): string {
  const model = cleanIdentityValue(value);
  if (!model) return "";

  const claude = /^claude-(opus|sonnet|haiku)-(\d+)-(\d+)$/i.exec(model);
  if (claude) {
    return `Claude ${titleCaseToken(claude[1])} ${claude[2]}.${claude[3]}`;
  }

  const gpt = /^gpt-(\d+(?:\.\d+)?)(?:-(.+))?$/i.exec(model);
  if (gpt) {
    const variant = gpt[2]
      ? ` ${gpt[2].split("-").map(titleCaseToken).join(" ")}`
      : "";
    return `GPT-${gpt[1]}${variant}`;
  }

  return model;
}

export function formatReasoningEffortLabel(value?: string | null): string {
  const effort = cleanIdentityValue(value).toLowerCase().replace(/_/g, "-");
  if (!effort) return "";
  if (effort === "xhigh" || effort === "x-high") return "X-high";
  return titleCaseToken(effort);
}

export default function AssistantIdentityBadge({
  model,
  reasoningEffort,
  thinkingLabel = "Thinking",
}: AssistantIdentityBadgeProps) {
  const rawModel = cleanIdentityValue(model);
  const rawEffort = cleanIdentityValue(reasoningEffort);
  const modelLabel = formatAssistantModelLabel(rawModel);
  const effortLabel = formatReasoningEffortLabel(rawEffort);
  const localizedThinkingLabel = cleanIdentityValue(thinkingLabel) || "Thinking";
  if (!modelLabel && !effortLabel) return null;

  const accessibleParts = [
    modelLabel ? `Model ${modelLabel}` : "",
    effortLabel ? `${localizedThinkingLabel} level ${effortLabel}` : "",
  ].filter(Boolean);
  const exactParts = [
    rawModel ? `Model: ${rawModel}` : "",
    rawEffort ? `${localizedThinkingLabel}: ${rawEffort}` : "",
  ].filter(Boolean);

  return (
    <span
      className={styles.badge}
      aria-label={accessibleParts.join(", ")}
      data-assistant-model={rawModel || undefined}
      data-assistant-reasoning={rawEffort || undefined}
      title={exactParts.join(" · ")}
    >
      {modelLabel && <span className={styles.modelMark} aria-hidden="true" />}
      {modelLabel && <span className={styles.model}>{modelLabel}</span>}
      {effortLabel && (
        <>
          {modelLabel && <span className={styles.divider} aria-hidden="true" />}
          <span className={styles.reasoning}>
            <span className={styles.reasoningMark} aria-hidden="true" />
            <span>{localizedThinkingLabel} · {effortLabel}</span>
          </span>
        </>
      )}
    </span>
  );
}
