// @ts-check
import { expect, test } from "@playwright/test";
import { urlNavigationLargeThread } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

/**
 * Install a controllable EventSource before the application evaluates.  The
 * browser's native EventSource deliberately hides several mobile failure
 * modes, so the fake lets these regressions exercise the real hook without
 * relying on a socket or wall-clock timing.
 * @param {import('@playwright/test').Page} page
 */
async function installMockEventSource(page) {
  await page.addInitScript(() => {
    /** @type {MockEventSource[]} */
    const streams = [];

    class MockEventSource extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;

      /** @type {number} */
      readyState = MockEventSource.CONNECTING;
      /** @type {((event: Event) => void) | null} */
      onopen = null;
      /** @type {((event: Event) => void) | null} */
      onerror = null;

      constructor(_url, options) {
        super();
        this.withCredentials = options?.withCredentials ?? false;
        streams.push(this);
        queueMicrotask(() => {
          if (this.readyState === MockEventSource.CLOSED) return;
          this.readyState = MockEventSource.OPEN;
          this.onopen?.(new Event("open"));
        });
      }

      close() {
        this.readyState = MockEventSource.CLOSED;
      }

      emit(type, payload, lastEventId = "") {
        if (this.readyState === MockEventSource.CLOSED) return;
        this.dispatchEvent(new MessageEvent(type, {
          data: JSON.stringify(payload),
          lastEventId,
        }));
      }

      fail() {
        if (this.readyState === MockEventSource.CLOSED) return;
        this.onerror?.(new Event("error"));
      }
    }

    Object.defineProperty(window, "EventSource", {
      configurable: true,
      value: MockEventSource,
    });
    Object.defineProperty(window, "__mementoMockSse", {
      configurable: true,
      value: {
        streams,
        emit(type, payload, lastEventId = "") {
          streams.at(-1)?.emit(type, payload, lastEventId);
        },
        fail() {
          streams.at(-1)?.fail();
        },
      },
    });
  });
}

async function emitMessageSync(page, scenario, id) {
  await page.evaluate(({ eventId, documentId }) => {
    window.__mementoMockSse.emit("file_synced", {
      type: "file_synced",
      data: {
        document_id: documentId,
        changes: ["conversation.messages"],
      },
      timestamp: Date.now() / 1_000,
    }, eventId);
  }, { eventId: id, documentId: scenario.docId });
}

async function waitForStream(page, count) {
  await page.waitForFunction((expectedCount) => (
    window.__mementoMockSse.streams.length >= expectedCount
  ), count);
}

