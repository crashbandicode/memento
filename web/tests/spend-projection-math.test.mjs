import assert from "node:assert/strict";
import test from "node:test";

import {
  fmtDays,
  formatCrossDate,
  hasProjectionMath,
  projectionMathLineText,
  projectionMathTooltip,
} from "../src/lib/spend-projection-math.ts";

const nowMs = Date.parse("2026-08-21T16:00:00.000Z");
const math = {
  nowMs,
  endMs: nowMs + 10 * 86_400_000,
  remDays: 10,
  fullDaysLeft: 9,
  currentCents: 18000,
  limitCents: 50000,
  peakDayCents: 6000,
  avgDayCents: 4000,
  remainingTodayRealCents: 3000,
  remainingTodayAvgCents: 2000,
  worstEndCents: 78000,
  realEndCents: 75000,
  avgEndCents: 56000,
  worstCrossAtMs: nowMs + (32_000 / 6_000) * 86_400_000,
  realCrossAtMs: nowMs + 4 * 86_400_000,
  avgCrossAtMs: nowMs + 7.5 * 86_400_000,
};

const textLines = (tooltip) => tooltip.lines.map(projectionMathLineText);

test("projection explainers retain the original copy and equations for all scenarios", () => {
  const worst = projectionMathTooltip(math, "worst");
  assert.equal(worst.heading, "Worst projection");
  assert.equal(textLines(worst)[0], "Assumes every remaining day — including the rest of today — spends like your busiest day so far ($60.00).");
  assert.equal(textLines(worst)[1], "$180.00 now + $60.00/day × 10.0 days left = $780");
  assert.equal(worst.lines[0].segments.some((segment) => segment.bold && segment.text === "busiest day so far"), true);
  assert.match(textLines(worst).at(-1), /^Hits 100% /);

  const realistic = projectionMathTooltip(math, "realistic");
  assert.equal(realistic.heading, "Realistic projection");
  assert.equal(textLines(realistic)[0], "Uses the leftover from your previous peak day after this time of day ($30.00 still today), then $60.00/day for the 9 full days left.");
  assert.equal(textLines(realistic)[1], "$180.00 now + $30.00 today + $60.00 × 9 = $750");

  const average = projectionMathTooltip(math, "average");
  assert.equal(average.heading, "Average projection");
  assert.equal(textLines(average)[0], "Uses the typical leftover after this time of day ($20.00 still today), then your average day ($40.00) for the 9 full days left.");
  assert.equal(textLines(average)[1], "$180.00 now + $20.00 today + $40.00 × 9 = $560");
  assert.equal(average.lines[0].segments.some((segment) => segment.bold && segment.text === "average day"), true);
});

test("projection explainers retain full thousands-scale money precision", () => {
  const thousands = projectionMathTooltip({
    ...math,
    currentCents: 1_618_474,
    limitCents: 1_700_000,
    peakDayCents: 152_949,
    worstEndCents: 1_767_300,
  }, "worst");

  assert.equal(textLines(thousands)[0], "Assumes every remaining day — including the rest of today — spends like your busiest day so far ($1,529.49).");
  assert.equal(textLines(thousands)[1], "$16,184.74 now + $1,529.49/day × 10.0 days left = $17,673");
  assert.match(textLines(thousands).at(2), /^Reaches \$17,000 /);
});

test("hit-100 builders cover already-over, worst, fits-today, and spills-days branches", () => {
  const alreadyOver = projectionMathTooltip({ ...math, currentCents: 52000, worstEndCents: 112000 }, "worst");
  assert.equal(textLines(alreadyOver).at(-1), "Already over the limit: $520.00 now vs $500 cap.");

  const worst = projectionMathTooltip({ ...math, worstCrossAtMs: null }, "worst");
  assert.deepEqual(textLines(worst).slice(2), [
    "Reaches $500 when the remaining $320.00 is spent at the peak $60.00/day from now.",
    "$180.00 now + $60.00/day × 5.33 days = $500",
  ]);

  const fitsToday = projectionMathTooltip({
    ...math,
    remainingTodayRealCents: 35000,
    realEndCents: 107000,
    realCrossAtMs: null,
  }, "realistic");
  assert.deepEqual(textLines(fitsToday).slice(2), [
    "Reaches $500 later today: the remaining $320.00 fits in the $350.00 still projected today.",
    "$180.00 now + $320.00 of $350.00 still today = $500",
  ]);

  const spillsDays = projectionMathTooltip({ ...math, avgCrossAtMs: null }, "average");
  assert.deepEqual(textLines(spillsDays).slice(2), [
    "Reaches $500 after using the $20.00 still today, then $300.00 more at $40.00/day.",
    "$180.00 now + $20.00 today + $40.00 × 7.50 days = $500",
  ]);
});

test("day and cross-date formatting handle boundary cases deterministically", () => {
  assert.equal(fmtDays(Number.NaN), "0");
  assert.equal(fmtDays(0), "0");
  assert.equal(fmtDays(-1), "0");
  assert.equal(fmtDays(1.004), "1");
  assert.equal(fmtDays(1.006), "1.01");
  assert.equal(formatCrossDate(Date.parse("2026-08-21T19:05:00.000Z"), nowMs, "America/New_York"), "today at 3:05 PM EDT");
  assert.equal(formatCrossDate(Date.parse("2026-08-22T19:05:00.000Z"), nowMs, "America/New_York"), "Aug 22, 3:05 PM EDT");
});

test("zero-rate, no-limit, and incomplete contract cases never create invalid math", () => {
  const zeroRate = projectionMathTooltip({
    ...math,
    peakDayCents: 0,
    worstEndCents: 60000,
    worstCrossAtMs: null,
  }, "worst");
  assert.equal(textLines(zeroRate).at(-1), "$180.00 now + $0.00/day × 0 days = $500");
  assert.equal(textLines(zeroRate).join(" ").includes("Infinity"), false);

  const noLimit = projectionMathTooltip({ ...math, limitCents: 0 }, "average");
  assert.equal(noLimit.lines.length, 2);

  assert.equal(hasProjectionMath(math), true);
  assert.equal(hasProjectionMath(null), false);
  assert.equal(hasProjectionMath({ ...math, avgDayCents: Number.NaN }), false);
  assert.equal(hasProjectionMath({ ...math, realCrossAtMs: "tomorrow" }), false);
  assert.equal(hasProjectionMath({ ...math, remainingTodayAvgCents: undefined }), false);
});
