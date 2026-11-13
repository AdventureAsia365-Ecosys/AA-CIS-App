// frontend/lib/settingsRegistry.ts — S279 thresholds L1: Platform › Settings, the admin-editable settings registry.
//
// Types of the backend API (api/routers/admin_settings_registry.py, BFF /api/admin/settings/registry) and the pure
// helpers of the page (app/admin/platform/settings): the value shown, the parse of a typed value against its definition
// (the same rules as shared/settings/reader.py validate, so the form says why before the server answers 422), the
// proposal body, the grouping by area and the badges. No React here (tested by lib/settingsRegistry.test.mjs).

export type SettingApp = "cis" | "tripplanner" | "booking" | "crm";
export type ValueType = "int" | "numeric" | "bool" | "enum" | "duration_s" | "usd" | "pct" | "list" | "json";
export type SettingSource = "env" | "tenant" | "platform" | "default";
export type ValueStatus = "proposed" | "approved" | "rejected" | "superseded";

export type SettingValue = number | boolean | string | unknown[] | Record<string, unknown>;

export interface SettingRow {
  id: string;
  key: string;
  tenant_id: string | null;
  value: SettingValue;
  version: number;
  status: ValueStatus;
  effective_from: string | null;
  proposed_by: string;
  proposed_at: string | null;
  reason: string;
  evidence_ref: string | null;
  self_approved: boolean;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  previous_value?: SettingValue;
  /** The staff member's name or e-mail (null = no staff row: a script or a migration), read by the API. */
  proposed_by_name?: string | null;
  decided_by_name?: string | null;
}

/** AA-1096 l23 (CIS 417): the operator's guide of a setting, one per language. */
export const GUIDE_PARTS = ["what", "raise", "lower", "when", "example"] as const;
export type GuidePart = (typeof GUIDE_PARTS)[number];
export type GuideText = Record<GuidePart, string>;
export type SettingGuide = Partial<Record<"en" | "vi", GuideText>>;

export interface SettingDefinition {
  key: string;
  app: SettingApp;
  area: string;
  label: string;
  description: string;
  value_type: ValueType;
  unit: string | null;
  min_value: number | null;
  max_value: number | null;
  allowed_values: unknown[] | null;
  default_value: SettingValue;
  scope_kind: "platform" | "platform+tenant";
  impact: "high" | "medium" | "low";
  approval: "single" | "maker_checker";
  evidence_required: boolean;
  env_override: string | null;
  cache_s: number;
  enforced: boolean;
  /** null = no guide yet (CI fails on it; a CRM / TripPlanner row may lack one until its registry has it). */
  guide?: SettingGuide | null;
}

export interface SettingItem extends SettingDefinition {
  value: SettingValue;
  source: SettingSource;
  version: number | null;
  overridden_by_env: boolean;
  env_ignored: string | null;
  refused: string | null;
  last_change: SettingRow | null;
  proposals: SettingRow[];
}

export interface RegistryResp {
  app: SettingApp;
  areas: string[];
  settings: SettingItem[];
  two_person_approval: boolean;
  reader_error: string | null;
}

export interface HistoryResp {
  key: string;
  tenant_id: string | null;
  definition: SettingDefinition;
  history: SettingRow[];
}

export const API = "/api/admin/settings/registry";
/** The apps of the page, in tab order. Booking keeps its own policy page (Pricing › Policy, maker-checker): its tab
 *  links there. */
export const APPS: SettingApp[] = ["cis", "tripplanner", "crm", "booking"];
export const REASON_MIN = 20;
const NUMERIC: ValueType[] = ["int", "numeric", "duration_s", "usd", "pct"];
const WHOLE: ValueType[] = ["int", "duration_s"];

export function registryPath(app: SettingApp): string {
  return `${API}?app=${encodeURIComponent(app)}`;
}

export function historyPath(key: string, tenantId?: string | null): string {
  const q = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  return `${API}/${encodeURIComponent(key)}/history${q}`;
}

export function proposalPath(key: string): string {
  return `${API}/${encodeURIComponent(key)}/proposals`;
}

export function decisionPath(valueId: string, approve: boolean): string {
  return `${API}/proposals/${encodeURIComponent(valueId)}/${approve ? "approve" : "reject"}`;
}

