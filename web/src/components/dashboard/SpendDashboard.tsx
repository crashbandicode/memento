"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/aurora/Icon";
import { Chip, Glass, SectionLabel } from "@/components/aurora/primitives";
import { authFetch, getApiBase } from "@/lib/api-client";

type SpendSource = "all" | "claude" | "cursor" | "codex";

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
  remaining?: string;
  remainingCents?: number;
  resetsAt?: string | null;
  billingCycleStart?: string | null;
  parts?: SpendPart[];
  missing?: string[];
}

interface StackPoint {
  t: number;
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
  color?: string;
}

interface BreakdownView {
  available?: boolean;
  models?: BreakdownRow[];
  tools?: BreakdownRow[];
  coverage?: number | string;
  note?: string;
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
  age_seconds?: number;
  reason?: string;
  error?: string | null;
  snapshot?: SpendSnapshot | null;
}

const SOURCE_COLORS: Record<SpendSource, string> = {
  all: "#8B5CF6",
  claude: "#F97316",
  cursor: "#3B82F6",
  codex: "#10B981",
};

const DEFAULT_SOURCES: SourceMeta[] = [
  { id: "all", label: "All" },
  { id: "claude", label: "Claude" },
  { id: "cursor", label: "Cursor" },
  { id: "codex", label: "Codex" },
];

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function centsLabel(value: number): string {
  if (Math.abs(value) >= 100_000) {
    return `$${(value / 100_000).toFixed(value >= 1_000_000 ? 0 : 1)}k`;
  }
  return `$${(value / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function compactNumber(value: number): string {
  return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function resetLabel(value?: string | null): string {
  if (!value) return "Reset unavailable";
  const reset = new Date(value);
  if (Number.isNaN(reset.getTime())) return "Reset unavailable";
  const days = Math.max(0, Math.ceil((reset.getTime() - Date.now()) / 86_400_000));
  return `${days} day${days === 1 ? "" : "s"} to reset`;
}

function path(points: StackPoint[], x: (value: number) => number, y: (value: number) => number, key: "u" | "y1"): string {
  return points
    .filter((point) => Number.isFinite(point.t) && Number.isFinite(number(point[key])))
    .map((point, index) => `${index === 0 ? "M" : "L"}${x(point.t).toFixed(2)},${y(number(point[key])).toFixed(2)}`)
    .join(" ");
}

function SpendHistoryChart({ history, source }: { history?: HistoryView; source: SpendSource }) {
  const width = 760;
  const height = 240;
  const inset = { left: 48, right: 16, top: 16, bottom: 30 };
  const traces = history?.stack?.traces?.filter((trace) => (trace.points?.length || 0) > 1) ?? [];
  const outline = history?.stack?.outline?.length
    ? history.stack.outline
    : (history?.points ?? []);
  const allPoints = [...outline, ...traces.flatMap((trace) => trace.points ?? [])];
  const timestamps = allPoints.map((point) => number(point.t)).filter(Boolean);
  const minTime = Math.min(...timestamps);
  const maxTime = Math.max(...timestamps);
  const maxValue = Math.max(
    1,
    ...allPoints.map((point) => Math.max(number(point.u), number(point.y1), number(point.l))),
  );
  const hasChart = Number.isFinite(minTime) && Number.isFinite(maxTime) && maxTime > minTime && outline.length > 1;
  const x = (value: number) => inset.left + ((value - minTime) / (maxTime - minTime)) * (width - inset.left - inset.right);
  const y = (value: number) => height - inset.bottom - (value / (maxValue * 1.06)) * (height - inset.top - inset.bottom);
  const gridValues = [0, 0.25, 0.5, 0.75, 1].map((ratio) => maxValue * ratio);

  if (!hasChart) {
    return (
      <div className="spend-empty" role="status">
        <Icon name="activity" size={20} />
        <span>{source === "codex" ? "Codex history will appear after its analytics cache has a session mix." : "No billing history is available for this period yet."}</span>
      </div>
    );
  }

  return (
    <div className="spend-chart-wrap">
      <svg className="spend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${source} month-to-date spend history`}>
        <title>{source} month-to-date spend history</title>
        {gridValues.map((value) => (
          <g key={value}>
            <line x1={inset.left} x2={width - inset.right} y1={y(value)} y2={y(value)} stroke="var(--aurora-border)" strokeWidth="1" />
            <text x={inset.left - 8} y={y(value) + 4} textAnchor="end" fill="var(--aurora-fg4)" fontSize="10">{centsLabel(value)}</text>
          </g>
        ))}
        {traces.map((trace, index) => {
          const points = trace.points ?? [];
          const upper = points.map((point) => `${x(point.t).toFixed(2)},${y(number(point.y1)).toFixed(2)}`);
          const lower = [...points].reverse().map((point) => `${x(point.t).toFixed(2)},${y(number(point.y0)).toFixed(2)}`);
          const d = `M${upper.join(" L")} L${lower.join(" L")} Z`;
          return <path key={`${trace.model || trace.label || index}`} d={d} fill={trace.color || SOURCE_COLORS[source]} opacity="0.66" />;
        })}
        <path
          d={path(outline, x, y, "u") || path(outline, x, y, "y1")}
          fill="none"
          stroke={SOURCE_COLORS[source]}
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <text x={inset.left} y={height - 8} fill="var(--aurora-fg4)" fontSize="10">{new Date(minTime).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</text>
        <text x={width - inset.right} y={height - 8} textAnchor="end" fill="var(--aurora-fg4)" fontSize="10">{new Date(maxTime).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</text>
      </svg>
      {traces.length > 0 && (
        <div className="spend-legend">
          {traces.slice(0, 8).map((trace, index) => (
            <span key={`${trace.model || trace.label || index}`}><i style={{ background: trace.color || SOURCE_COLORS[source] }} />{trace.label || trace.model || "Other"}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function QuotaBars({ parts }: { parts: SpendPart[] }) {
  return (
    <div className="spend-quota-list">
      {parts.map((part) => {
        const source = (part.source?.toLowerCase() || "all") as SpendSource;
        const pct = Math.max(0, Math.min(100, number(part.pctUsed)));
        return (
          <div key={part.source || part.name}>
            <div className="spend-row-label"><span>{part.name || part.source}</span><span>{part.used || centsLabel(number(part.usedCents))} / {part.limit || centsLabel(number(part.limitCents))}</span></div>
            <div className="spend-progress"><i style={{ width: `${pct}%`, background: SOURCE_COLORS[source] || SOURCE_COLORS.all }} /></div>
          </div>
        );
      })}
    </div>
  );
}

function Breakdown({ title, rows, colors }: { title: string; rows: BreakdownRow[]; colors: string[] }) {
  if (!rows.length) return null;
  return (
    <div className="spend-breakdown">
      <div className="spend-subtitle">{title}</div>
      {rows.slice(0, 8).map((row, index) => {
        const label = row.label || row.model || row.tool || "Unknown";
        const share = Math.max(0, Math.min(100, number(row.share)));
        return (
          <div className="spend-breakdown-row" key={`${label}-${index}`}>
            <div className="spend-row-label">
              <span title={label}>{label}</span>
              <span>{row.totalCents != null ? centsLabel(row.totalCents) : row.totalTokens != null ? `${compactNumber(row.totalTokens)} tokens` : `${compactNumber(number(row.events))} events`}</span>
            </div>
            <div className="spend-progress"><i style={{ width: `${share}%`, background: row.color || colors[index % colors.length] || SOURCE_COLORS.all }} /></div>
          </div>
        );
      })}
    </div>
  );
}

function Projections({ projection }: { projection?: ProjectionView["projection"] }) {
  const scenarios = [
    ["Worst", projection?.worst],
    ["Realistic", projection?.realistic],
    ["Average", projection?.average],
  ] as const;
  if (!scenarios.some(([, scenario]) => scenario?.dollars || scenario?.cents != null)) return null;
  return (
    <div>
      <div className="spend-subtitle">Cycle projections</div>
      <div className="spend-projections">
        {scenarios.map(([label, scenario]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{scenario?.dollars || centsLabel(number(scenario?.cents))}</strong>
            <small>{number(scenario?.pctOfLimit).toFixed(0)}% of limit{scenario?.hits100Pct ? ` · ${scenario.hits100Pct}` : ""}</small>
          </div>
        ))}
      </div>
    </div>
  );
}

function LoadingCard() {
  return (
    <div style={{ marginBottom: 24 }} aria-label="Loading AI spend">
      <SectionLabel>AI spend</SectionLabel>
      <Glass padding={22} radius={22}>
        <div className="spend-skeleton" />
        <div className="spend-skeleton spend-skeleton-chart" />
      </Glass>
    </div>
  );
}

export default function SpendDashboard() {
  const [envelope, setEnvelope] = useState<SpendEnvelope | null>(null);
  const [selectedSource, setSelectedSource] = useState<SpendSource>("all");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async (force = false, signal?: AbortSignal) => {
    try {
      const response = await authFetch(`${getApiBase()}/api/dashboard/spend${force ? "?refresh=true" : ""}`, { signal });
      if (!response.ok) throw new Error(`Spend dashboard returned ${response.status}`);
      const next = await response.json() as SpendEnvelope;
      setEnvelope(next);
      setFailed(false);
      const preferred = next.snapshot?.ui?.defaultSource;
      if (preferred && DEFAULT_SOURCES.some((source) => source.id === preferred)) setSelectedSource(preferred);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        console.error(error);
        setFailed(true);
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void load(false, controller.signal), 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [load]);

  const snapshot = envelope?.snapshot;
  const sources = snapshot?.ui?.sources?.filter((source) => DEFAULT_SOURCES.some((candidate) => candidate.id === source.id)) || DEFAULT_SOURCES;
  const summary = snapshot?.spend?.[selectedSource];
  const modelView = snapshot?.models?.[selectedSource];
  const toolView = snapshot?.tools?.[selectedSource];
  const history = snapshot?.history?.[selectedSource];
  const projection = snapshot?.projections?.[selectedSource]?.projection;
  const modelColors = snapshot?.ui?.modelBarColors || ["#8B5CF6", "#EC4899", "#3B82F6", "#10B981", "#F97316"];
  const models = useMemo(() => modelView?.models || [], [modelView]);
  const tools = useMemo(() => toolView?.tools || [], [toolView]);

  if (loading && !envelope) return <LoadingCard />;
  if (envelope?.reason === "not_configured") return null;
  if ((!envelope?.available || !snapshot) && failed) return null;
  if (!envelope?.available || !snapshot || !summary) return null;

  const pct = Math.max(0, Math.min(100, number(summary.pctUsed)));
  const fetchedAt = snapshot.fetchedAt || envelope.cached_at;

  return (
    <section className="spend-section" aria-labelledby="spend-dashboard-title">
      <div className="spend-section-heading">
        <SectionLabel style={{ margin: 0 }}><span id="spend-dashboard-title">AI spend</span></SectionLabel>
        <div>
          {envelope.stale && <Chip tone="warn">Updating</Chip>}
          <span className="spend-fetched">{fetchedAt ? `As of ${new Date(fetchedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Latest snapshot"}</span>
          <button className="spend-refresh" type="button" aria-label="Refresh spending data" title="Refresh spending data" disabled={refreshing} onClick={() => { setRefreshing(true); void load(true); }}>
            <Icon name="refresh" size={14} />
          </button>
        </div>
      </div>
      <Glass padding={0} radius={24} style={{ overflow: "hidden" }}>
        <div className="spend-tabs" role="tablist" aria-label="Spending source">
          {sources.map((source) => (
            <button
              type="button"
              role="tab"
              aria-selected={selectedSource === source.id}
              className={selectedSource === source.id ? "active" : ""}
              key={source.id}
              onClick={() => setSelectedSource(source.id)}
            >
              <i style={{ background: SOURCE_COLORS[source.id] }} />{source.label}
            </button>
          ))}
        </div>

        <div className="spend-content">
          <div className="spend-summary">
            <div><span>Used</span><strong>{summary.used || centsLabel(number(summary.usedCents))}</strong></div>
            <div><span>Limit</span><strong>{summary.limit || centsLabel(number(summary.limitCents))}</strong></div>
            <div><span>Remaining</span><strong>{summary.remaining || centsLabel(number(summary.remainingCents))}</strong></div>
            <div><span>Cycle</span><strong>{projection?.daysLeft != null ? `${projection.daysLeft} days left` : resetLabel(summary.resetsAt)}</strong></div>
          </div>
          <div className="spend-total-progress" aria-label={`${pct.toFixed(1)} percent of limit used`}><i style={{ width: `${pct}%`, background: SOURCE_COLORS[selectedSource] }} /></div>

          <div className="spend-main-grid">
            <div>
              <div className="spend-subtitle">Month-to-date usage</div>
              <SpendHistoryChart history={history} source={selectedSource} />
            </div>
            <div className="spend-side">
              {selectedSource === "all" && (summary.parts?.length || 0) > 0 ? (
                <>
                  <div className="spend-subtitle">Provider quotas</div>
                  <QuotaBars parts={summary.parts || []} />
                </>
              ) : (
                <Projections projection={projection} />
              )}
            </div>
          </div>

          {selectedSource === "all" && <Projections projection={projection} />}
          {(models.length > 0 || tools.length > 0) && (
            <details className="spend-details">
              <summary>Model and tool detail <span>{models.length + tools.length} rows</span></summary>
              <div className="spend-detail-grid">
                <Breakdown title={`Models${modelView?.coverage ? ` · ${modelView.coverage} coverage` : ""}`} rows={models} colors={modelColors} />
                <Breakdown title="Tools" rows={tools} colors={modelColors} />
              </div>
              {modelView?.note && <p className="spend-note">{modelView.note}</p>}
            </details>
          )}
        </div>
      </Glass>
      <style jsx>{`
        .spend-section { margin-bottom: 24px; min-width: 0; }
        .spend-section-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:8px 4px 12px; }
        .spend-section-heading > div { display:flex; align-items:center; gap:8px; }
        .spend-fetched { color:var(--aurora-fg4); font-size:11px; }
        .spend-refresh { width:30px; height:30px; display:grid; place-items:center; border:1px solid var(--aurora-border); border-radius:10px; color:var(--aurora-fg3); background:var(--aurora-surface); cursor:pointer; }
        .spend-refresh:disabled { opacity:.45; cursor:wait; }
        .spend-tabs { display:flex; gap:6px; overflow-x:auto; padding:14px 18px 0; scrollbar-width:none; }
        .spend-tabs::-webkit-scrollbar { display:none; }
        .spend-tabs button { flex:0 0 auto; display:flex; align-items:center; gap:7px; border:1px solid transparent; border-radius:999px; padding:8px 13px; background:transparent; color:var(--aurora-fg3); font-size:12px; font-weight:600; cursor:pointer; }
        .spend-tabs button i { width:7px; height:7px; border-radius:50%; }
        .spend-tabs button.active { color:var(--aurora-fg1); border-color:var(--aurora-border); background:var(--aurora-surface); box-shadow:0 6px 20px -14px rgba(15,23,42,.5); }
        .spend-content { padding:18px 22px 22px; min-width:0; }
        .spend-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
        .spend-summary > div { min-width:0; padding:12px 14px; border-radius:15px; background:var(--aurora-surface); border:1px solid var(--aurora-border); }
        .spend-summary span { display:block; color:var(--aurora-fg4); font-size:10px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; }
        .spend-summary strong { display:block; margin-top:4px; color:var(--aurora-fg1); font-size:clamp(16px,2.5vw,21px); font-weight:650; letter-spacing:-.03em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .spend-total-progress,.spend-progress { overflow:hidden; background:var(--aurora-chip); border-radius:999px; }
        .spend-total-progress { height:5px; margin:13px 2px 22px; }
        .spend-progress { height:5px; }
        .spend-total-progress i,.spend-progress i { display:block; height:100%; border-radius:inherit; transition:width .25s ease; }
        .spend-main-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(230px,1fr); gap:20px; align-items:start; }
        .spend-subtitle { color:var(--aurora-fg3); font-size:11px; font-weight:650; letter-spacing:.04em; text-transform:uppercase; margin:0 0 10px; }
        .spend-chart-wrap { min-width:0; }
        .spend-chart { display:block; width:100%; height:auto; min-height:180px; overflow:visible; }
        .spend-legend { display:flex; flex-wrap:wrap; gap:7px 12px; padding-left:48px; margin-top:5px; }
        .spend-legend span { display:inline-flex; align-items:center; gap:5px; color:var(--aurora-fg4); font-size:10px; }
        .spend-legend i { width:7px; height:7px; border-radius:2px; }
        .spend-empty { min-height:190px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; padding:22px; border:1px dashed var(--aurora-border); border-radius:16px; color:var(--aurora-fg4); text-align:center; font-size:12px; }
        .spend-side { min-width:0; padding:14px; border-radius:17px; border:1px solid var(--aurora-border); background:var(--aurora-surface); }
        .spend-quota-list,.spend-breakdown { display:grid; gap:12px; min-width:0; }
        .spend-row-label { min-width:0; display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:5px; color:var(--aurora-fg3); font-size:11px; }
        .spend-row-label span:first-child { color:var(--aurora-fg2); font-weight:550; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .spend-row-label span:last-child { flex:0 0 auto; }
        .spend-projections { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-bottom:16px; }
        .spend-projections > div { min-width:0; padding:12px; border-radius:14px; border:1px solid var(--aurora-border); background:var(--aurora-surface); }
        .spend-projections span,.spend-projections small { display:block; color:var(--aurora-fg4); font-size:10px; }
        .spend-projections strong { display:block; color:var(--aurora-fg1); font-size:16px; margin:3px 0; overflow:hidden; text-overflow:ellipsis; }
        .spend-details { margin-top:8px; border-top:1px solid var(--aurora-border); padding-top:14px; }
        .spend-details summary { display:flex; align-items:center; justify-content:space-between; color:var(--aurora-fg2); font-size:12px; font-weight:600; cursor:pointer; list-style:none; }
        .spend-details summary::-webkit-details-marker { display:none; }
        .spend-details summary span { color:var(--aurora-fg4); font-weight:400; }
        .spend-detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:24px; padding-top:16px; }
        .spend-note { margin:12px 0 0; color:var(--aurora-fg4); font-size:10px; }
        .spend-skeleton { width:180px; height:24px; border-radius:10px; background:var(--aurora-chip); animation:spend-pulse 1.4s ease-in-out infinite; }
        .spend-skeleton-chart { width:100%; height:190px; margin-top:18px; }
        @keyframes spend-pulse { 50% { opacity:.45; } }
        @media (max-width: 760px) {
          .spend-content { padding:14px 14px 18px; }
          .spend-tabs { padding:12px 12px 0; }
          .spend-summary { grid-template-columns:repeat(2,minmax(0,1fr)); }
          .spend-main-grid { grid-template-columns:minmax(0,1fr); }
          .spend-side { padding:13px; }
          .spend-detail-grid { grid-template-columns:minmax(0,1fr); gap:18px; }
          .spend-projections { grid-template-columns:minmax(0,1fr); }
          .spend-chart { min-height:150px; }
          .spend-legend { padding-left:0; }
          .spend-fetched { display:none; }
        }
      `}</style>
    </section>
  );
}
