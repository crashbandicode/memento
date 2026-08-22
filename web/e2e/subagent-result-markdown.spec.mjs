// @ts-check
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { subagentMarkdownSummary } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

const artifactDir = path.resolve(process.cwd(), "..", "artifacts", "subagent-result-markdown");
mkdirSync(artifactDir, { recursive: true });

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`completed subagent result renders Markdown and collapses on ${viewport.name}`, async ({ page }) => {
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConversation(page, subagentMarkdownSummary);

    const card = page.locator('[data-agent-event][data-agent-kind="completed"]');
    const summary = card.locator("[data-agent-result-summary]");
    const toggle = card.locator("[data-agent-summary-toggle] button");
    await expect(card).toContainText("API/SLO Mongo feature inventory");
    await expect(summary.locator("h1")).toHaveText("MongoDB feature inventory");
    await expect(summary.locator("strong")).toContainText("read path");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(summary).not.toContainText("Keep the compatibility probe focused");

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(summary.locator("table")).toBeVisible();
    await expect(summary).toContainText("Keep the compatibility probe focused");
    await expect(summary).not.toContainText("**read path**");

    const overflows = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      card: (() => {
        const element = document.querySelector("[data-agent-result-summary]");
        return element ? element.scrollWidth - element.clientWidth : 0;
      })(),
    }));
    expect(overflows.document).toBeLessThanOrEqual(0);
    expect(overflows.card).toBeLessThanOrEqual(0);
    await page.screenshot({
      path: path.join(artifactDir, `${viewport.name}.png`),
      fullPage: true,
    });

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });
}
