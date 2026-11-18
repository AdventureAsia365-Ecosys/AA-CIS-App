"use client";
// app/admin/platform/delivery/RangeCharts.tsx — AA-1135: the range picker, card sorting and the charts of Platform ›
// Delivery v2: DORA trend (one app or every app compared), the delivery flow diagram (median / p95 per step, the
// slowest step highlighted, a text list that screen readers read as is), the merge-queue charts (wait histogram,
// ejection reasons, weekly ejection rate) and the churn treemap with drill-down.
//
// The range lives in the URL (?range=7d|28d|90d|custom&from=&to=) and applies to every tab; the API serves the
// collector's preset or recomputes a custom range (api/services/delivery_range.py). Pure helpers:
// lib/deliveryRange.ts. Kit only.

import { useMemo, useState } from "react";
import { Bar, BarChart, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Button, CHART_TOOLTIP, DataTable, DateField, K, Pressable, Section, SegmentedControl, Select, controlStyle, formatDay,
  useFormat, useLang, useT, useUrlFilters, type ColumnDef,
} from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import { fmtHours, type DeliveryApp } from "@/lib/deliveryMetrics";
import type { ChurnRow, GitWindow, QueueReason, QueueSection } from "@/lib/deliveryActivity";
import {
  FLOW_POINTS, PRESETS, SERIES_VARS, TREND_METRICS, Y_AXIS, appTrendRows, customProblem, flowSteps, histRows,
  parseRange, rangeParams, rangeWhen, squarify, trendRows, weeklyRows, windowNote, type RangeMeta, type RangeProblem,
  type RangeWindow, type TrendMetric,
} from "@/lib/deliveryRange";
import { RANGE } from "./range.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const row = { display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" } as const;
// AA-1135 follow-up: the DORA trend series (lib/deliveryRange SERIES_VARS: six hues apart in both themes)
const SERIES = SERIES_VARS;
// the custom range's two date boxes: the kit control look, the width of their 150 px wrapper
const DATE_BOX = { ...controlStyle("sm"), width: "100%" } as const;

export function useRangeT() {
  return useT(RANGE);
}
type RT = ReturnType<typeof useRangeT>;
type RKey = Parameters<RT>[0];

const DAY_MS = 86_400_000;
const shiftDay = (day: string, days: number) => new Date(Date.parse(`${day}T00:00:00Z`) + days * DAY_MS)
  .toISOString().slice(0, 10);

function problemText(p: RangeProblem, meta: RangeMeta | null, t: RT): string {
  if (p === "tooLong") return t("probtooLong", meta?.max_days ?? 0);
  if (p === "beforeStore") return t("probbeforeStore", meta?.store_from ?? "");
  if (p === "afterLast") return t("probafterLast", meta?.last_day ?? "");
  return t(`prob${p}` as RKey);
}

function Showing({ meta, t }: { meta: RangeMeta | null; t: RT }) {
  const { formatDateTime } = useFormat();
  if (!meta?.start || !meta?.end) return null;
  return (
    <span style={small} data-testid="delivery-range-showing">
      {t("showing", formatDateTime(meta.start), formatDateTime(meta.end))}
      {meta.cached ? ` · ${t("cached")}` : ""}
    </span>
  );
}

/** "over the last N days" / "from <day> to <day>": the page's range in a sentence (EN / VI, kit day format). */
export function useRangeWhen(): string {
  const t = useRangeT();
  const { get } = useUrlFilters();
  const lang = useLang();
  const w = rangeWhen(parseRange(get));
  return w.custom ? t("whenDates", formatDay(w.from, lang), formatDay(w.to, lang)) : t("whenDays", w.days);
}

/** An app's window in its header: the dates of a custom range or "Last N days", and a note when the range starts
 *  before the app's complete history (its numbers then cover fewer days than asked). `clipOnly` shows the note alone
 *  (the cards); `completeSince` clips a window the API serves unclipped (the git activity). */
export function WindowLabel({ window, completeSince, clipOnly = false, testId }: {
  window: RangeWindow | null | undefined; completeSince?: string | null; clipOnly?: boolean; testId?: string;
}) {
  const t = useRangeT();
  const { get } = useUrlFilters();
  const n = windowNote(window, parseRange(get).range === "custom", completeSince);
  if (!n || (clipOnly && !n.clipped)) return null;
  const label = n.custom ? t("dates", n.from, n.to) : t("lastDays", n.days);
  const note = n.clipped ? t("covered", n.coveredDays, n.startsOn) : "";
  return (
    <span data-testid={testId}>
      {clipOnly ? ` · ${note}` : note ? `${label} · ${note}` : label}
    </span>
  );
}

