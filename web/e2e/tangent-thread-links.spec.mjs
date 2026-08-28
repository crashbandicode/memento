// @ts-check
import { expect, test } from "@playwright/test";

import {
  tangentMultipleBranchesThread,
  tangentSingleBranchThread,
} from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";


for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`tangent parent and single branch render safely (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConversation(page, tangentSingleBranchThread);

    const parentLink = page.locator("[data-tangent-parent]");
    await expect(parentLink).toBeVisible();
    await expect(parentLink).toContainText("Branched from");
    await expect(parentLink).toContainText(
      tangentSingleBranchThread.meta.tangent_parent.title,
    );
    await expect(parentLink).toHaveAttribute(
      "href",
      tangentSingleBranchThread.meta.tangent_parent.canonical_url,
    );

    const singleBranchChip = page.locator("[data-tangent-branch]");
    await expect(singleBranchChip).toHaveCount(1);
    await expect(singleBranchChip).toContainText("Tangent →");
    await expect(singleBranchChip).toContainText(
      tangentSingleBranchThread.meta.tangent_branches[0].title,
    );
    await expect(singleBranchChip).toHaveAttribute(
      "href",
      tangentSingleBranchThread.meta.tangent_branches[0].canonical_url,
    );
    await expect(page.locator("[data-handoff-continue-reading]")).toHaveCount(0);

    const [parentBox, exportBox, branchBox] = await Promise.all([
      parentLink.boundingBox(),
      page.getByRole("button", { name: "Export" }).boundingBox(),
      singleBranchChip.boundingBox(),
    ]);
    expect(parentBox).not.toBeNull();
    expect(exportBox).not.toBeNull();
    expect(branchBox).not.toBeNull();
    if (!parentBox || !exportBox || !branchBox) {
      throw new Error("Expected tangent header controls to have measurable bounds");
    }
    const parentOverlapsExport = parentBox.x < exportBox.x + exportBox.width
      && parentBox.x + parentBox.width > exportBox.x
      && parentBox.y < exportBox.y + exportBox.height
      && parentBox.y + parentBox.height > exportBox.y;
    expect(parentOverlapsExport).toBe(false);
    expect(branchBox.x).toBeGreaterThanOrEqual(0);
    expect(branchBox.x + branchBox.width).toBeLessThanOrEqual(viewport.width);
  });

  test(`tangent branches expand without header overlap (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConversation(page, tangentMultipleBranchesThread);

    const group = page.locator("[data-tangent-branches]");
    const toggle = page.locator("[data-tangent-branches-toggle]");
    await expect(group).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(toggle).toContainText("Tangents (3)");
    await expect(page.locator("[data-tangent-branch]")).toHaveCount(0);
    await expect(page.locator("[data-handoff-continue-reading]")).toHaveCount(0);

    const [groupBox, exportBox] = await Promise.all([
      group.boundingBox(),
      page.getByRole("button", { name: "Export" }).boundingBox(),
    ]);
    expect(groupBox).not.toBeNull();
    expect(exportBox).not.toBeNull();
    if (!groupBox || !exportBox) {
      throw new Error("Expected tangent branch group and export control bounds");
    }
    const groupOverlapsExport = groupBox.x < exportBox.x + exportBox.width
      && groupBox.x + groupBox.width > exportBox.x
      && groupBox.y < exportBox.y + exportBox.height
      && groupBox.y + groupBox.height > exportBox.y;
    expect(groupOverlapsExport).toBe(false);
    expect(groupBox.x).toBeGreaterThanOrEqual(0);
    expect(groupBox.x + groupBox.width).toBeLessThanOrEqual(viewport.width);

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const branchLinks = page.locator("[data-tangent-branch]");
    await expect(branchLinks).toHaveCount(3);
    for (const [index, branch] of tangentMultipleBranchesThread.meta.tangent_branches.entries()) {
      await expect(branchLinks.nth(index)).toContainText(branch.title);
      await expect(branchLinks.nth(index)).toHaveAttribute("href", branch.canonical_url);
    }
  });
}
