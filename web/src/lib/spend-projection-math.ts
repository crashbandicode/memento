export type ProjectionMathKey = "worst" | "realistic" | "average";

export interface ProjectionMath {
  nowMs: number;
  endMs: number;
  remDays: number;
  fullDaysLeft: number;
  currentCents: number;
  limitCents: number;
  peakDayCents: number;
  avgDayCents: number;
  remainingTodayRealCents: number;
  remainingTodayAvgCents: number;
  worstEndCents: number;
  realEndCents: number;
  avgEndCents: number;
  worstCrossAtMs: number | null;
  realCrossAtMs: number | null;
  avgCrossAtMs: number | null;
}

export interface ProjectionMathSegment {
  text: string;
  bold?: boolean;
}

export interface ProjectionMathLine {
  kind: "copy" | "equation";
  segments: ProjectionMathSegment[];
}

export interface ProjectionMathTooltip {
  heading: string;
  lines: ProjectionMathLine[];
}

const REQUIRED_NUMBER_KEYS = [
  "nowMs",
  "endMs",
  "remDays",
  "fullDaysLeft",
  "currentCents",
  "limitCents",
  "peakDayCents",
  "avgDayCents",
  "remainingTodayRealCents",
  "remainingTodayAvgCents",
  "worstEndCents",
  "realEndCents",
  "avgEndCents",
] as const;

const CROSS_AT_KEYS = ["worstCrossAtMs", "realCrossAtMs", "avgCrossAtMs"] as const;

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Accept only the full additive contract, allowing nullable cross timestamps. */
export function hasProjectionMath(value: unknown): value is ProjectionMath {
  if (!isRecord(value)) return false;
  return REQUIRED_NUMBER_KEYS.every((key) => isFiniteNumber(value[key]))
    && CROSS_AT_KEYS.every((key) => value[key] === null || isFiniteNumber(value[key]));
}

export function projectionMoney(value: number, digits = 0): string {
  return `$${(value / 100).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function fmtDays(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (Math.abs(value - Math.round(value)) < 0.005) return String(Math.round(value));
  return value.toFixed(2);
}

function localDayKey(value: number): string {
  const date = new Date(value);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

/** Formats a source-dashboard-style local cross time, with an optional fixed zone for tests. */
export function formatCrossDate(crossAtMs: number | null, nowMs: number, timeZone?: string): string {
  if (!isFiniteNumber(crossAtMs)) return "";
  const date = new Date(crossAtMs);
  const timeOptions: Intl.DateTimeFormatOptions = {
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
    ...(timeZone ? { timeZone } : {}),
  };
  const time = date.toLocaleTimeString(undefined, timeOptions);
  const sameDay = timeZone
    ? new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date)
      === new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(nowMs))
    : localDayKey(crossAtMs) === localDayKey(nowMs);
  if (sameDay) return `today at ${time}`;
  return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(timeZone ? { timeZone } : {}) })}, ${time}`;
}

function line(kind: ProjectionMathLine["kind"], ...segments: ProjectionMathSegment[]): ProjectionMathLine {
  return { kind, segments };
}

function scenarioValues(math: ProjectionMath, key: ProjectionMathKey) {
  if (key === "worst") {
    return {
      endCents: math.worstEndCents,
      crossAtMs: math.worstCrossAtMs,
      todayAddCents: 0,
      rateCents: math.peakDayCents,
    };
  }
  if (key === "realistic") {
    return {
      endCents: math.realEndCents,
      crossAtMs: math.realCrossAtMs,
      todayAddCents: math.remainingTodayRealCents,
      rateCents: math.peakDayCents,
    };
  }
  return {
    endCents: math.avgEndCents,
    crossAtMs: math.avgCrossAtMs,
    todayAddCents: math.remainingTodayAvgCents,
    rateCents: math.avgDayCents,
  };
}

