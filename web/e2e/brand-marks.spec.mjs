// @ts-check
import { expect, test } from "@playwright/test";

test("Codex and Cursor use their real marks at mobile size", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/splash");

  const codex = page.locator('[data-brand-mark="codex"]').first();
  const cursor = page.locator('[data-brand-mark="cursor"]').first();

  await expect(codex).toBeVisible();
  await expect(codex).toHaveAttribute("data-brand-source", "bundled-official");
  await expect(cursor).toBeVisible();
  await expect(cursor).toHaveAttribute("data-brand-source", "cursor-brand-kit");

  // Guard the two exact regression paths: Memento's old Codex wireframe and
  // faceted Cursor fallback both had multiple paths/groups. These marks are
  // the single-path OpenAI and official Cursor 2D cube assets respectively.
  await expect(codex.locator("path")).toHaveCount(1);
  await expect(cursor.locator("path")).toHaveCount(1);
  await expect(cursor.locator("path")).toHaveAttribute("d", /^M457\.43 125\.94/);
});
