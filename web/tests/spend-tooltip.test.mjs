import assert from "node:assert/strict";
import test from "node:test";

import {
  bucketLabel,
  chartTooltipPlacement,
  computeSpendBuckets,
  formatTokenCount,
  nearestBucketIndex,
  scaledModelRows,
  tokenDayForHover,
  tooltipResolution,
} from "../src/lib/spend-tooltip.ts";

test("chart tooltip placement stays point-relative and flips below only when above would clip", () => {
  assert.deepEqual(chartTooltipPlacement(190, 120), { placement: "above", offset: -12 });
  assert.deepEqual(chartTooltipPlacement(120, 120), { placement: "below", offset: 16 });
  assert.deepEqual(chartTooltipPlacement(Number.NaN, 120), { placement: "above", offset: -12 });
});

test("hour buckets use each bucket's last cumulative value as the next baseline", () => {
  const buckets = computeSpendBuckets([
    { t: "2026-08-21T10:05:00", u: 100, l: 1000 },
    { t: "2026-08-21T10:45:00", u: 130, l: 1000 },
    { t: "2026-08-21T11:10:00", u: 175, l: 1000 },
    { t: "2026-08-21T11:50:00", u: 190, l: 1000 },
  ], "hour");

  assert.equal(buckets.length, 2);
  assert.deepEqual(buckets.map(({ spend, cum, limit }) => ({ spend, cum, limit })), [
    { spend: 30, cum: 130, limit: 1000 },
    { spend: 60, cum: 190, limit: 1000 },
  ]);
  assert.equal(nearestBucketIndex(buckets, Date.parse("2026-08-21T11:47:00")), 1);
  assert.equal(tooltipResolution("24h"), "hour");
  assert.equal(tooltipResolution("7d"), "day");
  assert.match(bucketLabel(buckets[0], "hour"), /,/);
});

test("model tooltip rows proportionally scale the nearest raw model-day mix", () => {
  const buckets = computeSpendBuckets([
    { t: "2026-08-21T10:00:00Z", u: 100 },
    { t: "2026-08-22T10:00:00Z", u: 145 },
  ], "day");
  const bucket = buckets[1];
  const rows = scaledModelRows({
    layers: ["gpt-5.6", "gpt-5.6-codex"],
    days: [
      { t: "2026-08-21T10:00:00Z", models: { "gpt-5.6": 9, "gpt-5.6-codex": 1 } },
    ],
  }, bucket);

  assert.deepEqual(rows, [
    { model: "gpt-5.6", value: 40.5, percent: 90 },
    { model: "gpt-5.6-codex", value: 4.5, percent: 10 },
  ]);
  assert.equal(rows.reduce((sum, row) => sum + row.value, 0), bucket.spend);
});

test("cursor token matching honors ledger timezone, then the 18-hour nearest-day fallback", () => {
  const ledger = {
    timezone: "America/New_York",
    days: [
      { key: "2026-08-20", t: "2026-08-21T02:00:00Z", total: 3400000 },
      { key: "2026-08-22", t: "2026-08-22T08:00:00Z", total: 5600 },
    ],
  };
  assert.equal(tokenDayForHover(ledger, "2026-08-21T02:30:00Z"), ledger.days[0]);
  assert.equal(tokenDayForHover(ledger, "2026-08-22T15:00:00Z"), ledger.days[1]);
  assert.equal(tokenDayForHover(ledger, "2026-08-25T15:00:00Z"), null);
});

test("token formatter uses the source dashboard's compact unit labels", () => {
  assert.equal(formatTokenCount(560), "560");
  assert.equal(formatTokenCount(5600), "5.6k");
  assert.equal(formatTokenCount(3400000), "3.4M");
  assert.equal(formatTokenCount(1200000000), "1.2B");
  assert.equal(formatTokenCount(undefined), "0");
});
