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

/**
 * Keeps the lightweight prompt outline independent from the expensive
 * transcript renderer. Large conversations can therefore refresh the mobile
 * navigator before their message body has finished rendering.
 */
export function useConversationPrompts(documentId: string) {
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
      if (event.data.document_id !== documentId) return;
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
