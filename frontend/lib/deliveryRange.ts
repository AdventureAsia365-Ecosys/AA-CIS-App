// frontend/lib/deliveryRange.ts
// AA-1135: the time range, card sorting, DORA trend, delivery flow and chart helpers of Platform › Delivery
// (app/admin/platform/delivery). The API (GET /admin/platform/delivery?range=7d|28d|90d|custom&from=&to=) serves the
// collector's presets or a custom range recomputed from the collector's day files, and says in `range` what it served
// and what the date picker may offer. Pure functions only (tests: deliveryRange.test.mjs).

import type { DeliveryApp, MetricStatus, SegmentKey, SegmentStat, WeekRow } from "./deliveryMetrics";

// the lead-time segments in flow order (deliveryMetrics.SEGMENTS; a type-only import keeps this file runnable by
// node --experimental-strip-types)
const STEPS: SegmentKey[] = ["to_first_green", "to_queue", "to_merge", "to_deploy", "to_verify"];

export type RangeKey = "7d" | "28d" | "90d" | "custom";
export const PRESETS: Exclude<RangeKey, "custom">[] = ["7d", "28d", "90d"];
export const DEFAULT_RANGE: RangeKey = "28d";

export interface RangeChoice { range: RangeKey; from: string; to: string }

/** What the API says about the range it served (`range` of the answer). */
export interface RangeMeta {
  key: RangeKey; store_from: string | null; last_day: string | null; presets: string[]; custom: boolean;
  max_days: number; start?: string; end?: string; from?: string; to?: string; cached?: boolean;
}

const DAY = /^\d{4}-\d{2}-\d{2}$/;

export function isDay(v: string | null | undefined): v is string {
  return typeof v === "string" && DAY.test(v) && !Number.isNaN(Date.parse(`${v}T00:00:00Z`));
}

/** The range in the URL (?range=, ?from=, ?to=); anything unknown is the 28-day default. */
export function parseRange(get: (key: string) => string): RangeChoice {
  const r = get("range");
  if (r === "custom") {
    const from = get("from");
    const to = get("to");
    if (isDay(from) && isDay(to)) return { range: "custom", from, to };
    return { range: DEFAULT_RANGE, from: "", to: "" };
  }
  return { range: (PRESETS as string[]).includes(r) ? (r as RangeKey) : DEFAULT_RANGE, from: "", to: "" };
}

/** The query of a range ("" for the default, so the default view keeps its old URL and cache key). */
export function rangeQuery(c: RangeChoice): string {
  if (c.range === "custom") return new URLSearchParams({ range: "custom", from: c.from, to: c.to }).toString();
  return c.range === DEFAULT_RANGE ? "" : new URLSearchParams({ range: c.range }).toString();
}

/** `path` with the range query appended. */
export function withRange(path: string, c: RangeChoice): string {
  const q = rangeQuery(c);
  if (!q) return path;
  return `${path}${path.includes("?") ? "&" : "?"}${q}`;
}

/** The URL changes of a choice (null removes a parameter). */
export function rangeParams(c: RangeChoice): Record<string, string | null> {
  if (c.range === "custom") return { range: "custom", from: c.from, to: c.to };
  return { range: c.range === DEFAULT_RANGE ? null : c.range, from: null, to: null };
}

/** Days from `from` to `to`, both included. */
export function dayCount(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000) + 1;
}

export type RangeProblem = "missing" | "reversed" | "tooLong" | "beforeStore" | "afterLast" | "unavailable";

/** Why a custom range cannot be asked for (the API checks the same), or null. */
export function customProblem(from: string, to: string, meta: RangeMeta | null | undefined): RangeProblem | null {
  if (!isDay(from) || !isDay(to)) return "missing";
  if (meta && !meta.custom) return "unavailable";
  if (to < from) return "reversed";
  return boundsProblem(from, to, meta ?? null);
}

function boundsProblem(from: string, to: string, meta: RangeMeta | null): RangeProblem | null {
  if (!meta) return null;
  if (meta.max_days && dayCount(from, to) > meta.max_days) return "tooLong";
  if (meta.store_from && from < meta.store_from) return "beforeStore";
  if (meta.last_day && to > meta.last_day) return "afterLast";
  return null;
}

