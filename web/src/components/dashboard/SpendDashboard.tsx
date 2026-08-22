"use client";

import { PointerEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/aurora/Icon";
import { Chip, Glass, SectionLabel } from "@/components/aurora/primitives";
import { authFetch, getApiBase } from "@/lib/api-client";

type SpendSource = "all" | "claude" | "cursor" | "codex";
type SpendRange = "6h" | "24h" | "7d" | "30d" | "mtd";
type Timestamp = number | string;

interface SourceMeta {
  id: SpendSource;
  label: string;
  stacked?: boolean;
}

interface SpendPart {
  source?: string;
  name?: string;
  used?: string;
  limit?: string;
  usedCents?: number;
  limitCents?: number;
  pctUsed?: number;
}

interface SpendSummary extends SpendPart {
  authenticated?: boolean;
  email?: string | null;
  planType?: string | null;
  remaining?: string;
  remainingCents?: number;
  resetsAt?: string | null;
  billingCycleStart?: string | null;
  parts?: SpendPart[];
  missing?: string[];
  metricNote?: string;
  coverage?: string;
}

interface StackPoint {
  t: Timestamp;
  y0?: number;
  y1?: number;
  value?: number;
  u?: number;
  l?: number;
}

interface StackTrace {
  model?: string;
  label?: string;
  color?: string;
  points?: StackPoint[];
}

interface HistoryView {
  points?: StackPoint[];
  stack?: {
    traces?: StackTrace[];
    outline?: StackPoint[];
  };
}

interface BreakdownRow {
  model?: string;
  tool?: string;
  label?: string;
  share?: number;
  totalCents?: number;
  totalTokens?: number;
  events?: number;
  calls?: number;
  color?: string;
}

interface BreakdownView {
  available?: boolean;
  models?: BreakdownRow[];
  tools?: BreakdownRow[];
  coverage?: number | string | { attributed?: number; conversations?: number };
  note?: string;
  primary?: string;
}

interface ProjectionScenario {
  cents?: number;
  dollars?: string;
  pctOfLimit?: number;
  hits100Pct?: string | null;
}

interface ProjectionView {
  projection?: {
    daysLeft?: number;
    current?: number;
    limit?: number;
    worst?: ProjectionScenario;
    realistic?: ProjectionScenario;
    average?: ProjectionScenario;
  };
}

interface SpendSnapshot {
  fetchedAt?: string;
  ui?: {
    sources?: SourceMeta[];
    defaultSource?: SpendSource;
    defaultRange?: string;
    modelBarColors?: string[];
  };
  spend?: Partial<Record<SpendSource, SpendSummary>>;
  models?: Partial<Record<SpendSource, BreakdownView>>;
  tools?: Partial<Record<SpendSource, BreakdownView>>;
  projections?: Partial<Record<SpendSource, ProjectionView>>;
  history?: Partial<Record<SpendSource, HistoryView>>;
}

interface SpendEnvelope {
  available: boolean;
  stale?: boolean;
  cached_at?: string;
  reason?: string;
  snapshot?: SpendSnapshot | null;
}

const SOURCE_COLORS: Record<SpendSource, string> = {
  all: "#d5b36d",
  claude: "#d97757",
  cursor: "#5b8def",
  codex: "#3d9b74",
};

const PROJECTION_COLORS = {
  worst: "#ef5b5b",
  realistic: "#e6a93f",
  average: "#61bd82",
};

const DEFAULT_SOURCES: SourceMeta[] = [
  { id: "all", label: "All" },
  { id: "claude", label: "Claude", stacked: true },
  { id: "cursor", label: "Cursor", stacked: true },
  { id: "codex", label: "Codex", stacked: true },
];

const RANGES: Array<{ id: SpendRange; label: string; hours?: number }> = [
  { id: "6h", label: "6h", hours: 6 },
  { id: "24h", label: "24h", hours: 24 },
  { id: "7d", label: "7d", hours: 24 * 7 },
  { id: "30d", label: "30d", hours: 24 * 30 },
  { id: "mtd", label: "MTD" },
];

function useNarrowSpendLayout(): boolean {
  const [narrow, setNarrow] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 760px)");
    const update = () => setNarrow(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return narrow;
}

function finite(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function timestamp(value: Timestamp): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : Number.NaN;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function centsLabel(value: number, digits = 0): string {
  if (Math.abs(value) >= 100_000) {
    return `$${(value / 100_000).toFixed(value >= 1_000_000 ? 0 : 1)}k`;
  }
  return `$${(value / 100).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function compactNumber(value: number): string {
  return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function sharePercent(value: unknown): number {
  const raw = finite(value);
  return Math.max(0, Math.min(100, raw > 1 ? raw : raw * 100));
}

function coverageLabel(value: BreakdownView["coverage"]): string | undefined {
  if (typeof value === "string" || typeof value === "number") return `${value} coverage`;
  if (!value) return undefined;
  const attributed = finite(value.attributed);
  const conversations = finite(value.conversations);
  return conversations > 0 ? `${attributed}/${conversations} chats` : undefined;
}

function shortModel(value: string): string {
  return value.replace(/^claude-/i, "").replace(/-\d{8}$/i, "");
}

function dateLabel(value?: string | null): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unavailable";
  return date.toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" });
}

function filterRange(points: StackPoint[], range: SpendRange, cycleStart?: string | null): StackPoint[] {
  const valid = points.filter((point) => Number.isFinite(timestamp(point.t)));
  if (!valid.length) return valid;
  const latest = Math.max(...valid.map((point) => timestamp(point.t)));
  const config = RANGES.find((candidate) => candidate.id === range);
  const cycle = cycleStart ? Date.parse(cycleStart) : Number.NaN;
  const cutoff = config?.hours
    ? latest - config.hours * 3_600_000
    : Number.isFinite(cycle)
      ? cycle
      : Number.NEGATIVE_INFINITY;
  const selected = valid.filter((point) => timestamp(point.t) >= cutoff);
  return selected.length > 1 ? selected : valid.slice(-2);
}

function linePath(
  points: StackPoint[],
  x: (value: Timestamp) => number,
  y: (value: number) => number,
  key: "u" | "y1",
): string {
  return points
    .filter((point) => Number.isFinite(timestamp(point.t)) && Number.isFinite(finite(point[key])))
    .map((point, index) => `${index === 0 ? "M" : "L"}${x(point.t).toFixed(2)},${y(finite(point[key])).toFixed(2)}`)
    .join(" ");
}

function formatHit(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return ` · hits 100% ${value}`;
  return ` · hits 100% ${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

function ProjectionRows({ projection }: { projection?: ProjectionView["projection"] }) {
  const scenarios = [
    ["Worst", "worst", projection?.worst],
    ["Realistic", "realistic", projection?.realistic],
    ["Average", "average", projection?.average],
  ] as const;
  if (!scenarios.some(([, , scenario]) => scenario?.dollars || scenario?.cents != null)) return null;
  return (
    <div className="spend-projection-rows" aria-label="Cycle projections">
      {scenarios.map(([label, key, scenario]) => (
        <div key={key}>
          <span>{label}</span>
          <strong style={{ color: PROJECTION_COLORS[key] }}>
            {scenario?.dollars || centsLabel(finite(scenario?.cents))}
          </strong>
          <small>
            {finite(scenario?.pctOfLimit).toFixed(0)}% of limit{formatHit(scenario?.hits100Pct)}
          </small>
        </div>
      ))}
    </div>
  );
}

function SpendHistoryChart({
  history,
  source,
  range,
  cycleStart,
  summary,
  projection,
  projectionsVisible,
}: {
  history?: HistoryView;
  source: SpendSource;
  range: SpendRange;
  cycleStart?: string | null;
  summary: SpendSummary;
  projection?: ProjectionView["projection"];
  projectionsVisible: boolean;
}) {
  const narrowLayout = useNarrowSpendLayout();
  const width = 600;
  const height = 300;
  const inset = { left: 46, right: 18, top: 22, bottom: 35 };
  const rawTraces = source === "all" ? [] : history?.stack?.traces ?? [];
  const traces = rawTraces
    .map((trace) => ({ ...trace, points: filterRange(trace.points ?? [], range, cycleStart) }))
    .filter((trace) => (trace.points?.length ?? 0) > 1);
  const rawOutline = history?.stack?.outline?.length ? history.stack.outline : history?.points ?? [];
  const outline = filterRange(rawOutline, range, cycleStart);
  const allPoints = [...outline, ...traces.flatMap((trace) => trace.points ?? [])];
  const timestamps = allPoints.map((point) => timestamp(point.t)).filter(Number.isFinite);
  const minTime = Math.min(...timestamps);
  const maxTime = Math.max(...timestamps);
  const maxUsage = Math.max(
    1,
    ...allPoints.map((point) => Math.max(finite(point.u), finite(point.y1), finite(point.l))),
  );
  const projectionValues = projectionsVisible
    ? [projection?.worst?.cents, projection?.realistic?.cents, projection?.average?.cents]
        .map(finite)
        .filter((value) => value > 0)
    : [];
  const usageTop = Math.max(maxUsage * 1.14, finite(projection?.current), 1);
  const highMax = Math.max(usageTop, finite(summary.limitCents), ...projectionValues);
  const brokenScale = projectionValues.length > 0 && highMax > usageTop * 2.2;
  const plotBottom = height - inset.bottom;
  const plotTop = inset.top;
  const usageBandTop = brokenScale ? 145 : plotTop;
  const projectionFloor = brokenScale ? 114 : plotTop;
  const resetTime = summary.resetsAt ? Date.parse(summary.resetsAt) : Number.NaN;
  const showProjectionRays = projectionsVisible
    && projectionValues.length > 0
    && !narrowLayout
    && Number.isFinite(resetTime)
    && resetTime > maxTime;
  const plotWidth = width - inset.left - inset.right;
  const historyWidth = plotWidth * (showProjectionRays ? 0.72 : 1);
  const futureWidth = plotWidth - historyWidth;
  const historySpan = Math.max(1, maxTime - minTime);
  const futureSpan = Math.max(1, resetTime - maxTime);
  const x = (value: Timestamp) => {
    const time = timestamp(value);
    if (!showProjectionRays || time <= maxTime) {
      return inset.left + ((time - minTime) / historySpan) * historyWidth;
    }
    return inset.left + historyWidth + ((time - maxTime) / futureSpan) * futureWidth;
  };
  const y = (value: number) => {
    if (!brokenScale || value <= usageTop) {
      const top = brokenScale ? usageBandTop : plotTop;
      return plotBottom - (Math.max(0, value) / (brokenScale ? usageTop : highMax * 1.05)) * (plotBottom - top);
    }
    const span = Math.max(1, highMax - usageTop);
    return projectionFloor - ((value - usageTop) / span) * (projectionFloor - plotTop);
  };
  const hasChart = Number.isFinite(minTime) && Number.isFinite(maxTime) && maxTime > minTime && outline.length > 1;
  const [hover, setHover] = useState<number | null>(null);

  const setHoverFromPointer = (event: PointerEvent<SVGSVGElement>) => {
    if (!hasChart) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const localX = ((event.clientX - rect.left) / rect.width) * width;
    const target = minTime + ((localX - inset.left) / historyWidth) * historySpan;
    let best = 0;
    let distance = Number.POSITIVE_INFINITY;
    outline.forEach((point, index) => {
      const next = Math.abs(timestamp(point.t) - target);
      if (next < distance) {
        distance = next;
        best = index;
      }
    });
    setHover(best);
  };

  if (!hasChart) {
    return (
      <div className="spend-empty" role="status">
        <Icon name="activity" size={20} />
        <span>{source === "codex" ? "Codex history will appear after its analytics cache has a session mix." : "No usage history is available for this period yet."}</span>
      </div>
    );
  }

  const current = outline[outline.length - 1];
  const currentValue = finite(current.u) || finite(current.y1);
  const currentX = x(current.t);
  const currentY = y(currentValue);
  const rayEndX = showProjectionRays ? x(resetTime) : width - inset.right;
  const hoverPoint = hover == null ? null : outline[hover];
  const hoverValue = hoverPoint ? finite(hoverPoint.u) || finite(hoverPoint.y1) : 0;
  const tickValues = brokenScale ? [0, usageTop, highMax] : [0, highMax / 2, highMax];

  return (
    <div className="spend-chart-wrap">
      <div className="spend-plot">
        <svg
          className="spend-chart"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`${source} month-to-date spend history`}
          onPointerMove={setHoverFromPointer}
          onPointerLeave={() => setHover(null)}
        >
          <title>{source} month-to-date spend history</title>
          {tickValues.map((value) => (
            <g key={value}>
              <line x1={inset.left} x2={width - inset.right} y1={y(value)} y2={y(value)} stroke="var(--aurora-border)" strokeWidth="1" />
              <text x={inset.left - 8} y={y(value) + 4} textAnchor="end" fill="var(--aurora-fg4)" fontSize="10">{centsLabel(value)}</text>
            </g>
          ))}
          {brokenScale && (
            <path d={`M${inset.left - 5},${usageBandTop - 6} l6,5 l-6,5 l6,5`} fill="none" stroke="var(--aurora-fg4)" strokeWidth="2" />
          )}
          {traces.map((trace, index) => {
            const points = trace.points ?? [];
            const upper = points.map((point) => `${x(point.t).toFixed(2)},${y(finite(point.y1)).toFixed(2)}`);
            const lower = [...points].reverse().map((point) => `${x(point.t).toFixed(2)},${y(finite(point.y0)).toFixed(2)}`);
            return <path key={`${trace.model || trace.label || index}`} d={`M${upper.join(" L")} L${lower.join(" L")} Z`} fill={trace.color || SOURCE_COLORS[source]} opacity="0.72" />;
          })}
          {source === "all" && (
            <path d={`${linePath(outline, x, y, "u")} L${x(outline[outline.length - 1].t)},${plotBottom} L${x(outline[0].t)},${plotBottom} Z`} fill={`${SOURCE_COLORS.all}33`} />
          )}
          <path d={linePath(outline, x, y, "u") || linePath(outline, x, y, "y1")} fill="none" stroke="var(--aurora-fg1)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          {finite(summary.limitCents) > 0 && (
            <g>
              <line x1={inset.left} x2={width - inset.right} y1={y(finite(summary.limitCents))} y2={y(finite(summary.limitCents))} stroke={PROJECTION_COLORS.realistic} strokeWidth="1.5" strokeDasharray="5 5" />
              <text x={inset.left + 4} y={y(finite(summary.limitCents)) - 5} fill={PROJECTION_COLORS.realistic} fontSize="9">limit {summary.limit || centsLabel(finite(summary.limitCents))}</text>
            </g>
          )}
          {showProjectionRays && (
            <g className="spend-projection-rays">
              {(["worst", "realistic", "average"] as const).map((key) => {
                const scenario = projection?.[key];
                if (!scenario?.cents) return null;
                return <line key={key} x1={currentX} y1={currentY} x2={rayEndX} y2={y(scenario.cents)} stroke={PROJECTION_COLORS[key]} strokeWidth="1.5" strokeDasharray="6 5" />;
              })}
            </g>
          )}
          <circle cx={currentX} cy={currentY} r="3" fill="var(--aurora-fg1)" />
          {hoverPoint && (
            <g>
              <line x1={x(hoverPoint.t)} x2={x(hoverPoint.t)} y1={plotTop} y2={plotBottom} stroke="var(--aurora-fg4)" strokeWidth="1" strokeDasharray="3 4" />
              <circle cx={x(hoverPoint.t)} cy={y(hoverValue)} r="4" fill={SOURCE_COLORS[source]} stroke="var(--aurora-surface)" strokeWidth="2" />
            </g>
          )}
          <text x={inset.left} y={height - 8} fill="var(--aurora-fg4)" fontSize="10">{new Date(minTime).toLocaleDateString(undefined, { month: "numeric", day: "numeric" })}</text>
          {showProjectionRays && (
            <text x={currentX} y={height - 8} textAnchor="middle" fill="var(--aurora-fg4)" fontSize="10">{new Date(maxTime).toLocaleDateString(undefined, { month: "numeric", day: "numeric" })}</text>
          )}
          <text x={width - inset.right} y={height - 8} textAnchor="end" fill="var(--aurora-fg4)" fontSize="10">{new Date(showProjectionRays ? resetTime : maxTime).toLocaleDateString(undefined, { month: "numeric", day: "numeric" })}</text>
        </svg>
        {hoverPoint && (
          <div className="spend-tooltip" style={{ left: `${Math.min(78, Math.max(8, (x(hoverPoint.t) / width) * 100))}%` }}>
            <span>{new Date(timestamp(hoverPoint.t)).toLocaleString()}</span>
            <strong>{centsLabel(hoverValue, 2)}</strong>
          </div>
        )}
      </div>
      {projectionsVisible && <ProjectionRows projection={projection} />}
      {traces.length > 0 && (
        <div className="spend-legend">
          {traces.slice(0, 8).map((trace, index) => (
            <span key={`${trace.model || trace.label || index}`}><i style={{ background: trace.color || SOURCE_COLORS[source] }} />{shortModel(trace.label || trace.model || "Other")}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function UsageRing({ pct, source }: { pct: number; source: SpendSource }) {
  const radius = 54;
  const circumference = Math.PI * 2 * radius;
  return (
    <div className="spend-ring" aria-label={`${pct.toFixed(1)} percent used`}>
      <svg viewBox="0 0 130 130" aria-hidden="true">
        <circle cx="65" cy="65" r={radius} fill="none" stroke="var(--aurora-chip)" strokeWidth="12" />
        <circle cx="65" cy="65" r={radius} fill="none" stroke={SOURCE_COLORS[source]} strokeWidth="12" strokeLinecap="round" strokeDasharray={circumference} strokeDashoffset={circumference * (1 - pct / 100)} />
      </svg>
      <div><strong>{pct.toFixed(1)}%</strong><span>used</span></div>
    </div>
  );
}

function ProviderRows({ parts, projections }: { parts: SpendPart[]; projections?: ProjectionView["projection"] }) {
  if (!parts.length) return null;
  return (
    <div className="spend-provider-list">
      {parts.map((part) => {
        const source = (part.source?.toLowerCase() || "all") as SpendSource;
        const pct = Math.max(0, Math.min(100, finite(part.pctUsed)));
        return (
          <div className="spend-provider" key={part.source || part.name}>
            <strong>{part.name || part.source}</strong>
            <div>
              <span className="spend-provider-track"><i style={{ width: `${pct}%`, background: SOURCE_COLORS[source] || SOURCE_COLORS.all }} /></span>
              <small><b>{part.used || centsLabel(finite(part.usedCents))}</b> of {part.limit || centsLabel(finite(part.limitCents))}</small>
            </div>
          </div>
        );
      })}
      <ProjectionRows projection={projections} />
    </div>
  );
}

function Breakdown({ title, rows, colors, subtitle }: { title: string; rows: BreakdownRow[]; colors: string[]; subtitle?: string }) {
  if (!rows.length) return null;
  return (
    <div className="spend-breakdown">
      <div className="spend-breakdown-heading"><strong>{title}</strong>{subtitle && <span>{subtitle}</span>}</div>
      <div className="spend-breakdown-list">
        {rows.slice(0, 8).map((row, index) => {
          const label = shortModel(row.label || row.model || row.tool || "Unknown");
          const share = sharePercent(row.share);
          const meta = row.totalTokens != null
            ? `${finite(row.calls || row.events)} calls · ${compactNumber(row.totalTokens)} tok`
            : row.totalCents != null
              ? centsLabel(row.totalCents, 2)
              : `${compactNumber(finite(row.events))} events`;
          return (
            <div className="spend-breakdown-row" key={`${label}-${index}`}>
              <div><strong title={label}>{label}</strong><small>{meta}</small></div>
              <div className="spend-breakdown-track"><i style={{ width: `${share}%`, background: row.color || colors[index % colors.length] || SOURCE_COLORS.all }} /></div>
              <b>{share.toFixed(share < 1 ? 1 : 0)}%</b>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LoadingCard() {
  return (
    <div className="spend-loading" aria-label="Loading AI spend">
      <SectionLabel>AI spend</SectionLabel>
      <Glass padding={22} radius={22}><div className="spend-skeleton" /><div className="spend-skeleton spend-skeleton-chart" /></Glass>
    </div>
  );
}

export default function SpendDashboard() {
  const [envelope, setEnvelope] = useState<SpendEnvelope | null>(null);
  const [selectedSource, setSelectedSource] = useState<SpendSource>("all");
  const [selectedRange, setSelectedRange] = useState<SpendRange>("mtd");
  const [projectionsVisible, setProjectionsVisible] = useState(true);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (force = false, signal?: AbortSignal) => {
    try {
      const response = await authFetch(`${getApiBase()}/api/dashboard/spend${force ? "?refresh=true" : ""}`, { signal });
      if (!response.ok) throw new Error(`Spend dashboard returned ${response.status}`);
      const next = await response.json() as SpendEnvelope;
      setEnvelope(next);
      const preferred = next.snapshot?.ui?.defaultSource;
      if (preferred && DEFAULT_SOURCES.some((source) => source.id === preferred)) setSelectedSource(preferred);
      const preferredRange = next.snapshot?.ui?.defaultRange;
      if (RANGES.some((range) => range.id === preferredRange)) setSelectedRange(preferredRange as SpendRange);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) console.error(error);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void load(false, controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [load]);

  const snapshot = envelope?.snapshot;
  const sources = snapshot?.ui?.sources?.filter((source) => DEFAULT_SOURCES.some((candidate) => candidate.id === source.id)) || DEFAULT_SOURCES;
  const summary = snapshot?.spend?.[selectedSource];
  const modelView = snapshot?.models?.[selectedSource];
  const toolView = snapshot?.tools?.[selectedSource];
  const history = snapshot?.history?.[selectedSource];
  const projection = snapshot?.projections?.[selectedSource]?.projection;
  const modelColors = snapshot?.ui?.modelBarColors || ["#2dd4bf", "#5b8def", "#6cc08a", "#c47ab3", "#e0a64a"];
  const models = useMemo(() => modelView?.models || [], [modelView]);
  const tools = useMemo(() => toolView?.tools || [], [toolView]);

  if (loading && !envelope) return <LoadingCard />;
  if (envelope?.reason === "not_configured" || !envelope?.available || !snapshot || !summary) return null;

  const pct = Math.max(0, Math.min(100, finite(summary.pctUsed)));
  const fetchedAt = snapshot.fetchedAt || envelope.cached_at;

  return (
    <section className="spend-section" aria-labelledby="spend-dashboard-title">
      <div className="spend-section-heading">
        <SectionLabel style={{ margin: 0 }}><span id="spend-dashboard-title">AI spend</span></SectionLabel>
        <div>{envelope.stale && <Chip tone="warn">Updating</Chip>}<span>{fetchedAt ? `As of ${new Date(fetchedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Latest snapshot"}</span></div>
      </div>

      <div className="spend-shell">
        <div className="spend-tabs" role="tablist" aria-label="Spending source">
          {sources.map((source) => (
            <button type="button" role="tab" aria-selected={selectedSource === source.id} className={selectedSource === source.id ? "active" : ""} key={source.id} onClick={() => setSelectedSource(source.id)}>{source.label}</button>
          ))}
        </div>

        <Glass padding={0} radius={22} style={{ overflow: "hidden" }}>
          <div className="spend-card">
            <header className="spend-account">
              <div className="spend-avatar" style={{ background: SOURCE_COLORS[selectedSource] }}>{selectedSource === "all" ? "AI" : selectedSource.slice(0, 1).toUpperCase()}</div>
              <div><strong>{selectedSource === "all" ? "Combined AI usage" : `${summary.name || sources.find((item) => item.id === selectedSource)?.label || selectedSource} usage`}</strong><span>{summary.email || summary.planType || "Current billing cycle"}</span></div>
              <Chip>{summary.planType || selectedSource}</Chip>
            </header>

            <div className="spend-overview">
              <UsageRing pct={pct} source={selectedSource} />
              <div className="spend-used"><strong>{summary.used || centsLabel(finite(summary.usedCents), 2)}</strong><span>of {summary.limit || centsLabel(finite(summary.limitCents), 2)} / month</span></div>
              <div className="spend-days"><strong>{projection?.daysLeft != null ? Math.ceil(projection.daysLeft) : "—"}</strong><span>days left</span></div>
            </div>

            <div className="spend-facts">
              <div><span>{selectedSource === "all" ? "AI quota left" : "Remaining this month"}</span><strong>{summary.remaining || centsLabel(finite(summary.remainingCents), 2)} left</strong></div>
              <div><span>Monthly limit</span><strong>{summary.limit || centsLabel(finite(summary.limitCents), 2)}</strong></div>
              <div><span>Resets</span><strong>{dateLabel(summary.resetsAt)}</strong></div>
              {summary.metricNote && <div><span>Metric</span><strong>{summary.metricNote}</strong></div>}
            </div>

            {selectedSource === "all" && <ProviderRows parts={summary.parts || []} projections={projection} />}

            <div className="spend-trend">
              <div className="spend-trend-heading">
                <strong>Usage trend</strong>
                <button type="button" className={projectionsVisible ? "active" : ""} onClick={() => setProjectionsVisible((value) => !value)}>◔ Projection</button>
              </div>
              <div className="spend-ranges" role="group" aria-label="Usage range">
                {RANGES.map((range) => <button type="button" key={range.id} className={selectedRange === range.id ? "active" : ""} onClick={() => setSelectedRange(range.id)}>{range.label}</button>)}
              </div>
              <SpendHistoryChart history={history} source={selectedSource} range={selectedRange} cycleStart={summary.billingCycleStart} summary={summary} projection={projection} projectionsVisible={projectionsVisible} />
            </div>

            <Breakdown title="Models this cycle" subtitle={coverageLabel(modelView?.coverage) || (modelView?.primary ? `by ${modelView.primary}` : undefined)} rows={models} colors={modelColors} />
            <Breakdown title="Tool mix" rows={tools} colors={modelColors} />
            {modelView?.note && <p className="spend-note">{modelView.note}</p>}

            <footer className="spend-footer">
              <span>{fetchedAt ? `Updated ${new Date(fetchedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Latest snapshot"}</span>
              <button type="button" disabled={refreshing} onClick={() => { setRefreshing(true); void load(true); }}><Icon name="refresh" size={14} />{refreshing ? "Refreshing" : "Refresh"}</button>
            </footer>
          </div>
        </Glass>
      </div>

      <style jsx global>{`
        .spend-section{margin-bottom:28px;min-width:0}.spend-section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:8px 4px 12px}.spend-section-heading>div{display:flex;align-items:center;gap:8px;color:var(--aurora-fg4);font-size:11px}.spend-shell{width:100%;max-width:680px;margin:0 auto}.spend-tabs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px;margin-bottom:12px;padding:4px;border:1px solid var(--aurora-border);border-radius:14px;background:color-mix(in srgb,var(--aurora-chip) 65%,transparent)}.spend-tabs button{min-height:40px;border:0;border-radius:10px;background:transparent;color:var(--aurora-fg3);font:inherit;font-size:13px;font-weight:650;cursor:pointer}.spend-tabs button.active{background:var(--aurora-surface);color:var(--aurora-fg1);box-shadow:0 2px 9px rgba(15,23,42,.08)}.spend-card{padding:26px 28px 22px}.spend-account{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:11px;margin-bottom:20px}.spend-avatar{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;color:white;font-size:13px;font-weight:750}.spend-account>div:nth-child(2){min-width:0}.spend-account strong,.spend-account span{display:block}.spend-account strong{color:var(--aurora-fg1);font-size:14px}.spend-account span{color:var(--aurora-fg4);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.spend-overview{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:22px;margin:4px 0 20px}.spend-ring{position:relative;width:130px;height:130px}.spend-ring svg{width:100%;height:100%;transform:rotate(-90deg)}.spend-ring>div{position:absolute;inset:0;display:grid;place-content:center;text-align:center}.spend-ring strong,.spend-ring span{display:block}.spend-ring strong{font-size:24px;color:var(--aurora-fg1);letter-spacing:-.03em}.spend-ring span{font-size:10px;color:var(--aurora-fg4)}.spend-used strong,.spend-days strong{display:block;color:var(--aurora-fg1);font-size:clamp(27px,5vw,38px);line-height:1;letter-spacing:-.035em}.spend-used span,.spend-days span{display:block;margin-top:5px;color:var(--aurora-fg4);font-size:12px}.spend-days{text-align:right}.spend-facts{display:grid;gap:9px;padding:16px 0;border-top:1px solid var(--aurora-border);border-bottom:1px solid var(--aurora-border)}.spend-facts>div{display:flex;justify-content:space-between;gap:16px;font-size:12px}.spend-facts span{color:var(--aurora-fg4)}.spend-facts strong{max-width:65%;color:var(--aurora-fg1);text-align:right;overflow-wrap:anywhere}.spend-provider-list{display:grid;gap:9px;padding:14px 0;border-bottom:1px solid var(--aurora-border)}.spend-provider{display:grid;grid-template-columns:90px 1fr;align-items:center;gap:10px}.spend-provider>strong{font-size:12px;color:var(--aurora-fg2)}.spend-provider>div{display:grid;grid-template-columns:minmax(80px,1fr) auto;align-items:center;gap:10px}.spend-provider-track,.spend-breakdown-track{height:7px;overflow:hidden;border-radius:999px;background:var(--aurora-chip)}.spend-provider-track i,.spend-breakdown-track i{display:block;height:100%;border-radius:inherit}.spend-provider small{color:var(--aurora-fg4);font-size:10px;white-space:nowrap}.spend-provider small b{color:var(--aurora-fg2)}.spend-trend{margin-top:16px;padding-top:2px}.spend-trend-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.spend-trend-heading>strong{color:var(--aurora-fg1);font-size:13px}.spend-trend-heading button{min-height:32px;border:1px solid var(--aurora-border);border-radius:9px;padding:5px 12px;background:transparent;color:var(--aurora-fg3);font-size:11px;cursor:pointer}.spend-trend-heading button.active{border-color:${PROJECTION_COLORS.average};background:color-mix(in srgb,${PROJECTION_COLORS.average} 12%,transparent);color:${PROJECTION_COLORS.average};font-weight:700}.spend-ranges{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px;margin:10px 0}.spend-ranges button{min-height:32px;border:1px solid var(--aurora-border);border-radius:8px;background:transparent;color:var(--aurora-fg4);font-size:11px;cursor:pointer}.spend-ranges button.active{border-color:${SOURCE_COLORS.claude};background:${SOURCE_COLORS.claude};color:white;font-weight:700}.spend-chart-wrap{min-width:0}.spend-plot{position:relative}.spend-chart{display:block;width:100%;height:auto;touch-action:pan-y}.spend-tooltip{position:absolute;top:42%;z-index:2;transform:translate(-50%,-100%);display:grid;gap:2px;padding:7px 9px;border:1px solid var(--aurora-border);border-radius:9px;background:var(--aurora-surface);box-shadow:0 8px 24px rgba(15,23,42,.18);pointer-events:none;white-space:nowrap}.spend-tooltip span{font-size:9px;color:var(--aurora-fg4)}.spend-tooltip strong{font-size:11px;color:var(--aurora-fg1)}.spend-projection-rows{display:grid;gap:7px;margin-top:8px}.spend-projection-rows>div{display:flex;align-items:baseline;gap:8px;min-width:0}.spend-projection-rows span{width:64px;color:var(--aurora-fg4);font-size:11px}.spend-projection-rows strong{font-size:12px}.spend-projection-rows small{min-width:0;color:var(--aurora-fg4);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.spend-legend{display:flex;flex-wrap:wrap;gap:7px 13px;margin-top:11px}.spend-legend span{display:inline-flex;align-items:center;gap:5px;color:var(--aurora-fg4);font-size:10px}.spend-legend i{width:8px;height:8px;border-radius:2px}.spend-empty{min-height:210px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;padding:20px;color:var(--aurora-fg4);font-size:12px;text-align:center}.spend-breakdown{margin-top:18px;padding-top:15px;border-top:1px solid var(--aurora-border)}.spend-breakdown-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px}.spend-breakdown-heading strong{color:var(--aurora-fg1);font-size:13px}.spend-breakdown-heading span{color:var(--aurora-fg4);font-size:10px}.spend-breakdown-list{display:grid;gap:10px}.spend-breakdown-row{display:grid;grid-template-columns:minmax(110px,1.1fr) minmax(100px,1.5fr) auto;align-items:center;gap:10px}.spend-breakdown-row>div:first-child{min-width:0}.spend-breakdown-row strong,.spend-breakdown-row small{display:block}.spend-breakdown-row strong{overflow:hidden;color:var(--aurora-fg2);font-size:12px;text-overflow:ellipsis;white-space:nowrap}.spend-breakdown-row small{margin-top:2px;color:var(--aurora-fg4);font-size:10px}.spend-breakdown-row>b{min-width:40px;color:var(--aurora-fg1);font-size:12px;text-align:right}.spend-note{margin:12px 0 0;color:var(--aurora-fg4);font-size:10px}.spend-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:18px;color:var(--aurora-fg4);font-size:10px}.spend-footer button{display:inline-flex;align-items:center;gap:6px;min-height:34px;border:1px solid var(--aurora-border);border-radius:9px;padding:6px 12px;background:var(--aurora-surface);color:var(--aurora-fg2);font-size:11px;cursor:pointer}.spend-footer button:disabled{opacity:.5;cursor:wait}.spend-skeleton{width:180px;height:24px;border-radius:10px;background:var(--aurora-chip);animation:spend-pulse 1.4s ease-in-out infinite}.spend-skeleton-chart{width:100%;height:240px;margin-top:18px}@keyframes spend-pulse{50%{opacity:.45}}
        @media(max-width:760px){.spend-section-heading>div>span{display:none}.spend-card{padding:18px 14px 16px}.spend-account{margin-bottom:14px}.spend-overview{grid-template-columns:auto 1fr auto;gap:10px}.spend-ring{width:96px;height:96px}.spend-ring strong{font-size:18px}.spend-used strong,.spend-days strong{font-size:26px}.spend-used span,.spend-days span{font-size:10px}.spend-facts>div{font-size:11px}.spend-provider{grid-template-columns:76px 1fr}.spend-provider>div{grid-template-columns:1fr}.spend-provider small{text-align:right}.spend-trend-heading{align-items:stretch}.spend-trend-heading button{min-width:140px}.spend-ranges{margin-top:8px}.spend-projection-rays{display:none}.spend-projection-rows{margin-top:2px}.spend-projection-rows>div{display:grid;grid-template-columns:60px auto minmax(0,1fr)}.spend-breakdown-row{grid-template-columns:1fr auto;grid-template-areas:"label pct" "bar bar";gap:4px 10px}.spend-breakdown-row>div:first-child{grid-area:label}.spend-breakdown-row>.spend-breakdown-track{grid-area:bar}.spend-breakdown-row>b{grid-area:pct}.spend-footer button{min-width:120px;justify-content:center}}
        @media(max-width:390px){.spend-overview{grid-template-columns:auto 1fr;grid-template-areas:"ring used" "ring days";gap:5px 10px}.spend-ring{grid-area:ring}.spend-used{grid-area:used}.spend-days{grid-area:days;text-align:left}.spend-ranges{grid-template-columns:repeat(3,minmax(0,1fr))}.spend-ranges button:last-child{grid-column:2}.spend-projection-rows small{white-space:normal}.spend-account>:last-child{display:none}}
      `}</style>
    </section>
  );
}
