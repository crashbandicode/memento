// @ts-check
import { expect, test } from "@playwright/test";

import { pinnedMessageThread } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";


const predecessor = {
  document_id: "22222222-2222-4222-8222-222222222222",
  tool_id: "claude_code",
  title: "Previous implementation thread",
  canonical_url: "/conversations/claude/22222222-2222-4222-8222-222222222222",
};
const successor = {
  document_id: "33333333-3333-4333-8333-333333333333",
  tool_id: "claude_code",
  title: "Continuation implementation thread",
  canonical_url: "/conversations/claude/33333333-3333-4333-8333-333333333333",
};

const handoffScenario = {
  ...pinnedMessageThread,
  docId: "11111111-1111-4111-8111-111111111111",
  meta: {
    ...pinnedMessageThread.meta,
    tool_id: "claude_code",
    title: "Current implementation thread",
    handoff_predecessor: predecessor,
    handoff_successor: successor,
  },
};

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`handoff links render and continue reading (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConversation(page, handoffScenario);

    const predecessorLink = page.locator("[data-handoff-predecessor]");
    await expect(predecessorLink).toBeVisible();
    await expect(predecessorLink).toContainText("Continued from");
    await expect(predecessorLink).toContainText(predecessor.title);
    await expect(predecessorLink).toHaveAttribute("href", predecessor.canonical_url);

    const successorChip = page.locator("[data-handoff-successor]");
    await expect(successorChip).toBeVisible();
    await expect(successorChip).toContainText("Handed off →");
    await expect(successorChip).toContainText(successor.title);
    await expect(successorChip).toHaveAttribute("href", successor.canonical_url);

    const [predecessorBox, exportBox, successorBox] = await Promise.all([
      predecessorLink.boundingBox(),
      page.getByRole("button", { name: "Export" }).boundingBox(),
      successorChip.boundingBox(),
    ]);
    expect(predecessorBox).not.toBeNull();
    expect(exportBox).not.toBeNull();
    expect(successorBox).not.toBeNull();
    if (!predecessorBox || !exportBox || !successorBox) {
      throw new Error("Expected all handoff header controls to have measurable bounds");
    }
    const linksOverlapExport = predecessorBox.x < exportBox.x + exportBox.width
      && predecessorBox.x + predecessorBox.width > exportBox.x
      && predecessorBox.y < exportBox.y + exportBox.height
      && predecessorBox.y + predecessorBox.height > exportBox.y;
    expect(linksOverlapExport).toBe(false);
    expect(successorBox.x).toBeGreaterThanOrEqual(0);
    expect(successorBox.x + successorBox.width).toBeLessThanOrEqual(viewport.width);

    const continueReading = page.locator("[data-handoff-continue-reading]");
    await expect(continueReading).toBeVisible();
    await expect(continueReading).toContainText("Continue reading →");
    await expect(continueReading).toContainText(successor.title);
    await expect(continueReading).toHaveAttribute("href", successor.canonical_url);

    await continueReading.click();
    await expect.poll(() => new URL(page.url()).pathname).toBe(successor.canonical_url);
    await expect(page.locator("[data-conversation-viewer]")).toBeVisible();
  });
}
