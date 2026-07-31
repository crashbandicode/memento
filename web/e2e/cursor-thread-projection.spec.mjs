// @ts-check
import { expect, test } from "@playwright/test";
import path from "node:path";

import { cursorThreadProjection } from "./fixtures/conversation-scenarios.mjs";
import { openConversation, transcript } from "./support/conversation-page.mjs";

const artifactDir = path.resolve(
  process.cwd(),
  "..",
  "artifacts",
  "cursor-thread-32034817",
);

for (const viewport of [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "mobile", width: 472, height: 1024 },
]) {
  test(`Cursor task and child projection stays untangled (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => pageErrors.push(String(error)));

    await openConversation(page, cursorThreadProjection);
    const viewer = transcript(page);

    await expect(page.getByText("Active task list")).toHaveCount(1);
    await expect(page.getByText("Task update")).toHaveCount(0);
    await expect(viewer.locator('[data-message-id="1"]')).toHaveCount(0);
    await expect(viewer.locator('[data-message-id="2"]')).toHaveAttribute(
      "data-prompt-line",
      "2",
    );
    await expect(viewer.locator("[data-message-id]")).toHaveCount(6);

    const renderedLines = await viewer.locator("[data-message-id]").evaluateAll(
      (messages) => messages.map((message) =>
        Number(message.id.replace("conversation-line-", ""))),
    );
    expect(renderedLines).toEqual([2, 3, 4, 5, 6, 7]);

    await expect(page.locator("[data-prompt-item]")).toHaveCount(1);
    await expect(page.locator('[data-prompt-item="2"]')).toHaveCount(1);
    await expect(viewer.locator('[data-message-id="6"]')).toContainText(
      "Audit UI projection",
    );
    await expect(viewer.locator('[data-message-id="7"]')).toContainText(
      "Audit source ordering",
    );

    await page.screenshot({
      path: path.join(artifactDir, `after-${viewport.name}-thread.png`),
      fullPage: true,
    });

    await page.getByRole("button", { name: /2 subagents/i }).click();
    const subagentCards = page.locator("[data-subagent-key]");
    await expect(subagentCards).toHaveCount(2);
    await expect(subagentCards.nth(0)).toContainText("Audit source ordering");
    await expect(subagentCards.nth(1)).toContainText("Audit UI projection");
    await expect(subagentCards.nth(0)).toContainText("Completed");
    await expect(subagentCards.nth(1)).toContainText("Completed");
    await page.screenshot({
      path: path.join(artifactDir, `after-${viewport.name}-subagents.png`),
      fullPage: true,
    });

    // The hermetic harness deliberately aborts its inert SSE route to exercise
    // reconnect behavior; Chromium reports that expected abort as ERR_FAILED.
    expect(
      consoleErrors.filter((message) =>
        message !== "Failed to load resource: net::ERR_FAILED"),
    ).toEqual([]);
    expect(pageErrors).toEqual([]);
  });
}
