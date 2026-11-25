// frontend/lib/opsStatusText.ts
// AA-1140 (S289 live check, VI mode) — the words of the Ecosystem status page built from the structured fields the API
// sends (`msg` / `detail_msg` on attention items, `msg` on events, `reason_code` on services): one message key of
// app/admin/overview/status.messages.ts (EN + VI) and its arguments, already formatted. The API's English title stays
// the fallback for a key this page does not know. Outside free text (alarm descriptions, ops check messages) is not
// built here: the page shows it as written, labelled. No React; tested in lib/opsStatusText.test.mjs.

import { fmtMs, type Msg } from "./opsStatus.ts";

/** Every `msg.key` the API sends (api/services/ops_status/verdict.py and sections.py); a backend test reads this list. */
export const MSG_KEYS = [
  "health_down", "health_degraded", "errors", "client_errors", "p95", "service", "alarm", "rollout_failed", "check",
  "queue_no_worker", "queue_no_worker_detail", "queue_wait", "ecs_tasks", "resource", "ai_cap", "ai_near", "ai_spend",
  "of_requests", "guard_failed", "guard_missing", "guard_value", "guard_age", "main_red", "red_for", "deploy",
  "service_down", "job_failures", "forecast", "anomaly",
  // AA-1142 (S293): the error budget burn and the external checks (title and detail)
  "slo_burn", "slo_burn_detail", "external_fails", "external_cert", "external_stale", "external_stale_detail",
] as const;
export type MsgKey = (typeof MSG_KEYS)[number];

/** The words the phrases need from the page's language (app names, level words, tile names, units, service reasons). */
export interface Words {
  app: (id: string) => string;
  level: (level: string) => string;
  tile: (id: string) => string;
  unit: (tile: string, unit: string) => string;
  why: (code: string, running: number | null, desired: number | null) => string | null;
  /** A time of the API (ISO) in the page's format; the ISO text when absent. */
  time?: (iso: string) => string;
}
/** A message key of the page catalogue and its formatted arguments. */
export interface Phrase { key: string; args: string[] }

const str = (v: unknown): string => (v == null || v === "" ? "—" : String(v));
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** "14.96 %", "1.50 s", "20 min", "4 GiB", "15.5 %": a value with its unit, ms as seconds above 1000. */
export function withUnit(v: unknown, unit: string): string {
  const n = num(v);
  if (unit === "ms") return fmtMs(n);
  if (n == null) return "—";
  const g = String(Number(n.toFixed(2)));   // at most two decimals, no trailing zeros: "15.5", "14.83", "4"
  return unit ? `${g} ${unit}` : g;
}

const appOnly = (m: Msg, w: Words): string[] => [w.app(str(m.app))];
const nameOnly = (m: Msg): string[] => [str(m.name)];
const share = (m: Msg, w: Words): string[] => [w.app(str(m.app)), withUnit(m.value, "%"), withUnit(m.limit, "%")];

function serviceDownWhy(m: Msg, w: Words): string {
  if (m.zero) return w.why("scaled_to_zero", null, null) ?? "—";
  const running = num(m.running);
  return w.why(running === 0 ? "none_running" : "below_desired", running, num(m.desired)) ?? "—";
}

const PHRASES: Readonly<Record<Exclude<MsgKey, "resource" | "forecast">, (m: Msg, w: Words) => string[]>> = {
  health_down: appOnly,
  health_degraded: appOnly,
  errors: share,
  client_errors: share,
  p95: (m, w) => [w.app(str(m.app)), withUnit(m.value, "ms"), withUnit(m.limit, "ms")],
  service: (m, w) => [str(m.subject), w.why(str(m.code), num(m.running), num(m.desired)) ?? w.level(str(m.state))],
  alarm: nameOnly,
  rollout_failed: (m) => [str(m.subject), str(m.rev)],
  check: nameOnly,
  queue_no_worker: nameOnly,
  queue_no_worker_detail: () => [],
  queue_wait: (m) => [str(m.name), withUnit(m.value, "min"), withUnit(m.limit, "min")],
  ecs_tasks: (m) => [str(m.running), str(m.desired)],
  ai_cap: appOnly,
  ai_near: (m, w) => [w.app(str(m.app)), withUnit(m.value, "%")],
  ai_spend: (m) => [withUnit(m.spent, ""), withUnit(m.cap, "")],
  of_requests: (m) => [str(m.n), str(m.total)],
  guard_failed: nameOnly,
  guard_missing: nameOnly,
  guard_value: (m) => [str(m.value), str(m.day)],
  guard_age: (m) => [str(m.hours)],
  main_red: nameOnly,
  red_for: (m) => [str(m.hours)],
  deploy: (m) => [str(m.subject), m.tag ? String(m.tag) : "", str(m.rev)],
  service_down: (m, w) => [str(m.subject), serviceDownWhy(m, w)],
  job_failures: (m) => [str(m.n), str(m.kind)],
  // AA-1141: an AI spend hour above N x its median hour (times is null when the median hour is 0: above the floor)
  anomaly: (m, w) => [w.app(str(m.app)), str(m.route), withUnit(m.value, "USD"),
    num(m.times) == null ? "" : `${withUnit(m.times, "")}×`, withUnit(m.median, "USD")],
  // AA-1142: a burn window over its factor; its detail (bad of events, the objective, the budget left)
  slo_burn: (m, w) => [w.app(str(m.app)), windowText(m.minutes), withUnit(m.value, ""), withUnit(m.limit, "")],
  slo_burn_detail: (m) => [str(m.bad), str(m.events), m.basis === "requests" ? "requests" : "samples",
    withUnit(m.target, "%"), withUnit(m.budget_left, "%")],
  // AA-1142 (S293): a public host failing from outside (what failed, the sources that see it), a certificate near its
  // end, a feed whose every source stopped
  external_fails: (m) => [str(m.host), str(m.code), num(m.status) == null ? "" : String(m.status),
    Array.isArray(m.sources) ? m.sources.filter((s) => s === "worker" || s === "github").join(",") : ""],
  external_cert: (m) => [str(m.host), withUnit(m.days, "")],
  external_stale: (m, w) => [m.since ? (w.time ? w.time(String(m.since)) : String(m.since)) : "—"],
  external_stale_detail: () => [],
};

