import type { ConversationTokenUsage } from "@/lib/api-client";

function compactCount(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: value >= 1_000 ? "compact" : "standard",
    maximumFractionDigits: value >= 10_000 ? 1 : 0,
  }).format(value);
}

export default function TokenUsageBadge({
  usage,
}: {
  usage?: ConversationTokenUsage | null;
}) {
  const inputTokens = Math.max(0, Number(usage?.input_tokens) || 0);
  const outputTokens = Math.max(0, Number(usage?.output_tokens) || 0);
  if (!usage || (!inputTokens && !outputTokens)) return null;

  const details = [
    `Input: ${inputTokens.toLocaleString()} tokens`,
    `Output: ${outputTokens.toLocaleString()} tokens`,
    usage.cached_input_tokens
      ? `Cache read: ${usage.cached_input_tokens.toLocaleString()}`
      : "",
    usage.cache_write_input_tokens
      ? `Cache write: ${usage.cache_write_input_tokens.toLocaleString()}`
      : "",
    usage.reasoning_output_tokens
      ? `Reasoning output: ${usage.reasoning_output_tokens.toLocaleString()}`
      : "",
  ].filter(Boolean).join(" · ");

  return (
    <span
      title={details}
      aria-label={details}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 9px",
        border: "1px solid color-mix(in srgb, var(--aurora-accent) 18%, var(--aurora-border))",
        borderRadius: 999,
        background: "color-mix(in srgb, var(--aurora-accent-soft) 55%, transparent)",
        color: "var(--aurora-fg2)",
        fontSize: 10.5,
        fontWeight: 650,
        whiteSpace: "nowrap",
      }}
    >
      <span aria-hidden="true" style={{ color: "var(--aurora-accent)" }}>↳</span>
      {compactCount(inputTokens)} in
      <span aria-hidden="true" style={{ opacity: 0.42 }}>·</span>
      <span aria-hidden="true" style={{ color: "var(--aurora-accent)" }}>↲</span>
      {compactCount(outputTokens)} out
    </span>
  );
}
