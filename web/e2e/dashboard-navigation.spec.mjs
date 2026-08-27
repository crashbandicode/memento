// @ts-check
import { expect, test } from "@playwright/test";

import { urlNavigationLargeThread } from "./fixtures/conversation-scenarios.mjs";
import { installConversationMocks, seedAuth } from "./support/conversation-page.mjs";

const dashboard = {
  tools: [],
  recent_conversations: [],
  daily: [],
  tool_daily: {},
  devices: [],
  stats: {
    total_documents: 0,
    total_projects: 0,
    total_tools: 0,
    total_devices: 0,
    today_total: 0,
    today_conversations: 0,
  },
};
const canonicalConversation = {
  ...urlNavigationLargeThread,
  meta: {
    ...urlNavigationLargeThread.meta,
    canonical_url: "/conversations/codex/018d5532-7d42-7b44-b2da-d4ca8b4f70e1",
  },
};

/** @param {import('@playwright/test').Page} page */
async function installDashboardNavigationMocks(page, scenario = urlNavigationLargeThread) {
  await seedAuth(page);
  await installConversationMocks(page, scenario);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "GET" && pathname === "/api/dashboard") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboard),
      });
      return;
    }
    await route.fallback();
  });
}

/** @param {import('@playwright/test').Page} page */
async function installUrlTransitionLog(page) {
  await page.addInitScript(() => {
    const transitions = [];
    const record = (source) => transitions.push({ source, href: location.href });
    const pushState = history.pushState.bind(history);
    const replaceState = history.replaceState.bind(history);
    history.pushState = function (...args) {
      pushState(...args);
      record("pushState");
    };
    history.replaceState = function (...args) {
      replaceState(...args);
      record("replaceState");
    };
    window.addEventListener("popstate", () => record("popstate"));
    window.addEventListener("hashchange", () => record("hashchange"));
    record("initial");
    Object.defineProperty(window, "__mementoUrlTransitions", {
      configurable: true,
      value: transitions,
    });
  });
}

test("Dashboard navigates on one click from a canonical conversation URL", async ({ page }) => {
  await installUrlTransitionLog(page);
  await installDashboardNavigationMocks(page, canonicalConversation);

  await page.goto(`/conversations/${urlNavigationLargeThread.docId}`);
  await expect(page.locator("[data-conversation-viewer]")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Large URL navigation regression" })).toBeVisible();
  await expect.poll(() => new URL(page.url()).pathname).toBe(canonicalConversation.meta.canonical_url);

  await page.getByRole("link", { name: "Dashboard" }).click({ noWaitAfter: true });
  await expect.poll(() => new URL(page.url()).pathname).toBe("/app");
  await page.waitForTimeout(1_000);
  const navigation = await page.evaluate(() => ({
    pathname: location.pathname,
    dashboardVisible: [...document.querySelectorAll("h1, h2")]
      .some((heading) => heading.textContent?.trim() === "Dashboard"),
    conversationVisible: Boolean(document.querySelector("[data-conversation-viewer]")),
    transitions: window.__mementoUrlTransitions,
  }));
  expect(
    {
      pathname: navigation.pathname,
      dashboardVisible: navigation.dashboardVisible,
      conversationVisible: navigation.conversationVisible,
    },
    `URL transitions: ${JSON.stringify(navigation.transitions)}`,
  ).toEqual({ pathname: "/app", dashboardVisible: true, conversationVisible: false });
});

test("Dashboard navigates on one click from Pins and Daily", async ({ page }) => {
  await installDashboardNavigationMocks(page);

  for (const sourcePath of ["/pins", "/daily"]) {
    await page.goto(sourcePath);
    await page.getByRole("link", { name: "Dashboard" }).click();
    await expect(page).toHaveURL(/\/app$/);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  }
});
