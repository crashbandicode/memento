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

const projectionMath = {
  nowMs: now,
  endMs: now + 10 * 86400000,
  remDays: 10,
  fullDaysLeft: 8,
  currentCents: 18000,
  limitCents: 50000,
  peakDayCents: 4400,
  avgDayCents: 3000,
  remainingTodayRealCents: 800,
  remainingTodayAvgCents: 0,
  // Contract acceptance identities: current + rate * days (+ today) = end.
  worstEndCents: 62000,
  realEndCents: 54000,
  avgEndCents: 42000,
  worstCrossAtMs: now + (32000 / 4400) * 86400000,
  realCrossAtMs: now + (31200 / 4400) * 86400000,
  avgCrossAtMs: null,
};

const claudeProjection = {
  daysLeft: 10,
  current: 9000,
  limit: 20000,
  worst: { cents: 39000, dollars: "$390.00", pctOfLimit: 195, hits100Pct: at(4 * 86400000) },
  realistic: { cents: 33000, dollars: "$330.00", pctOfLimit: 165, hits100Pct: at(4 * 86400000) },
  average: { cents: 25000, dollars: "$250.00", pctOfLimit: 125, hits100Pct: at(6 * 86400000) },
  math: {
    nowMs: now,
    endMs: now + 10 * 86400000,
    remDays: 10,
    fullDaysLeft: 8,
    currentCents: 9000,
    limitCents: 20000,
    peakDayCents: 3000,
    avgDayCents: 2000,
    remainingTodayRealCents: 0,
    remainingTodayAvgCents: 0,
    worstEndCents: 39000,
    realEndCents: 33000,
    avgEndCents: 25000,
    worstCrossAtMs: now + (11000 / 3000) * 86400000,
    realCrossAtMs: now + (11000 / 3000) * 86400000,
    avgCrossAtMs: now + (11000 / 2000) * 86400000,
  },
};

const hygiene = {
  source: "all",
  available: true,
  status: "crit",
  hopLine: 200,
  hardLine: 400,
  metric: "cache-read / generated",
  parts: [
    {
      source: "claude",
      available: true,
      status: "ok",
      shown: { dayKey: "2026-08-21", partial: true, cacheRead: 45_468_162, generated: 488_407, ratio: 93, status: "ok" },
    },
    {
      source: "cursor",
      available: true,
      status: "warn",
      shown: { dayKey: "2026-08-21", partial: true, cacheRead: 16_042_610, generated: 73_476, ratio: 218, status: "warn" },
    },
    {
      source: "codex",
      available: true,
      status: "crit",
      usingYesterday: true,
      shown: { dayKey: "2026-08-20", partial: false, cacheRead: 69_156_608, generated: 163_210, ratio: 424, status: "crit" },
    },
  ],
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
    all: { projection: { ...projection, math: projectionMath } },
    claude: { projection: claudeProjection },
    // Cursor deliberately has no math block to exercise safe rollout fallback.
    cursor: { projection },
    codex: { projection },
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
  hygiene,
};

