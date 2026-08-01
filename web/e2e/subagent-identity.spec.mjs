// @ts-check
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import {
  dreamlandParallelSubagents,
  parentAgentLabeling,
} from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

const artifactDir = path.resolve(process.cwd(), "..", "artifacts", "subagent-lifecycle");
mkdirSync(artifactDir, { recursive: true });

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`parallel subagent cards retain identity and runtime badges (${viewport.name})`, async ({ page }) => {
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConversation(page, dreamlandParallelSubagents);

    const cards = page.locator(
      '[data-agent-event][data-agent-kind="started"][data-agent-activity-type="subagent"]',
    );
    await expect(cards).toHaveCount(2);
    await expect(cards.filter({ hasText: "#131 attribute-grouped bjobs capture" })).toHaveCount(1);
    await expect(cards.filter({ hasText: "Tune stale threshold to CLEAN_PERIOD" })).toHaveCount(1);

    for (const card of [cards.nth(0), cards.nth(1)]) {
      await expect(card.locator('[data-assistant-model="claude-opus-4-8"]')).toBeVisible();
      await expect(card.locator('[data-assistant-reasoning="xhigh"]')).toBeVisible();
      const overflow = await card.evaluate((element) => element.scrollWidth - element.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    }

    const documentOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(documentOverflow).toBeLessThanOrEqual(0);
    await page.screenshot({
      path: path.join(artifactDir, `fixture-${viewport.name}.png`),
      fullPage: true,
    });

    const trigger = page.locator('button[title="Browse 2 subagents"]');
    await expect(trigger).toBeVisible();
    await trigger.click();
    const panel = page.getByRole("dialog", { name: "2 subagents" });
    await expect(panel).toContainText("#131 attribute-grouped bjobs capture");
    await expect(panel).toContainText("Tune stale threshold to CLEAN_PERIOD");
    await expect(panel.locator('[data-assistant-model="claude-opus-4-8"]')).toHaveCount(2);
    await expect(panel.locator('[data-assistant-reasoning="xhigh"]')).toHaveCount(2);

    await openConversation(page, parentAgentLabeling);
    await expect(page.getByText("Parent agent", { exact: true })).toBeVisible();
    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });
}