// ── the window an app was measured over ──────────────────────────────────────────────────────────────────────────

/** An app's `window` as the API serves it (a range block's: `requested_start` and `covered_days` since AA-1135). */
export interface RangeWindow {
  start: string; end: string; days?: number; covered_days?: number; requested_start?: string;
}

/** What an app header says about its window: the dates asked for (`from`–`to`, UTC days, `to` included), and when
 *  the start was moved up to the app's complete history (`clipped`), how many days it covers from `startsOn`. */
export interface WindowNote {
  custom: boolean; days: number; from: string; to: string; clipped: boolean; coveredDays: number; startsOn: string;
}

const MS_DAY = 86_400_000;

/** The last UTC day a window covers: the day before an end at midnight (a custom range's `to` + 1 day), else the
 *  end's own day (a preset ends at the collection time). */
function lastDayOf(endMs: number): string {
  const midnight = endMs % MS_DAY === 0;
  return new Date(midnight ? endMs - MS_DAY : endMs).toISOString().slice(0, 10);
}

/** The note of a window, or null without one. `custom` labels it by dates instead of "last N days"; `completeSince`
 *  (YYYY-MM-DD) clips a window the API did not clip itself (the git activity starts at the asked day, its history at
 *  the commits' complete day). */
export function windowNote(w: RangeWindow | null | undefined, custom: boolean,
  completeSince?: string | null): WindowNote | null {
  if (!w?.start || !w?.end) return null;
  const asked = Date.parse(w.requested_start ?? w.start);
  let start = Date.parse(w.start);
  const end = Date.parse(w.end);
  if ([asked, start, end].some(Number.isNaN)) return null;
  const since = isDay(completeSince) ? Date.parse(`${completeSince}T00:00:00Z`) : NaN;
  const own = !Number.isNaN(since) && since > start;
  if (own) start = Math.min(since, end);
  const clipped = start - asked >= 1000;
  const tenth = (ms: number) => Math.round((Math.max(ms, 0) / MS_DAY) * 10) / 10;
  const days = typeof w.days === "number" ? w.days : tenth(end - asked);
  const covered = own || typeof w.covered_days !== "number" ? tenth(end - start) : w.covered_days;
  return {
    custom, days: Math.round(days), from: new Date(asked).toISOString().slice(0, 10), to: lastDayOf(end), clipped,
    coveredDays: clipped ? covered : days, startsOn: new Date(start).toISOString().slice(0, 10),
  };
}

// ── card sorting ─────────────────────────────────────────────────────────────────────────────────────────────────

export type CardSort = string; // "worst" | "name" | a metric key

const STATUS_RANK: Record<MetricStatus, number> = { red: 0, amber: 1, green: 2, few: 3, none: 4 };

export function statusRank(s: MetricStatus | undefined): number {
  return STATUS_RANK[s ?? "none"] ?? 4;
}

const CARD_NUMBER: Record<string, (app: DeliveryApp) => number | null | undefined> = {
  deployment_frequency: (a) => a.dora?.deployment_frequency?.value,
  lead_time: (a) => a.dora?.lead_time?.p50_h,
  change_failure_rate: (a) => a.dora?.change_failure_rate?.value,
  recovery_time: (a) => a.dora?.recovery_time?.p50_h,
  rework_rate: (a) => a.dora?.rework_rate?.value,
  ci_p95: (a) => a.ci?.slowest?.p95_s,
};

/** The number behind a card metric (in the API's unit), for sorting; null without a value. */
export function doraNumber(app: DeliveryApp, metric: string): number | null {
  const x = CARD_NUMBER[metric]?.(app);
  return typeof x === "number" && Number.isFinite(x) ? x : null;
}

/** The sort in the URL when it is one of `allowed`, else "worst". */
export function pickSort(asked: string, allowed: string[]): CardSort {
  return asked === "name" || allowed.includes(asked) ? asked : "worst";
}