/** A value as one line of text: numbers as typed (USD with $), booleans on/off, a list or table as compact JSON. */
export function formatValue(value: SettingValue | undefined | null, d: Pick<SettingDefinition, "value_type" | "unit">):
  string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "boolean") return value ? "on" : "off";
  if (typeof value === "number") {
    if (d.value_type === "usd") return `$${value}`;
    if (d.value_type === "pct") return `${value} %`;
    return d.unit ? `${value} ${d.unit}` : String(value);
  }
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/** The bounds as text ("1 – 1000 posts/day", "one of soft, hard"); "" when the setting has none. */
export function boundsText(d: Pick<SettingDefinition, "value_type" | "min_value" | "max_value" | "allowed_values" | "unit">):
  string {
  if (d.value_type === "enum" && d.allowed_values) return `one of ${d.allowed_values.join(", ")}`;
  if (d.min_value == null && d.max_value == null) return "";
  const lo = d.min_value == null ? "…" : String(d.min_value);
  const hi = d.max_value == null ? "…" : String(d.max_value);
  return `${lo} – ${hi}${d.unit ? ` ${d.unit}` : ""}`;
}

function inBounds(d: SettingDefinition, n: number): string | null {
  if (d.min_value != null && n < d.min_value) return `below the minimum ${d.min_value}`;
  if (d.max_value != null && n > d.max_value) return `above the maximum ${d.max_value}`;
  return null;
}

/** A typed number: {value} or {error}. A decimal key takes "3.364" and "3,364" alike (one point or one comma as the
 *  decimal mark); a whole-number key takes digits only, so "2,000" is refused, never read as 2. No thousands
 *  separator. Pure. */
export function parseNumber(text: string, whole: boolean): { value?: number; error?: string } {
  const raw = text.trim();
  if (whole && /^-?\d+[.,]\d+$/.test(raw)) return { error: "Enter a whole number, without separators" };
  const ok = whole ? /^-?\d+$/.test(raw) : /^-?\d+([.,]\d+)?$/.test(raw);
  return ok ? { value: Number(raw.replace(",", ".")) } : { error: whole ? "Enter a whole number" : "Enter a number" };
}

/** The value typed in the form, parsed and checked against the definition: {value} or {error}. Pure. */
export function parseDraft(d: SettingDefinition, text: string): { value?: SettingValue; error?: string } {
  const raw = text.trim();
  if (NUMERIC.includes(d.value_type)) {
    const num = parseNumber(raw, WHOLE.includes(d.value_type));
    if (num.value === undefined) return { error: num.error };
    const out = inBounds(d, num.value);
    return out ? { error: `The value is ${out}` } : { value: num.value };
  }
  if (d.value_type === "bool") {
    if (raw === "true" || raw === "on") return { value: true };
    if (raw === "false" || raw === "off") return { value: false };
    return { error: "Choose on or off" };
  }
  if (d.value_type === "enum") {
    return (d.allowed_values ?? []).includes(raw) ? { value: raw } : { error: `Choose one of ${(d.allowed_values ?? []).join(", ")}` };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: "Enter valid JSON" };
  }
  if (d.value_type === "list") {
    if (!Array.isArray(parsed)) return { error: "Enter a JSON list" };
    const bad = d.allowed_values ? parsed.find((x) => !(d.allowed_values ?? []).includes(x)) : undefined;
    return bad !== undefined ? { error: `${JSON.stringify(bad)} is not allowed` } : { value: parsed };
  }
  if (parsed === null || typeof parsed !== "object") return { error: "Enter a JSON object or list" };
  if (!Array.isArray(parsed)) {
    for (const v of Object.values(parsed as Record<string, unknown>)) {
      if (typeof v !== "number") {
        if (d.min_value != null || d.max_value != null) return { error: "Every entry must be a number" };
        continue;
      }
      const out = inBounds(d, v);
      if (out) return { error: `An entry is ${out}` };
    }
  }
  return { value: parsed as SettingValue };
}

