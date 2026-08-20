// @ts-check
import { expect, test } from "@playwright/test";
import path from "node:path";

import { urlNavigationLargeThread } from "./fixtures/conversation-scenarios.mjs";
import {
  installConversationMocks,
  seedAuth,
} from "./support/conversation-page.mjs";

const nativeId = "018d5532-7d42-7b44-b2da-d4ca8b4f70e1";
const canonicalPath = `/conversations/codex/${nativeId}`;
const windowsProjectPath = "C:\\Users\\intpa\\lsf-data-api-simplified";
const scenario = {
  ...urlNavigationLargeThread,
  meta: {
    ...urlNavigationLargeThread.meta,
    tool_id: "codex",
    native_id: nativeId,
    resume_id: nativeId,
    canonical_url: canonicalPath,
    location: {
      host: "dreamland-yoga",
      path: windowsProjectPath,
      platform: "Windows",
    },
  },
};
const artifactDir = path.resolve(
  process.cwd(),
  "..",
  "artifacts",
  "native-conversation-url",
);

for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`legacy document links canonicalize and reload by native id (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await seedAuth(page);
    await installConversationMocks(page, scenario);
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (value) => {
            (/** @type {any} */ (window)).__copiedResumeCommand = value;
          },
        },
      });
    });

    const legacyPath = `/conversations/${scenario.docId}`;
    await page.goto(`${legacyPath}?unrelated=keep#native-id-check`);
    const historyLength = await page.evaluate(() => history.length);

    await expect(page.locator("[data-conversation-viewer]")).toBeVisible();
    await expect.poll(() => new URL(page.url()).pathname).toBe(canonicalPath);
    expect(new URL(page.url()).searchParams.get("unrelated")).toBe("keep");
    expect(new URL(page.url()).hash).toBe("#native-id-check");
    expect(await page.evaluate(() => history.length)).toBe(historyLength);

    await page.reload();
    await expect(page.locator("[data-conversation-viewer]")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe(canonicalPath);
    await expect(page.getByRole("heading", {
      name: "Large URL navigation regression",
    })).toBeVisible();

    const resume = page.locator("[data-resume-command]");
    await expect(resume).toBeVisible();
    await expect(resume.getByText("Resume with", { exact: true })).toBeVisible();
    await expect(resume.getByRole("tab", { name: "PowerShell" })).toHaveAttribute("aria-selected", "true");
    const command = resume.locator("[data-resume-command-value]");
    const powerShellCommand = `Set-Location -LiteralPath '${windowsProjectPath}' && codex resume '${nativeId}'`;
    await expect(command).toHaveAttribute("data-resume-command-value", powerShellCommand);

    await resume.getByRole("tab", { name: "WSL2 Bash" }).click();
    const bashCommand = `cd '/mnt/c/Users/intpa/lsf-data-api-simplified' && codex resume '${nativeId}'`;
    await expect(command).toHaveAttribute("data-resume-command-value", bashCommand);
    await resume.getByRole("button", { name: "Copy resume command" }).click();
    await expect(resume.locator("[data-copy-status]"))
      .toHaveAttribute("data-copy-status", "copied");
    expect(await page.evaluate(() => (/** @type {any} */ (window)).__copiedResumeCommand))
      .toBe(bashCommand);

    await page.screenshot({
      path: path.join(artifactDir, `${viewport.name}-native-url.png`),
      fullPage: true,
    });
  });
}

test("native Linux resume commands keep POSIX paths and a Bash default", async ({ page }) => {
  const linuxProjectPath = "/home/intpa/lsf-data-api-simplified";
  const linuxScenario = {
    ...scenario,
    meta: {
      ...scenario.meta,
      location: {
        host: "dreamland-yoga",
        path: linuxProjectPath,
        platform: "Linux",
      },
    },
  };
  await seedAuth(page);
  await installConversationMocks(page, linuxScenario);
  await page.goto(`/conversations/${scenario.docId}`);

  const resume = page.locator("[data-resume-command]");
  await expect(resume).toBeVisible();
  await expect(resume.getByRole("tab", { name: "Bash", exact: true }))
    .toHaveAttribute("aria-selected", "true");
  await expect(resume.getByRole("tab", { name: "WSL2 Bash" })).toHaveCount(0);
  await expect(resume.locator("[data-resume-command-value]")).toHaveAttribute(
    "data-resume-command-value",
    `cd '${linuxProjectPath}' && codex resume '${nativeId}'`,
  );
});
