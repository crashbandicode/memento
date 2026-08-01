// @ts-check
import { expect, test } from "@playwright/test";
import path from "node:path";

import { urlNavigationLargeThread } from "./fixtures/conversation-scenarios.mjs";
import { openConversation, transcript } from "./support/conversation-page.mjs";

const artifactDir = path.resolve(
  process.cwd(),
  "..",
  "artifacts",
  "url-navigation",
);

function collectPageErrors(page) {
  /** @type {string[]} */
  const consoleErrors = [];
  /** @type {string[]} */
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  return { consoleErrors, pageErrors };
}

function expectNoPageErrors(errors) {
  expect(
    errors.consoleErrors.filter((message) =>
      message !== "Failed to load resource: net::ERR_FAILED"),
  ).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
}

async function waitForRestoredLine(page, line) {
  const target = page.locator(`#conversation-line-${line}`);
  await expect(target).toBeVisible({ timeout: 15_000 });
  await expect.poll(
    () => new URL(page.url()).searchParams.get("line"),
    { timeout: 15_000 },
  ).toBe(String(line));
  await page.waitForTimeout(950);
  return target;
}

async function anchorRatio(page, line) {
  return page.locator(`#conversation-line-${line}`).evaluate((target) => {
    const viewer = document.querySelector("[data-conversation-viewer]");
    if (!(viewer instanceof HTMLElement) || !(target instanceof HTMLElement)) {
      return null;
    }
    const viewerBounds = viewer.getBoundingClientRect();
    const targetBounds = target.getBoundingClientRect();
    return Math.max(
      0,
      Math.min(1, (viewerBounds.top + 16 - targetBounds.top) / targetBounds.height),
    );
  });
}

