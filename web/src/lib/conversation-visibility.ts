/** Shared display/export content filters for conversation threads. */

export type ConversationVisibility = {
  user: boolean;
  assistant: boolean;
  tools: boolean;
  tasks: boolean;
  agents: boolean;
  thinking: boolean;
  context: boolean;
};

export const DEFAULT_CONVERSATION_VISIBILITY: ConversationVisibility = {
  user: true,
  assistant: true,
  tools: true,
  tasks: true,
  agents: true,
  thinking: true,
  context: true,
};

const STORAGE_PREFIX = "memento.conversationVisibility.";

function isVisibility(value: unknown): value is ConversationVisibility {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.user === "boolean"
    && typeof record.assistant === "boolean"
    && typeof record.tools === "boolean"
    && typeof record.tasks === "boolean"
    && typeof record.agents === "boolean"
    && typeof record.thinking === "boolean"
    && typeof record.context === "boolean"
  );
}

export function readConversationVisibility(documentId: string): ConversationVisibility {
  if (typeof window === "undefined") return DEFAULT_CONVERSATION_VISIBILITY;
  try {
    const raw = window.sessionStorage.getItem(`${STORAGE_PREFIX}${documentId}`);
    if (!raw) return DEFAULT_CONVERSATION_VISIBILITY;
    const parsed: unknown = JSON.parse(raw);
    if (!isVisibility(parsed)) return DEFAULT_CONVERSATION_VISIBILITY;
    return parsed;
  } catch {
    return DEFAULT_CONVERSATION_VISIBILITY;
  }
}

export function writeConversationVisibility(
  documentId: string,
  visibility: ConversationVisibility,
): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${STORAGE_PREFIX}${documentId}`, JSON.stringify(visibility));
  } catch {
    // Ignore quota / private-mode failures.
  }
}