function CustomFields({ meta, from, to, setFrom, setTo, onApply, disabled, t }: {
  meta: RangeMeta | null; from: string; to: string; setFrom: (v: string) => void; setTo: (v: string) => void;
  onApply: () => void; disabled: boolean; t: RT;
}) {
  const min = meta?.store_from ?? undefined;
  const max = meta?.last_day ?? undefined;
  return (
    <span style={row}>
      <DateField aria-label={t("from")} value={from} onChange={setFrom} min={min} max={max}
        style={DATE_BOX} wrapperStyle={{ width: 150 }} />
      <span aria-hidden style={small}>–</span>
      <DateField aria-label={t("to")} value={to} onChange={setTo} min={min} max={max} style={DATE_BOX}
        wrapperStyle={{ width: 150 }} />
      <span data-testid="delivery-range-apply"><Button size="sm" disabled={disabled} onClick={onApply}>
        {t("apply")}</Button></span>
    </span>
  );
}

function RangeNotes({ problem, meta, error, t }: {
  problem: RangeProblem | null; meta: RangeMeta | null; error?: string | null; t: RT;
}) {
  return (
    <>
      {problem && problem !== "missing" && <span role="status" style={{ ...small, color: K.amber }}>
        {problemText(problem, meta, t)}</span>}
      {error && <span role="alert" style={{ ...small, color: K.danger }}>{t("rangeError")}: {error}</span>}
    </>
  );
}

/** The one range picker of the page: presets, or a custom from / to applied with a button. */
export function RangeBar({ meta, error }: { meta: RangeMeta | null; error?: string | null }) {
  const t = useRangeT();
  const { get, setMany } = useUrlFilters();
  const choice = parseRange(get);
  const last = meta?.last_day ?? "";
  const [mode, setMode] = useState<string>(choice.range);
  const [from, setFrom] = useState(choice.from || (last ? shiftDay(last, -13) : ""));
  const [to, setTo] = useState(choice.to || last);
  const problem = mode === "custom" ? customProblem(from, to, meta) : null;
  const pick = (v: string) => {
    setMode(v);
    if (v !== "custom") setMany(rangeParams({ range: v as typeof choice.range, from: "", to: "" }));
  };
  const options = [...PRESETS.map((p) => ({ value: p, label: t("preset", Number(p.slice(0, -1))) })),
    { value: "custom", label: t("custom") }];
  return (
    <div style={{ display: "grid", gap: 6 }} data-testid="delivery-range">
      <div style={row}>
        <SegmentedControl<string> label={t("rangePicker")} size="sm" value={mode} onChange={pick}
          testId="delivery-range-picker" options={options} />
        {mode === "custom" && <CustomFields meta={meta} from={from} to={to} setFrom={setFrom} setTo={setTo} t={t}
          disabled={problem !== null} onApply={() => setMany(rangeParams({ range: "custom", from, to }))} />}
        <Showing meta={meta} t={t} />
      </div>
      <RangeNotes problem={problem} meta={meta} error={error} t={t} />
    </div>
  );
}

/** The card sort (worst first, by name, or by a metric worst first); it lives in the URL (?sort=). */
export function SortPicker({ value, metrics, onChange }: {
  value: string; metrics: { key: string; label: string }[]; onChange: (v: string) => void;
}) {
  const t = useRangeT();
  return (
    <span style={{ ...row, gap: 6 }} data-testid="delivery-sort">
      <span style={small}>{t("sortLabel")}</span>
      <Select size="sm" aria-label={t("sortLabel")} value={value} onChange={(e) => onChange(e.target.value)}
        style={{ width: "auto", minWidth: 170 }}
        options={[{ value: "worst", label: t("sortWorst") }, { value: "name", label: t("sortName") },
          ...metrics.map((m) => ({ value: m.key, label: t("sortBy", m.label) }))]} />
    </span>
  );
}

// ── DORA trend ───────────────────────────────────────────────────────────────────────────────────────────────────

function useSeriesColors(n: number): string[] {
  const vars = Object.fromEntries(SERIES.map((v, i) => [`c${i}`, v]));
  const c = useChartColors({ ...vars, axis: "--aa-muted" }) as Record<string, string>;
  return Array.from({ length: n }, (_, i) => c[`c${i % SERIES.length}`]);
}

