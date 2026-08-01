// @ts-check
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import {
  canvasScenarios,
  claudeCanvas,
  codexCanvas,
  cursorCanvas,
  urlNavigationCanvasThread,
} from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

const ART_DIR = path.resolve(process.cwd(), "..", "artifacts", "canvas-viewer");
mkdirSync(ART_DIR, { recursive: true });

// The hermetic mock aborts the SSE stream on purpose; ignore that benign noise.
const BENIGN =
  /(events\/stream|EventSource|ERR_ABORTED|Failed to load resource|net::|favicon)/i;

/** @param {import('@playwright/test').Page} page */
function trackErrors(page) {
  /** @type {string[]} */
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && !BENIGN.test(msg.text())) {
      errors.push(`console: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => {
    if (!BENIGN.test(String(err))) errors.push(`pageerror: ${String(err)}`);
  });
  return errors;
}

test.describe("canvas viewer", () => {
  for (const scenario of canvasScenarios) {
    test(`renders a distinctive canvas chip for ${scenario.meta.tool_id}`, async ({ page }) => {
      await openConversation(page, scenario);
      const chip = page.getByTestId("smart-link-canvas");
      await expect(chip).toBeVisible();
      await expect(chip).toContainText("Canvas");
      await expect(chip).toHaveAttribute("aria-haspopup", "dialog");
      await expect(chip).not.toHaveAttribute("data-file-type");
      await expect(chip.locator("[data-file-icon]")).toHaveCount(0);
    });
  }

  test("Cursor canvas renders captured output in an isolated iframe", async ({ page }) => {
    const errors = trackErrors(page);
    await openConversation(page, cursorCanvas);

    const chip = page.getByTestId("smart-link-canvas");
    await expect(chip).toBeVisible();
    await chip.click();

    const dialog = page.getByTestId("canvas-viewer");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("role", "dialog");
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    await expect(dialog).toHaveAttribute("aria-labelledby", /.+/);
    await expect(dialog).toHaveAttribute("data-canvas-mode", "interactive");
    await expect(dialog).toHaveAttribute("data-canvas-layout", "modal");

    const frame = page.getByTestId("canvas-frame");
    await expect(frame).toBeVisible();
    const sandbox = (await frame.getAttribute("sandbox")) ?? "";
    expect(sandbox).toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-same-origin");
    expect(sandbox).not.toContain("allow-popups");
    const srcdoc = (await frame.getAttribute("srcdoc")) ?? "";
    expect(srcdoc).toContain("default-src 'none'");
    expect(srcdoc).toContain("connect-src 'none'");
    expect(srcdoc).toContain("frame-src 'none'");
    await expect(
      page
        .frameLocator('[data-testid="canvas-frame"]')
        .locator("#captured-canvas-marker"),
    ).toContainText("Captured Cursor canvas rendered");

    await page.getByTestId("canvas-source-toggle").click();
    await expect(page.getByTestId("canvas-source")).toContainText(
      "export default function BillingReview",
    );
    await page.getByTestId("canvas-source-toggle").click();
    await expect(frame).toBeVisible();

    const focusInside = await page.evaluate(() => {
      const dlg = document.querySelector('[data-testid="canvas-viewer"]');
      return !!dlg && dlg.contains(document.activeElement);
    });
    expect(focusInside).toBe(true);

    await page.screenshot({ path: path.join(ART_DIR, "cursor-captured-desktop.png"), fullPage: true });

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);

    const restored = await page.evaluate(
      () => document.activeElement?.getAttribute("data-testid") ?? null,
    );
    expect(restored).toBe("smart-link-canvas");
    expect(errors).toEqual([]);
  });

  test("Codex canvas embeds a self-contained artifact in a locked-down iframe", async ({ page }) => {
    const errors = trackErrors(page);
    await openConversation(page, codexCanvas);

    await page.getByTestId("smart-link-canvas").click();
    const dialog = page.getByTestId("canvas-viewer");
    await expect(dialog).toHaveAttribute("data-canvas-mode", "embed");

    const frame = page.getByTestId("canvas-frame");
    await expect(frame).toBeVisible();

    const sandbox = (await frame.getAttribute("sandbox")) ?? "";
    expect(sandbox).toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-same-origin");
    expect(sandbox).not.toContain("allow-popups");
    await expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    await expect(frame).toHaveAttribute("allow", /camera 'none'.*microphone 'none'/);

    const srcdoc = (await frame.getAttribute("srcdoc")) ?? "";
    expect(srcdoc).toContain("default-src 'none'");
    expect(srcdoc).toContain("connect-src 'none'");
    expect(srcdoc).toContain("frame-src 'none'");
    expect(srcdoc.indexOf("Content-Security-Policy")).toBeLessThan(
      srcdoc.indexOf("codex-canvas-marker"),
    );

    // The artifact renders INSIDE the sandboxed frame (opaque origin).
    const marker = page
      .frameLocator('[data-testid="canvas-frame"]')
      .locator("#codex-canvas-marker");
    await expect(marker).toContainText("Codex canvas rendered");

    await page.screenshot({ path: path.join(ART_DIR, "codex-embed-desktop.png"), fullPage: true });

    await page.getByTestId("canvas-viewer-close").click();
    await expect(dialog).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  test("Claude canvas without bytes shows the honest unsupported fallback", async ({ page }) => {
    const errors = trackErrors(page);
    await openConversation(page, claudeCanvas);

    await page.getByTestId("smart-link-canvas").click();
    const dialog = page.getByTestId("canvas-viewer");
    await expect(dialog).toHaveAttribute("data-canvas-mode", "unsupported");
    await expect(page.getByTestId("canvas-unsupported")).toBeVisible();
    await expect(page.getByTestId("canvas-path")).toContainText("security-audit.canvas.tsx");
    // Fallback never executes or embeds anything.
    await expect(page.getByTestId("canvas-frame")).toHaveCount(0);

    await page.screenshot({ path: path.join(ART_DIR, "claude-unsupported-desktop.png"), fullPage: true });
    expect(errors).toEqual([]);
  });

  test("mobile viewport opens a full-screen sheet with a working close control", async ({ page }) => {
    const errors = trackErrors(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await openConversation(page, cursorCanvas);

    await page.getByTestId("smart-link-canvas").click();
    const dialog = page.getByTestId("canvas-viewer");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("data-canvas-layout", "sheet");

    const box = await dialog.boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(360);

    await expect(page.getByTestId("canvas-frame")).toBeVisible();
    await page.screenshot({ path: path.join(ART_DIR, "cursor-captured-mobile.png"), fullPage: true });

    await page.getByTestId("canvas-viewer-close").click();
    await expect(dialog).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  test("Canvas close preserves URL search state across mobile refresh and history", async ({ page }) => {
    const errors = trackErrors(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await openConversation(page, urlNavigationCanvasThread);

    const input = page.locator("[data-conversation-search-input]");
    await input.fill(urlNavigationCanvasThread.searchQuery);
    await input.press("Enter");
    const hit = page.locator(
      `[data-conversation-search-hit="${urlNavigationCanvasThread.longMessageLine}"]`,
    );
    await expect(hit).toBeVisible();
    await hit.click();

    const target = page.locator(
      `#conversation-line-${urlNavigationCanvasThread.longMessageLine}`,
    );
    await expect(target).toBeVisible();
    await expect.poll(
      () => new URL(page.url()).searchParams.get("line"),
    ).toBe(String(urlNavigationCanvasThread.longMessageLine));
    const anchoredUrl = page.url();
    const params = new URL(anchoredUrl).searchParams;
    expect(params.get("q")).toBe(urlNavigationCanvasThread.searchQuery);
    expect(params.get("scope")).toBe("messages");
    expect(params.get("match")).not.toBeNull();

    await target.getByTestId("smart-link-canvas").click();
    await expect(page.getByTestId("canvas-viewer")).toBeVisible();
    await page.getByTestId("canvas-viewer-close").click();
    await expect(page.getByTestId("canvas-viewer")).toHaveCount(0);
    expect(page.url()).toBe(anchoredUrl);

    await page.reload();
    await expect(target).toBeVisible({ timeout: 15_000 });
    await expect(input).toHaveValue(urlNavigationCanvasThread.searchQuery);
    expect(page.url()).toBe(anchoredUrl);
    await expect(page.getByTestId("canvas-viewer")).toHaveCount(0);

    await page.goBack();
    await expect.poll(() => page.url()).not.toBe(anchoredUrl);
    await expect(page.getByTestId("canvas-viewer")).toHaveCount(0);

    await page.goForward();
    await expect(target).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => page.url()).toBe(anchoredUrl);
    await expect(input).toHaveValue(urlNavigationCanvasThread.searchQuery);
    expect(errors).toEqual([]);
  });
});
