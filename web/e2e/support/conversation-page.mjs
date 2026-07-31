// @ts-check
/**
 * Playwright support helpers that wire a real (dev-served) Memento page to the
 * deterministic fixtures. No live backend is ever contacted: every `/api/**`
 * request is intercepted and answered from `resolveConversationRoute`.
 *
 * These helpers take the Playwright `page` as an argument, so this module has
 * no direct `@playwright/test` import and stays trivially reusable.
 */

import { FIXTURE_TOKEN } from "../fixtures/conversation-scenarios.mjs";
import { resolveConversationRoute } from "../fixtures/mock-router.mjs";

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
  "access-control-allow-headers": "authorization,content-type",
};

/**
 * Seed the JWT + locale into storage before any app script runs, so the auth
 * provider boots straight into the authenticated shell (no login redirect).
 * @param {import('@playwright/test').Page} page
 */
export async function seedAuth(page) {
  await page.addInitScript((token) => {
    try {
      window.localStorage.setItem("dr_token", token);
      window.localStorage.setItem("dr_remember_me", "1");
      window.localStorage.setItem("dr_locale", "en-US");
    } catch {
      /* storage disabled — nothing to seed */
    }
  }, FIXTURE_TOKEN);
}

/**
 * Intercept every API request and answer it from the scenario fixtures.
 * @param {import('@playwright/test').Page} page
 * @param {any} scenario
 */
export async function installConversationMocks(page, scenario) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();

    // Answer CORS preflights locally (harmless even when same-origin).
    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS, body: "" });
      return;
    }

    const result = resolveConversationRoute({
      url: request.url(),
      method,
      scenario,
    });

    if (result.action === "abort") {
      if (new URL(request.url()).pathname.endsWith("/api/events/stream")) {
        // A 204 is the EventSource-defined clean stop signal. It keeps the
        // fixture stream inert without generating a browser console error.
        await route.fulfill({ status: 204, body: "" });
        return;
      }
      await route.abort();
      return;
    }

    await route.fulfill({
      status: result.status,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify(result.json),
    });
  });
}

/**
 * Seed auth, install mocks, open the conversation page and wait for the viewer.
 * @param {import('@playwright/test').Page} page
 * @param {any} scenario
 */
export async function openConversation(page, scenario) {
  await seedAuth(page);
  await installConversationMocks(page, scenario);
  await page.goto(`/conversations/${scenario.docId}`);
  await page.waitForSelector("[data-conversation-viewer]", { timeout: 15000 });
  // The initial message page has finished loading once the viewer reports it.
  await page.waitForFunction(() => {
    const el = document.querySelector("[data-conversation-viewer]");
    return !!el && Number(el.getAttribute("data-loaded-messages")) >= 0;
  }, { timeout: 15000 });
}

/** Convenience: the transcript scroll container (excludes the attention banner). */
export function transcript(page) {
  return page.locator("[data-conversation-viewer]");
}
