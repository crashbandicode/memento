// @ts-check
import { expect, test } from "@playwright/test";

import { metadataOnlyPrompts } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

test("plain-text markdown export failures preserve the server detail", async ({ page }) => {
  const serverDetail = "Export service is temporarily unavailable.";
  await openConversation(page, metadataOnlyPrompts);
  await page.route("**/api/exports/conversations/**", async (route) => {
    await route.fulfill({
      status: 502,
      contentType: "text/plain",
      body: serverDetail,
    });
  });

  await page.getByRole("button", { name: "Export", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Export thread as Markdown" })).toBeVisible();
  await page.getByRole("button", { name: "Export thread", exact: true }).click();

  await expect(page.getByRole("dialog").getByRole("alert")).toHaveText(serverDetail);
});
