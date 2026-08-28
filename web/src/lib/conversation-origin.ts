export function isParentAgentMessage(
  msg: { role?: string | null; message_type?: string | null; origin?: "parent_agent" | "human" | null },
  userRoleOrigin?: "parent_agent" | null,
): boolean {
  if ((msg.role || msg.message_type) !== "user") return false;
  if (msg.origin === "human") return false;
  if (msg.origin === "parent_agent") return true;
  return userRoleOrigin === "parent_agent";
}
