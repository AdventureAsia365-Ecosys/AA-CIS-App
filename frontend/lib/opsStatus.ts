// frontend/lib/opsStatus.ts
// AA-1140 (S289) — the pure side of the Ecosystem status page (app/admin/overview/page.tsx): the shape of
// GET /api/admin/overview/status (backend api/services/ops_status/), the view state kept in the URL (time range,
// compare, app / severity / status filters, chart legends, sorts), the filters and sorts of every list, and the chart
// helpers (rows on one time axis, thresholds, headroom). No React here; tested in lib/opsStatus.test.mjs.
//
// Rules the page relies on (S285 advisory, mockup v2):
// - a signal with no source is "not measured yet" (grey), never green: levelTone("none" | "not_measured") is neutral;
// - every limit comes from the API's `thresholds` (cis.ops.* settings) or the alarm, never a literal in the page;
// - the app filter scopes every section except shared platform rows (app "shared" always shows).

export const STATUS_APPS = ["cis", "tripplanner", "booking", "crm"] as const;
export type AppId = (typeof STATUS_APPS)[number];
export const APP_LABEL: Readonly<Record<string, string>> = {
  cis: "CIS", tripplanner: "TripPlanner", booking: "Booking", crm: "Partner CRM", shared: "Shared platform",
};
export const RANGE_KEYS = ["1h", "6h", "24h", "7d", "custom"] as const;
export type RangeKey = (typeof RANGE_KEYS)[number];
export const DEFAULT_RANGE: RangeKey = "24h";
export const SEVERITIES = ["sev1", "sev2", "sev3"] as const;
export const ITEM_STATES = ["open", "acked"] as const;
export const RUNTIME_FILTERS = ["all", "attention", "healthy", "no_sample"] as const;
export type RuntimeFilter = (typeof RUNTIME_FILTERS)[number];
export const EVENT_KINDS = ["sev1", "sev2", "sev3", "info", "pass"] as const;
export const CHARTS = ["traffic", "errors", "latency", "queue"] as const;
export type ChartId = (typeof CHARTS)[number];

// ── API shape ───────────────────────────────────────────────────────────────────────────────────────────────────

export interface Section<T> {
  status: string;
  as_of?: string | null;
  refresh_s?: number;
  data?: T;
  reason?: string | null;
  message?: string | null;
}
export interface RangeInfo { key: string; start: string; end: string; step_s: number; span_s: number }
export interface Limit { warn?: number | null; crit?: number | null; warn_key?: string; crit_key?: string; dir?: string;
  value?: number; key?: string }
export interface AttentionItem {
  id: string; severity: string; app: string; title: string; detail: string; since: string | null; count: number;
  status: string; source: string; links: { label: string; href: string }[];
}
export interface Verdict {
  status: "ok" | "attention" | "unknown"; open: number; attention: AttentionItem[]; unknown: string[];
  resolved: { id: string; app: string | null; text: string; t: string; t2: string | null }[];
  chips: { alarms_firing: number; open_alerts: number; deploy_gate: "open" | "closed" | null;
    deploy_gate_blocked_by: string[] };
}
export interface Traffic {
  status: string; reason?: string; requests?: number; errors?: number; client_errors?: number | null;
  p95_ms?: number | null; error_rate_pct?: number | null; client_error_rate_pct?: number | null;
  errors_level?: string; client_errors_level?: string; p95_level?: string;
}
export interface Activity { status: string; reason?: string; unit?: string; items?: { metric: string; n: number }[] }
export interface AppTile {
  app: string; label: string; overall: { status: string; reason?: string }; traffic: Traffic;
  deployed_sha: string | null; ready: string | null; workers: number | null;
  health: { state: string; reason?: string; sampled_at: string } | null | undefined; activity: Activity;
}
export interface SignalApp { measured: boolean; rate_per_min?: (number | null)[]; error_pct?: (number | null)[];
  p95_ms?: (number | null)[]; requests?: (number | null)[] }
export interface Signals { status: string; reason?: string; message?: string; as_of?: string;
  apps?: Record<string, SignalApp>; deploys?: { t: string; app: string; text: string }[];
  alarm_bands?: { start: string; end: string | null; app: string; severity: string; text: string }[];
  previous?: { status: string; apps?: Record<string, SignalApp>; range: RangeInfo } }
