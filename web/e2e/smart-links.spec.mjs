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
      await expect(sourceLink).toHaveAttribute("data-file-type", "react");
      await expect(sourceLink.locator('[data-file-icon="react"]')).toBeVisible();

      const packageLink = page.getByTestId("smart-link-file").filter({ hasText: "package.json" });
      await expect(packageLink).toHaveAttribute("data-file-type", "package");
      await expect(packageLink.locator('[data-file-icon="package"]')).toBeVisible();

      const dockerLink = page.getByTestId("smart-link-file").filter({ hasText: "Dockerfile" });
      await expect(dockerLink).toHaveAttribute("data-file-type", "docker");
      await expect(dockerLink.locator('[data-file-icon="docker"]')).toBeVisible();

      const imageLink = page.getByTestId("smart-link-file").filter({ hasText: "logo.svg" });
      await expect(imageLink).toHaveAttribute("data-file-type", "image");
      await expect(imageLink.locator('[data-file-icon="image"]')).toBeVisible();

      // Each type renders a real vector glyph (brand logo or conceptual
      // pictogram) — an <svg> with a role="img" accessible type label, never a
      // two-letter monogram. The symbol (not just color) tells kinds apart.
      const pythonLink = page.getByTestId("smart-link-file").filter({ hasText: "engine.py" });
      await expect(pythonLink).toHaveAttribute("data-file-type", "python");
      const pythonIcon = pythonLink.locator('[data-file-icon="python"]');
      await expect(pythonIcon).toBeVisible();
      await expect(pythonIcon).toHaveAttribute("role", "img");
      await expect(pythonIcon).toHaveAttribute("aria-label", "Python file");
      await expect(pythonIcon.locator("svg")).toHaveCount(1);
      await expect(pythonIcon).toHaveText("");
      const reactIcon = sourceLink.locator('[data-file-icon="react"]');
      await expect(reactIcon).toHaveAttribute("aria-label", "React component");
      await expect(reactIcon.locator("svg")).toHaveCount(1);
      await expect(reactIcon).toHaveText("");

      // No file chip may fall back to a text monogram: every type glyph is a
      // labelled <svg> with empty text content (PY/MD/TS/… are gone for good).
      const typeIcons = page.locator("[data-file-icon]");
      const iconCount = await typeIcons.count();
      expect(iconCount).toBeGreaterThan(6);
      for (let index = 0; index < iconCount; index++) {
        const icon = typeIcons.nth(index);
        await expect(icon).toHaveText("");
        await expect(icon.locator("svg")).toHaveCount(1);
        await expect(icon).toHaveAttribute("role", "img");
        expect((await icon.getAttribute("aria-label"))?.length ?? 0).toBeGreaterThan(3);
      }

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
      await expect(fileChip).toHaveAttribute("data-file-type", "toml");
      await expect(fileChip.locator('[data-file-icon="toml"]')).toBeVisible();

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
        ["SmartLink.tsx", "react"],
        ["engine.py", "python"],
        ["mod.rs", "rust"],
        ["main.go", "go"],
        ["package.json", "package"],
        ["Dockerfile", "docker"],
        ["logo.svg", "image"],
        ["ci.yaml", "yaml"],
        ["analysis.ipynb", "notebook"],
        ["architecture.pdf", "pdf"],
      ]) {
        const link = page.getByTestId("smart-link-file").filter({ hasText: label });
        await expect(link).toBeVisible();
        await expect(link).toHaveAttribute("data-file-type", kind);
        // The glyph is a labelled SVG, never a text monogram, even on mobile.
        const icon = link.locator(`[data-file-icon="${kind}"]`);
        await expect(icon.locator("svg")).toHaveCount(1);
        await expect(icon).toHaveText("");
        const box = await link.boundingBox();
        expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
        expect((box?.x ?? 390) + (box?.width ?? 1)).toBeLessThanOrEqual(390);
      }
    });
  }

  test("fenced code has an exact, accessible copy control", async ({ page }) => {
    await openConversation(page, smartLinkScenarios[0]);
    await page.getByTestId("message-expand-toggle").click();

    const fencedCode = page.locator("pre code");
    const codeBlock = page.getByTestId("markdown-code-block");
    const copy = page.getByTestId("code-block-copy");
    const inlineCode = page.getByTestId("inline-code");

    await expect(codeBlock).toHaveCount(1);
    await expect(copy).toBeVisible();
    await expect(copy).toHaveAttribute("data-copy-status", "idle");
    await expect(copy).toHaveAccessibleName("Copy");
    await expect(inlineCode.locator("xpath=ancestor::*[@data-testid='markdown-code-block']")).toHaveCount(0);

    const pre = codeBlock.locator("pre");
    const idlePresentation = await copy.evaluate((element) => ({
      opacity: Number.parseFloat(window.getComputedStyle(element).opacity),
    }));
    const prePresentation = await pre.evaluate((element) => ({
      paddingTop: Number.parseFloat(window.getComputedStyle(element).paddingTop),
    }));
    const [preBox, copyBox] = await Promise.all([pre.boundingBox(), copy.boundingBox()]);
    expect(idlePresentation.opacity).toBeLessThanOrEqual(0.55);
    expect(prePresentation.paddingTop).toBeLessThanOrEqual(20);
    expect(copyBox?.y ?? -1).toBeGreaterThanOrEqual(preBox?.y ?? 0);

    await codeBlock.hover();
    await expect.poll(() => copy.evaluate(
      (element) => Number.parseFloat(window.getComputedStyle(element).opacity),
    )).toBeGreaterThanOrEqual(0.95);

    await page.mouse.move(0, 0);
    await copy.focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    await expect(copy).toBeFocused();
    await expect.poll(() => copy.evaluate(
      (element) => Number.parseFloat(window.getComputedStyle(element).opacity),
    )).toBeGreaterThanOrEqual(0.95);

    await page.evaluate(() => {
      /** @type {any} */ (window).__copiedFencedCode = undefined;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (value) => {
            /** @type {any} */ (window).__copiedFencedCode = value;
          },
        },
      });
    });

    await copy.click();
    await expect(copy).toHaveAttribute("data-copy-status", "copied");
    await expect(copy).toHaveAccessibleName("Copied");
    expect(await page.evaluate(() => /** @type {any} */ (window).__copiedFencedCode))
      .toBe("run_refresh_active_via_bjobs()");

    // The control wraps the block rather than its highlighted contents, so
    // copy affordance changes do not alter the source or highlight class.
    await expect(fencedCode).toHaveText("run_refresh_active_via_bjobs()");
    await expect(fencedCode).toHaveClass(/(?:^|\s)language-python(?:\s|$)/);
  });

  test("fenced code copy control stays compact and touch-safe over long mobile code", async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      userAgent: "Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 Chrome/138 Mobile Safari/537.36",
    });
    const page = await context.newPage();
    const base = smartLinkScenarios[0];
    const longLine = "$ExportedNamespace = 'hwinf-cisd-lsf-data-api-fastapi-production-with-a-deliberately-long-mobile-value'";
    const scenario = {
      ...base,
      docId: base.docId + "-long-code-mobile",
      meta: { ...base.meta, id: base.meta.id + "-long-code-mobile" },
      messages: base.messages.map((message) => message.id === 2
        ? {
            ...message,
            content: message.content
              .replace("```python", "```powershell")
              .replace("run_refresh_active_via_bjobs()", longLine),
          }
        : message),
    };

    await openConversation(page, scenario);
    await page.getByTestId("message-expand-toggle").click();

    const codeBlock = page.getByTestId("markdown-code-block");
    const pre = codeBlock.locator("pre");
    const copy = codeBlock.getByTestId("code-block-copy");
    await expect(copy).toBeVisible();
    const [box, preBox, blockBox] = await Promise.all([
      copy.boundingBox(),
      pre.boundingBox(),
      codeBlock.boundingBox(),
    ]);
    const presentation = await copy.evaluate((element) => {
      const styles = window.getComputedStyle(element);
      return {
        opacity: Number.parseFloat(styles.opacity),
        width: Number.parseFloat(styles.width),
      };
    });
    const prePresentation = await pre.evaluate((element) => {
      const styles = window.getComputedStyle(element);
      return {
        paddingTop: Number.parseFloat(styles.paddingTop),
      };
    });

    expect(preBox?.x ?? -1).toBeGreaterThanOrEqual(blockBox?.x ?? 0);
    expect((preBox?.x ?? 0) + (preBox?.width ?? 390)).toBeLessThanOrEqual(box?.x ?? -1);
    expect((box?.x ?? 390) + (box?.width ?? 1)).toBeLessThanOrEqual((blockBox?.x ?? 0) + (blockBox?.width ?? 390));
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
    expect(presentation.width).toBeLessThanOrEqual(46);
    expect(presentation.opacity).toBeLessThanOrEqual(0.55);
    expect(prePresentation.paddingTop).toBeLessThanOrEqual(20);
    await expect(pre.locator("code")).toContainText(longLine);
    await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true);

    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async () => undefined },
      });
    });
    await copy.tap();
    await expect(copy).toHaveAttribute("data-copy-status", "copied");
    await expect.poll(() => copy.evaluate(
      (element) => Number.parseFloat(window.getComputedStyle(element).opacity),
    )).toBeGreaterThanOrEqual(0.95);
    await expect(copy).toHaveAttribute("data-copy-status", "idle", { timeout: 3_000 });
    await expect.poll(() => copy.evaluate(
      (element) => Number.parseFloat(window.getComputedStyle(element).opacity),
    )).toBeLessThanOrEqual(0.55);
    await context.close();
  });

  test("Mermaid remains a diagram without a code copy control", async ({ page }) => {
    const base = smartLinkScenarios[0];
    const scenario = {
      ...base,
      docId: `${base.docId}-mermaid`,
      meta: { ...base.meta, id: `${base.meta.id}-mermaid` },
      messages: base.messages.map((message) => message.id === 2
        ? {
            ...message,
            content: `${message.content}\n\n\`\`\`mermaid\ngraph LR\n  A[Source] --> B[Diagram]\n\`\`\``,
          }
        : message),
    };
    await openConversation(page, scenario);
    await page.getByTestId("message-expand-toggle").click();

    const diagram = page.locator("[data-mermaid-diagram]");
    await expect(diagram).toBeVisible();
    await expect(diagram.getByTestId("code-block-copy")).toHaveCount(0);
    await expect(diagram.getByTestId("markdown-code-block")).toHaveCount(0);
  });
});
