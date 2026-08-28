export function partitionDashboardRecent<T extends {
  id: string;
  orchestration?: string | null;
  claw_delegate?: boolean;
  is_low_activity: boolean;
  pending_question_count?: number;
}>(recent: T[]) {
  const attention = recent.filter((conversation) => (conversation.pending_question_count || 0) > 0);
  const attentionIds = new Set(attention.map((conversation) => conversation.id));
  const clawDelegates = recent.filter(
    (conversation) => (
      // Prefer the server's group-membership flag (matches the aggregate
      // count exactly); fall back to raw orchestration only for older
      // payloads that predate it.
      (conversation.claw_delegate ?? conversation.orchestration === "claw")
      && !attentionIds.has(conversation.id)
    ),
  );
  const clawIds = new Set(clawDelegates.map((conversation) => conversation.id));
  const active = recent
    .filter((conversation) => (
      !conversation.is_low_activity
      && !attentionIds.has(conversation.id)
      && !clawIds.has(conversation.id)
    ))
    .slice(0, 10);
  const lowActivity = recent.filter((conversation) => (
    conversation.is_low_activity
    && !attentionIds.has(conversation.id)
    && !clawIds.has(conversation.id)
  ));
  return { attention, active, lowActivity, clawDelegates };
}

export function clawDelegateGroupCount(
  sample: { id: string }[],
  totalCount?: number | null,
) {
  if (typeof totalCount === "number" && totalCount >= sample.length) {
    return totalCount;
  }
  return sample.length;
}