export interface RuntimeRow {
  kind: string; subject: string; app: string; role: string; state: string; reason?: string | null;
  running?: number | null; desired?: number | null; rollout?: string | null; version?: string | null;
  sampled_at?: string; cells?: string[]; uptime_pct?: number | null; budget_left_pct?: number | null;
  below_slo?: boolean;
}
export interface ResourceTile { id: string; label: string; unit?: string; series: (number | null)[];
  latest?: number | null; worst?: number | null; warn?: number | null; crit?: number | null; dir?: string;
  warn_key?: string; crit_key?: string; level: string; note?: string | null; capacity?: number; status?: string }
export interface Queue { id: string; name: string; app: string; status: string; reason?: string; queued?: number;
  running?: number; failed?: number; oldest_wait_min?: number; workers?: number | null; level?: string;
  series_wait_min?: (number | null)[] | null; failed_window?: string }
export interface Jobs { queues: Queue[]; deploy_gate: { safe_to_deploy: boolean | null; blocked_by: string[] };
  failing_kinds: { kind: string; n: number; last_at: string; last_error: string | null; queue: string }[] }
export interface AiApp { app: string; spent_usd: number | null; cap_usd: number | null; used_pct: number | null;
  level: string }
export interface AiConn { key: string; cost: number; requests: number; failed: number; series: number[];
  error_pct: number | null }
export interface Ai { day: string; apps: AiApp[]; near_pct: number; crm_note: string | null; connections: AiConn[];
  range_by_app: Record<string, number> }
export interface Cost { month: string; spent_usd: number; ai_usd: number; aws_usd: number; forecast_usd: number;
  budget_usd: number | null; forecast_pct_of_budget: number | null; headroom_usd: number | null }
export interface EventRow { id: string; t: string; t2: string | null; lane: string; kind: string; severity: string;
  app: string; text: string; detail?: string | null; open: boolean }
export interface Delivery { repos: { id: string; label: string; main: string; red_hours?: number | null }[];
  repos_note: string | null; guards: { key: string; label: string; state: string; detail?: string; day?: string }[] }
export interface SourceRow { section: string; source: string; refresh_s: number; as_of: string | null; state: string;
  reason?: string | null }
export interface StatusResp {
  generated_at: string; environment: string; range: RangeInfo; previous: RangeInfo | null; buckets: string[];
  verdict: Verdict; thresholds: Record<string, Limit>;
  sections: { signals: Signals; apps: Section<AppTile[]>; runtimes: Section<{ slo_pct: number; rows: RuntimeRow[] }>;
    resources: Section<{ tiles: ResourceTile[] }>; jobs: Section<Jobs>; ai: Section<Ai>; cost: Section<Cost>;
    events: Section<EventRow[]>; delivery: Section<Delivery> };
  sources: SourceRow[];
}

// ── view state in the URL ───────────────────────────────────────────────────────────────────────────────────────

export interface View {
  range: RangeKey; from: string; to: string; compare: boolean; apps: string[]; sev: string[]; st: string[];
  svc: RuntimeFilter; ev: string[]; hide: Record<string, string[]>; sort: Record<string, string>;
}
type Getter = (key: string) => string;

const list = (raw: string, allowed?: readonly string[]): string[] =>
  raw.split(",").map((s) => s.trim()).filter((s) => s && (!allowed || allowed.includes(s)));

function pick<T extends string>(raw: string, allowed: readonly T[], fallback: T): T {
  return (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
}

/** "traffic:cis,errors:booking" -> {traffic: ["cis"], errors: ["booking"]}. */
export function parseHidden(raw: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const part of list(raw)) {
    const [chart, id] = part.split(":");
    if (!id || !(CHARTS as readonly string[]).includes(chart)) continue;
    out[chart] = [...(out[chart] ?? []), id];
  }
  return out;
}

export function hiddenParam(hide: Record<string, string[]>): string {
  return Object.entries(hide).flatMap(([c, ids]) => ids.map((id) => `${c}:${id}`)).join(",");
}

/** "att:since:desc,svc:uptime:asc" -> {att: "since:desc", svc: "uptime:asc"}. */
export function parseSorts(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of list(raw)) {
    const [table, col, dir] = part.split(":");
    if (table && col && (dir === "asc" || dir === "desc")) out[table] = `${col}:${dir}`;
  }
  return out;
}

export function parseView(get: Getter): View {
  return {
    range: pick(get("range"), RANGE_KEYS, DEFAULT_RANGE), from: get("from"), to: get("to"),
    compare: get("compare") === "1", apps: list(get("apps"), STATUS_APPS), sev: list(get("sev"), SEVERITIES),
    st: list(get("st"), ITEM_STATES), svc: pick(get("svc"), RUNTIME_FILTERS, "all"), ev: list(get("ev"), EVENT_KINDS),
    hide: parseHidden(get("hide")), sort: parseSorts(get("sort")),
  };
}