for (const viewport of [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`refresh restores Unicode search and long-message position (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const errors = collectPageErrors(page);
    await openConversation(page, urlNavigationLargeThread);

    const input = page.locator("[data-conversation-search-input]");
    await input.fill(urlNavigationLargeThread.searchQuery);
    await input.press("Enter");
    const longHit = page.locator(
      `[data-conversation-search-hit="${urlNavigationLargeThread.longMessageLine}"]`,
    );
    await expect(longHit).toBeVisible();
    await longHit.click();
    const target = await waitForRestoredLine(
      page,
      urlNavigationLargeThread.longMessageLine,
    );

    const historyBeforePassiveScroll = await page.evaluate(() => history.length);
    await transcript(page).evaluate((viewer, line) => {
      const message = viewer.querySelector(`#conversation-line-${line}`);
      if (!(message instanceof HTMLElement)) throw new Error("long target missing");
      viewer.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: 400 }));
      viewer.scrollTop += message.getBoundingClientRect().height * 0.45;
      viewer.dispatchEvent(new Event("scroll"));
    }, urlNavigationLargeThread.longMessageLine);
    await expect.poll(
      () => Number(new URL(page.url()).searchParams.get("pos")),
    ).toBeGreaterThan(250);
    expect(await page.evaluate(() => history.length)).toBe(historyBeforePassiveScroll);

    const savedUrl = page.url();
    const savedPosition = Number(new URL(savedUrl).searchParams.get("pos"));
    const ratioBeforeRefresh = await anchorRatio(
      page,
      urlNavigationLargeThread.longMessageLine,
    );
    expect(ratioBeforeRefresh).not.toBeNull();

    await page.reload();
    await waitForRestoredLine(page, urlNavigationLargeThread.longMessageLine);
    await expect(input).toHaveValue(urlNavigationLargeThread.searchQuery);
    const selected = page.locator(
      `[data-conversation-search-hit="${urlNavigationLargeThread.longMessageLine}"]`,
    );
    await expect(selected).toHaveAttribute("aria-selected", "true");
    expect(Number(new URL(page.url()).searchParams.get("pos"))).toBe(savedPosition);
    const ratioAfterRefresh = await anchorRatio(
      page,
      urlNavigationLargeThread.longMessageLine,
    );
    expect(Math.abs(ratioAfterRefresh - ratioBeforeRefresh)).toBeLessThan(0.08);
    expect(page.url()).toBe(savedUrl);

    const bounds = await target.evaluate((element) => {
      const viewer = document.querySelector("[data-conversation-viewer]");
      const targetBounds = element.getBoundingClientRect();
      const viewerBounds = viewer.getBoundingClientRect();
      return {
        targetTop: targetBounds.top,
        viewerTop: viewerBounds.top,
        viewerBottom: viewerBounds.bottom,
      };
    });
    expect(bounds.targetTop).toBeLessThan(bounds.viewerBottom);

    await page.screenshot({
      path: path.join(artifactDir, `${viewport.name}-search-restored.png`),
      fullPage: true,
    });
    expectNoPageErrors(errors);
  });

  test(`search next/previous participates in Back and Forward (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const errors = collectPageErrors(page);
    await openConversation(page, urlNavigationLargeThread);

    const input = page.locator("[data-conversation-search-input]");
    await input.fill(urlNavigationLargeThread.searchQuery);
    await input.press("Enter");
    await page.locator('[data-conversation-search-ordinal="1"]').click();
    await waitForRestoredLine(page, 13);
    expect(new URL(page.url()).searchParams.get("match")).toBe("1");

    await page.locator("[data-conversation-search-next]").click();
    await waitForRestoredLine(page, 26);
    expect(new URL(page.url()).searchParams.get("match")).toBe("2");

    await page.goBack();
    await waitForRestoredLine(page, 13);
    await expect(input).toHaveValue(urlNavigationLargeThread.searchQuery);
    expect(new URL(page.url()).searchParams.get("match")).toBe("1");

    await page.goForward();
    await waitForRestoredLine(page, 26);
    expect(new URL(page.url()).searchParams.get("match")).toBe("2");

    await page.locator("[data-conversation-search-previous]").click();
    await waitForRestoredLine(page, 13);
    expect(new URL(page.url()).searchParams.get("match")).toBe("1");
    expectNoPageErrors(errors);
  });

  test(`legacy line links load target windows without history spam (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const errors = collectPageErrors(page);
    await openConversation(
      page,
      urlNavigationLargeThread,
      `?line=${urlNavigationLargeThread.deepLinkLine}&unrelated=keep`,
    );
    const target = await waitForRestoredLine(
      page,
      urlNavigationLargeThread.deepLinkLine,
    );
    await expect(transcript(page)).toHaveAttribute("data-has-earlier", "true");
    expect(
      Number(await transcript(page).getAttribute("data-contiguous-start")),
    ).toBeGreaterThan(0);
    expect(new URL(page.url()).searchParams.get("unrelated")).toBe("keep");

    const clearance = await target.evaluate((element) => {
      const viewer = document.querySelector("[data-conversation-viewer]");
      return element.getBoundingClientRect().top - viewer.getBoundingClientRect().top;
    });
    expect(clearance).toBeGreaterThanOrEqual(8);

    const historyBeforeScroll = await page.evaluate(() => history.length);
    await transcript(page).evaluate((viewer) => {
      viewer.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: 700 }));
      viewer.scrollTop += 700;
      viewer.dispatchEvent(new Event("scroll"));
    });
    await expect.poll(
      () => new URL(page.url()).searchParams.get("line"),
    ).not.toBe(String(urlNavigationLargeThread.deepLinkLine));
    expect(await page.evaluate(() => history.length)).toBe(historyBeforeScroll);
    expect(new URL(page.url()).searchParams.get("unrelated")).toBe("keep");

    const historyBeforePromptJump = await page.evaluate(() => history.length);
    if (viewport.name === "mobile") {
      await page.locator("[data-mobile-prompt-trigger]").click();
      await page.locator('[data-mobile-prompt-item="251"]').click();
    } else {
      const promptNavigator = page.locator("aside[data-prompt-navigator]");
      await promptNavigator.hover();
      const prompt = promptNavigator.locator('[data-prompt-item="251"]');
      await expect(prompt).toBeVisible();
      await prompt.click();
    }
    await waitForRestoredLine(page, 251);
    expect(await page.evaluate(() => history.length)).toBe(historyBeforePromptJump + 1);

    await page.screenshot({
      path: path.join(artifactDir, `${viewport.name}-legacy-line-window.png`),
      fullPage: true,
    });
    expectNoPageErrors(errors);
  });
}
