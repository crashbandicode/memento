"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ConversationPrompt,
  invalidateConversationMessages,
  invalidateConversationPrompts,
  invalidateConversationSearch,
} from "./api-client";
import { useSSE } from "./use-sse";

interface ConversationSyncScope {
  toolId?: string | null;
  relativePath?: string | null;
}

function pathLinkedRootId(relativePath: string): string {
  const normalized = relativePath.replaceAll("\\", "/");
  const childMatch = normalized.match(/\/([^/]+)\/subagents\/[^/]+$/);
  if (childMatch?.[1]) return childMatch[1];
  const filename = normalized.split("/").at(-1) || "";
  return filename.replace(/\.[^.]+$/, "");
}

function isCompanionConversationEvent(
  event: {
    data: {
      tool_id?: string;
      category?: string;
      relative_path?: string;
    };
  },
  scope: ConversationSyncScope,
): boolean {
  const eventPath = event.data.relative_path || "";
  if (
    event.data.category !== "conversation"
    || !eventPath.replaceAll("\\", "/").includes("/subagents/")
  ) return false;

  // If metadata has not loaded yet, one extra refresh is safer than missing a
  // newly created child. SSE is already user-scoped, and child creation is rare.
  if (!scope.toolId || !scope.relativePath) return true;
  if (
    event.data.tool_id !== scope.toolId
    || !["claude_code", "cursor"].includes(scope.toolId)
  ) return false;
  return pathLinkedRootId(eventPath) === pathLinkedRootId(scope.relativePath);
}

/**
 * Keeps the lightweight prompt outline independent from the expensive
 * transcript renderer. Large conversations can therefore refresh the mobile
 * navigator before their message body has finished rendering.
 */
export function useConversationPrompts(
  documentId: string,
  scope: ConversationSyncScope = {},
) {
  const [promptState, setPromptState] = useState<{
    documentId: string;
    prompts: ConversationPrompt[];
  }>({ documentId, prompts: [] });
  const [syncVersion, setSyncVersion] = useState(0);
  const refreshTimer = useRef<number | null>(null);
  const prompts = promptState.documentId === documentId
    ? promptState.prompts
    : [];

  const refresh = useCallback(async () => {
    try {
      const response = await api.getPrompts(documentId);
      setPromptState({ documentId, prompts: response.prompts });
    } catch (error) {
      console.error("Failed to load prompt outline:", error);
    }
  }, [documentId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [documentId, refresh]);

  const scheduleCatchUp = useCallback((delay: number) => {
    if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      invalidateConversationPrompts(documentId);
      invalidateConversationMessages(documentId);
      invalidateConversationSearch(documentId);
      setSyncVersion((version) => version + 1);
      void refresh();
    }, delay);
  }, [documentId, refresh]);

  useSSE(
    (event) => {
      if (
        event.data.document_id !== documentId
        && !isCompanionConversationEvent(event, scope)
      ) return;
      scheduleCatchUp(250);
    },
    {
      // Mobile browsers may suspend EventSource without firing `error`.
      // Always reconcile the prompt outline and message tail on resume even
      // if no replayable SSE event survived the suspension window.
      onResume: () => scheduleCatchUp(0),
    },
  );

  useEffect(() => () => {
    if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
  }, []);

  return { prompts, syncVersion };
}
