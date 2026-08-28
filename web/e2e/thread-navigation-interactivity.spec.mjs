// @ts-check
import { expect, test } from "@playwright/test";

import { pinnedMessageThread } from "./fixtures/conversation-scenarios.mjs";
import { installConversationMocks, seedAuth } from "./support/conversation-page.mjs";

const scenario = {
  ...pinnedMessageThread,
  docId: "12121212-1212-4121-8121-121212121212",
  meta: {
    ...pinnedMessageThread.meta,
    id: "12121212-1212-4121-8121-121212121212",
    title: "First client-side navigation regression",
    canonical_url: "/conversations/codex/12121212-1212-4121-8121-121212121212",
  },
};

const dashboard = {
  tools: [],
  recent_conversations: [{
    id: scenario.docId,
    tool_id: scenario.meta.tool_id,
    title: scenario.meta.title,
    activity_at: "2026-08-28T12:00:00Z",
    synced_at: "2026-08-28T12:00:00Z",
    project_title: null,
    message_count: scenario.meta.message_count,
    is_low_activity: false,
  }],
  daily: [],
  tool_daily: {},
  devices: [],
  stats: {
    total_documents: 1,
    total_projects: 0,
    total_tools: 1,
    total_devices: 0,
    today_total: 1,
    today_conversations: 1,
  },
};

/** @param {import('@playwright/test').Page} page */
function collectPageErrors(page) {
  /** @type {string[]} */
  const consoleErrors = [];
  /** @type {string[]} */
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.stack || String(error)));
  return { consoleErrors, pageErrors };
}

function expectNoPageErrors(errors) {
  expect(
    errors.consoleErrors.filter((message) =>
      message !== "Failed to load resource: net::ERR_FAILED",
    ),
    `console errors: ${JSON.stringify(errors.consoleErrors)}`,
  ).toEqual([]);
  expect(errors.pageErrors, `page errors: ${JSON.stringify(errors.pageErrors)}`).toEqual([]);
}

/** @param {import('@playwright/test').Page} page */
async function openDashboardWithConversation(page) {
  await seedAuth(page);
  await installConversationMocks(page, scenario);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    if (request.method() === "GET" && new URL(request.url()).pathname === "/api/dashboard") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboard),
      });
      return;
    }
    await route.fallback();
  });

  await page.goto("/app");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

/** @param {import('@playwright/test').Page} page */
async function openConversationFromDashboard(page) {
  await page.getByRole("link", { name: scenario.meta.title }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe(scenario.meta.canonical_url);
  await expect(page.locator("[data-conversation-viewer]")).toBeVisible();
  await page.waitForTimeout(500);
}

test("first dashboard-to-conversation navigation remains interactive", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openDashboardWithConversation(page);
  await openConversationFromDashboard(page);

  expectNoPageErrors(errors);
  await page.getByRole("button", { name: "Export" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("Dashboard link navigates after the first dashboard-to-conversation click", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openDashboardWithConversation(page);
  await openConversationFromDashboard(page);

  await page.getByRole("link", { name: "Dashboard" }).click();
  await expect(page).toHaveURL(/\/app$/);
  await page.waitForTimeout(500);
  expectNoPageErrors(errors);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
});
