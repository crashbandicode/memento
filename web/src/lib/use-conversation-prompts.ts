"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ConversationPrompt, invalidateConversationPrompts } from "./api-client";
import { useSSE } from "./use-sse";

/**
 * Keeps the lightweight prompt outline independent from the expensive
 * transcript renderer. Large conversations can therefore refresh the mobile
 * navigator before their message body has finished rendering.
 */
export function useConversationPrompts(documentId: string): ConversationPrompt[] {
  const [prompts, setPrompts] = useState<ConversationPrompt[]>([]);
  const refreshTimer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await api.getPrompts(documentId);
      setPrompts(response.prompts);
    } catch (error) {
      console.error("Failed to load prompt outline:", error);
    }
  }, [documentId]);

  useEffect(() => {
    setPrompts([]);
    void refresh();
  }, [documentId, refresh]);

  useSSE((event) => {
    if (event.data.document_id !== documentId) return;
    if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      invalidateConversationPrompts(documentId);
      void refresh();
    }, 250);
  });

  useEffect(() => () => {
    if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
  }, []);

  return prompts;
}
