// @ts-check
import { expect, test } from "@playwright/test";
import {
  BUTTERBRIDGE_DEVICE_ID,
  mobileToolBrowse,
} from "./fixtures/tool-browse-scenario.mjs";
import {
  installConversationMocks,
  seedAuth,
} from "./support/conversation-page.mjs";

test.describe("mobile dashboard to tool browsing", () => {
  test.use({ viewport: { width: 472, height: 1024 } });

  test("legacy phone device scope resets so Claude shows every dashboard file", async ({ page }) => {
    const scopedRequests = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/dashboard" || url.pathname.startsWith("/api/tools/claude_code")) {
        scopedRequests.push(url);
      }
    });

    await seedAuth(page);
    await page.addInitScript((legacyDeviceId) => {
      // Reproduces the phone-only bug: v0.1.51 restored this browser-specific
      // legacy key, while the dashboard still displayed all-device counts.
      window.localStorage.setItem("dr_device_id", legacyDeviceId);
      window.localStorage.removeItem("dr_device_scope_v2");
    }, BUTTERBRIDGE_DEVICE_ID);
    await installConversationMocks(page, mobileToolBrowse);

    await page.goto("/app");
    await expect(page.getByRole("combobox")).toHaveValue("all");

    const claudeCard = page.locator('a[href="/tools/claude_code"]');
    await expect(claudeCard).toContainText("Claude Code");
    await expect(claudeCard.getByText("3", { exact: true })).toBeVisible();
    await claudeCard.click();

    await expect(page).toHaveURL(/\/tools\/claude_code$/);
    await expect(page.getByText("3 files", { exact: false }).first()).toBeVisible();
    await expect(page.getByText("Files (3)", { exact: true })).toBeVisible();
    await expect(page.locator('main a[href^="/conversations/"]')).toHaveCount(3);
    expect(scopedRequests.length).toBeGreaterThanOrEqual(3);
    expect(scopedRequests.every((url) => !url.searchParams.has("device_id"))).toBe(true);
  });

  test("an intentional device scope filters dashboard and tool page together", async ({ page }) => {
    const scopedRequests = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/dashboard" || url.pathname.startsWith("/api/tools/claude_code")) {
        scopedRequests.push(url);
      }
    });

    await seedAuth(page);
    await page.addInitScript((deviceId) => {
      window.localStorage.removeItem("dr_device_id");
      window.localStorage.setItem("dr_device_scope_v2", deviceId);
    }, BUTTERBRIDGE_DEVICE_ID);
    await installConversationMocks(page, mobileToolBrowse);

    await page.goto("/app");
    await expect(page.getByRole("combobox")).toHaveValue(BUTTERBRIDGE_DEVICE_ID);

    const claudeCard = page.locator('a[href="/tools/claude_code"]');
    await expect(claudeCard.getByText("1", { exact: true })).toBeVisible();
    await claudeCard.click();

    await expect(page).toHaveURL(/\/tools\/claude_code$/);
    await expect(page.getByText("Files (1)", { exact: true })).toBeVisible();
    await expect(page.locator('main a[href^="/documents/"]')).toHaveCount(1);
    expect(scopedRequests.length).toBeGreaterThanOrEqual(3);
    expect(
      scopedRequests.every(
        (url) => url.searchParams.get("device_id") === BUTTERBRIDGE_DEVICE_ID,
      ),
    ).toBe(true);
  });
});
