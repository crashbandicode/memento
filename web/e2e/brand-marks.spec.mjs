// @ts-check
import { expect, test } from "@playwright/test";

test("Codex and Cursor use their real marks at mobile size", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/splash");

  const codex = page.locator('[data-brand-mark="codex"]').first();
  const cursor = page.locator('[data-brand-mark="cursor"]').first();

  await expect(codex).toBeVisible();
  await expect(codex).toHaveAttribute("data-brand-source", "codex-product");
  await expect(cursor).toBeVisible();
  await expect(cursor).toHaveAttribute("data-brand-source", "cursor-brand-kit");

  // Guard regression paths: Codex uses the cloud-terminal product mark (not
  // bundled react-icons); Cursor uses the official 2D cube from the brand kit.
  await expect(codex.locator('[data-codex-product-mark="cloud-terminal"]')).toBeVisible();
  await expect(codex.locator("path")).toHaveCount(2);
  await expect(cursor.locator("path")).toHaveCount(1);
  await expect(cursor.locator("path")).toHaveAttribute("d", /^M457\.43 125\.94/);
});