/** Cards sorted: worst colour first (then name), by name, or by a metric worst first (no value last). */
export function sortCards<T extends { id: string; name: string }>(
  items: T[], sort: CardSort, worst: (t: T) => number, value: (t: T, metric: string) => number | null,
  higherIsBetter: (metric: string) => boolean,
): T[] {
  const byName = (a: T, b: T) => a.name.localeCompare(b.name);
  const out = [...items];
  if (sort === "name") return out.sort(byName);
  if (sort === "worst") return out.sort((a, b) => worst(a) - worst(b) || byName(a, b));
  const up = higherIsBetter(sort);
  return out.sort((a, b) => {
    const va = value(a, sort);
    const vb = value(b, sort);
    if (va === null || vb === null) return (va === null ? 1 : 0) - (vb === null ? 1 : 0) || byName(a, b);
    return (up ? va - vb : vb - va) || byName(a, b);
  });
}

// ── DORA trend ───────────────────────────────────────────────────────────────────────────────────────────────────

export type TrendMetric = "deploys" | "lead_time" | "cfr" | "recovery";
export const TREND_METRICS: TrendMetric[] = ["deploys", "lead_time", "cfr", "recovery"];

/** One week's value of a trend metric (cfr in percent); null for a week before the complete history. */
export function trendValue(w: WeekRow, m: TrendMetric): number | null {
  if (w.complete === false) return null;
  switch (m) {
    case "deploys": return w.deploys;
    case "lead_time": return w.lead_time_p50_h;
    case "cfr": return w.cfr === null ? null : Math.round(w.cfr * 1000) / 10;
    default: return w.recovery_p50_h;
  }
}

/** Rows of the trend chart: one per ISO week, one column per app id (compare) — the weeks of the first app. */
export function trendRows(apps: DeliveryApp[], m: TrendMetric): Record<string, number | string | null>[] {
  const weeks = apps[0]?.trend?.weeks ?? [];
  return weeks.map((w) => {
    const row: Record<string, number | string | null> = { week: w.week, label: w.week.slice(5) };
    for (const a of apps) {
      const hit = (a.trend?.weeks ?? []).find((x) => x.week === w.week);
      row[a.id] = hit ? trendValue(hit, m) : null;
    }
    return row;
  });
}

/** Rows of one app's four trend metrics. */
export function appTrendRows(app: DeliveryApp): Record<string, number | string | null>[] {
  return (app.trend?.weeks ?? []).map((w) => {
    const row: Record<string, number | string | null> = { week: w.week, label: w.week.slice(5) };
    for (const m of TREND_METRICS) row[m] = trendValue(w, m);
    return row;
  });
}

// ── delivery flow ────────────────────────────────────────────────────────────────────────────────────────────────

export type FlowPoint = "opened" | "first_green" | "queued" | "merged" | "deployed" | "verified";
export const FLOW_POINTS: FlowPoint[] = ["opened", "first_green", "queued", "merged", "deployed", "verified"];

export interface FlowStep {
  segment: SegmentKey; from: FlowPoint; to: FlowPoint; p50_h: number | null; p95_h: number | null; n: number;
  slowest: boolean; share: number;
}

/** The five steps of the flow with their median and p95; `slowest` marks the longest median (n > 0), `share` its
 *  part of the summed medians (0..1, for a proportional bar). */
export function flowSteps(segments: SegmentStat[] | undefined): FlowStep[] {
  const by = new Map((segments ?? []).map((s) => [s.segment, s]));
  const rows = STEPS.map((seg, i) => {
    const s = by.get(seg);
    const p95 = s?.p95_h ?? s?.p90_h ?? null;
    return { segment: seg, from: FLOW_POINTS[i], to: FLOW_POINTS[i + 1], p50_h: s?.p50_h ?? null, p95_h: p95,
      n: s?.n ?? 0, slowest: false, share: 0 };
  });
  const have = rows.filter((r) => r.n > 0 && r.p50_h !== null);
  const total = have.reduce((t, r) => t + (r.p50_h ?? 0), 0);
  const top = have.reduce<FlowStep | null>((a, r) => (!a || (r.p50_h ?? 0) > (a.p50_h ?? 0) ? r : a), null);
  for (const r of rows) {
    r.slowest = top !== null && r.segment === top.segment && (top.p50_h ?? 0) > 0;
    r.share = total > 0 && r.p50_h !== null && r.n > 0 ? r.p50_h / total : 0;
  }
  return rows;
}

