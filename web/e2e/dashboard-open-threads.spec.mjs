// @ts-check
import { expect, test } from "@playwright/test";

import { FIXTURE_TOKEN, FIXTURE_USER } from "./fixtures/conversation-scenarios.mjs";
import { seedAuth } from "./support/conversation-page.mjs";

const dashboard = {
  tools: [],
  recent_conversations: [],
  daily: [
    { date: "2026-09-01", count: 3 },
    { date: "2026-09-02", count: 5 },
  ],
  tool_daily: {},
  devices: [],
  open_thread_active_minutes: 20,
  open_threads_truncated: false,
  open_thread_groups: [
    {
      machine: {
        id: "machine-yoga",
        name: "Yoga",
        host: "yoga",
        platform: "Windows",
      },
      threads: [
        {
          document_id: "doc-codex",
          tool_id: "codex",
          title: "Fix Memento open thread dashboard",
          canonical_url: "/conversations/codex/01a-open-thread",
          resume_id: "01a-open-thread",
          resume_command: "Set-Location -LiteralPath 'C:\\Users\\intpa\\memento'; codex resume '01a-open-thread'",
          location: {
            host: "yoga",
            path: "C:\\Users\\intpa\\memento",
            platform: "Windows",
          },
          activity_at: "2026-09-07T13:00:00.000Z",
          pinned: false,
          health: {
            available: true,
            source: "codex",
            ratio: 330,
            status: "warn",
            hop_line: 325,
            hard_line: 575,
          },
        },
      ],
    },
    {
      machine: {
        id: "machine-butter",
        name: "Butter Bridge",
        host: "butter-bridge",
        platform: "Linux",
      },
      threads: [
        {
          document_id: "doc-claude",
          tool_id: "claude_code",
          title: "Collector follow-up",
          canonical_url: "/conversations/claude/b064-open-thread",
          resume_id: "b064-open-thread",
          resume_command: "cd -- '/home/patrick/memento' && claude --resume 'b064-open-thread'",
          location: {
            host: "butter-bridge",
            path: "/home/patrick/memento",
            platform: "Linux",
          },
          activity_at: "2026-09-07T12:30:00.000Z",
          pinned: true,
          health: null,
        },
      ],
    },
  ],
  stats: {
    total_documents: 2,
    total_projects: 1,
    total_tools: 2,
    total_devices: 2,
    today_total: 2,
    today_conversations: 2,
  },
};

test("dashboard replaces weekly activity with grouped open threads and thread actions", async ({ page }) => {
  await seedAuth(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value) => {
          window.__copiedResumeCommand = value;
        },
      },
    });
  });

  const pinRequests = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "GET" && pathname === "/api/auth/me") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(FIXTURE_USER) });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/auth/refresh") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ access_token: FIXTURE_TOKEN, token_type: "bearer", user_id: FIXTURE_USER.id, role: FIXTURE_USER.role }),
      });
      return;
    }
    if (request.method() === "GET" && /\/api\/(?:hierarchy\/)?devices\/?$/.test(pathname)) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (pathname === "/api/events/stream") {
      await route.abort();
      return;
    }
    if (request.method() === "GET" && pathname === "/api/events/session") {
      await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
      return;
    }
    if (request.method() === "GET" && pathname === "/api/dashboard") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(dashboard),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api/conversations/doc-codex/pin") {
      pinRequests.push({ method: request.method(), pathname });
      await route.fulfill({ status: 204 });
      return;
    }
    if (request.method() === "DELETE" && pathname === "/api/conversations/doc-claude/pin") {
      pinRequests.push({ method: request.method(), pathname });
      await route.fulfill({ status: 204 });
      return;
    }
    if (request.method() === "GET" && pathname === "/api/dashboard/spend") {
      await route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto("/app");

  await expect(page.getByText("Open threads", { exact: true })).toBeVisible();
  await expect(page.getByText("Running sessions, activity in the last 20 minutes, and pinned threads", { exact: true })).toBeVisible();
  await expect(page.getByText("Last 7 Days", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Yoga", { exact: true })).toBeVisible();
  await expect(page.getByText("Butter Bridge", { exact: true })).toBeVisible();

  const codexThread = page.locator('[data-open-thread="doc-codex"]');
  const claudeThread = page.locator('[data-open-thread="doc-claude"]');
  await expect(codexThread.getByRole("link", { name: "Fix Memento open thread dashboard" }))
    .toHaveAttribute("href", "/conversations/codex/01a-open-thread");
  await expect(codexThread.getByText("330:1 · Handoff soon", { exact: true })).toBeVisible();
  await expect(claudeThread.getByText("Health not reported", { exact: true })).toBeVisible();

  await codexThread.getByRole("button", { name: "Copy resume command" }).click();
  await expect.poll(() => page.evaluate(() => window.__copiedResumeCommand)).toBe(
    "Set-Location -LiteralPath 'C:\\Users\\intpa\\memento'; codex resume '01a-open-thread'",
  );

  const codexPin = codexThread.getByRole("button", { name: "Pin thread" });
  await expect(codexPin).toHaveAttribute("aria-pressed", "false");
  await codexPin.click();
  await expect(codexThread.getByRole("button", { name: "Unpin thread" }))
    .toHaveAttribute("aria-pressed", "true");

  const claudePin = claudeThread.getByRole("button", { name: "Unpin thread" });
  await claudePin.click();
  await expect(claudeThread.getByRole("button", { name: "Pin thread" }))
    .toHaveAttribute("aria-pressed", "false");

  expect(pinRequests).toEqual([
    { method: "POST", pathname: "/api/conversations/doc-codex/pin" },
    { method: "DELETE", pathname: "/api/conversations/doc-claude/pin" },
  ]);
});
