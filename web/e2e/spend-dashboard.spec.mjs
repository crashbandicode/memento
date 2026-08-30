// @ts-check
import { expect, test } from "@playwright/test";

import { FIXTURE_TOKEN, FIXTURE_USER } from "./fixtures/conversation-scenarios.mjs";
import { seedAuth } from "./support/conversation-page.mjs";

const JSON_HEADERS = { "content-type": "application/json" };
const now = Date.UTC(2026, 7, 21, 16);
const at = (offset) => new Date(now + offset).toISOString();

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
  current: 18000,
  limit: 50000,
  worst: { cents: 62000, dollars: "$620.00", pctOfLimit: 124, hits100Pct: at(7 * 86400000) },
  realistic: { cents: 54000, dollars: "$540.00", pctOfLimit: 108, hits100Pct: at(9 * 86400000) },
  average: { cents: 42000, dollars: "$420.00", pctOfLimit: 84 },
};

const snapshot = {
  fetchedAt: new Date(now).toISOString(),
  purpose: "Read-only dashboard view.",
  ui: {
    defaultSource: "all",
    defaultRange: "mtd",
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
      billingCycleStart: at(-20 * 86400000), resetsAt: at(10 * 86400000),
      // Real snapshot contract: parts use id/label/usageCents (no source/used/
      // pctUsed) and carry the ACCOUNT name, which must not replace the label.
      parts: [
        { id: "claude", label: "Claude", authenticated: true, unit: "cents", usageCents: 9000, limitCents: 20000, remainingCents: 11000, name: "Account One", email: "claude@example.test" },
        { id: "cursor", label: "Cursor", authenticated: true, unit: "cents", usageCents: 6000, limitCents: 20000, remainingCents: 14000, name: "Account Two", email: "claude@example.test" },
        { id: "codex", label: "Codex", authenticated: true, unit: "cents", usageCents: 3000, limitCents: 10000, remainingCents: 7000, name: "account-three", email: "claude@example.test" },
      ],
    },
    claude: { name: "Claude", email: "claude@example.test", used: "$90.00", limit: "$200.00", remaining: "$110.00", usedCents: 9000, limitCents: 20000, pctUsed: 45, billingCycleStart: at(-20 * 86400000), resetsAt: at(10 * 86400000) },
    cursor: { used: "$60.00", limit: "$200.00", remaining: "$140.00", usedCents: 6000, limitCents: 20000, pctUsed: 30, billingCycleStart: at(-20 * 86400000), resetsAt: at(10 * 86400000) },
    codex: { used: "$30.00", limit: "$100.00", remaining: "$70.00", usedCents: 3000, limitCents: 10000, pctUsed: 30, billingCycleStart: at(-20 * 86400000), resetsAt: at(10 * 86400000) },
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
    // These fields mirror getHistoryView() in the source dashboard exactly:
    // modelSeries has layers/totals/days/chart; cursor tokenLedger has a
    // timezone and days with key/t/input/output/cacheRead/cacheWrite/total.
    all: { points: [{ t: at(-172800000), u: 3000, l: 50000 }, { t: at(-86400000), u: 10000, l: 50000 }, { t: at(0), u: 18000, l: 50000 }] },
    claude: {
      modelSeries: {
        layers: ["claude-opus-4-8", "claude-sonnet-4-6"],
        totals: { "claude-opus-4-8": 6300, "claude-sonnet-4-6": 2700 },
        days: [
          { t: at(-172800000), u: 1800, l: 20000, models: { "claude-opus-4-8": 1000, "claude-sonnet-4-6": 800 }, modelCum: { "claude-opus-4-8": 1000, "claude-sonnet-4-6": 800 } },
          { t: at(-86400000), u: 6000, l: 20000, models: { "claude-opus-4-8": 3000, "claude-sonnet-4-6": 1200 }, modelCum: { "claude-opus-4-8": 4000, "claude-sonnet-4-6": 2000 } },
          { t: at(0), u: 9000, l: 20000, models: { "claude-opus-4-8": 2300, "claude-sonnet-4-6": 700 }, modelCum: { "claude-opus-4-8": 6300, "claude-sonnet-4-6": 2700 } },
        ],
        chart: [
          { t: at(-172800000), u: 1800, l: 20000, models: { "claude-opus-4-8": 1000, "claude-sonnet-4-6": 800 }, modelCum: { "claude-opus-4-8": 1000, "claude-sonnet-4-6": 800 } },
          { t: at(-86400000), u: 6000, l: 20000, models: { "claude-opus-4-8": 3000, "claude-sonnet-4-6": 1200 }, modelCum: { "claude-opus-4-8": 4000, "claude-sonnet-4-6": 2000 } },
          { t: at(0), u: 9000, l: 20000, models: { "claude-opus-4-8": 2300, "claude-sonnet-4-6": 700 }, modelCum: { "claude-opus-4-8": 6300, "claude-sonnet-4-6": 2700 } },
        ],
      },
      stack: {
        traces: [
          { model: "claude-opus-4-8", label: "Claude Opus 4.8", color: "#F97316", points: [{ t: at(-172800000), y0: 0, y1: 1000 }, { t: at(-86400000), y0: 0, y1: 4000 }, { t: at(0), y0: 0, y1: 6300 }] },
          { model: "claude-sonnet-4-6", label: "Claude Sonnet 4.6", color: "#8B5CF6", points: [{ t: at(-172800000), y0: 1000, y1: 1800 }, { t: at(-86400000), y0: 4000, y1: 6000 }, { t: at(0), y0: 6300, y1: 9000 }] },
        ],
        outline: [{ t: at(-172800000), u: 1800, l: 20000 }, { t: at(-86400000), u: 6000, l: 20000 }, { t: at(0), u: 9000, l: 20000 }],
      },
    },
    cursor: {
      points: [{ t: now - 86400000, u: 2000, l: 20000 }, { t: now, u: 6000, l: 20000 }],
      tokenLedger: {
        timezone: "America/New_York",
        days: [
          { key: "2026-08-20", t: at(-86400000), input: 1200000, output: 3400000, cacheRead: 400000, cacheWrite: 200000, total: 5200000 },
          { key: "2026-08-21", t: at(0), input: 5600, output: 3400, cacheRead: 1200, cacheWrite: 800, total: 11000 },
        ],
      },
    },
    codex: {
      points: [{ t: at(-86400000), u: 1000, l: 10000 }, { t: at(0), u: 3000, l: 10000 }],
      modelSeries: {
        layers: ["gpt-5.6", "gpt-5.6-codex"],
        totals: { "gpt-5.6": 2200, "gpt-5.6-codex": 800 },
        days: [
          { t: at(-86400000), u: 1000, l: 10000, models: { "gpt-5.6": 700, "gpt-5.6-codex": 300 }, modelCum: { "gpt-5.6": 700, "gpt-5.6-codex": 300 } },
          { t: at(0), u: 3000, l: 10000, models: { "gpt-5.6": 1500, "gpt-5.6-codex": 500 }, modelCum: { "gpt-5.6": 2200, "gpt-5.6-codex": 800 } },
        ],
        chart: [
          { t: at(-86400000), u: 1000, l: 10000, models: { "gpt-5.6": 700, "gpt-5.6-codex": 300 }, modelCum: { "gpt-5.6": 700, "gpt-5.6-codex": 300 } },
          { t: at(0), u: 3000, l: 10000, models: { "gpt-5.6": 1500, "gpt-5.6-codex": 500 }, modelCum: { "gpt-5.6": 2200, "gpt-5.6-codex": 800 } },
        ],
      },
      stack: {
        traces: [
          { model: "gpt-5.6", label: "gpt-5.6", color: "#3B82F6", points: [{ t: at(-86400000), y0: 0, y1: 700 }, { t: at(0), y0: 0, y1: 2200 }] },
          { model: "gpt-5.6-codex", label: "gpt-5.6-codex", color: "#8B5CF6", points: [{ t: at(-86400000), y0: 700, y1: 1000 }, { t: at(0), y0: 2200, y1: 3000 }] },
        ],
        outline: [{ t: at(-86400000), u: 1000, l: 10000 }, { t: at(0), u: 3000, l: 10000 }],
      },
    },
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
  await expect(page.getByText("AI quota left")).toBeVisible();
  await expect(page.getByRole("button", { name: "MTD" })).toHaveClass(/active/);
  await expect(page.getByRole("button", { name: "Projection" })).toHaveClass(/active/);

  // Combined provider rows must show provider labels (not account names) with
  // real used amounts derived from usageCents/limitCents.
  const providerRows = page.locator(".spend-provider");
  await expect(providerRows).toHaveCount(3);
  await expect(providerRows.filter({ hasText: "Claude" })).toContainText("$90 of $200");
  await expect(providerRows.filter({ hasText: "Cursor" })).toContainText("$60 of $200");
  await expect(providerRows.filter({ hasText: "Codex" })).toContainText("$30 of $100");
  await expect(providerRows.filter({ hasText: "Account One" })).toHaveCount(0);

  await page.getByRole("tab", { name: "Claude" }).click();
  await expect(page.getByRole("img", { name: "claude month-to-date spend history" })).toBeVisible();
  await expect(page.getByText("Claude Opus 4.8").first()).toBeVisible();
  await expect(page.getByText("Models this cycle")).toBeVisible();
  await expect(page.getByText("56/95 coverage", { exact: false })).toBeVisible();
  await expect(page.getByText("125K tok", { exact: false })).toBeVisible();
  await expect(page.locator('svg[aria-label="claude month-to-date spend history"] path')).not.toHaveCount(0);

  await page.getByRole("tab", { name: "Codex" }).click();
  await expect(page.getByRole("img", { name: "codex month-to-date spend history" })).toBeVisible();
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true);
}