// ── merge queue charts ───────────────────────────────────────────────────────────────────────────────────────────

export interface HistBin { lo_h: number; hi_h: number | null; n: number }
export interface QueueWeek { week: string; start: string; merged: number; ejected: number; removed: number;
  ejection_rate: number | null }

/** Hours as a short bin edge ("15 min", "2 h", "1 d"). */
export function edgeLabel(h: number): string {
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${Math.round(h * 10) / 10} h`;
  return `${Math.round((h / 24) * 10) / 10} d`;
}

/** Histogram rows with a label per bin ("< 15 min", "15 min–30 min", "≥ 24 h"). */
export function histRows(bins: HistBin[] | undefined): { label: string; n: number }[] {
  return (bins ?? []).map((b) => ({
    label: b.hi_h === null ? `≥ ${edgeLabel(b.lo_h)}` : b.lo_h === 0 ? `< ${edgeLabel(b.hi_h)}`
      : `${edgeLabel(b.lo_h)}–${edgeLabel(b.hi_h)}`,
    n: b.n,
  }));
}

/** Weekly ejection rate rows in percent (null where nothing merged or was ejected). */
export function weeklyRows(weeks: QueueWeek[] | undefined): { label: string; rate: number | null; judged: number }[] {
  return (weeks ?? []).map((w) => ({
    label: w.week.slice(5), judged: w.merged + w.ejected,
    rate: w.ejection_rate === null ? null : Math.round(w.ejection_rate * 1000) / 10,
  }));
}

// ── churn treemap ────────────────────────────────────────────────────────────────────────────────────────────────

export interface TreeRect<T> { x: number; y: number; w: number; h: number; item: T; value: number }

function worstRatio(row: number[], side: number): number {
  const sum = row.reduce((a, b) => a + b, 0);
  const max = Math.max(...row);
  const min = Math.min(...row);
  return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
}

function layoutRow<T>(row: { value: number; item: T }[], x: number, y: number, w: number, h: number,
  scaled: number[]): { rects: TreeRect<T>[]; x: number; y: number; w: number; h: number } {
  const sum = scaled.reduce((a, b) => a + b, 0);
  const rects: TreeRect<T>[] = [];
  if (w >= h) {
    const cw = sum / h;
    let cy = y;
    row.forEach((r, i) => { const ch = scaled[i] / cw; rects.push({ x, y: cy, w: cw, h: ch, ...r }); cy += ch; });
    return { rects, x: x + cw, y, w: w - cw, h };
  }
  const rh = sum / w;
  let cx = x;
  row.forEach((r, i) => { const rw = scaled[i] / rh; rects.push({ x: cx, y, w: rw, h: rh, ...r }); cx += rw; });
  return { rects, x, y: y + rh, w, h: h - rh };
}

/** A squarified treemap of `items` (value > 0) in the box w × h, largest first. */
export function squarify<T>(items: { value: number; item: T }[], w: number, h: number): TreeRect<T>[] {
  const kept = items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value);
  const total = kept.reduce((t, i) => t + i.value, 0);
  if (!kept.length || w <= 0 || h <= 0) return [];
  const k = (w * h) / total;
  const out: TreeRect<T>[] = [];
  let box = { x: 0, y: 0, w, h };
  let row: { value: number; item: T }[] = [];
  for (const it of kept) {
    const side = Math.min(box.w, box.h);
    const next = [...row, it];
    if (row.length && worstRatio(next.map((r) => r.value * k), side) > worstRatio(row.map((r) => r.value * k), side)) {
      const laid = layoutRow(row, box.x, box.y, box.w, box.h, row.map((r) => r.value * k));
      out.push(...laid.rects);
      box = { x: laid.x, y: laid.y, w: laid.w, h: laid.h };
      row = [it];
    } else {
      row = next;
    }
  }
  if (row.length) out.push(...layoutRow(row, box.x, box.y, box.w, box.h, row.map((r) => r.value * k)).rects);
  return out;
}
