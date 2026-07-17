"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { api, getApiBase } from "./api-client";
import { getStoredAuthToken } from "./auth-storage";

export interface SSEEvent {
  type: string;
  data: {
    document_id?: string;
    tool_id?: string;
    category?: string;
    relative_path?: string;
    title?: string;
  };
  timestamp: number;
}

interface UseSSEOptions {
  /** Refresh page state after a suspended/backgrounded tab becomes active. */
  onResume?: () => void;
}

function documentIsHidden(): boolean {
  return document.visibilityState === "hidden";
}

/**
 * Hook that subscribes to the SSE event stream.
 * Calls `onEvent` whenever a new file_synced event arrives.
 * Auto-reconnects on disconnect.
 */
export function useSSE(
  onEvent: (event: SSEEvent) => void,
  { onResume }: UseSSEOptions = {},
) {
  const onEventRef = useRef(onEvent);
  const onResumeRef = useRef(onResume);
  // Sync the latest onEvent handler into ref AFTER render, not during.
  // This avoids the React 19 "refs during render" rule violation while
  // preserving the "always-fresh-callback" semantics inside the effect below.
  useLayoutEffect(() => {
    onEventRef.current = onEvent;
    onResumeRef.current = onResume;
  });

  useEffect(() => {
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let connecting = false;
    let stopped = false;
    let generation = 0;
    let windowBlurred = false;
    let lastResumeAt = 0;

    function clearReconnectTimer() {
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    function closeStream() {
      generation += 1;
      clearReconnectTimer();
      es?.close();
      es = null;
      connecting = false;
    }

    function scheduleReconnect() {
      if (stopped || documentIsHidden()) return;
      clearReconnectTimer();
      reconnectTimer = setTimeout(() => void connect(), 5000);
    }

    async function connect() {
      if (
        stopped
        || connecting
        || documentIsHidden()
        || es?.readyState === EventSource.OPEN
        || es?.readyState === EventSource.CONNECTING
      ) return;
      const base = getApiBase();
      const token = getStoredAuthToken();
      if (!token) return; // Not logged in — don't connect SSE
      const attempt = ++generation;
      connecting = true;
      try {
        await api.createEventSession(token);
        if (stopped || attempt !== generation || documentIsHidden()) return;
        const next = new EventSource(`${base}/api/events/stream`, {
          withCredentials: true,
        });
        es = next;

        next.addEventListener("file_synced", (e) => {
          try {
            const event: SSEEvent = JSON.parse(e.data);
            onEventRef.current(event);
          } catch {}
        });

        next.addEventListener("keepalive", () => {
          // ignore keepalives
        });

        next.onerror = () => {
          next.close();
          if (es === next) es = null;
          if (attempt === generation) generation += 1;
          scheduleReconnect();
        };
      } catch {
        if (attempt === generation) scheduleReconnect();
      } finally {
        if (attempt === generation) connecting = false;
      }
    }

    function resume() {
      if (stopped || documentIsHidden()) return;
      const now = Date.now();
      // visibilitychange, focus, pageshow, and online can arrive together.
      // One reconnect/catch-up is sufficient for the whole resume transition.
      if (now - lastResumeAt < 750) return;
      lastResumeAt = now;
      closeStream();
      onResumeRef.current?.();
      void connect();
    }

    function handleVisibilityChange() {
      if (documentIsHidden()) {
        closeStream();
        return;
      }
      resume();
    }

    function handlePageShow(event: PageTransitionEvent) {
      if (event.persisted) resume();
    }

    function handleBlur() {
      windowBlurred = true;
    }

    function handleFocus() {
      if (!windowBlurred) return;
      windowBlurred = false;
      resume();
    }

    void connect();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pageshow", handlePageShow);
    window.addEventListener("online", resume);
    window.addEventListener("blur", handleBlur);
    window.addEventListener("focus", handleFocus);

    return () => {
      stopped = true;
      closeStream();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("pageshow", handlePageShow);
      window.removeEventListener("online", resume);
      window.removeEventListener("blur", handleBlur);
      window.removeEventListener("focus", handleFocus);
    };
  }, []);
}
