// @ts-check
import { devices, expect, test } from "@playwright/test";
import { openConversation, transcript } from "./support/conversation-page.mjs";

const slackCopyScenario = {
  docId: "conv-slack-rich-copy",
  meta: {
    id: "conv-slack-rich-copy",
    tool_id: "codex",
    title: "Slack rich-copy fixture",
    relative_path: "codex/sessions/slack-rich-copy.jsonl",
    metadata: {},
    message_count: 2,
    subagent_count: 0,
    pending_question_count: 0,
    synced_at: "2026-08-11T12:00:10.000Z",
    activity_at: "2026-08-11T12:00:10.000Z",
  },
  messages: [
    {
      id: 1,
      line_number: 1,
      role: "user",
      content: "Summarize the release for Slack.",
      timestamp: "2026-08-11T12:00:00.000Z",
    },
    {
      id: 2,
      line_number: 2,
      role: "assistant",
      model: "gpt-5.6-sol",
      content: [
        "**Release ready**",
        "",
        "1. Deploy the API",
        "2. Verify the [runbook](https://example.com/runbook)",
        "",
        "```powershell",
        "Get-Service memento",
        "```",
      ].join("\n"),
      timestamp: "2026-08-11T12:00:10.000Z",
    },
  ],
  prompts: [
    {
      id: 1,
      line_number: 1,
      content: "Summarize the release for Slack.",
      timestamp: "2026-08-11T12:00:00.000Z",
    },
  ],
  pending: { count: 0, interactions: [], inferred_responses: [] },
  latestAgentLine: 2,
};

async function openSlackCopy(page) {
  await openConversation(page, slackCopyScenario);
  const message = transcript(page).locator("[data-message-copy-frame]").last();
  await message.locator('[data-message-copy="top"] summary').click();
  await message.locator('[data-copy-format="slack"]').click();
}

test.describe("Android Slack rich copy", () => {
  const pixel = devices["Pixel 7"];
  test.use({
    userAgent: pixel.userAgent,
    viewport: pixel.viewport,
    deviceScaleFactor: pixel.deviceScaleFactor,
    isMobile: pixel.isMobile,
    hasTouch: pixel.hasTouch,
  });

  test("publishes semantic HTML and plain text from the formatted mobile sheet", async ({ context, page }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await openSlackCopy(page);

    const sheet = page.locator("[data-slack-copy-sheet]");
    await expect(sheet).toBeVisible();
    await expect(sheet).toHaveAttribute("data-slack-copy-ready", "true");

    const preview = sheet.locator("[data-slack-copy-surface]");
    await expect(preview).toHaveAttribute("data-copy-surface-format", "html");
    await expect(preview.locator("b").filter({ hasText: "Release ready" })).toHaveCount(1);
    await expect(preview.locator("ol > li")).toHaveCount(2);
    await expect(preview.locator("pre code")).toContainText("Get-Service memento");
    await expect(preview.locator('a[href="https://example.com/runbook"]')).toContainText("runbook");
    if (process.env.MEMENTO_SLACK_MOBILE_SHOT) {
      await page.screenshot({ path: process.env.MEMENTO_SLACK_MOBILE_SHOT, fullPage: true });
    }

    await sheet.locator("[data-slack-copy-action]").click();
    await expect(sheet).toHaveAttribute("data-slack-copy-result", "rich");

    const clipboard = await page.evaluate(async () => {
      const [item] = await navigator.clipboard.read();
      const html = item.types.includes("text/html")
        ? await (await item.getType("text/html")).text()
        : "";
      const plain = item.types.includes("text/plain")
        ? await (await item.getType("text/plain")).text()
        : "";
      return { types: item.types, html, plain };
    });
    expect(clipboard.types).toContain("text/html");
    expect(clipboard.types).toContain("text/plain");
    expect(clipboard.html).toMatch(/<b[^>]*>Release ready<\/b>/i);
    expect(clipboard.html).toMatch(/<ol/i);
    expect(clipboard.plain).toContain("1. Deploy the API");
    expect(clipboard.plain).toContain("https://example.com/runbook");
  });

  test("offers a visible native-selection fallback without changing the viewport", async ({ page }) => {
    await openSlackCopy(page);
    const sheet = page.locator("[data-slack-copy-sheet]");
    const before = page.viewportSize();

    await sheet.locator("[data-slack-select-action]").click();
    await expect(sheet).toHaveAttribute("data-slack-copy-result", "selected");
    expect(page.viewportSize()).toEqual(before);
    await expect(sheet.locator("[data-slack-copy-surface]")).toBeInViewport();
  });
});

test("desktop Slack copy remains direct and does not open the Android sheet", async ({ context, page }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.setViewportSize({ width: 1365, height: 820 });
  await openSlackCopy(page);

  await expect(page.locator("[data-slack-copy-sheet]")).toHaveCount(0);
  const types = await page.evaluate(async () => {
    const [item] = await navigator.clipboard.read();
    return item.types;
  });
  expect(types).toContain("text/html");
  expect(types).toContain("text/plain");
});
