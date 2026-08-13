// @ts-check
import { expect, test } from "@playwright/test";
import {
  metadataOnlyPrompts,
  permissionWrappedQuestion,
  resolvedPermissionWithoutRecordedChoice,
  sameRowQuestionResponse,
} from "./fixtures/conversation-scenarios.mjs";
import { openConversation, transcript } from "./support/conversation-page.mjs";

/**
 * Prompt-visibility / wrapper-unwrap regression coverage.
 *
 * All data is injected via route interception (see support/conversation-page).
 * No live production endpoint is contacted.
 */
test.describe("prompt visibility & question wrapper unwrap", () => {
  test("PermissionRequest(AskUserQuestion) renders the real question, not a raw Yes/JSON dump [regression #1]", async ({ page }) => {
    await openConversation(page, permissionWrappedQuestion);

    const card = transcript(page).locator('[data-question-interaction="int-approve-db"]');
    await expect(card).toBeVisible();

    // Real, unwrapped human question + options are shown.
    await expect(card).toContainText("Which database should power the new service?");
    await expect(card.locator('[data-question-option="postgres"]')).toContainText("PostgreSQL");
    await expect(card.locator('[data-question-option="mysql"]')).toContainText("MySQL");
    // It is classified as a permission request in the header.
    await expect(card).toContainText("Permission request");

    // The raw permission envelope must NEVER be dumped into the DOM.
    await expect(page.locator("body")).not.toContainText("RAW_PERMISSION_ENVELOPE_SHOULD_NOT_RENDER");
    await expect(page.locator("body")).not.toContainText("permission_suggestion");
  });

  test("wrapper + real question are not both shown as duplicate cards [regression #3]", async ({ page }) => {
    await openConversation(page, permissionWrappedQuestion);

    // Exactly one question card for this interaction id inside the transcript.
    await expect(
      transcript(page).locator('[data-question-interaction="int-approve-db"]'),
    ).toHaveCount(1);
    // The question prompt itself appears exactly once.
    await expect(
      transcript(page).getByText("Which database should power the new service?"),
    ).toHaveCount(1);
  });

  test("question and response on one row share one navigation anchor", async ({ page }) => {
    await openConversation(page, sameRowQuestionResponse);

    await expect(
      transcript(page).locator('[data-question-interaction="int-cancelled-db"]'),
    ).toHaveCount(1);
    await expect(page.locator("#conversation-line-2")).toHaveCount(1);
  });

  test("resolved permission without an authoritative choice says so explicitly", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openConversation(page, resolvedPermissionWithoutRecordedChoice);

    const card = transcript(page).locator(
      '[data-question-interaction="permission-bash-resolved"]',
    );
    await expect(card).toContainText("Resolved");
    await expect(card.locator("[data-question-response-unavailable]")).toContainText(
      "Claude Code did not record which option was selected.",
    );
    await expect(card.locator('[data-question-option][data-selected="true"]')).toHaveCount(0);
  });

  test("live prompt outline appears on metadata-only ingest with no SSE stream [regression #2]", async ({ page }) => {
    await openConversation(page, metadataOnlyPrompts);

    const navigator = page.locator("[data-prompt-navigator]").first();
    await expect(navigator).toBeAttached();

    // Prompts came from GET .../prompts alone — the SSE stream is inert.
    const firstPrompt = navigator.locator('[data-prompt-item="1"]').first();
    await expect(firstPrompt).toBeAttached();
    await expect(firstPrompt).toHaveAttribute(
      "title",
      /Investigate the flaky checkout test/,
    );

    const secondPrompt = navigator.locator('[data-prompt-item="2"]').first();
    await expect(secondPrompt).toHaveAttribute("title", /retry backoff/);
  });
});
