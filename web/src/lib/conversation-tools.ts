/** Tool names whose payloads are never actionable conversation interactions. */
const META_TOOL_NAMES = new Set(["sendfeedback"]);

type ConversationInteractionItem = {
  interaction?: { tool_name?: string | null } | null;
};

export function isMetaConversationTool(value: string | null | undefined): boolean {
  return META_TOOL_NAMES.has(
    String(value || "").toLocaleLowerCase().replace(/[^a-z0-9]/g, ""),
  );
}

/** Remove historic meta-tool payloads before they reach interaction chrome. */
export function filterMetaConversationInteractions<T extends ConversationInteractionItem>(
  items: T[],
): T[] {
  return items.filter(
    (item) => !isMetaConversationTool(item.interaction?.tool_name),
  );
}
