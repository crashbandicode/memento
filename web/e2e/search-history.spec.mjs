// @ts-check
import { expect, test } from "@playwright/test";
import {
  FIXTURE_TOKEN,
  FIXTURE_USER,
  metadataOnlyPrompts,
} from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

async function openGlobalSearch(page) {
  await page.addInitScript(({ token }) => {
    window.localStorage.setItem("dr_token", token);
    window.localStorage.setItem("dr_remember_me", "1");
    window.localStorage.setItem("dr_locale", "en-US");
  }, { token: FIXTURE_TOKEN });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (url.pathname.endsWith("/api/events/stream")) {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (url.pathname.endsWith("/api/events/session")) {
      await route.fulfill({ status: 200, json: { ok: true } });
      return;
    }
    if (url.pathname.endsWith("/api/auth/me")) {
      await route.fulfill({ status: 200, json: FIXTURE_USER });
      return;
    }
    if (/\/api\/(?:hierarchy\/)?devices\/?$/.test(url.pathname)) {
      await route.fulfill({ status: 200, json: [] });
      return;
    }
    if (url.pathname.endsWith("/api/search/messages")) {
      await route.fulfill({
        status: 200,
        json: {
          query: url.searchParams.get("q") || "",
          corrected_query: null,
          results: [],
          next_cursor: null,
          has_more: false,
        },
      });
      return;
    }
    await route.fulfill({ status: 200, json: {} });
  });
  await page.goto("/search");
  await page.getByRole("heading", { name: "Search" }).waitFor();
}

for (const viewport of viewports) {
  test(`global search remembers only submitted queries (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openGlobalSearch(page);

    const input = page.getByPlaceholder("Search every user and assistant message...");
    await input.pressSequentially("Model usage", { delay: 5 });
    await expect(page.locator("[data-global-search-history]")).toHaveCount(0);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await input.fill("");

    const history = page.locator("[data-global-search-history]");
    await expect(history).toBeVisible();
    await expect(history.getByRole("button")).toHaveCount(1);
    await expect(history.getByRole("button")).toHaveText("Model usage");
  });

  test(`prompt search repairs prefix history and commits the finished query (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => {
      window.localStorage.setItem(
        "memento.promptNavigatorSearchHistory",
        JSON.stringify(["Model", "Mode", "Mod", "Mo", "M", "Why", "Wh", "W"]),
      );
    });
    await openConversation(page, metadataOnlyPrompts);

    if (viewport.name === "mobile") {
      await page.locator("[data-mobile-prompt-trigger]").click();
    } else {
      await page.locator("[data-prompt-navigator]").hover();
    }
    const input = page.locator(
      viewport.name === "mobile"
        ? "[data-mobile-prompt-search]"
        : "[data-desktop-prompt-search]",
    );
    const historySelector = viewport.name === "mobile"
      ? "[data-mobile-prompt-search-history]"
      : "[data-desktop-prompt-search-history]";
    const history = page.locator(historySelector);

    await expect(history.getByRole("button")).toHaveCount(2);
    await expect(history.getByRole("button").nth(0)).toHaveText("Model");
    await expect(history.getByRole("button").nth(1)).toHaveText("Why");

    await input.pressSequentially("retry backoff", { delay: 5 });
    await expect(history).toHaveCount(0);
    await input.press("Enter");
    await input.fill("");

    await expect(history.getByRole("button")).toHaveCount(3);
    await expect(history.getByRole("button").nth(0)).toHaveText("retry backoff");
    await expect(history.getByRole("button", { name: "retry backof", exact: true })).toHaveCount(0);
  });
}
