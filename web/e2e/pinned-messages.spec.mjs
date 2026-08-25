// @ts-check
import { expect, test } from "@playwright/test";
import { pinnedMessageThread } from "./fixtures/conversation-scenarios.mjs";
import { installConversationMocks, openConversation, seedAuth, transcript } from "./support/conversation-page.mjs";

async function chooseCopyFormat(message, format) {
  await message.locator('[data-message-copy="top"] summary').click();
  await message.locator(`[data-copy-format="${format}"]`).click();
}

test.describe("pinned messages and copy-prefix preference", () => {
  test("pins a message optimistically and adds it to the thread pinned section", async ({ page }) => {
    await openConversation(page, pinnedMessageThread);

    const threadPins = page.locator("[data-thread-pins]");
    await expect(threadPins).toContainText("0");

    const assistantMessage = transcript(page).locator('[data-message-id="102"]');
    const pinButton = assistantMessage.locator("[data-pin-message]");
    await pinButton.click();
    await expect(pinButton).toHaveAttribute("data-pinned", "true");

    await threadPins.locator("[data-thread-pins-toggle]").click();
    const pinnedRow = threadPins.locator('[data-pinned-message="102"]');
    await expect(pinnedRow).toContainText("The copied message body is retained without alteration.");
  });

  test("omits the role/timestamp header from Markdown, rich text, and Slack clipboard payloads", async ({ context, page }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await openConversation(page, pinnedMessageThread);

    await page.locator("[data-copy-omit-role-prefix] input").check();
    const assistantMessage = transcript(page).locator('[data-message-id="102"]');

    await chooseCopyFormat(assistantMessage, "markdown");
    const markdown = await page.evaluate(() => navigator.clipboard.readText());
    expect(markdown).toContain("The copied message body is retained without alteration.");
    expect(markdown).not.toContain("**Assistant**");

    await chooseCopyFormat(assistantMessage, "rich");
    const richPlain = await page.evaluate(async () => {
      const [item] = await navigator.clipboard.read();
      return (await item.getType("text/plain")).text();
    });
    expect(richPlain).toContain("The copied message body is retained without alteration.");
    expect(richPlain).not.toContain("**Assistant**");

    await chooseCopyFormat(assistantMessage, "slack");
    const slackPlain = await page.evaluate(async () => {
      const [item] = await navigator.clipboard.read();
      return (await item.getType("text/plain")).text();
    });
    expect(slackPlain).toContain("The copied message body is retained without alteration.");
    expect(slackPlain).not.toContain("Assistant");

    await page.getByRole("button", { name: "Export" }).click();
    await page.getByRole("button", { name: "Copy rich text" }).click();
    await expect(page.getByRole("status")).toContainText("Copied as rich text");
    const wholeThreadPlain = await page.evaluate(async () => {
      const [item] = await navigator.clipboard.read();
      return (await item.getType("text/plain")).text();
    });
    expect(wholeThreadPlain).toContain("Tool output remains part of the copied thread.");
    expect(wholeThreadPlain).not.toContain("Prompt 1 — You");
    expect(wholeThreadPlain).not.toContain("Assistant");
    expect(wholeThreadPlain).not.toContain("Tool ·");
  });

  test("renders all pinned messages on the global pins page", async ({ page }) => {
    await seedAuth(page);
    await installConversationMocks(page, pinnedMessageThread);
    await page.goto("/pins");

    const pinsPage = page.locator("[data-pins-page]");
    await expect(pinsPage.locator('[data-pinned-message="102"]')).toContainText(
      "Pinned message fixture",
    );
    await expect(pinsPage.locator("[data-pin-conversation-link]")).toHaveAttribute(
      "href",
      /\/conversations\/11111111-1111-4111-8111-111111111111\?line=2/,
    );
  });
});