type ParamOf = (v: Partial<View>) => string | null;
const joined = (k: "apps" | "sev" | "st" | "ev"): ParamOf => (v) => v[k]!.join(",") || null;
const PARAM: Readonly<Record<keyof View, ParamOf>> = {
  range: (v) => (v.range === DEFAULT_RANGE ? null : v.range!),
  from: (v) => v.from || null,
  to: (v) => v.to || null,
  compare: (v) => (v.compare ? "1" : null),
  apps: joined("apps"), sev: joined("sev"), st: joined("st"), ev: joined("ev"),
  svc: (v) => (v.svc === "all" ? null : v.svc!),
  hide: (v) => hiddenParam(v.hide!) || null,
  sort: (v) => Object.entries(v.sort!).map(([t, s]) => `${t}:${s}`).join(",") || null,
};

/** URL parameters of a view change; a default value removes its key (a clean link). */
export function viewParams(v: Partial<View>): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const k of Object.keys(v) as (keyof View)[]) if (v[k] !== undefined) out[k] = PARAM[k](v);
  return out;
}

/** Every key the page owns, cleared: "Reset view". */
export const RESET_VIEW: Record<string, null> = {
  range: null, from: null, to: null, compare: null, apps: null, sev: null, st: null, svc: null, ev: null, hide: null,
  sort: null,
};

/** The API query of a view: preset or custom from / to, and compare. */
export function statusQuery(v: Pick<View, "range" | "from" | "to" | "compare">): string {
  const q = new URLSearchParams({ range: v.range });
  if (v.range === "custom") {
    q.set("from", v.from);
    q.set("to", v.to);
  }
  if (v.compare) q.set("compare", "true");
  return q.toString();
}

// ── custom range ────────────────────────────────────────────────────────────────────────────────────────────────

export type RangeProblem = "missing" | "order" | "future" | "tooOld" | "tooShort";
const MIN_SPAN_MS = 10 * 60_000; // technical-constant: the backend's MIN_SPAN (two 5-minute samples)

/** Why a custom from / to (ISO with a zone) cannot be shown, or null. `historyDays` is the API's
 *  cis.ops.status_history_days. */
export function customProblem(from: string, to: string, historyDays: number, nowMs: number): RangeProblem | null {
  const a = Date.parse(from);
  const b = Date.parse(to);
  if (Number.isNaN(a) || Number.isNaN(b)) return "missing";
  if (b <= a) return "order";
  if (b > nowMs + 60_000) return "future";
  if (a < nowMs - historyDays * 86_400_000) return "tooOld";
  return b - a < MIN_SPAN_MS ? "tooShort" : null;
}

/** A datetime-local value ("2026-11-24T09:05", the viewer's clock) as ISO UTC, "" when empty or invalid. */
export function localToIso(local: string): string {
  const ms = Date.parse(local);
  return local && !Number.isNaN(ms) ? new Date(ms).toISOString() : "";
}

