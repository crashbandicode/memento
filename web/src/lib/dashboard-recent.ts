export function partitionDashboardRecent<T extends {
  id: string;
  orchestration?: string | null;
  is_low_activity: boolean;
  pending_question_count?: number;
}>(recent: T[]) {
  const attention = recent.filter((conversation) => (conversation.pending_question_count || 0) > 0);
  const attentionIds = new Set(attention.map((conversation) => conversation.id));
  const clawDelegates = recent.filter(
    (conversation) => conversation.orchestration === "claw" && !attentionIds.has(conversation.id),
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
