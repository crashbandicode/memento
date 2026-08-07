export type RealtimeChange =
  | "conversation.messages"
  | "conversation.metadata"
  | "conversation.pending_interactions"
  | "conversation.prompts"
  | "conversation.search"
  | "dashboard";

export interface RealtimeEventData {
  document_id?: string;
  tool_id?: string;
  category?: string;
  relative_path?: string;
  title?: string;
  changes?: RealtimeChange[];
  reason?: string;
}

export interface RealtimeEventLike {
  id?: string;
  type: string;
  data: RealtimeEventData;
  timestamp: number;
}

export interface ConversationSyncScope {
  toolId?: string | null;
  relativePath?: string | null;
}

export interface ConversationInvalidation {
  messages: boolean;
  metadata: boolean;
  pendingInteractions: boolean;
  prompts: boolean;
  search: boolean;
}

export const NO_CONVERSATION_INVALIDATION: ConversationInvalidation = {
  messages: false,
  metadata: false,
  pendingInteractions: false,
  prompts: false,
  search: false,
};

function allConversationInvalidations(): ConversationInvalidation {
  return {
    messages: true,
    metadata: true,
    pendingInteractions: true,
    prompts: true,
    search: true,
  };
}

function pathLinkedRootId(relativePath: string): string {
  const normalized = relativePath.replaceAll("\\", "/");
  const childMatch = normalized.match(/\/([^/]+)\/subagents\/[^/]+$/);
  if (childMatch?.[1]) return childMatch[1];
  const filename = normalized.split("/").at(-1) || "";
  return filename.replace(/\.[^.]+$/, "");
}

function isCompanionConversationEvent(
  event: RealtimeEventLike,
  scope: ConversationSyncScope,
): boolean {
  const eventPath = event.data.relative_path || "";
  if (
    event.data.category !== "conversation"
    || !eventPath.replaceAll("\\", "/").includes("/subagents/")
  ) return false;

  // Metadata can arrive before the page's initial summary. One bounded metadata
  // refresh is safer than permanently hiding a newly linked child.
  if (!scope.toolId || !scope.relativePath) return true;
  if (
    event.data.tool_id !== scope.toolId
    || !["claude_code", "cursor"].includes(scope.toolId)
  ) return false;
  return pathLinkedRootId(eventPath) === pathLinkedRootId(scope.relativePath);
}

export function conversationInvalidationForEvent(
  event: RealtimeEventLike,
  documentId: string,
  scope: ConversationSyncScope = {},
): ConversationInvalidation {
  if (event.type === "realtime_reset") return allConversationInvalidations();
  if (event.type !== "file_synced") return NO_CONVERSATION_INVALIDATION;

  const changes = event.data.changes;
  const legacyEvent = !Array.isArray(changes);
  if (event.data.document_id === documentId) {
    if (legacyEvent) return allConversationInvalidations();
    const selected = new Set(changes);
    return {
      messages: selected.has("conversation.messages"),
      metadata: selected.has("conversation.metadata"),
      pendingInteractions: selected.has("conversation.pending_interactions"),
      prompts: selected.has("conversation.prompts"),
      search: selected.has("conversation.search"),
    };
  }

  if (!isCompanionConversationEvent(event, scope)) {
    return NO_CONVERSATION_INVALIDATION;
  }
  return {
    ...NO_CONVERSATION_INVALIDATION,
    metadata: legacyEvent || changes.includes("conversation.metadata"),
  };
}

export function eventInvalidatesDashboard(event: RealtimeEventLike): boolean {
  if (event.type === "realtime_reset") return true;
  if (event.type !== "file_synced") return false;
  return !Array.isArray(event.data.changes)
    || event.data.changes.includes("dashboard");
}

export function buildEventStreamUrl(base: string, lastEventId: string): string {
  const endpoint = `${base}/api/events/stream`;
  if (!lastEventId) return endpoint;
  const query = new URLSearchParams({ cursor: lastEventId });
  return `${endpoint}?${query.toString()}`;
}