async function revealSpendTooltip(page, source, interaction) {
  await page.getByRole("tab", { name: source, exact: true }).click();
  const chart = page.getByRole("img", { name: `${source.toLowerCase()} month-to-date spend history` });
  const box = await chart.boundingBox();
  if (!box) throw new Error("Spend chart has no bounding box");
  const position = { x: box.width * 0.9, y: box.height * 0.45 };
  if (interaction === "tap") await chart.tap({ position });
  else await chart.hover({ position });
  const tooltip = page.getByTestId("spend-tooltip");
  await expect(tooltip).toBeVisible();
  return tooltip;
}

async function assertTooltipParity(page, interaction) {
  // `all` intentionally has no modelSeries or tokenLedger. It must retain the
  // common spend/cumulative/limit tooltip without data-section placeholders.
  let tooltip = await revealSpendTooltip(page, "All", interaction);
  await expect(tooltip).toContainText("This day");
  await expect(tooltip).toContainText("$80.00");
  await expect(tooltip).toContainText("Cumulative");
  await expect(tooltip).toContainText("$180.00");
  await expect(tooltip).toContainText("of limit");
  await expect(tooltip).toContainText("36.0%");
  await expect(tooltip.getByText("Daily by model")).toHaveCount(0);
  await expect(tooltip.getByText("By model")).toHaveCount(0);
  await expect(tooltip.getByText("Daily tokens")).toHaveCount(0);

  tooltip = await revealSpendTooltip(page, "Claude", interaction);
  await expect(tooltip).toContainText("This day");
  await expect(tooltip).toContainText("Daily by model");
  await expect(tooltip).toContainText("opus-4-8");
  await expect(tooltip).toContainText("$23.00");
  await expect(tooltip).toContainText("76.7%");
  await expect(tooltip).toContainText("Daily total");
  await expect(tooltip).toContainText("$30.00");
  await expect(tooltip).toContainText("Cumulative");
  await expect(tooltip).toContainText("$90.00");
  await expect(tooltip).toContainText("45.0%");

  tooltip = await revealSpendTooltip(page, "Cursor", interaction);
  await expect(tooltip).toContainText("Daily tokens");
  await expect(tooltip).toContainText("Generated");
  await expect(tooltip).toContainText("3.4k");
  await expect(tooltip).toContainText("Uncached in");
  await expect(tooltip).toContainText("5.6k");
  await expect(tooltip).toContainText("Cache read");
  await expect(tooltip).toContainText("Cache write");
  await expect(tooltip).toContainText("Token total");
  await expect(tooltip).toContainText("11.0k");
  await expect(tooltip).toContainText("Cumulative");
  await expect(tooltip).toContainText("$60.00");
  await expect(tooltip).toContainText("30.0%");

  tooltip = await revealSpendTooltip(page, "Codex", interaction);
  await expect(tooltip).toContainText("By model");
  await expect(tooltip).toContainText("5.6-codex");
  await expect(tooltip).toContainText("Daily total");
  await expect(tooltip).toContainText("$20.00");
  await expect(tooltip).toContainText("Cumulative");
  await expect(tooltip).toContainText("$30.00");
  await expect(tooltip).toContainText("30.0%");
}

