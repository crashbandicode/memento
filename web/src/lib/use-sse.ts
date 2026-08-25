"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { api, getApiBase, isEmbeddedInDesktop } from "./api-client";
import { getStoredAuthToken } from "./auth-storage";
import {
  buildEventStreamUrl,
  RealtimeEventData,
  RealtimeEventLike,
} from "./realtime-events";

export interface SSEEvent extends RealtimeEventLike {
  data: RealtimeEventData;
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
) {
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
    let generation = 0;
    let windowBlurred = false;
    let lastResumeAt = 0;
    let lastEventId = "";
    let streamAttempted = false;
    const MIN_RETRY_MS = 5000;
    const MAX_RETRY_MS = 60000;
    let retryDelay = MIN_RETRY_MS;

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
      // Back off on repeated failures so a stream that can't authenticate (or a
      // server that is down) never retries in a tight 5s loop. The delay resets
      // to the floor as soon as a connection actually opens or the tab resumes.
      const delay = retryDelay;
      retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS);
      reconnectTimer = setTimeout(() => void connect(), delay);
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
        const session = await api.createEventSession(token);
        if (stopped || attempt !== generation || documentIsHidden()) return;
        // Embedded webviews can't send the SameSite=lax session cookie on the
        // cross-site stream request, so they authenticate with the scoped,
        // short-lived token from the session response instead.
        const streamToken = isEmbeddedInDesktop() ? session?.stream_token : undefined;
        const next = new EventSource(buildEventStreamUrl(base, lastEventId, streamToken), {
          withCredentials: true,
        });
        next.onopen = () => { retryDelay = MIN_RETRY_MS; };
        streamAttempted = true;
        es = next;

        const handleEvent = (e: MessageEvent<string>) => {
          try {
            const event: SSEEvent = JSON.parse(e.data);
            if (e.lastEventId) {
              lastEventId = e.lastEventId;
              event.id = e.lastEventId;
            } else if (event.type === "realtime_reset") {
              // An expired Redis stream has no resumable watermark.
              lastEventId = "";
            }
            onEventRef.current(event);
          } catch {}
        };

        next.addEventListener("file_synced", handleEvent);
        next.addEventListener("realtime_reset", handleEvent);
        next.addEventListener("stream_ready", (e) => {
          if (e.lastEventId) lastEventId = e.lastEventId;
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
      // A deliberate resume (focus/visibility/online) should reconnect promptly,
      // not at a backed-off delay from earlier failures.
      retryDelay = MIN_RETRY_MS;
      const needsReconciliation = streamAttempted && !lastEventId;
      closeStream();
      if (needsReconciliation) {
        onEventRef.current({
          type: "realtime_reset",
          data: { reason: "missing_initial_watermark" },
          timestamp: Date.now() / 1_000,
        });
      }
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