function TrendChart({ rows, series, height = 220 }: {
  rows: Record<string, number | string | null>[]; series: { key: string; name: string }[]; height?: number;
}) {
  const colors = useSeriesColors(series.length);
  const axis = useChartColors({ axis: "--aa-muted" }).axis;
  return (
    <div style={{ width: "100%", height }} aria-hidden>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 6, right: 10, bottom: 4, left: Y_AXIS.marginLeft }}>
          <XAxis dataKey="label" tick={{ fontSize: 10, fill: axis }} interval="preserveStartEnd" minTickGap={10} />
          <YAxis tick={{ fontSize: Y_AXIS.fontSize, fill: axis }} width={Y_AXIS.width} />
          <Tooltip {...CHART_TOOLTIP} />
          {series.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
          {series.map((s, i) => <Line key={s.key} type="monotone" dataKey={s.key} name={s.name} stroke={colors[i]}
            strokeWidth={2} dot={{ r: 2 }} connectNulls isAnimationActive={false} />)}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

type TrendRow = Record<string, number | string | null>;

function TrendTable({ rows, series, t, id }: {
  rows: TrendRow[]; series: { key: string; name: string }[]; t: RT; id: string;
}) {
  const columns = useMemo<ColumnDef<TrendRow, unknown>[]>(() => [
    { id: "week", header: t("colWeek"), cell: ({ row: { original: r } }) => String(r.week) },
    ...series.map((s) => ({ id: s.key, header: s.name,
      cell: ({ row: { original: r } }: { row: { original: TrendRow } }) => String(r[s.key] ?? "—") })),
  ], [series, t]);
  return (
    <details style={small}>
      <summary style={{ cursor: "pointer" }}>{t("trendTable")}</summary>
      <DataTable<TrendRow> tableId={`delivery-trend-${id}`} data={rows} columns={columns}
        getRowId={(r) => String(r.week)} paginate={false} searchable={false} tableMinWidth={360} />
    </details>
  );
}

/** Every app compared on one DORA key (Overview). */
export function TrendCompare({ apps }: { apps: DeliveryApp[] }) {
  const t = useRangeT();
  const [metric, setMetric] = useState<TrendMetric>("deploys");
  const rows = useMemo(() => trendRows(apps, metric), [apps, metric]);
  const series = useMemo(() => apps.map((a) => ({ key: a.id, name: a.name })), [apps]);
  return (
    <Section title={t("secTrend")} description={t("secTrendDesc")} testId="delivery-trend"
      actions={<SegmentedControl<TrendMetric> label={t("trendMetric")} size="sm" appearance="chips" value={metric}
        onChange={setMetric} testId="delivery-trend-metric"
        options={TREND_METRICS.map((m) => ({ value: m, label: t(`trend${m}` as RKey) }))} />}>
      <div style={{ display: "grid", gap: 8 }} role="group" aria-label={`${t("secTrend")}: ${t(`trend${metric}` as RKey)}`}>
        <TrendChart rows={rows} series={series} />
        <TrendTable rows={rows} series={series} t={t} id="compare" />
      </div>
    </Section>
  );
}

/** One app's four DORA keys, a small chart each (app tab). */
export function AppTrend({ app }: { app: DeliveryApp }) {
  const t = useRangeT();
  const rows = useMemo(() => appTrendRows(app), [app]);
  const series = useMemo(() => TREND_METRICS.map((m) => ({ key: m, name: t(`trend${m}` as RKey) })), [t]);
  return (
    <Section title={t("secTrend")} description={t("secTrendDesc")} testId="delivery-app-trend">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))", gap: 12 }}>
        {series.map((s) => (
          <div key={s.key} style={{ display: "grid", gap: 4, minWidth: 0 }}>
            <span style={{ ...small, fontWeight: 600 }}>{s.name}</span>
            <TrendChart rows={rows} series={[s]} height={140} />
          </div>
        ))}
      </div>
      <div style={{ marginTop: 8 }}><TrendTable rows={rows} series={series} t={t} id={app.id} /></div>
    </Section>
  );
}

// ── delivery flow ────────────────────────────────────────────────────────────────────────────────────────────────

function FlowPointChip({ label }: { label: string }) {
  return <span style={{ display: "inline-flex", padding: "6px 10px", borderRadius: 999, border: `1px solid ${K.line}`,
    background: K.card, fontSize: 12.5, fontWeight: 600, color: K.ink, whiteSpace: "nowrap" }}>{label}</span>;
}