/** A burn window's length: "5 min" under an hour, "6 h" from an hour up; "—" without it. */
export function windowText(minutes: unknown): string {
  const n = num(minutes);
  if (n == null) return "—";
  return n < 60 ? withUnit(n, "min") : withUnit(n / 60, "h");
}

/** AA-1141: a capacity forecast item: the database storage, or one AI connection's monthly cap. */
function forecastPhrase(m: Msg): Phrase {
  const days = withUnit(m.days, "");
  return m.tile === "rds_free" ? { key: "m_forecast_rds", args: [days] }
    : { key: "m_forecast_ai", args: [str(m.label), days] };
}

/** A limit item of a shared resource tile: free storage against a share of the allocated storage (CIS 477) says the
 *  share and the GiB it equals; the other tiles say value and limit in their unit. */
function resourcePhrase(m: Msg, w: Words): Phrase {
  const tile = str(m.tile);
  const head = [w.tile(tile)];
  const level = w.level(str(m.level));
  if (num(m.value_pct) != null && num(m.limit_pct) != null) {
    return { key: "m_res_free_pct", args: [...head, withUnit(m.value, "GiB"), withUnit(m.value_pct, "%"),
      withUnit(m.allocated_gib, "GiB"), level, withUnit(m.limit_pct, "%"), withUnit(m.limit, "GiB")] };
  }
  const unit = w.unit(tile, str(m.unit));
  return { key: m.dir === "below" ? "m_res_below" : "m_res_above",
    args: [...head, withUnit(m.value, unit), level, withUnit(m.limit, unit)] };
}

/** The phrase of a structured text, or null for a key this page does not know (show the API's English fallback). */
export function phrase(m: Msg | null | undefined, w: Words): Phrase | null {
  if (!m || typeof m.key !== "string") return null;
  if (m.key === "resource") return resourcePhrase(m, w);
  if (m.key === "forecast") return forecastPhrase(m);
  const build = (PHRASES as Record<string, (m: Msg, w: Words) => string[]>)[m.key];
  return build ? { key: `m_${m.key}`, args: build(m, w) } : null;
}

/** Every message key a phrase can name (the catalogue must have each in EN and VI; lib/opsStatusText.test.mjs). */
export function phraseKeys(): string[] {
  return [...Object.keys(PHRASES).map((k) => `m_${k}`), "m_res_free_pct", "m_res_below", "m_res_above",
    "m_forecast_rds", "m_forecast_ai", ...CHANGE_KEYS.map((k) => `m_${k}`)];
}

/** The service reason codes of api/services/ops_status/sections.py service_reason_code. */
export const REASON_CODES = [
  "scaled_to_zero", "none_running", "rollout_failed", "below_desired", "rollout_in_progress", "running",
] as const;

/** "Free storage" threshold line: "20 % = 4 GiB" on the pct basis, "15 GiB" on the GiB fallback. */
export function freeLimitText(pct: number | null | undefined, gib: number | null | undefined): string {
  return pct == null ? withUnit(gib, "GiB") : `${withUnit(pct, "%")} = ${withUnit(gib, "GiB")}`;
}

// ── AA-1141: the What changed rows (api/services/ops_status/changes.py `msg`) ──────────────────────────────────────

export const CHANGE_KEYS = [
  "chg_deploy", "chg_deploy_failed", "chg_setting", "chg_migration", "chg_wave_opened", "chg_wave_paused",
  "chg_wave_closed", "chg_route",
] as const;

const CHANGE_PHRASES: Readonly<Record<(typeof CHANGE_KEYS)[number], (m: Msg) => string[]>> = {
  chg_deploy: (m) => [str(m.subject), m.tag ? String(m.tag) : ""],
  chg_deploy_failed: (m) => [str(m.subject), m.tag ? String(m.tag) : ""],
  chg_setting: (m) => [str(m.label), str(m.version), m.tenant ? "1" : ""],
  chg_migration: (m) => [str(m.version)],
  chg_wave_opened: (m) => [str(m.name)],
  chg_wave_paused: (m) => [str(m.name)],
  chg_wave_closed: (m) => [str(m.name)],
  chg_route: (m) => [str(m.version), str(m.action), m.preset ? String(m.preset) : ""],
};

/** The phrase of a What changed row, or null (show the API's English title). */
export function changePhrase(m: Msg | null | undefined): Phrase | null {
  if (!m || typeof m.key !== "string") return null;
  const build = (CHANGE_PHRASES as Record<string, (m: Msg) => string[]>)[m.key];
  return build ? { key: `m_${m.key}`, args: build(m) } : null;
}