test("spend snapshot paints supplied bands and remains coherent on desktop", async ({ page }) => {
  await installRoutes(page);
  await assertSpendDashboard(page);
  await assertTooltipParity(page, "hover");

  for (const source of ["Claude", "Cursor", "Codex"]) {
    await page.getByRole("tab", { name: source, exact: true }).click();
    const projectionRays = page.locator(`svg[aria-label="${source.toLowerCase()} month-to-date spend history"] .spend-projection-rays line`);
    await expect(projectionRays).toHaveCount(3);
    const coordinates = await projectionRays.evaluateAll((lines) => lines.map((line) => ({
      x1: Number(line.getAttribute("x1")),
      x2: Number(line.getAttribute("x2")),
    })));
    expect(coordinates.every(({ x1, x2 }) => Number.isFinite(x1) && Number.isFinite(x2) && x1 < x2)).toBe(true);
  }
});

test("spend snapshot remains usable at an Android viewport", async ({ browser }) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    userAgent: "Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 Chrome/138 Mobile Safari/537.36",
  });
  const page = await context.newPage();
  await installRoutes(page);
  await assertSpendDashboard(page);
  await page.getByRole("tab", { name: "Claude", exact: true }).click();
  await expect(page.locator('svg[aria-label="claude month-to-date spend history"] .spend-projection-rays')).toHaveCount(0);
  await assertTooltipParity(page, "tap");
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true);
  await context.close();
});
