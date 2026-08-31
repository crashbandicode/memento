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
    let wasBackgrounded = false;
    let lastResumeAt = 0;
    let lastReconciliationAt = 0;
    let lastEventId = "";
    let streamAttempted = false;
    const MIN_RETRY_MS = 5000;
    const MAX_RETRY_MS = 60000;
    let retryDelay = MIN_RETRY_MS;
    // Liveness watchdog: mobile networks silently drop the SSE socket (carrier
    // NAT / radio sleep) WITHOUT firing onerror, so a dead stream still reports
    // "open" and no reconnect is scheduled — the user sees stale data until a
    // manual refresh. The server sends a keepalive every ~25s precisely so a
    // client can notice this; if we go quiet past STALE_MS, force a
    // resume-from-cursor reconnect so buffered events replay.
    const STALE_MS = 45000;
    let lastActivityAt = Date.now();
    let livenessTimer: ReturnType<typeof setInterval> | null = null;
    function markActivity() { lastActivityAt = Date.now(); }

    function clearReconnectTimer() {
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    function clearLivenessTimer() {
      if (livenessTimer !== null) clearInterval(livenessTimer);
      livenessTimer = null;
    }

    function reconcile(reason: string) {
      const now = Date.now();
      // Foreground visibility, focus, and pageshow are often delivered as a
      // burst. Reconnect attempts have their own state machine, so keep this
      // notification coalescer separate from it.
      if (now - lastReconciliationAt < 750) return;
      lastReconciliationAt = now;
      onEventRef.current({
        type: "realtime_reset",
        data: { reason },
        timestamp: now / 1_000,
      });
    }

    function startLivenessWatchdog() {
      clearLivenessTimer();
      livenessTimer = setInterval(() => {
        if (stopped || documentIsHidden() || !es) return;
        if (Date.now() - lastActivityAt > STALE_MS) {
          // Reports open but silent past the keepalive window → treat as dead
          // and reconnect, resuming from lastEventId so missed events replay.
          closeStream();
          retryDelay = MIN_RETRY_MS;
          void connect();
        }
      }, 15000);
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
        next.onopen = () => { retryDelay = MIN_RETRY_MS; markActivity(); };
        streamAttempted = true;
        es = next;

        const handleEvent = (e: MessageEvent<string>) => {
          markActivity();
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
          markActivity();
          if (e.lastEventId) lastEventId = e.lastEventId;
        });

        next.addEventListener("keepalive", () => {
          // Keepalives carry no data, but they are the watchdog's heartbeat:
          // receiving one proves the stream is still alive.
          markActivity();
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
      // Coalesce only while a live attempt still exists. A mobile browser can
      // deliver focus -> hidden -> visible in the same task; hidden closes the
      // first attempt and clears its retry. Suppressing the final visible
      // event in that state leaves the stream permanently dead.
      const hasLiveAttempt = connecting
        || es?.readyState === EventSource.OPEN
        || es?.readyState === EventSource.CONNECTING;
      if (now - lastResumeAt < 750 && hasLiveAttempt) return;
      lastResumeAt = now;
      // A deliberate resume (focus/visibility/online) should reconnect promptly,
      // not at a backed-off delay from earlier failures.
      retryDelay = MIN_RETRY_MS;
      const needsReconciliation = streamAttempted && !lastEventId;
      closeStream();
      if (needsReconciliation) {
        reconcile("missing_initial_watermark");
      }
      void connect();
    }

    function handleVisibilityChange() {
      if (documentIsHidden()) {
        wasBackgrounded = true;
        closeStream();
        return;
      }
      if (wasBackgrounded) {
        wasBackgrounded = false;
        // Replay is cursor-based and may be unavailable after a long mobile
        // suspension. Reconcile a current snapshot even with a valid cursor.
        reconcile("foreground_restore");
      }
      resume();
    }

    function handlePageShow(event: PageTransitionEvent) {
      if (!event.persisted) return;
      // A bfcache restore can preserve a stale React tree even when the SSE
      // cursor reconnects successfully. Force every data consumer to fetch a
      // current snapshot before relying on replayed events.
      reconcile("bfcache_restore");
      resume();
    }

    function handleFreeze() {
      // Chromium's Page Lifecycle API can freeze a backgrounded mobile tab
      // without delivering the visibility/blur pair first. Stop the socket so
      // resume always creates a fresh stream instead of retaining a dead one.
      wasBackgrounded = true;
      closeStream();
    }

    function handleLifecycleResume() {
      // A resumed frozen document can retain a perfectly valid SSE watermark,
      // but replay alone cannot prove that every cached conversation snapshot
      // is current. Reconcile once even when visibilitychange was omitted.
      wasBackgrounded = false;
      reconcile("lifecycle_resume");
      resume();
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
    startLivenessWatchdog();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    document.addEventListener("freeze", handleFreeze);
    document.addEventListener("resume", handleLifecycleResume);
    window.addEventListener("pageshow", handlePageShow);
    window.addEventListener("online", resume);
    window.addEventListener("blur", handleBlur);
    window.addEventListener("focus", handleFocus);

    return () => {
      stopped = true;
      closeStream();
      clearLivenessTimer();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      document.removeEventListener("freeze", handleFreeze);
      document.removeEventListener("resume", handleLifecycleResume);
      window.removeEventListener("pageshow", handlePageShow);
      window.removeEventListener("online", resume);
      window.removeEventListener("blur", handleBlur);
      window.removeEventListener("focus", handleFocus);
    };
  }, []);
}
