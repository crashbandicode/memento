/** Tool names whose payloads are never actionable conversation interactions. */
const META_TOOL_NAMES = new Set(["sendfeedback"]);

export function isMetaConversationTool(value: string | null | undefined): boolean {
  return META_TOOL_NAMES.has(
    String(value || "").toLocaleLowerCase().replace(/[^a-z0-9]/g, ""),
  );
}
