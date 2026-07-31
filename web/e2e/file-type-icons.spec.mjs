// @ts-check
/**
 * Real-Chromium coverage for the type-specific smart file-link chips.
 *
 * For every simulated tool (Claude, Cursor, Codex) this:
 *   - proves the shared renderer emits typed chips with no console/page errors,
 *   - captures before/after-style evidence screenshots of the chip showcase in
 *     light + dark themes at desktop width and at a 390px mobile width.
 *
 * Screenshots land under `<repo>/artifacts/file-type-icons/` so the handoff can
 * reference stable, task-scoped paths.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";
import { smartLinkScenarios } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

const ARTIFACT_DIR = fileURLToPath(
  new URL("../../artifacts/file-type-icons/", import.meta.url),
);
mkdirSync(ARTIFACT_DIR, { recursive: true });

/** claude_code → claude, cursor → cursor, codex → codex */
const toolShort = (id) => id.replace(/_code$/, "");

test.describe("file-type icon chips", () => {
  for (const scenario of smartLinkScenarios) {
    const short = toolShort(scenario.meta.tool_id);

    test(`${scenario.meta.tool_id} renders typed chips cleanly and captures screenshots`, async ({ page }) => {
      /** @type {string[]} */
      const consoleErrors = [];
      /** @type {string[]} */
      const pageErrors = [];
      page.on("console", (msg) => {
        if (msg.type() === "error") consoleErrors.push(msg.text());
      });
      page.on("pageerror", (err) => pageErrors.push(String(err)));

      await openConversation(page, scenario);
      await page.getByTestId("message-expand-toggle").click();

      // Freeze transitions so theme swaps render instantly for crisp captures.
      await page.addStyleTag({
        content: "*, *::before, *::after { transition: none !important; animation: none !important; }",
      });

      const chipList = page.locator("ul").filter({ hasText: "engine.py" }).first();
      await expect(chipList).toBeVisible();

      // The distinctive glyphs must be present (symbol, not only color).
      await expect(
        page.getByTestId("smart-link-file").filter({ hasText: "engine.py" })
          .locator('[data-file-icon="python"]'),
      ).toHaveText("PY");
      await expect(
        page.getByTestId("smart-code-file").filter({ hasText: "components" })
          .locator('[data-file-icon="directory"]'),
      ).toBeVisible();

      /** @type {[string, Buffer][]} */
      const shots = [];

      await chipList.scrollIntoViewIfNeeded();
      shots.push([`after-desktop-light-${short}.png`, await chipList.screenshot()]);

      await page.evaluate(() => {
        document.documentElement.dataset.theme = "dark";
      });
      shots.push([`after-desktop-dark-${short}.png`, await chipList.screenshot()]);

      await page.evaluate(() => {
        document.documentElement.dataset.theme = "light";
      });
      await page.setViewportSize({ width: 390, height: 844 });
      await chipList.scrollIntoViewIfNeeded();
      shots.push([`after-mobile-${short}.png`, await chipList.screenshot()]);

      for (const [name, buffer] of shots) {
        writeFileSync(join(ARTIFACT_DIR, name), buffer);
        // Also publish canonical (tool-agnostic) names from the Claude run.
        if (scenario.meta.tool_id === "claude_code") {
          writeFileSync(join(ARTIFACT_DIR, name.replace(`-${short}`, "")), buffer);
        }
      }

      expect(consoleErrors, `console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
      expect(pageErrors, `page errors:\n${pageErrors.join("\n")}`).toEqual([]);
    });
  }
});
