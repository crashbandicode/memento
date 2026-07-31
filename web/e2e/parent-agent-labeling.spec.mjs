// @ts-check
import { expect, test } from "@playwright/test";
import { parentAgentLabeling } from "./fixtures/conversation-scenarios.mjs";
import { openConversation, transcript } from "./support/conversation-page.mjs";

/**
 * Parent-agent labeling + launch-description title regression coverage.
 *
 * Fixture: a subagent thread (user_role_origin = parent_agent) carrying an
 * `agent_launch_description`. The header must title it by that launch
 * description, and the parent agent's dispatch turn must be labeled "Parent
 * agent" — never mistaken for the human's own chat input.
 */
test.describe("parent-agent labeling", () => {
  test("header titles the subagent by its launch description + codename [regression #5]", async ({ page }) => {
    await openConversation(page, parentAgentLabeling);

    // The launch-description-derived subagent chip. "Subagent ·" only appears
    // on this chip (the dispatch card uses "Parent agent"), so it uniquely
    // identifies the launch-description title path.
    const chip = page.getByText(/Subagent ·/);
    await expect(chip).toBeVisible();
    await expect(chip).toContainText("Investigate checkout regression");
    await expect(chip).toContainText("brave-otter");
  });

  test("the parent agent's dispatch turn is labeled, not shown as a human prompt [regression #5]", async ({ page }) => {
    await openConversation(page, parentAgentLabeling);

    const dispatch = transcript(page).locator('[data-parent-agent-message="true"]');
    await expect(dispatch).toBeVisible();
    await expect(dispatch).toContainText("Parent agent");
    await expect(dispatch).toContainText("Please investigate the checkout regression");

    // It must not be treated as a human prompt (no prompt-line anchor).
    const wrapper = transcript(page).locator('[data-message-id="1"]');
    await expect(wrapper).toHaveAttribute("data-message-category", "context");
    await expect(wrapper).not.toHaveAttribute("data-prompt-line", /\d+/);
  });
});