async function installRoutes(page, spendSnapshot = snapshot) {
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
      await route.fulfill({ status: 200, headers: JSON_HEADERS, body: JSON.stringify({ available: true, stale: false, cached_at: new Date(now).toISOString(), snapshot: spendSnapshot }) });
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

async function expectChartTooltipAboveHoveredPoint(page) {
  const [tooltipBox, dotBox] = await Promise.all([
    page.getByTestId("spend-tooltip").boundingBox(),
    page.getByTestId("spend-tooltip-dot").boundingBox(),
  ]);
  if (!tooltipBox || !dotBox) throw new Error("Spend tooltip or hover point has no bounding box");
  expect(tooltipBox.y + tooltipBox.height).toBeLessThanOrEqual(dotBox.y + dotBox.height / 2);
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
  await expectChartTooltipAboveHoveredPoint(page);

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

async function assertChartTouchDismissal(page) {
  await revealSpendTooltip(page, "All", "tap");
  await page.evaluate(() => window.scrollBy(0, 1));
  await expect(page.getByTestId("spend-tooltip")).toHaveCount(0);

  await revealSpendTooltip(page, "All", "tap");
  await page.waitForTimeout(1900);
  await expect(page.getByTestId("spend-tooltip")).toHaveCount(0);
}

async function trendProjectionRows(page) {
  const rows = page.locator(".spend-trend .spend-projection-rows");
  await expect(rows).toBeVisible();
  return rows;
}

async function assertProjectionRaysReachChartEnd(page, source) {
  const chart = page.locator(`svg[aria-label="${source.toLowerCase()} month-to-date spend history"]`);
  const projectionRays = chart.locator(".spend-projection-rays line");
  await expect(projectionRays).toHaveCount(3);
  const geometry = await chart.evaluate((svg) => {
    const plotRight = svg.viewBox.baseVal.x + svg.viewBox.baseVal.width - 18;
    return {
      plotRight,
      rays: Array.from(svg.querySelectorAll(".spend-projection-rays line")).map((line) => {
        const styles = window.getComputedStyle(line);
        const bounds = line.getBoundingClientRect();
        return {
          x1: Number(line.getAttribute("x1")),
          x2: Number(line.getAttribute("x2")),
          width: bounds.width,
          display: styles.display,
          visibility: styles.visibility,
          opacity: Number(styles.opacity),
        };
      }),
    };
  });

  for (const ray of geometry.rays) {
    expect(ray.x1).toBeLessThan(ray.x2);
    expect(ray.x2).toBeCloseTo(geometry.plotRight, 3);
    expect(ray.width).toBeGreaterThan(0);
    expect(ray.display).not.toBe("none");
    expect(ray.visibility).not.toBe("hidden");
    expect(ray.opacity).toBeGreaterThan(0);
  }
}

async function assertProjectionMathDesktop(page) {
  await page.getByRole("tab", { name: "All", exact: true }).click();
  const rows = await trendProjectionRows(page);
  const worst = rows.locator(".spend-projection-row").filter({ hasText: "Worst" });
  const tooltip = page.getByTestId("projection-math-tooltip");

  await worst.hover();
  await expect(tooltip).toContainText("Worst projection");
  await expect(tooltip).toContainText("Assumes every remaining day");
  await expect(tooltip).toContainText("$180.00 now + $44.00/day × 10.0 days left = $620");

  // The same content must be available to keyboard users, not only hover.
  await page.mouse.move(0, 0);
  await expect(tooltip).toHaveCount(0);
  await worst.focus();
  await expect(tooltip).toContainText("Worst projection");

  // The cursor fixture has no additive math contract yet: retain the existing
  // projection rows without focus/hover bindings or runtime console errors.
  const consoleErrors = [];
  const onConsole = (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  };
  page.on("console", onConsole);
  await page.getByRole("tab", { name: "Cursor", exact: true }).click();
  const cursorRows = await trendProjectionRows(page);
  await expect(cursorRows.locator(".spend-projection-row-interactive")).toHaveCount(0);
  await cursorRows.locator(".spend-projection-row").first().hover();
  await expect(page.getByTestId("projection-math-tooltip")).toHaveCount(0);
  page.off("console", onConsole);
  expect(consoleErrors).toEqual([]);
}

async function assertProjectionMathMobile(page) {
  await page.getByRole("tab", { name: "All", exact: true }).click();
  const rows = await trendProjectionRows(page);
  const worst = rows.locator(".spend-projection-row").filter({ hasText: "Worst" });
  const tooltip = page.getByTestId("projection-math-tooltip");

  await worst.tap();
  await expect(tooltip).toContainText("Worst projection");
  await expect(tooltip).toContainText("$180.00 now + $44.00/day × 10.0 days left = $620");
  await worst.tap();
  await expect(tooltip).toHaveCount(0);
}

test("spend snapshot paints supplied bands and remains coherent on desktop", async ({ page }) => {
  await installRoutes(page);
  await assertSpendDashboard(page);
  await assertTooltipParity(page, "hover");
  await assertProjectionMathDesktop(page);

  for (const source of ["Claude", "Cursor", "Codex"]) {
    await page.getByRole("tab", { name: source, exact: true }).click();
    await assertProjectionRaysReachChartEnd(page, source);
  }
});

test("projection math tooltip stays opaque and keeps thousands-scale precision", async ({ page }) => {
  const largeMath = {
    ...projectionMath,
    currentCents: 1_618_474,
    limitCents: 1_700_000,
    peakDayCents: 152_949,
    worstEndCents: 1_767_300,
  };
  const largeSnapshot = {
    ...snapshot,
    projections: {
      ...snapshot.projections,
      all: {
        projection: {
          ...snapshot.projections.all.projection,
          math: largeMath,
        },
      },
    },
  };
  await installRoutes(page, largeSnapshot);
  await page.goto("/app");
  await expect(page.getByRole("region", { name: "AI spend" })).toBeVisible();
  const rows = await trendProjectionRows(page);
  const worst = rows.locator(".spend-projection-row").filter({ hasText: "Worst" });
  const tooltip = page.getByTestId("projection-math-tooltip");

  await worst.hover();
  await expect(tooltip).toContainText("$16,184.74 now + $1,529.49/day × 10.0 days left = $17,673");
  await expect(tooltip).toContainText("Reaches $17,000");
  await expect(tooltip).toContainText("$16,184.74 now + $1,529.49/day × 0.53 days = $17,000");
  await expect(tooltip).toContainText("Hits 100%");

  for (const theme of ["light", "dark"]) {
    await page.evaluate((value) => {
      document.documentElement.dataset.theme = value;
    }, theme);
    const presentation = await tooltip.evaluate((element) => {
      const parse = (value) => {
        const channels = value.match(/rgba?\(([^)]+)\)/)?.[1].split(",").map(Number);
        if (!channels || channels.length < 3) throw new Error(`Unexpected CSS color: ${value}`);
        return { red: channels[0], green: channels[1], blue: channels[2], alpha: channels[3] ?? 1 };
      };
      const luminance = ({ red, green, blue }) => [red, green, blue]
        .map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.03928
            ? normalized / 12.92
            : ((normalized + 0.055) / 1.055) ** 2.4;
        })
        .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
      const background = parse(window.getComputedStyle(element).backgroundColor);
      const foreground = parse(window.getComputedStyle(element.querySelector("p")).color);
      const [light, dark] = [luminance(background), luminance(foreground)].sort((a, b) => b - a);
      return { alpha: background.alpha, contrast: (light + 0.05) / (dark + 0.05) };
    });
    expect(presentation.alpha).toBe(1);
    expect(presentation.contrast).toBeGreaterThanOrEqual(4.5);
  }
});