/** The text a form starts from for the value in force. */
export function draftText(d: SettingDefinition, value: SettingValue): string {
  if (typeof value === "boolean") return value ? "on" : "off";
  if (typeof value === "number" || typeof value === "string") return String(value);
  return JSON.stringify(value, null, 1);
}

export interface ProposalDraft {
  text: string;
  reason: string;
  evidence: string;
  effectiveFrom: string;   // "" = now; else an ISO date-time from the form
  apply: boolean;
}

/** Why the form cannot be sent (null = it can), checked like the server: value, reason length, evidence. */
export function draftProblem(d: SettingDefinition, draft: ProposalDraft): string | null {
  const parsed = parseDraft(d, draft.text);
  if (parsed.error) return parsed.error;
  if (draft.reason.trim().length < REASON_MIN) return `Give a reason of at least ${REASON_MIN} characters`;
  if (d.evidence_required && draft.evidence.trim().length < 3) return "This setting needs an evidence link (an eval or replay run)";
  return null;
}

/** The POST body of a proposal (the server re-checks everything). */
export function proposalBody(d: SettingDefinition, item: Pick<SettingItem, "version">, draft: ProposalDraft) {
  const parsed = parseDraft(d, draft.text);
  return {
    value: parsed.value,
    reason: draft.reason.trim(),
    evidence_ref: draft.evidence.trim() || null,
    effective_from: draft.effectiveFrom ? new Date(draft.effectiveFrom).toISOString() : null,
    base_version: item.version ?? 0,
    apply: draft.apply,
  };
}

/** The badges of one row, in display order. */
export type BadgeKey = "env" | "notEnforced" | "pending" | "refused" | "evidence" | "changed";
export function badgesOf(item: SettingItem): BadgeKey[] {
  const out: BadgeKey[] = [];
  if (item.overridden_by_env) out.push("env");
  if (!item.enforced) out.push("notEnforced");
  if (item.proposals.length) out.push("pending");
  if (item.refused) out.push("refused");
  if (item.evidence_required) out.push("evidence");
  if (item.source === "platform" || item.source === "tenant") out.push("changed");
  return out;
}

/** Settings of one area, sorted by label; areas in the API's order. */
export function groupByArea(items: SettingItem[]): { area: string; items: SettingItem[] }[] {
  const by = new Map<string, SettingItem[]>();
  for (const it of items) by.set(it.area, [...(by.get(it.area) ?? []), it]);
  return [...by.entries()].sort(([a], [b]) => a.localeCompare(b))
    .map(([area, list]) => ({ area, items: [...list].sort((x, y) => x.label.localeCompare(y.label)) }));
}

/** The actor id of a row without its prefix ("admin:<id>" -> "<id>"). */
export function actorText(actor: string | null | undefined): string {
  if (!actor) return "—";
  return actor.startsWith("admin:") ? actor.slice(6) : actor;
}

/** Who made a change, as people read it: the staff member's name or e-mail (the API's *_name), else the actor id;
 *  the tooltip always carries the id. */
export function actorLabel(actor: string | null | undefined, name: string | null | undefined):
  { text: string; title: string } {
  const id = actorText(actor);
  return { text: name?.trim() || id, title: id };
}

/** The guide of a setting in the page's language, else English, else null when it has none. */
export function guideFor(d: Pick<SettingDefinition, "guide">, lang: string): GuideText | null {
  const g = d.guide;
  if (!g) return null;
  return (lang === "vi" ? g.vi : undefined) ?? g.en ?? g.vi ?? null;
}

/** How the page names the two directions of a value type: a number goes up or down, an on/off switch goes on or
 *  off, a choice, list or table is made stricter or looser. */
export type DirectionWords = "number" | "switch" | "choice";
export function directionWords(t: ValueType): DirectionWords {
  if (NUMERIC.includes(t)) return "number";
  return t === "bool" ? "switch" : "choice";
}

/** AA-1096 l23 review: a guide's example names the default, because the value in force can change. For the keys
 *  whose meaning is a sum of other values (the blog source words a topic needs, the repeat cleanup, the blog length
 *  aim) the page adds a line computed from the values in force: valueOf(key) reads one of them (the app's items, or
 *  the draft in the change dialog). The line is a message id and its numbers, formatted for the page's language (the
 *  words live in the page's messages.ts). null = nothing to add (another key, or a value it needs is missing). Pure. */