/** ISO -> a datetime-local value on the viewer's clock. */
export function isoToLocal(iso: string): string {
  const ms = Date.parse(iso);
  if (!iso || Number.isNaN(ms)) return "";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ── filters and sorts ───────────────────────────────────────────────────────────────────────────────────────────

/** The app filter: nothing chosen = every app; shared platform rows always show. */
export function inApps(app: string | null | undefined, apps: readonly string[]): boolean {
  return apps.length === 0 || !app || app === "shared" || apps.includes(app);
}

export function filterAttention(items: readonly AttentionItem[], v: Pick<View, "apps" | "sev" | "st">):
  AttentionItem[] {
  return items.filter((i) => inApps(i.app, v.apps) && (v.sev.length === 0 || v.sev.includes(i.severity))
    && (v.st.length === 0 || v.st.includes(i.status)));
}

export function countBy<T>(rows: readonly T[], key: (r: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of rows) out[key(r)] = (out[key(r)] ?? 0) + 1;
  return out;
}

const ATTENTION_STATES = new Set(["warn", "degraded", "down"]);

/** The services grid filter. */
export function runtimeMatches(r: RuntimeRow, f: RuntimeFilter): boolean {
  if (f === "attention") return ATTENTION_STATES.has(r.state) || r.below_slo === true;
  if (f === "healthy") return r.state === "ok" && r.below_slo !== true;
  if (f === "no_sample") return r.state === "no_sample" || r.state === "not_measured";
  return true;
}

/** The severity chip an event belongs to (sev1..3, info, pass). */
export function eventKind(e: Pick<EventRow, "severity">): string {
  return (EVENT_KINDS as readonly string[]).includes(e.severity) ? e.severity : "info";
}

export function filterEvents(rows: readonly EventRow[], v: Pick<View, "apps" | "ev">, search: string): EventRow[] {
  const q = search.trim().toLowerCase();
  return rows.filter((e) => inApps(e.app, v.apps) && (v.ev.length === 0 || v.ev.includes(eventKind(e)))
    && (!q || `${e.text} ${e.detail ?? ""} ${e.app}`.toLowerCase().includes(q)));
}

export type SortDir = "asc" | "desc";
export function parseSort(raw: string | undefined, fallback: string): { col: string; dir: SortDir } {
  const [col, dir] = (raw || fallback).split(":");
  return { col, dir: dir === "asc" ? "asc" : "desc" };
}

/** A stable sort on one value; missing values (null, undefined, "") always last. */
export function sortRows<T>(rows: readonly T[], value: (r: T) => string | number | null | undefined, dir: SortDir): T[] {
  const sign = dir === "asc" ? 1 : -1;
  return rows.map((r, i) => ({ r, i, v: value(r) })).sort((a, b) => {
    const am = a.v == null || a.v === "";
    const bm = b.v == null || b.v === "";
    if (am || bm) return am === bm ? a.i - b.i : am ? 1 : -1;
    const c = typeof a.v === "number" && typeof b.v === "number" ? a.v - b.v : String(a.v).localeCompare(String(b.v));
    return c === 0 ? a.i - b.i : sign * c;
  }).map((x) => x.r);
}

/** The next sort after a click on a column: same column flips, a new one starts descending. */
export function nextSort(current: { col: string; dir: SortDir }, col: string): string {
  return current.col === col ? `${col}:${current.dir === "asc" ? "desc" : "asc"}` : `${col}:desc`;
}

/** A DataTable sorting state from a view sort ("col:dir"), and back. */
export function tableSorting(raw: string | undefined): { id: string; desc: boolean }[] {
  if (!raw) return [];
  const { col, dir } = parseSort(raw, "");
  return col ? [{ id: col, desc: dir === "desc" }] : [];
}

export function sortingParam(state: readonly { id: string; desc: boolean }[]): string {
  return state.length ? `${state[0].id}:${state[0].desc ? "desc" : "asc"}` : "";
}

/** The view's sorts with one table's sort replaced (an empty value removes it). */
export function withSort(sorts: Record<string, string>, table: string, value: string): Record<string, string> {
  const next = { ...sorts };
  if (value) next[table] = value;
  else delete next[table];
  return next;
}

const SEV_RANK: Readonly<Record<string, number>> = { sev1: 3, sev2: 2, sev3: 1 };
export const severityRank = (s: string): number => SEV_RANK[s] ?? 0;

// ── levels ──────────────────────────────────────────────────────────────────────────────────────────────────────

export type ToneName = "neutral" | "success" | "warning" | "danger" | "info";
const TONE_OF: Readonly<Record<string, ToneName>> = {
  ok: "success", healthy: "success", passed: "success", green: "success", open: "success",
  warn: "warning", degraded: "warning", attention: "warning", sev3: "warning", red: "danger", closed: "warning",
  crit: "danger", down: "danger", failed: "danger", sev1: "danger", sev2: "danger",
};

/** Colour of a level / state; anything unknown (none, few, no_sample, not_measured, unknown, stale) is grey. */
export function levelTone(level: string | null | undefined): ToneName {
  return TONE_OF[level ?? ""] ?? "neutral";
}

/** A section that did not answer, or answered "not measured": the page shows its reason, never a number. */
export function sectionDown(s: Section<unknown> | null | undefined): boolean {
  return !s || s.status !== "ok";
}

// ── charts ──────────────────────────────────────────────────────────────────────────────────────────────────────

export interface Series { id: string; label: string; values: readonly (number | null)[];
  previous?: readonly (number | null)[] }

/** Recharts rows on the range's steps: {t, <id>, <id>__prev}. */
export function chartRows(buckets: readonly string[], series: readonly Series[]): Record<string, number | null>[] {
  return buckets.map((b, i) => {
    const row: Record<string, number | null> = { t: Date.parse(b) };
    for (const s of series) {
      row[s.id] = s.values[i] ?? null;
      if (s.previous) row[`${s.id}__prev`] = s.previous[i] ?? null;
    }
    return row;
  });
}

export function latest(values: readonly (number | null | undefined)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return values[i] as number;
  return null;
}

export function peak(values: readonly (number | null | undefined)[]): number | null {
  const v = values.filter((x): x is number => x != null);
  return v.length ? Math.max(...v) : null;
}

/** Room left before a limit: limit - value above (a ceiling), value - limit below (a floor); null without both. */
export function headroom(value: number | null | undefined, limit: number | null | undefined, dir = "above"):
  number | null {
  if (value == null || limit == null) return null;
  return dir === "below" ? value - limit : limit - value;
}

/** Top of the Y axis: the highest value or limit drawn, plus 10 % so a line at the top stays visible. */
export function yTop(values: readonly (number | null)[], limits: readonly (number | null | undefined)[]): number {
  const all = [...values, ...limits].filter((x): x is number => x != null && Number.isFinite(x));
  const top = all.length ? Math.max(...all) : 1;
  return top > 0 ? top * 1.1 : 1;
}

/** Series shown after the legend tick boxes (hidden ids removed). */
export function shownSeries<T extends { id: string }>(series: readonly T[], hidden: readonly string[] = []): T[] {
  return series.filter((s) => !hidden.includes(s.id));
}

export function toggleHidden(hidden: readonly string[], id: string): string[] {
  return hidden.includes(id) ? hidden.filter((x) => x !== id) : [...hidden, id];
}

/** "Only this one": every other series hidden; clicking "only" on the only shown one shows all again. */
export function onlyHidden(all: readonly string[], hidden: readonly string[], id: string): string[] {
  const others = all.filter((x) => x !== id);
  const alreadyOnly = !hidden.includes(id) && others.every((x) => hidden.includes(x));
  return alreadyOnly ? [] : others;
}

function seriesOf(sig: Signals, app: string, metric: "rate_per_min" | "error_pct" | "p95_ms"): Series {
  const prev = sig.previous?.apps?.[app];
  const previous = prev?.measured ? prev[metric] ?? [] : undefined;
  return { id: app, label: APP_LABEL[app], values: sig.apps?.[app]?.[metric] ?? [], previous };
}

/** One golden signal per app from the signals section (apps that are not measured are left out). */
export function signalSeries(sig: Signals, metric: "rate_per_min" | "error_pct" | "p95_ms",
  apps: readonly string[]): Series[] {
  return STATUS_APPS.filter((a) => sig.apps?.[a]?.measured && inApps(a, apps)).map((a) => seriesOf(sig, a, metric));
}

/** The apps a chart has no series for (grey "not measured yet" chips under it). */
export function unmeasuredApps(sig: Signals, apps: readonly string[]): string[] {
  return STATUS_APPS.filter((a) => inApps(a, apps) && !sig.apps?.[a]?.measured);
}

/** The oldest-wait series of each queue that has one. */
export function queueSeries(jobs: Jobs | undefined, apps: readonly string[]): Series[] {
  return (jobs?.queues ?? []).filter((q) => q.series_wait_min && inApps(q.app, apps))
    .map((q) => ({ id: q.id, label: q.name, values: q.series_wait_min ?? [] }));
}

/** An x-axis tick: time of day for a range up to two days, day and hour beyond. */
export function tickLabel(ms: number, spanS: number, lang: "en" | "vi" = "en"): string {
  const d = new Date(ms);
  const locale = lang === "vi" ? "vi-VN" : "en-GB";
  const time = d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  return spanS <= 2 * 86_400 ? time : `${d.toLocaleDateString(locale, { day: "numeric", month: "short" })} ${time}`;
}

/** Requests of one app in the previous period (the sum of its step counts), null when not measured. */
export function previousRequests(sig: Signals, app: string): number | null {
  const p = sig.previous?.apps?.[app];
  if (!p?.measured || !p.requests) return null;
  return p.requests.reduce<number>((a, v) => a + (v ?? 0), 0);
}

/** Change in percent against the previous period; null without a usable previous value. */
export function deltaPct(cur: number | null | undefined, prev: number | null | undefined): number | null {
  if (cur == null || prev == null || prev === 0) return null;
  return Math.round(((cur - prev) / prev) * 1000) / 10;
}

// ── numbers ─────────────────────────────────────────────────────────────────────────────────────────────────────

export function fmtMs(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? "—" : `${v.toFixed(digits)} %`;
}

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(digits);
}

/** A short commit for a tile ("dev-1a2b3c4d…" -> "1a2b3c4"). */
export function shortSha(v: string | null | undefined): string {
  if (!v) return "—";
  return v.replace(/^dev-/, "").slice(0, 7);
}

/** Age of a source in minutes (rounded), null without a time. */
export function ageMinutes(asOf: string | null | undefined, nowMs: number): number | null {
  const ms = asOf ? Date.parse(asOf) : NaN;
  return Number.isNaN(ms) ? null : Math.max(0, Math.round((nowMs - ms) / 60_000));
}