test("handoff hygiene follows the selected spend source", async ({ page }) => {
  await installRoutes(page);
  await page.goto("/app");
  await expect(page.getByRole("region", { name: "AI spend" })).toBeVisible();

  const panel = page.getByRole("region", { name: "Handoff hygiene" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText("cache-read / generated · hop at 200:1")).toBeVisible();
  await expect(panel.locator(".spend-hygiene-card")).toHaveCount(3);
  await expect(panel.getByText("93:1", { exact: true })).toBeVisible();
  await expect(panel.getByText("218:1", { exact: true })).toBeVisible();
  await expect(panel.getByText("424:1", { exact: true })).toBeVisible();
  await expect(panel.locator('.spend-hygiene-card[data-status="crit"]')).toContainText("Codex");
  await expect(panel.getByText("yesterday", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Claude", exact: true }).click();
  await expect(panel.locator(".spend-hygiene-card")).toHaveCount(1);
  await expect(panel).toContainText("Claude");
  await expect(panel).toContainText("93:1");
  await expect(panel).not.toContainText("Cursor");
});

test("projection rays and hygiene stay responsive across chart widths", async ({ page }) => {
  await installRoutes(page);
  await page.goto("/app");
  await expect(page.getByRole("region", { name: "AI spend" })).toBeVisible();
  const panel = page.getByRole("region", { name: "Handoff hygiene" });

  for (const width of [390, 540, 760, 900]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole("tab", { name: "All", exact: true }).click();
    await assertProjectionRaysReachChartEnd(page, "All");
    await expect(panel.locator(".spend-hygiene-card")).toHaveCount(3);
    const layout = await panel.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return {
        left: bounds.left,
        right: bounds.right,
        viewportWidth: window.innerWidth,
        cards: Array.from(element.querySelectorAll(".spend-hygiene-card")).map((card) => {
          const rect = card.getBoundingClientRect();
          return { left: rect.left, right: rect.right, width: rect.width };
        }),
        documentOverflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });
    expect(layout.left).toBeGreaterThanOrEqual(0);
    expect(layout.right).toBeLessThanOrEqual(layout.viewportWidth);
    expect(layout.documentOverflow).toBe(false);
    for (const card of layout.cards) {
      expect(card.width).toBeGreaterThan(0);
      expect(card.left).toBeGreaterThanOrEqual(layout.left);
      expect(card.right).toBeLessThanOrEqual(layout.right);
    }
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
  await assertProjectionRaysReachChartEnd(page, "Claude");
  await assertTooltipParity(page, "tap");
  await assertChartTouchDismissal(page);
  await assertProjectionMathMobile(page);
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true);
  await context.close();
});

test("long Claude projection tooltip stacks cleanly on mobile", async ({ browser }) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    userAgent: "Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 Chrome/138 Mobile Safari/537.36",
  });
  const page = await context.newPage();
  const longClaudeMath = {
    ...claudeProjection.math,
    currentCents: 1_618_474,
    limitCents: 1_700_000,
    peakDayCents: 152_949,
    worstEndCents: 1_767_300,
  };
  const longClaudeSnapshot = {
    ...snapshot,
    projections: {
      ...snapshot.projections,
      claude: {
        projection: {
          ...claudeProjection,
          math: longClaudeMath,
        },
      },
    },
  };
  await installRoutes(page, longClaudeSnapshot);
  const spendResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/dashboard/spend");
  await page.goto("/app");
  await expect((await spendResponse).json()).resolves.toMatchObject({
    snapshot: { projections: { claude: { projection: { math: { currentCents: 1_618_474 } } } } },
  });
  await page.getByRole("tab", { name: "Claude", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Claude", exact: true })).toHaveAttribute("aria-selected", "true");
  const rows = await trendProjectionRows(page);
  const worst = rows.locator(".spend-projection-row").filter({ hasText: "Worst" });
  const tooltip = page.getByTestId("projection-math-tooltip");

  await expect(worst).toHaveClass(/spend-projection-row-interactive/);
  await worst.scrollIntoViewIfNeeded();
  await worst.hover();
  await expect(tooltip).toContainText("$16,184.74 now + $1,529.49/day × 10.0 days left = $17,673");
  const layout = await tooltip.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      display: window.getComputedStyle(element).display,
      left: bounds.left,
      right: bounds.right,
      viewportWidth: window.innerWidth,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      paragraphs: Array.from(element.querySelectorAll("p")).map((paragraph) => {
        const rect = paragraph.getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right };
      }),
    };
  });

  expect(layout.display).not.toMatch(/^(grid|flex)$/);
  expect(layout.left).toBeGreaterThanOrEqual(8);
  expect(layout.right).toBeLessThanOrEqual(layout.viewportWidth - 8);
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  expect(layout.paragraphs.length).toBeGreaterThan(2);
  for (let index = 1; index < layout.paragraphs.length; index++) {
    expect(layout.paragraphs[index].top).toBeGreaterThanOrEqual(layout.paragraphs[index - 1].bottom);
  }
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true);
  await context.close();
});
