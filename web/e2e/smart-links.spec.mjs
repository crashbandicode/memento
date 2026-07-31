// @ts-check
import { expect, test } from "@playwright/test";
import { smartLinkScenarios } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

test.describe("smart conversation links", () => {
  for (const scenario of smartLinkScenarios) {
    test(`${scenario.meta.tool_id} uses the shared rich link renderer`, async ({ page }) => {
      await openConversation(page, scenario);

      const expand = page.getByTestId("message-expand-toggle");
      await expect(expand).toBeVisible();
      await expect(expand).toHaveAttribute("aria-expanded", "false");
      await expand.click();
      await expect(expand).toHaveAttribute("aria-expanded", "true");

      const fileLink = page.getByTestId("smart-link-file").filter({ hasText: "HANDOFF.md" });
      await expect(fileLink).toBeVisible();
      await expect(fileLink).toContainText("HANDOFF.md");
      await expect(fileLink).toHaveAttribute("href", "docs/HANDOFF.md");
      await expect(fileLink).toHaveAttribute("data-file-type", "markdown");
      await expect(fileLink.locator('[data-file-icon="markdown"]')).toBeVisible();
      await expect(page.getByTestId("smart-file-stats")).toHaveText("+40-0");

      const sourceLink = page.getByTestId("smart-link-file").filter({ hasText: "SmartLink.tsx" });
      await expect(sourceLink).toHaveAttribute("data-file-type", "typescript");
      await expect(sourceLink.locator('[data-file-icon="typescript"]')).toBeVisible();

      const packageLink = page.getByTestId("smart-link-file").filter({ hasText: "package.json" });
      await expect(packageLink).toHaveAttribute("data-file-type", "package");
      await expect(packageLink.locator('[data-file-icon="package"]')).toBeVisible();

      const dockerLink = page.getByTestId("smart-link-file").filter({ hasText: "Dockerfile" });
      await expect(dockerLink).toHaveAttribute("data-file-type", "docker");
      await expect(dockerLink.locator('[data-file-icon="docker"]')).toBeVisible();

      const imageLink = page.getByTestId("smart-link-file").filter({ hasText: "logo.svg" });
      await expect(imageLink).toHaveAttribute("data-file-type", "image");
      await expect(imageLink.locator('[data-file-icon="image"]')).toBeVisible();

      // Languages render a distinctive letter monogram, so the symbol (not just
      // the color) tells related source kinds apart.
      const pythonLink = page.getByTestId("smart-link-file").filter({ hasText: "engine.py" });
      await expect(pythonLink).toHaveAttribute("data-file-type", "python");
      const pythonIcon = pythonLink.locator('[data-file-icon="python"]');
      await expect(pythonIcon).toBeVisible();
      await expect(pythonIcon).toHaveText("PY");
      await expect(sourceLink.locator('[data-file-icon="typescript"]')).toHaveText("TS");

      const iconColors = await Promise.all(
        [fileLink, sourceLink, packageLink, dockerLink, imageLink, pythonLink].map(
          (link) => link.locator("[data-file-icon]").evaluate(
            (element) => window.getComputedStyle(element).color,
          ),
        ),
      );
      expect(new Set(iconColors).size).toBeGreaterThan(3);

      const fileChip = page.getByTestId("smart-code-file").filter({ hasText: "prod.toml" });
      await expect(fileChip).toBeVisible();
      await expect(fileChip).toHaveText("prod.toml");
      await expect(fileChip).toHaveAttribute("title", "config/prod.toml");
      await expect(fileChip).toHaveAttribute("data-file-type", "config");
      await expect(fileChip.locator('[data-file-icon="config"]')).toBeVisible();

      const shellChip = page.getByTestId("smart-code-file").filter({ hasText: "release.ps1" });
      await expect(shellChip).toHaveAttribute("data-file-type", "shell");
      await expect(shellChip.locator('[data-file-icon="shell"]')).toBeVisible();

      const directoryChip = page.getByTestId("smart-code-file").filter({ hasText: "components" });
      await expect(directoryChip).toBeVisible();
      await expect(directoryChip).toHaveText("components");
      await expect(directoryChip).toHaveAttribute("data-file-type", "directory");
      await expect(directoryChip.locator('[data-file-icon="directory"]')).toBeVisible();

      const compare = page.getByTestId("smart-link-git-compare");
      await expect(compare).toBeVisible();
      await expect(compare).toHaveAttribute("data-provider", "gitlab");
      await expect(compare).toHaveAttribute("target", "_blank");
      await expect(compare.getByTestId("smart-ref-pill")).toHaveCount(2);
      await expect(compare.getByTestId("smart-ref-pill").first()).toHaveText("f54a57bd");
      await expect(compare.getByTestId("smart-ref-pill").last()).toHaveText("13ab85e7");

      const commit = page.getByTestId("smart-link-git-commit");
      await expect(commit).toBeVisible();
      await expect(commit).toHaveAttribute("data-provider", "github");
      await expect(commit.getByTestId("smart-ref-pill")).toHaveText("9c216b8aa5");

      await expect(page.getByTestId("smart-code-sha")).toHaveText("9c216b8");

      const inlineCode = page.getByTestId("inline-code");
      await expect(inlineCode).toHaveText("run_refresh_active_via_bjobs");
      const inlineStyles = await inlineCode.evaluate((element) => {
        const styles = window.getComputedStyle(element);
        return {
          backgroundColor: styles.backgroundColor,
          bodyColor: window.getComputedStyle(element.parentElement).color,
          borderRadius: styles.borderTopLeftRadius,
          color: styles.color,
          fontFamily: styles.fontFamily,
          paddingLeft: Number.parseFloat(styles.paddingLeft),
        };
      });
      expect(inlineStyles.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
      expect(inlineStyles.color).not.toBe(inlineStyles.bodyColor);
      expect(inlineStyles.borderRadius).toBe("4px");
      expect(inlineStyles.fontFamily).toContain("ui-monospace");
      expect(inlineStyles.paddingLeft).toBeGreaterThan(0);

      const fencedCode = page.locator("pre code");
      await expect(fencedCode).toHaveText("run_refresh_active_via_bjobs()");
      await expect(fencedCode).toHaveClass(/(?:^|\s)language-python(?:\s|$)/);
      await expect(fencedCode).not.toHaveAttribute("data-testid", "inline-code");
      const fencedStyles = await fencedCode.evaluate((element) => {
        const styles = window.getComputedStyle(element);
        return {
          backgroundColor: styles.backgroundColor,
          display: styles.display,
          paddingLeft: Number.parseFloat(styles.paddingLeft),
        };
      });
      expect(fencedStyles.backgroundColor).not.toBe(inlineStyles.backgroundColor);
      expect(fencedStyles.display).toBe("block");
      expect(fencedStyles.paddingLeft).toBeGreaterThan(inlineStyles.paddingLeft);

      const webLink = page.getByTestId("smart-link-web");
      await expect(webLink).toBeVisible();
      await expect(webLink).toContainText("Memento deployment");
      await expect(webLink).toContainText("memento.babypotatofarm.com");
      await expect(webLink).toHaveAttribute("target", "_blank");

      await page.evaluate(() => {
        document.documentElement.dataset.theme = "dark";
      });
      const darkInlineStyles = await inlineCode.evaluate((element) => {
        const styles = window.getComputedStyle(element);
        return {
          backgroundColor: styles.backgroundColor,
          bodyColor: window.getComputedStyle(element.parentElement).color,
          color: styles.color,
        };
      });
      expect(darkInlineStyles.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
      expect(darkInlineStyles.color).not.toBe(darkInlineStyles.bodyColor);
    });

    test(`${scenario.meta.tool_id} keeps typed file links readable on mobile`, async ({ page }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      await openConversation(page, scenario);
      await page.getByTestId("message-expand-toggle").click();

      for (const [label, kind] of [
        ["HANDOFF.md", "markdown"],
        ["SmartLink.tsx", "typescript"],
        ["engine.py", "python"],
        ["package.json", "package"],
        ["Dockerfile", "docker"],
        ["logo.svg", "image"],
      ]) {
        const link = page.getByTestId("smart-link-file").filter({ hasText: label });
        await expect(link).toBeVisible();
        await expect(link).toHaveAttribute("data-file-type", kind);
        const box = await link.boundingBox();
        expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
        expect((box?.x ?? 390) + (box?.width ?? 1)).toBeLessThanOrEqual(390);
      }
    });
  }
});