export type ValueOf = (key: string) => SettingValue | undefined;
export type NowLine =
  | { id: "nowSeedRatio"; ratio: string; words: string; floor: string; off: boolean }
  | { id: "nowSeedRatioZero" }
  | { id: "nowSeedMin"; flat: string; sized: string; need: string }
  | { id: "nowSeedMinOff" }
  | { id: "nowRepeat"; share: string; pct: string }
  | { id: "nowLengthAim"; margin: string; aim: string; floor: string };

const BLOG_FLOOR = "cis.t9.blog_chars_min";
const BLOG_CEILING = "cis.t9.blog_chars_max";
const SEED_RATIO = "cis.t7.blog_chars_per_seed_word";
const SEED_MIN = "cis.t7.blog_min_seed_words";

function numberOf(valueOf: ValueOf, key: string): number | null {
  const v = valueOf(key);
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

type Fmt = (n: number, digits?: number) => string;

function seedRatioNow(valueOf: ValueOf, f: Fmt): NowLine | null {
  const r = numberOf(valueOf, SEED_RATIO);
  const floor = numberOf(valueOf, BLOG_FLOOR);
  if (r === null || floor === null) return null;
  if (r <= 0) return { id: "nowSeedRatioZero" };
  return { id: "nowSeedRatio", ratio: f(r), words: f(Math.ceil(floor / r), 0), floor: f(floor, 0),
    off: numberOf(valueOf, SEED_MIN) === 0 };
}

function seedMinNow(valueOf: ValueOf, f: Fmt): NowLine | null {
  const flat = numberOf(valueOf, SEED_MIN);
  if (flat === null) return null;
  if (flat <= 0) return { id: "nowSeedMinOff" };
  const r = numberOf(valueOf, SEED_RATIO) ?? 0;
  const sized = r > 0 ? Math.ceil((numberOf(valueOf, BLOG_FLOOR) ?? 0) / r) : 0;
  return { id: "nowSeedMin", flat: f(flat, 0), sized: f(sized, 0), need: f(Math.max(flat, sized), 0) };
}

function repeatNow(valueOf: ValueOf, f: Fmt): NowLine | null {
  const v = numberOf(valueOf, "cis.t9.repeat_drop_near_exact");
  return v === null ? null : { id: "nowRepeat", share: f(v), pct: f(v * 100, 1) };
}

function lengthAimNow(valueOf: ValueOf, f: Fmt): NowLine | null {
  const m = numberOf(valueOf, "cis.quality.t9_blog_length_target_margin");
  const floor = numberOf(valueOf, BLOG_FLOOR);
  if (m === null || floor === null) return null;
  const aim = Math.min(Math.round(floor * (1 + m)), numberOf(valueOf, BLOG_CEILING) ?? Infinity);
  return { id: "nowLengthAim", margin: f(m), aim: f(aim, 0), floor: f(floor, 0) };
}

const NOW_LINES: Record<string, (valueOf: ValueOf, f: Fmt) => NowLine | null> = {
  [SEED_RATIO]: seedRatioNow,
  [SEED_MIN]: seedMinNow,
  "cis.t9.repeat_drop_near_exact": repeatNow,
  "cis.quality.t9_blog_length_target_margin": lengthAimNow,
};

export function nowExample(key: string, valueOf: ValueOf, lang: string): NowLine | null {
  const line = NOW_LINES[key];
  const locale = lang === "vi" ? "vi-VN" : "en-US";
  return line ? line(valueOf, (n, digits = 3) => n.toLocaleString(locale, { maximumFractionDigits: digits })) : null;
}

/** valueOf over a list of items (their values in force). Pure. */
export function valuesInForce(items: Pick<SettingItem, "key" | "value">[]): ValueOf {
  const m = new Map<string, SettingValue>(items.map((s) => [s.key, s.value]));
  return (key) => m.get(key);
}

/** valueOf with one key replaced (the value typed in the change dialog). Pure. */
export function withValue(valueOf: ValueOf, key: string, value: SettingValue): ValueOf {
  return (k) => (k === key ? value : valueOf(k));
}