/** Pull request opened → CI green → in the queue → merged → deployed → verified, median and p95 per step. */
export function FlowDiagram({ app, stepLabel }: { app: DeliveryApp; stepLabel: (segment: string) => string }) {
  const t = useRangeT();
  const steps = flowSteps(app.lead_time_segments);
  const slow = steps.find((s) => s.slowest);
  const any = steps.some((s) => s.n > 0);
  return (
    <Section title={t("secFlow")} description={t("secFlowDesc")} testId="delivery-flow">
      {!any ? <span style={small}>{t("flowNone")}</span> : (
        <div style={{ display: "grid", gap: 10 }}>
          {slow && <span style={{ ...small, color: K.ink2 }} data-testid="delivery-flow-summary">
            {t("flowSummary", stepLabel(slow.segment), fmtHours(slow.p50_h), fmtHours(slow.p95_h))}</span>}
          <ol aria-label={t("secFlow")} style={{ listStyle: "none", margin: 0, padding: 0, display: "flex",
            flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <li><FlowPointChip label={t(`point${FLOW_POINTS[0]}` as RKey)} /></li>
            {steps.map((s) => (
              <li key={s.segment} style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                <span data-testid={`delivery-flow-${s.segment}`} data-slowest={s.slowest || undefined}
                  style={{ display: "grid", gap: 3, minWidth: 128, padding: "6px 10px", borderRadius: 8,
                    border: `${s.slowest ? 2 : 1}px solid ${s.slowest ? K.amber : K.line2}`,
                    background: s.slowest ? K.amberSoft : "transparent" }}>
                  <span style={{ ...small, color: K.ink2 }}>→ {stepLabel(s.segment)}</span>
                  <span style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>
                    {t("median")} {fmtHours(s.p50_h)} · p95 {fmtHours(s.p95_h)}</span>
                  <span style={small}>n {s.n}{s.slowest ? ` · ${t("slowest")}` : ""}</span>
                  <span aria-hidden style={{ height: 4, borderRadius: 2, background: K.line2, overflow: "hidden" }}>
                    <span style={{ display: "block", height: "100%", width: `${Math.round(s.share * 100)}%`,
                      background: s.slowest ? K.amber : K.accent }} />
                  </span>
                </span>
                <FlowPointChip label={t(`point${s.to}` as RKey)} />
              </li>
            ))}
          </ol>
        </div>
      )}
    </Section>
  );
}

// ── merge queue charts ───────────────────────────────────────────────────────────────────────────────────────────

function SmallBars({ rows, dataKey, name, color, height = 180, vertical }: {
  rows: Record<string, unknown>[]; dataKey: string; name: string; color: string; height?: number; vertical?: boolean;
}) {
  const axis = useChartColors({ axis: "--aa-muted" }).axis;
  return (
    <div style={{ width: "100%", height }} aria-hidden>
      <ResponsiveContainer width="100%" height="100%">
        {vertical ? (
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 12, bottom: 4, left: 8 }}>
            <XAxis type="number" allowDecimals={false} tick={{ fontSize: 10, fill: axis }} />
            <YAxis type="category" dataKey="label" width={120} tick={{ fontSize: 10, fill: axis }} />
            <Tooltip {...CHART_TOOLTIP} />
            <Bar dataKey={dataKey} name={name} fill={color} isAnimationActive={false} />
          </BarChart>
        ) : (
          <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: Y_AXIS.marginLeft }}>
            <XAxis dataKey="label" tick={{ fontSize: 10, fill: axis }} interval={0} angle={-20} textAnchor="end"
              height={42} />
            <YAxis allowDecimals={false} tick={{ fontSize: Y_AXIS.fontSize, fill: axis }} width={Y_AXIS.width} />
            <Tooltip {...CHART_TOOLTIP} />
            <Bar dataKey={dataKey} name={name} fill={color} isAnimationActive={false} />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

function TextList({ items }: { items: string[] }) {
  return <ul style={{ ...small, margin: "6px 0 0", paddingLeft: 18 }}>{items.map((x) => <li key={x}>{x}</li>)}</ul>;
}

/** Wait histogram, ejection reasons and the weekly ejection rate of one repo's merge queue. */
export function QueueCharts({ queue, reasonLabel }: {
  queue: QueueSection; reasonLabel: (r: QueueReason) => string;
}) {
  const t = useRangeT();
  const c = useChartColors({ wait: "--aa-accent", reason: "--aa-red", rate: "--aa-kit-amber", axis: "--aa-muted" });
  const hist = histRows(queue.wait_histogram);
  const reasons = (queue.reasons ?? []).map((r) => ({ label: reasonLabel(r), n: r.n }));
  const weeks = weeklyRows(queue.weekly);
  const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 320px), 1fr))", gap: 14 };
  return (
    <div style={grid} data-testid="queue-charts">
      <Section title={t("secHist")} description={t("secHistDesc")} testId="queue-histogram">
        {hist.some((h) => h.n > 0) ? <>
          <SmallBars rows={hist} dataKey="n" name={t("entries")} color={c.wait} />
          <TextList items={hist.map((h) => `${h.label}: ${h.n}`)} />
        </> : <span style={small}>{t("noData")}</span>}
      </Section>
      <Section title={t("secReasonsChart")} testId="queue-reason-bars">
        {reasons.length ? <>
          <SmallBars rows={reasons} dataKey="n" name={t("entries")} color={c.reason} vertical
            height={Math.max(120, reasons.length * 32 + 40)} />
          <TextList items={reasons.map((r) => `${r.label}: ${r.n}`)} />
        </> : <span style={small}>{t("noData")}</span>}
      </Section>
      <Section title={t("secWeekly")} description={t("secWeeklyDesc")} testId="queue-weekly">
        {weeks.some((w) => w.judged > 0) ? <>
          <SmallBars rows={weeks} dataKey="rate" name={t("ejectionRate")} color={c.rate} />
          <TextList items={weeks.map((w) => `${w.label}: ${w.rate === null ? "—" : `${w.rate} %`} (n ${w.judged})`)} />
        </> : <span style={small}>{t("noData")}</span>}
      </Section>
    </div>
  );
}

