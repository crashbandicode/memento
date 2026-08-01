// @ts-check
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { subagentLifecycleMatrix } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

const artifactDir = path.resolve(
  process.cwd(),
  "..",
  "artifacts",
  "subagent-lifecycle",
);
mkdirSync(artifactDir, { recursive: true });

const staticMatrix = {
  ...subagentLifecycleMatrix,
  liveTransitionMeta: null,
  liveTransitionEvent: null,
};

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`authoritative lifecycle matrix fits ${viewport.name}`, async ({ page }) => {
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConversation(page, staticMatrix);
    await page.locator('button[title="Browse 4 subagents"]').click();

    const panel = page.getByRole("dialog", { name: "4 subagents" });
    await expect(panel).toContainText("Authoritative active child");
    await expect(panel).toContainText("Finished child transcript");
    await expect(panel).toContainText("Failed child transcript");
    await expect(panel).toContainText("Missing child source");
    await expect(panel.getByText("Running", { exact: true })).toHaveCount(1);
    await expect(panel.getByText("Completed", { exact: true })).toHaveCount(1);
    await expect(panel.getByText("Failed", { exact: true })).toHaveCount(1);
    await expect(panel.getByText("Disconnected", { exact: true })).toHaveCount(1);
    await expect(panel.locator("[data-assistant-model]")).toHaveCount(4);
    await expect(panel.locator("[data-assistant-reasoning]")).toHaveCount(4);

    const panelOverflow = await panel.evaluate(
      (element) => element.scrollWidth - element.clientWidth,
    );
    const documentOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(panelOverflow).toBeLessThanOrEqual(0);
    expect(documentOverflow).toBeLessThanOrEqual(0);
    await page.screenshot({
      path: path.join(artifactDir, `reconciliation-${viewport.name}.png`),
      fullPage: true,
    });
    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });
}

test("child terminal SSE refreshes the open parent without reload", async ({ page }) => {
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await openConversation(page, subagentLifecycleMatrix);
  await page.locator('button[title="Browse 4 subagents"]').click();

  const liveCard = page.locator('[data-subagent-key="toolu-live"]');
  await expect(liveCard).toContainText("Running");
  await expect(liveCard).toContainText("Completed", { timeout: 10_000 });
  await expect(liveCard).not.toContainText("Running");
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});
