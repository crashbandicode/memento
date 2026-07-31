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

      const fileLink = page.getByTestId("smart-link-file");
      await expect(fileLink).toBeVisible();
      await expect(fileLink).toContainText("HANDOFF.md");
      await expect(fileLink).toHaveAttribute("href", "docs/HANDOFF.md");
      await expect(page.getByTestId("smart-file-stats")).toHaveText("+40-0");

      const fileChip = page.getByTestId("smart-code-file");
      await expect(fileChip).toBeVisible();
      await expect(fileChip).toHaveText("prod.toml");
      await expect(fileChip).toHaveAttribute("title", "config/prod.toml");

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

      const webLink = page.getByTestId("smart-link-web");
      await expect(webLink).toBeVisible();
      await expect(webLink).toContainText("Memento deployment");
      await expect(webLink).toContainText("memento.babypotatofarm.com");
      await expect(webLink).toHaveAttribute("target", "_blank");
    });
  }
});
