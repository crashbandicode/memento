// @ts-check
import { expect, test } from "@playwright/test";

import { FIXTURE_TOKEN, FIXTURE_USER } from "./fixtures/conversation-scenarios.mjs";
import { seedAuth } from "./support/conversation-page.mjs";

const JSON_HEADERS = { "content-type": "application/json" };
const now = Date.UTC(2026, 7, 21, 16);

const dashboard = {
  tools: [],
  recent_conversations: [],
  daily: [],
  tool_daily: {},
  devices: [],
  stats: {
    total_documents: 0,
    total_projects: 0,
    total_tools: 0,
    total_devices: 0,
    today_total: 0,
    today_conversations: 0,
  },
};

const projection = {
  daysLeft: 10,
  worst: { dollars: "$420.00", pctOfLimit: 84 },
  realistic: { dollars: "$330.00", pctOfLimit: 66 },
  average: { dollars: "$290.00", pctOfLimit: 58 },
};

const snapshot = {
  fetchedAt: new Date(now).toISOString(),
  purpose: "Read-only dashboard view.",
  ui: {
    defaultSource: "all",
    sources: [
      { id: "all", label: "All", stacked: false },
      { id: "claude", label: "Claude", stacked: true },
      { id: "cursor", label: "Cursor", stacked: true },
      { id: "codex", label: "Codex", stacked: true },
    ],
    modelBarColors: ["#F97316", "#8B5CF6", "#3B82F6"],
  },
  spend: {
    all: {
      used: "$180.00", limit: "$500.00", remaining: "$320.00",
      usedCents: 18000, limitCents: 50000, remainingCents: 32000, pctUsed: 36,
      parts: [
        { source: "claude", name: "Claude", used: "$90.00", limit: "$200.00", pctUsed: 45 },
        { source: "cursor", name: "Cursor", used: "$60.00", limit: "$200.00", pctUsed: 30 },
        { source: "codex", name: "Codex", used: "$30.00", limit: "$100.00", pctUsed: 30 },
      ],
    },
    claude: { used: "$90.00", limit: "$200.00", remaining: "$110.00", usedCents: 9000, limitCents: 20000, pctUsed: 45 },
    cursor: { used: "$60.00", limit: "$200.00", remaining: "$140.00", usedCents: 6000, limitCents: 20000, pctUsed: 30 },
    codex: { used: "$30.00", limit: "$100.00", remaining: "$70.00", usedCents: 3000, limitCents: 10000, pctUsed: 30 },
  },
  models: {
    claude: {
      coverage: "56/95",
      models: [
        { model: "Claude Opus 4.8", share: 70, totalCents: 6300 },
        { model: "Claude Sonnet 4.6", share: 30, totalCents: 2700 },
      ],
    },
    cursor: { models: [{ model: "Grok 4.5", share: 100, totalCents: 6000 }] },
    codex: { models: [] },
  },
  tools: {
    claude: { tools: [{ tool: "Read", share: 65, totalTokens: 125000 }, { tool: "Bash", share: 35, events: 42 }] },
    cursor: { tools: [{ tool: "Shell", share: 100, events: 20 }] },
    codex: { tools: [] },
  },
  projections: {
    all: { projection }, claude: { projection }, cursor: { projection }, codex: { projection },
  },
  history: {
    all: { points: [{ t: now - 172800000, u: 3000, l: 50000 }, { t: now - 86400000, u: 10000, l: 50000 }, { t: now, u: 18000, l: 50000 }] },
    claude: {
      stack: {
        traces: [
          { model: "opus", label: "Claude Opus 4.8", color: "#F97316", points: [{ t: now - 172800000, y0: 0, y1: 1000 }, { t: now - 86400000, y0: 0, y1: 4000 }, { t: now, y0: 0, y1: 6300 }] },
          { model: "sonnet", label: "Claude Sonnet 4.6", color: "#8B5CF6", points: [{ t: now - 172800000, y0: 1000, y1: 1800 }, { t: now - 86400000, y0: 4000, y1: 6000 }, { t: now, y0: 6300, y1: 9000 }] },
        ],
        outline: [{ t: now - 172800000, u: 1800 }, { t: now - 86400000, u: 6000 }, { t: now, u: 9000 }],
      },
    },
    cursor: { points: [{ t: now - 86400000, u: 2000 }, { t: now, u: 6000 }] },
    codex: { points: [] },
  },
};

async function installRoutes(page) {
  await seedAuth(page);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/api/auth/me") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: JSON.stringify(FIXTURE_USER) });
      return;
    }
    if (request.method() === "POST" && url.pathname === "/api/auth/refresh") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: JSON.stringify({ access_token: FIXTURE_TOKEN, user_id: FIXTURE_USER.id, role: FIXTURE_USER.role }) });
      return;
    }
    if (url.pathname === "/api/events/stream") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/api/hierarchy/devices") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: "[]" });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/api/dashboard/spend") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: JSON.stringify({ available: true, stale: false, cached_at: new Date(now).toISOString(), snapshot }) });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/api/dashboard") {
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: JSON.stringify(dashboard) });
      return;
    }
    await route.fulfill({ status: 200, headers: JSON_HEADERS, body: "{}" });
  });
}

async function assertSpendDashboard(page) {
  await page.goto("/app");
  await expect(page.getByRole("region", { name: "AI spend" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("img", { name: "all month-to-date spend history" })).toBeVisible();
  await expect(page.getByText("Provider quotas")).toBeVisible();

  await page.getByRole("tab", { name: "Claude" }).click();
  await expect(page.getByRole("img", { name: "claude month-to-date spend history" })).toBeVisible();
  await expect(page.getByText("Claude Opus 4.8").first()).toBeVisible();
  await page.getByText("Model and tool detail").click();
  await expect(page.getByText("56/95 coverage", { exact: false })).toBeVisible();
  await expect(page.getByText("125K tokens")).toBeVisible();

  await page.getByRole("tab", { name: "Codex" }).click();
  await expect(page.getByText("Codex history will appear after its analytics cache has a session mix.")).toBeVisible();
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true);
}

test("spend snapshot paints supplied bands and remains coherent on desktop", async ({ page }) => {
  await installRoutes(page);
  await assertSpendDashboard(page);
});

test("spend snapshot remains usable at an Android viewport", async ({ browser }) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    userAgent: "Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 Chrome/138 Mobile Safari/537.36",
  });
  const page = await context.newPage();
  await installRoutes(page);
  await assertSpendDashboard(page);
  await context.close();
});
