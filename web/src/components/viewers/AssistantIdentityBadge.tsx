import type { IconType } from "react-icons";
import { FiZap } from "react-icons/fi";
import {
  SiAnthropic,
  SiGooglegemini,
  SiMeta,
  SiMistralai,
  SiOpenai,
} from "react-icons/si";
import styles from "./AssistantIdentityBadge.module.css";

interface AssistantIdentityBadgeProps {
  model?: string | null;
  reasoningEffort?: string | null;
  serviceTier?: string | null;
  thinkingLabel?: string;
}

const FAST_SERVICE_TIERS = new Set(["fast", "priority", "priority-processing"]);

function cleanIdentityValue(value?: string | null): string {
  return (value || "").replace(/[\u0000-\u001F\u007F]/g, "").trim();
}

function titleCaseToken(value: string): string {
  return value ? value[0].toUpperCase() + value.slice(1).toLowerCase() : "";
}

type AssistantModelProvider =
  | "anthropic"
  | "openai"
  | "xai"
  | "google"
  | "mistral"
  | "meta"
  | "deepseek"
  | "qwen"
  | "generic";

interface AssistantProviderIdentity {
  id: AssistantModelProvider;
  label: string;
  icon?: IconType;
  monogram?: string;
}

export function assistantModelProvider(value?: string | null): AssistantProviderIdentity {
  const model = cleanIdentityValue(value).toLowerCase();
  if (/claude|anthropic/.test(model)) return { id: "anthropic", label: "Anthropic", icon: SiAnthropic };
  if (/grok|(?:^|[-_/])xai(?:[-_/]|$)/.test(model)) return { id: "xai", label: "xAI", monogram: "xAI" };
  if (/gemini|google/.test(model)) return { id: "google", label: "Google", icon: SiGooglegemini };
  if (/mistral|mixtral/.test(model)) return { id: "mistral", label: "Mistral AI", icon: SiMistralai };
  if (/llama|(?:^|[-_/])meta(?:[-_/]|$)/.test(model)) return { id: "meta", label: "Meta", icon: SiMeta };
  if (/deepseek/.test(model)) return { id: "deepseek", label: "DeepSeek", monogram: "DS" };
  if (/qwen|alibaba/.test(model)) return { id: "qwen", label: "Qwen", monogram: "Q" };
  if (/openai|codex|(?:^|[-_/])gpt(?:[-_/]|$)|^o(?:1|3|4)(?:[-_/]|$)/.test(model)) {
    return { id: "openai", label: "OpenAI", icon: SiOpenai };
  }
  return { id: "generic", label: "AI model" };
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

  const branded = /^(grok|gemini|mistral|mixtral|llama|deepseek|qwen)[-_](.+)$/i.exec(model);
  if (branded) {
    return `${titleCaseToken(branded[1])} ${branded[2].split(/[-_]/).map(titleCaseToken).join(" ")}`;
  }

  return model;
}

export function formatReasoningEffortLabel(value?: string | null): string {
  const effort = cleanIdentityValue(value).toLowerCase().replace(/_/g, "-");
  if (!effort) return "";
  if (effort === "xhigh" || effort === "x-high") return "X-high";
  return titleCaseToken(effort);
}

export function isFastServiceTier(value?: string | null): boolean {
  const tier = cleanIdentityValue(value).toLowerCase().replace(/_/g, "-");
  return FAST_SERVICE_TIERS.has(tier);
}

export default function AssistantIdentityBadge({
  model,
  reasoningEffort,
  serviceTier,
  thinkingLabel = "Thinking",
}: AssistantIdentityBadgeProps) {
  const rawModel = cleanIdentityValue(model);
  const rawEffort = cleanIdentityValue(reasoningEffort);
  const rawServiceTier = cleanIdentityValue(serviceTier);
  const modelLabel = formatAssistantModelLabel(rawModel);
  const provider = assistantModelProvider(rawModel);
  const effortLabel = formatReasoningEffortLabel(rawEffort);
  const fastMode = isFastServiceTier(rawServiceTier);
  const localizedThinkingLabel = cleanIdentityValue(thinkingLabel) || "Thinking";
  if (!modelLabel && !effortLabel && !fastMode) return null;

  const accessibleParts = [
    modelLabel ? `Model ${modelLabel}` : "",
    effortLabel ? `${localizedThinkingLabel} level ${effortLabel}` : "",
    fastMode ? "Fast mode" : "",
  ].filter(Boolean);
  const exactParts = [
    rawModel ? `Model: ${rawModel}` : "",
    rawEffort ? `${localizedThinkingLabel}: ${rawEffort}` : "",
    rawServiceTier ? `Service tier: ${rawServiceTier}` : "",
  ].filter(Boolean);

  return (
    <span
      className={styles.badge}
      aria-label={accessibleParts.join(", ")}
      data-assistant-model={rawModel || undefined}
      data-assistant-reasoning={rawEffort || undefined}
      data-assistant-service-tier={rawServiceTier || undefined}
      data-assistant-provider={provider.id}
      title={exactParts.join(" · ")}
    >
      {modelLabel && (
        <span className={styles.modelMark} title={provider.label} aria-hidden="true">
          {provider.icon ? (
            <provider.icon className={styles.providerIcon} />
          ) : provider.monogram ? (
            <span className={styles.providerMonogram}>{provider.monogram}</span>
          ) : (
            <span className={styles.genericMark} />
          )}
        </span>
      )}
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
      {fastMode && (
        <>
          {(modelLabel || effortLabel) && <span className={styles.divider} aria-hidden="true" />}
          <span className={styles.serviceTier}>
            <FiZap aria-hidden="true" />
            <span>Fast</span>
          </span>
        </>
      )}
    </span>
  );
}
