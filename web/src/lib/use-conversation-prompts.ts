"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ConversationPrompt,
  invalidateConversationMessages,
  invalidateConversationPrompts,
  invalidateConversationSearch,
} from "./api-client";
import {
  ConversationInvalidation,
  ConversationSyncScope,
  conversationInvalidationForEvent,
  NO_CONVERSATION_INVALIDATION,
} from "./realtime-events";
import { useSSE } from "./use-sse";

interface ConversationSyncVersions {
  messages: number;
  metadata: number;
  pendingInteractions: number;
  search: number;
  controlSessions: number;
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
  const [syncVersions, setSyncVersions] = useState<ConversationSyncVersions>({
    messages: 0,
    metadata: 0,
    pendingInteractions: 0,
    search: 0,
    controlSessions: 0,
  });
  const refreshTimer = useRef<number | null>(null);
  const projectionRef = useRef<{
    documentId: string;
    generation?: number;
    projectedThroughLine?: number;
  }>({ documentId });
  const pendingInvalidation = useRef<ConversationInvalidation>({
    ...NO_CONVERSATION_INVALIDATION,
  });
  const prompts = promptState.documentId === documentId
    ? promptState.prompts
    : [];

  const refresh = useCallback(async () => {
    try {
      const cursor = projectionRef.current.documentId === documentId
        ? projectionRef.current
        : { documentId };
      const response = await api.getPrompts(
        documentId,
        cursor.projectedThroughLine,
        cursor.generation,
      );
      setPromptState((previous) => {
        if (
          response.reset
          || previous.documentId !== documentId
          || cursor.projectedThroughLine === undefined
        ) {
          return { documentId, prompts: response.prompts };
        }
        const byId = new Map(previous.prompts.map((prompt) => [prompt.id, prompt]));
        response.prompts.forEach((prompt) => byId.set(prompt.id, prompt));
        return {
          documentId,
          prompts: Array.from(byId.values()).sort(
            (left, right) => left.line_number - right.line_number,
          ),
        };
      });
      projectionRef.current = {
        documentId,
        generation: response.generation,
        projectedThroughLine: response.projected_through_line,
      };
    } catch (error) {
      console.error("Failed to load prompt outline:", error);
    }
  }, [documentId]);

  useEffect(() => {
    projectionRef.current = { documentId };
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [documentId, refresh]);

  const scheduleCatchUp = useCallback((
    invalidation: ConversationInvalidation,
    delay: number,
  ) => {
    if (!Object.values(invalidation).some(Boolean)) return;
    pendingInvalidation.current = {
      messages: pendingInvalidation.current.messages || invalidation.messages,
      metadata: pendingInvalidation.current.metadata || invalidation.metadata,
      pendingInteractions: (
        pendingInvalidation.current.pendingInteractions
        || invalidation.pendingInteractions
      ),
      prompts: pendingInvalidation.current.prompts || invalidation.prompts,
      search: pendingInvalidation.current.search || invalidation.search,
    };
    if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      const next = pendingInvalidation.current;
      pendingInvalidation.current = { ...NO_CONVERSATION_INVALIDATION };
      if (next.prompts) invalidateConversationPrompts(documentId);
      if (next.messages) invalidateConversationMessages(documentId);
      if (next.search) invalidateConversationSearch(documentId);
      setSyncVersions((versions) => ({
        ...versions,
        messages: versions.messages + Number(next.messages),
        metadata: versions.metadata + Number(next.metadata),
        pendingInteractions: (
          versions.pendingInteractions + Number(next.pendingInteractions)
        ),
        search: versions.search + Number(next.search),
      }));
      if (next.prompts) void refresh();
    }, delay);
  }, [documentId, refresh]);

  useSSE((event) => {
    // Managed-session state rides the page's single event stream; opening a
    // second EventSource per component starves fixtures and server slots.
    if (event.type === "control_session") {
      const data = (event.data ?? {}) as unknown as Record<string, unknown>;
      if (data.document_id === documentId) {
        setSyncVersions((versions) => ({
          ...versions,
          controlSessions: versions.controlSessions + 1,
        }));
      }
      return;
    }
    scheduleCatchUp(
      conversationInvalidationForEvent(event, documentId, scope),
      event.type === "realtime_reset" ? 0 : 250,
    );
  });

  useEffect(() => () => {
    if (refreshTimer.current !== null) clearTimeout(refreshTimer.current);
  }, []);

  return { prompts, syncVersions };
}
