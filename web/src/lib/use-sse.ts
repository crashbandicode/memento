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

/**
 * Hook that subscribes to the SSE event stream.
 * Calls `onEvent` whenever a new file_synced event arrives.
 * Auto-reconnects on disconnect.
 */
export function useSSE(onEvent: (event: SSEEvent) => void) {
  const onEventRef = useRef(onEvent);
  // Sync the latest onEvent handler into ref AFTER render, not during.
  // This avoids the React 19 "refs during render" rule violation while
  // preserving the "always-fresh-callback" semantics inside the effect below.
  useLayoutEffect(() => {
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let connecting = false;
    let stopped = false;

    function scheduleReconnect() {
      if (stopped) return;
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => void connect(), 5000);
    }

    async function connect() {
      if (stopped || connecting) return;
      const base = getApiBase();
      const token = getStoredAuthToken();
      if (!token) return; // Not logged in — don't connect SSE
      connecting = true;
      try {
        await api.createEventSession(token);
        if (stopped) return;
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
          scheduleReconnect();
        };
      } catch {
        scheduleReconnect();
      } finally {
        connecting = false;
      }
    }

    void connect();

    return () => {
      stopped = true;
      es?.close();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    };
  }, []);
}
