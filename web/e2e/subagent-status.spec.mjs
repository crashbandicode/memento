// @ts-check
import { expect, test } from "@playwright/test";
import { runningSubagent } from "./fixtures/conversation-scenarios.mjs";
import { openConversation } from "./support/conversation-page.mjs";

/**
 * Subagent lifecycle regression coverage.
 *
 * Fixture: the subagent metadata reports `running`, a launch (`started`) event
 * exists, and a live snapshot still lists it as running — but NO completion has
 * arrived. The UI must never present this subagent as "Completed".
 */
test.describe("subagent status", () => {
  test("a running subagent is shown Running, not Completed, in the browser panel [regression #4]", async ({ page }) => {
    await openConversation(page, runningSubagent);

    const trigger = page.locator('button[title="Browse 1 subagent"]');
    await expect(trigger).toBeVisible();
    await trigger.click();

    const panel = page.getByRole("dialog").first();
    await expect(panel).toBeVisible();
    await expect(panel).toContainText("Harden ingest pipeline");
    await expect(panel).toContainText("Running");
    // The still-running child must not be reported as completed.
    await expect(panel).not.toContainText("Completed");
  });

  test("the inline agent snapshot keeps the agent Running and shows no completion card [regression #4]", async ({ page }) => {
    await openConversation(page, runningSubagent);

    // The launch event is present...
    const started = page.locator('[data-agent-kind="started"]');
    await expect(started).toBeVisible();
    await expect(started).toContainText("Harden ingest pipeline");
    await expect(started).not.toContainText(
      "INTERNAL_ASYNC_LAUNCH_METADATA_MUST_NOT_RENDER",
    );

    // ...the snapshot still lists the agent as running...
    const snapshot = page.locator('[data-agent-kind="snapshot"]');
    await expect(snapshot).toBeVisible();
    await expect(snapshot).toContainText("Running");
    await expect(snapshot).not.toContainText("Completed");

    // ...and nothing anywhere renders a completion for it.
    await expect(page.locator('[data-agent-kind="completed"]')).toHaveCount(0);
  });
});