function hitLimitLines(math: ProjectionMath, key: ProjectionMathKey): ProjectionMathLine[] {
  const { endCents, crossAtMs, todayAddCents, rateCents } = scenarioValues(math, key);
  if (math.limitCents <= 0 || endCents <= math.limitCents) return [];

  const cap = projectionMoney(math.limitCents);
  const current = projectionMoney(math.currentCents, 2);
  if (math.currentCents >= math.limitCents) {
    return [line("copy", { text: `Already over the limit: ${current} now vs ${cap} cap.` })];
  }

  const need = math.limitCents - math.currentCents;
  const needMoney = projectionMoney(need, 2);
  const when = formatCrossDate(crossAtMs, math.nowMs);
  const whenLine = when ? [line("copy", { text: `Hits 100% ${when}` })] : [];
  if (key === "worst") {
    const days = math.peakDayCents > 0 ? need / math.peakDayCents : 0;
    const peak = projectionMoney(math.peakDayCents, 2);
    return [
      line("copy", { text: `Reaches ${cap} when the remaining ${needMoney} is spent at the peak ${peak}/day from now.` }),
      line("equation", { text: `${current} now + ${peak}/day × ${fmtDays(days)} days = ${cap}` }),
      ...whenLine,
    ];
  }

  const today = projectionMoney(todayAddCents, 2);
  const rate = projectionMoney(rateCents, 2);
  if (todayAddCents >= need) {
    return [
      line("copy", { text: `Reaches ${cap} later today: the remaining ${needMoney} fits in the ${today} still projected today.` }),
      line("equation", { text: `${current} now + ${needMoney} of ${today} still today = ${cap}` }),
      ...whenLine,
    ];
  }

  const after = Math.max(0, need - todayAddCents);
  const extraDays = rateCents > 0 ? after / rateCents : 0;
  return [
    line("copy", { text: `Reaches ${cap} after using the ${today} still today, then ${projectionMoney(after, 2)} more at ${rate}/day.` }),
    line("equation", { text: `${current} now + ${today} today + ${rate} × ${fmtDays(extraDays)} days = ${cap}` }),
    ...whenLine,
  ];
}

/** Builds the exact source-dashboard explanation copy from additive raw projection math. */
export function projectionMathTooltip(math: ProjectionMath, key: ProjectionMathKey): ProjectionMathTooltip {
  const current = projectionMoney(math.currentCents, 2);
  const peak = projectionMoney(math.peakDayCents, 2);
  const average = projectionMoney(math.avgDayCents, 2);
  const full = math.fullDaysLeft;
  const plural = full === 1 ? "" : "s";
  let heading: string;
  let lines: ProjectionMathLine[];

  if (key === "worst") {
    heading = "Worst projection";
    lines = [
      line("copy", { text: "Assumes every remaining day — including the rest of today — spends like your " }, { text: "busiest day so far", bold: true }, { text: ` (${peak}).` }),
      line("equation", { text: `${current} now + ${peak}/day × ${math.remDays.toFixed(1)} days left = ${projectionMoney(math.worstEndCents)}` }),
    ];
  } else if (key === "realistic") {
    heading = "Realistic projection";
    const today = projectionMoney(math.remainingTodayRealCents, 2);
    lines = [
      line("copy", { text: `Uses the leftover from your previous peak day after this time of day (${today} still today), then ${peak}/day for the ${full} full day${plural} left.` }),
      line("equation", { text: `${current} now + ${today} today + ${peak} × ${full} = ${projectionMoney(math.realEndCents)}` }),
    ];
  } else {
    heading = "Average projection";
    const today = projectionMoney(math.remainingTodayAvgCents, 2);
    lines = [
      line("copy", { text: "Uses the typical leftover after this time of day (" }, { text: `${today} still today), then your ` }, { text: "average day", bold: true }, { text: ` (${average}) for the ${full} full day${plural} left.` }),
      line("equation", { text: `${current} now + ${today} today + ${average} × ${full} = ${projectionMoney(math.avgEndCents)}` }),
    ];
  }

  return { heading, lines: [...lines, ...hitLimitLines(math, key)] };
}

export function projectionMathLineText(lineValue: ProjectionMathLine): string {
  return lineValue.segments.map((segment) => segment.text).join("");
}
