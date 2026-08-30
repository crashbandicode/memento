export type SpendTooltipResolution = "hour" | "day";

export interface SpendTooltipPoint {
  t: number | string;
  u?: number;
  l?: number;
}

export interface SpendTooltipBucket {
  t0: number;
  first: number;
  last: number;
  lastT: number | string;
  limit: number;
  spend: number;
  cum: number;
}

export interface ModelSeriesDay {
  t: number | string;
  models?: Record<string, number>;
}

export interface ModelSeriesForTooltip {
  layers?: string[];
  days?: ModelSeriesDay[];
}

export interface TooltipModelRow {
  model: string;
  value: number;
  percent: number;
}

export interface TokenLedgerDay {
  key?: string;
  t?: number | string;
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  total?: number;
}

export interface TokenLedgerForTooltip {
  timezone?: string;
  days?: TokenLedgerDay[];
}

function finite(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function timestamp(value: number | string | undefined): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : Number.NaN;
  if (typeof value !== "string") return Number.NaN;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

export function tooltipResolution(range: "6h" | "24h" | "7d" | "30d" | "mtd"): SpendTooltipResolution {
  return range === "6h" || range === "24h" ? "hour" : "day";
}

export function bucketStart(value: number | string, resolution: SpendTooltipResolution): number {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return Number.NaN;
  if (resolution === "hour") date.setMinutes(0, 0, 0);
  else date.setHours(0, 0, 0, 0);
  return date.getTime();
}

/** Groups cumulative samples just like the source dashboard's computeBuckets(). */
export function computeSpendBuckets(
  points: SpendTooltipPoint[],
  resolution: SpendTooltipResolution,
): SpendTooltipBucket[] {
  const buckets = new Map<number, SpendTooltipBucket>();
  for (const point of points) {
    const start = bucketStart(point.t, resolution);
    if (!Number.isFinite(start)) continue;
    const cumulative = finite(point.u);
    let bucket = buckets.get(start);
    if (!bucket) {
      bucket = {
        t0: start,
        first: cumulative,
        last: cumulative,
        lastT: point.t,
        limit: finite(point.l),
        spend: 0,
        cum: cumulative,
      };
      buckets.set(start, bucket);
    }
    bucket.last = cumulative;
    bucket.lastT = point.t;
    bucket.limit = finite(point.l);
  }

  const result = [...buckets.values()].sort((left, right) => left.t0 - right.t0);
  let previous: SpendTooltipBucket | undefined;
  for (const bucket of result) {
    bucket.spend = Math.max(0, previous ? bucket.last - previous.last : bucket.last - bucket.first);
    bucket.cum = bucket.last;
    previous = bucket;
  }
  return result;
}

export function bucketLabel(bucket: SpendTooltipBucket, resolution: SpendTooltipResolution): string {
  const date = new Date(bucket.t0);
  const day = date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  if (resolution !== "hour") return day;
  return `${day}, ${date.toLocaleTimeString(undefined, { hour: "numeric" })}`;
}

export function nearestBucketIndex(buckets: SpendTooltipBucket[], target: number): number | null {
  if (!Number.isFinite(target) || !buckets.length) return null;
  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  buckets.forEach((bucket, index) => {
    const distance = Math.abs(timestamp(bucket.lastT) - target);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  });
  return bestDistance === Number.POSITIVE_INFINITY ? null : bestIndex;
}

export function chartTooltipPlacement(pointTop: number, tooltipHeight: number): { placement: "above" | "below"; offset: number } {
  if (!Number.isFinite(pointTop) || !Number.isFinite(tooltipHeight) || pointTop - tooltipHeight - 14 >= 0) {
    return { placement: "above", offset: -12 };
  }
  return { placement: "below", offset: 16 };
}

/** Scales the nearest raw model-day mix so tooltip rows add up to the hover bucket spend. */
export function scaledModelRows(
  series: ModelSeriesForTooltip | undefined,
  bucket: SpendTooltipBucket,
): TooltipModelRow[] {
  const layers = series?.layers ?? [];
  const days = series?.days ?? [];
  if (!layers.length || !days.length || bucket.spend <= 0) return [];

  const target = timestamp(bucket.lastT);
  if (!Number.isFinite(target)) return [];
  let nearest: ModelSeriesDay | undefined;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const day of days) {
    const distance = Math.abs(timestamp(day.t) - target);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearest = day;
    }
  }
  if (!nearest) return [];

  const mixTotal = layers.reduce((sum, model) => sum + finite(nearest.models?.[model]), 0);
  if (mixTotal <= 0) return [];
  return layers
    .map((model) => {
      const value = bucket.spend * (finite(nearest.models?.[model]) / mixTotal);
      return { model, value, percent: (value / bucket.spend) * 100 };
    })
    .filter((row) => row.value > 0);
}

function ledgerDayKey(value: number, timezone?: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone || "America/New_York",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(value));
    const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((entry) => entry.type === type)?.value;
    const year = part("year");
    const month = part("month");
    const day = part("day");
    if (year && month && day) return `${year}-${month}-${day}`;
  } catch {
    // A malformed server timezone must not prevent a graceful UTC fallback.
  }
  return new Date(value).toISOString().slice(0, 10);
}

/** Finds the matching ledger day, falling back to a non-empty day within 18 hours. */
export function tokenDayForHover(
  ledger: TokenLedgerForTooltip | undefined,
  lastT: number | string,
): TokenLedgerDay | null {
  const days = ledger?.days ?? [];
  const target = timestamp(lastT);
  if (!days.length || !Number.isFinite(target)) return null;
  const key = ledgerDayKey(target, ledger?.timezone);
  const exact = days.find((day) => day.key === key);
  if (exact && finite(exact.total) > 0) return exact;

  let nearest: TokenLedgerDay | undefined;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const day of days) {
    const dayTime = timestamp(day.t);
    const distance = Math.abs(dayTime - target);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearest = day;
    }
  }
  if (nearestDistance > 18 * 3_600_000 || !nearest || finite(nearest.total) <= 0) return null;
  return nearest;
}

export function formatTokenCount(value: unknown): string {
  const tokens = finite(value);
  if (tokens >= 1_000_000_000) return `${(tokens / 1_000_000_000).toFixed(1)}B`;
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}k`;
  return String(Math.round(tokens));
}
