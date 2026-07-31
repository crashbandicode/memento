// @ts-check
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { claudeSideTailLivePrompt } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

function collectPageErrors(page) {
  /** @type {string[]} */
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => errors.push(`page: ${error.message}`));
  return errors;
}

async function capturePhaseScreenshot(page, viewport) {
  const phase = process.env.MEMENTO_PROMPT_SCREENSHOT_PHASE;
  if (!phase) return;
  const directory = resolve(
    process.cwd(),
    "..",
    "artifacts",
    "claude-prompt-mobile-glitches",
  );
  mkdirSync(directory, { recursive: true });
  await page.screenshot({
    path: resolve(directory, `${phase}-${viewport}.png`),
    fullPage: true,
  });
}

async function assertPromptText(page) {
  const card = page.locator(
    '[data-pending-interactions] [data-question-interaction="toolu-side-tail"]',
  );
  await expect(card).toBeVisible();
  await expect(card.getByText(
    "Proceed to eliminate the accept/switch side-tail and source forwarding from JOB_START?",
    { exact: true },
  )).toBeVisible();
  await expect(card.getByText(
    "Yes — delete it, verify on real data first",
    { exact: true },
  )).toBeVisible();
  await expect(card.getByText("Yes — delete it now", { exact: true })).toBeVisible();
  await expect(card.getByText(
    "Not yet — keep side-tail",
    { exact: true },
  )).toBeVisible();
  await expect(card.getByText(
    "JOB_SWITCH queue freshness once the side-tail is gone?",
    { exact: true },
  )).toBeVisible();
  await expect(page.locator("body")).not.toContainText("â€”");
  await expect(page.getByText("Line 0", { exact: true })).toHaveCount(0);
  return card;
}

test.describe("Claude live prompt mobile regressions", () => {
  test("desktop renders the recovered two-question prompt and navigator", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openConversation(page, claudeSideTailLivePrompt);
    await capturePhaseScreenshot(page, "desktop");

    await assertPromptText(page);

    const navigator = page.locator("[data-prompt-navigator]");
    await navigator.hover();
    const secondPrompt = navigator.locator('[data-prompt-item="2"]');
    await expect(secondPrompt).toBeVisible();
    await secondPrompt.click();
    await expect(secondPrompt).toHaveAttribute("aria-current", "true");
    expect(errors).toEqual([]);
  });

  test("persisted positive line locations remain navigable", async ({ page }) => {
    const errors = collectPageErrors(page);
    const persistedScenario = {
      ...claudeSideTailLivePrompt,
      pending: {
        ...claudeSideTailLivePrompt.pending,
        interactions: claudeSideTailLivePrompt.pending.interactions.map((item) => ({
          ...item,
          message_id: 2,
          line_number: 2,
        })),
      },
    };
    await openConversation(page, persistedScenario);

    const lineLink = page.getByRole("button", { name: "Line 2", exact: true });
    await expect(lineLink).toBeVisible();
    await lineLink.click();
    await expect(page.locator("#conversation-line-2")).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("mobile keeps question two clear of the prompt navigator", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.setViewportSize({ width: 472, height: 1024 });
    await openConversation(page, claudeSideTailLivePrompt);
    await capturePhaseScreenshot(page, "mobile");

    const card = await assertPromptText(page);
    const secondQuestion = card.locator('[data-question-index="1"]');
    await expect(secondQuestion).toBeVisible();

    const trigger = page.locator("[data-mobile-prompt-trigger]");
    await expect(trigger).toBeVisible();
    const [questionBox, triggerBox] = await Promise.all([
      secondQuestion.boundingBox(),
      trigger.boundingBox(),
    ]);
    expect(questionBox).not.toBeNull();
    expect(triggerBox).not.toBeNull();
    expect(questionBox.y + questionBox.height).toBeLessThanOrEqual(
      triggerBox.y - 8,
    );

    await trigger.click();
    const sheet = page.locator("[data-mobile-prompt-sheet]");
    await expect(sheet).toBeVisible();
    await sheet.locator('[data-mobile-prompt-item="1"]').click();
    await expect(sheet).toBeHidden();
    await expect(page.locator("#conversation-line-1")).toBeVisible();
    expect(errors).toEqual([]);
  });
});
