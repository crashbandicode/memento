// Ad-hoc visual check for the managed-session start dialog in dark mode.
// Confirms the approval-policy / sandbox selects render legible (solid
// surface, not transparent) with an option selected. Screenshot only.
import { test, expect } from "@playwright/test";
import { metadataOnlyPrompts } from "./fixtures/conversation-scenarios.mjs";
import { installConversationMocks, seedAuth } from "./support/conversation-page.mjs";

test("start dialog selects are legible in dark mode", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await seedAuth(page);
  await installConversationMocks(page, metadataOnlyPrompts);
  await page.route("**/api/devices", async (route) => {
    await route.fulfill({
      status: 200,
      json: [
        {
          id: "99999999-8888-4777-8666-555555555555",
          name: "butterbridge (Windows)",
          device_id: "device-fixture",
          collector_version: "0.0.46",
          last_heartbeat: new Date().toISOString(),
          created_at: "2026-08-01T00:00:00Z",
          document_count: 12,
          tools: ["codex", "claude_code"],
          managed_agents: ["codex"],
        },
      ],
    });
  });
  await page.route("**/api/devices/*/discovery", async (route) => {
    await route.fulfill({
      status: 200,
      json: { device_id: "x", tools: { codex: { root: "C:/Users/intpa/.codex" } } },
    });
  });
  await page.route("**/api/control/sessions", async (route) => {
    await route.fulfill({ status: 200, json: [] });
  });

  await page.goto("/devices");
  await page.locator("[data-start-codex-session]").click();
  await expect(page.locator("[data-start-control-session]")).toBeVisible();
  await page.locator("[data-start-session-approval-policy]").selectOption("untrusted");
  await page.locator("[data-start-session-sandbox]").selectOption("workspace-write");

  const select = page.locator("[data-start-session-approval-policy]");
  const bg = await select.evaluate((el) => getComputedStyle(el).backgroundColor);
  // Must not be transparent — that was the dark-mode invisibility bug.
  expect(bg).not.toBe("rgba(0, 0, 0, 0)");
  expect(bg).not.toBe("transparent");

  await page.locator("[data-start-control-session]").screenshot({
    path: "test-results/start-dialog-dark.png",
  });
});