// ── churn treemap ────────────────────────────────────────────────────────────────────────────────────────────────

const TREE_W = 1000;
const TREE_H = 420;

/** Directories (or a directory's children) sized by churn; a top-level box opens its children (?dir=). */
export function ChurnTreemap({ win, dir, onPick }: {
  win: GitWindow | undefined; dir: ChurnRow | null; onPick: (path: string | null) => void;
}) {
  const t = useRangeT();
  const rows = useMemo(() => (dir ? dir.children ?? [] : win?.dirs ?? []), [dir, win]);
  const rects = useMemo(() => squarify(rows.map((r) => ({ value: r.churn, item: r })), TREE_W, TREE_H), [rows]);
  const max = Math.max(1, ...rows.map((r) => r.churn));
  return (
    <Section title={t("secTreemap")} description={t("secTreemapDesc")} testId="git-treemap"
      actions={dir ? <Button size="sm" variant="secondary" onClick={() => onPick(null)}>{t("allDirs")}</Button>
        : undefined}>
      {!rects.length ? <span style={small}>{t("noData")}</span> : (
        <div role="list" aria-label={t("secTreemap")} data-testid="git-treemap-boxes"
          style={{ position: "relative", width: "100%", height: 300,
          borderRadius: 8, overflow: "hidden", background: K.line2 }}>
          {rects.map((r) => {
            const label = t("treemapItem", r.item.path, r.item.churn, r.item.commits);
            const box = { position: "absolute", left: `${(r.x / TREE_W) * 100}%`, top: `${(r.y / TREE_H) * 100}%`,
              width: `${(r.w / TREE_W) * 100}%`, height: `${(r.h / TREE_H) * 100}%`, padding: 1,
              boxSizing: "border-box" } as const;
            const inner = (
              <span style={{ position: "relative", display: "block", width: "100%", height: "100%", borderRadius: 4,
                overflow: "hidden", background: K.card, border: `1px solid ${K.line}`, textAlign: "left" }}>
                <span aria-hidden style={{ position: "absolute", inset: 0, background: K.accent,
                  opacity: 0.12 + 0.5 * (r.item.churn / max) }} />
                <span style={{ position: "relative", display: "block", padding: "3px 5px", fontSize: 11, color: K.ink,
                  overflowWrap: "anywhere", lineHeight: 1.25 }}>{r.item.path} <span style={{ color: K.muted }}>
                  {r.item.churn}</span></span>
              </span>
            );
            return (
              <span role="listitem" key={r.item.path} style={box} title={label}>
                {!dir ? (
                  <Pressable ariaLabel={label} onClick={() => onPick(r.item.path)}
                    style={{ height: "100%", borderRadius: 4 }}>{inner}</Pressable>
                ) : <span aria-label={label} style={{ display: "block", width: "100%", height: "100%" }}>{inner}</span>}
              </span>
            );
          })}
        </div>
      )}
    </Section>
  );
}

/** The URL sort for a card list (worst / name / one of `metrics`). */
export function useCardSort(metrics: string[]): [string, (v: string) => void] {
  const { get, set } = useUrlFilters();
  const asked = get("sort");
  const value = asked === "name" || metrics.includes(asked) ? asked : "worst";
  return [value, (v: string) => set("sort", v === "worst" ? null : v)];
}