test.describe("live conversation resilience", () => {
  test("background message syncs never toggle the reader loading indicator", async ({ page }) => {
    await installMockEventSource(page);
    await openConversation(page, urlNavigationLargeThread);
    await waitForStream(page, 1);
    const viewer = page.locator("[data-conversation-viewer]");
    await expect(viewer).toHaveAttribute("data-conversation-loading", "false");

    await page.evaluate(() => {
      const viewer = document.querySelector("[data-conversation-viewer]");
      const isLoading = () => viewer?.getAttribute("data-conversation-loading") === "true";
      const transitions = [];
      let previous = isLoading();
      const observer = new MutationObserver(() => {
        const next = isLoading();
        if (next !== previous) transitions.push(next);
        previous = next;
      });
      observer.observe(viewer, { childList: true, subtree: true, characterData: true });
      window.__mementoLoadingTransitions = { observer, transitions, isLoading };
    });

    for (let index = 1; index <= 4; index += 1) {
      await emitMessageSync(page, urlNavigationLargeThread, `sync-${index}`);
      await page.waitForTimeout(325);
    }

    const transitions = await page.evaluate(() => {
      const monitor = window.__mementoLoadingTransitions;
      monitor.observer.disconnect();
      return { transitions: monitor.transitions, visible: monitor.isLoading() };
    });
    expect(transitions.visible).toBe(false);
    expect(transitions.transitions).toEqual([]);
  });

  test("quick mobile hide/visible cycles reconnect instead of being swallowed by resume coalescing", async ({ page }) => {
    await installMockEventSource(page);
    let tailRequests = 0;
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        url.pathname === `/api/conversations/${urlNavigationLargeThread.docId}/messages`
        && url.searchParams.get("tail") === "true"
      ) {
        tailRequests += 1;
      }
    });
    await openConversation(page, urlNavigationLargeThread);
    await waitForStream(page, 1);
    const initialStreamCount = await page.evaluate(() => window.__mementoMockSse.streams.length);

    let sessionRequests = 0;
    /** @type {(() => void) | undefined} */
    let releaseFirstResumeSession;
    await page.route("**/api/events/session", async (route) => {
      sessionRequests += 1;
      if (sessionRequests === 1) {
        await new Promise((resolve) => {
          releaseFirstResumeSession = resolve;
        });
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });

    // A focus-triggered resume followed immediately by a mobile hide/visible
    // cycle used to clear the in-flight attempt, then suppress the only retry
    // under the 750ms resume debounce.
    await page.evaluate(() => {
      window.dispatchEvent(new Event("blur"));
      window.dispatchEvent(new Event("focus"));
      let visibility = "hidden";
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => visibility,
      });
      document.dispatchEvent(new Event("visibilitychange"));
      visibility = "visible";
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => sessionRequests).toBeGreaterThanOrEqual(1);
    releaseFirstResumeSession?.();
    await waitForStream(page, initialStreamCount + 1);

    const beforeEvent = tailRequests;
    await emitMessageSync(page, urlNavigationLargeThread, "after-mobile-resume");
    await expect.poll(() => tailRequests).toBeGreaterThan(beforeEvent);
  });

  test("a bfcache restore forces a full conversation refresh before resuming SSE", async ({ page }) => {
    await installMockEventSource(page);
    let metadataRequests = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === `/api/conversations/${urlNavigationLargeThread.docId}`) {
        metadataRequests += 1;
      }
    });
    await openConversation(page, urlNavigationLargeThread);
    await waitForStream(page, 1);
    await emitMessageSync(page, urlNavigationLargeThread, "watermark-before-bfcache");
    await page.waitForTimeout(325);
    const beforeRestore = metadataRequests;

    await page.evaluate(() => {
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    });

    await expect.poll(() => metadataRequests).toBeGreaterThan(beforeRestore);
    await waitForStream(page, 2);
  });

  test("mobile foreground reconciliation refreshes an open thread despite a valid SSE watermark", async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      userAgent: "Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 Chrome/138 Mobile Safari/537.36",
    });
    const page = await context.newPage();
    const scenario = structuredClone(urlNavigationLargeThread);
    const resumedMessage = "MOBILE_FOREGROUND_RECONCILIATION_MESSAGE";
    let tailRequests = 0;
    let metadataRequests = 0;
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        url.pathname === `/api/conversations/${scenario.docId}/messages`
        && url.searchParams.get("tail") === "true"
      ) tailRequests += 1;
      if (url.pathname === `/api/conversations/${scenario.docId}`) metadataRequests += 1;
    });

    await installMockEventSource(page);
    await openConversation(page, scenario);
    await waitForStream(page, 1);
    const initialStreamCount = await page.evaluate(() => window.__mementoMockSse.streams.length);
    await page.evaluate(() => {
      window.__mementoMockSse.emit("stream_ready", { type: "stream_ready" }, "watermark-before-mobile-background");
      let visibility = "visible";
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => visibility,
      });
      window.__mementoSetVisibility = (next) => {
        visibility = next;
        document.dispatchEvent(new Event("visibilitychange"));
      };
    });
    await expect(page.getByText(resumedMessage, { exact: true })).toHaveCount(0);

    await page.evaluate(() => {
      window.dispatchEvent(new Event("blur"));
      window.__mementoSetVisibility("hidden");
    });
    const nextLine = scenario.messages.length + 1;
    scenario.messages.push({
      id: 99_999,
      line_number: nextLine,
      role: "assistant",
      model: "cursor-fixture-model",
      content: resumedMessage,
      timestamp: new Date().toISOString(),
    });
    scenario.meta.message_count = scenario.messages.length;
    const beforeReconciliation = { tailRequests, metadataRequests };

    await page.evaluate(() => {
      window.__mementoSetVisibility("visible");
      window.dispatchEvent(new Event("focus"));
    });
    await waitForStream(page, initialStreamCount + 1);
    await expect(page.getByText(resumedMessage, { exact: true })).toBeVisible();
    await expect(page.getByText(`${scenario.meta.message_count} messages`, { exact: true })).toBeVisible();
    await expect.poll(() => tailRequests).toBe(beforeReconciliation.tailRequests + 1);
    await expect.poll(() => metadataRequests).toBe(beforeReconciliation.metadataRequests + 1);
    await page.waitForTimeout(1_000);
    expect(tailRequests).toBe(beforeReconciliation.tailRequests + 1);
    expect(metadataRequests).toBe(beforeReconciliation.metadataRequests + 1);
    await context.close();
  });

  test("a rejected stream is re-sessioned and resumes event delivery", async ({ page }) => {
    await installMockEventSource(page);
    let sessionRequests = 0;
    let tailRequests = 0;
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/events/session") {
        sessionRequests += 1;
      }
      if (
        url.pathname === `/api/conversations/${urlNavigationLargeThread.docId}/messages`
        && url.searchParams.get("tail") === "true"
      ) tailRequests += 1;
    });
    await openConversation(page, urlNavigationLargeThread);
    await waitForStream(page, 1);
    const firstSession = sessionRequests;

    await page.evaluate(() => window.__mementoMockSse.fail());
    await page.waitForTimeout(5_250);
    await expect.poll(() => sessionRequests).toBeGreaterThan(firstSession);
    await waitForStream(page, 2);
    expect(await page.evaluate(() => window.__mementoMockSse.streams.at(-1)?.withCredentials)).toBe(true);
    const beforeEvent = tailRequests;
    await emitMessageSync(page, urlNavigationLargeThread, "after-resession");
    await expect.poll(() => tailRequests).toBeGreaterThan(beforeEvent);
  });
});
