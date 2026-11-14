// frontend/lib/settingsRegistry.ts — S279 thresholds L1: Platform › Settings, the admin-editable settings registry.
//
// Types of the backend API (api/routers/admin_settings_registry.py, BFF /api/admin/settings/registry) and the pure
// helpers of the page (app/admin/platform/settings): the value shown, the parse of a typed value against its definition
// (the same rules as shared/settings/reader.py validate, so the form says why before the server answers 422), the
// proposal body, the grouping by area and the badges. No React here (tested by lib/settingsRegistry.test.mjs).
// AA-1096 (S280 UX fix round): the parse problems are message ids (the page words them in English or Vietnamese:
// messages.ts), formatValue / boundsText take the page's words (on / off, "one of", units), a change may not be
// dated in the past (the API refuses it too), the base version of a change is the approved version in force
// (approvedVersion: also under an env override or a refused stored value), scheduled changes and the open proposal of
// each scope are read here, and Save and approve is offered for a single-approval key while two-person approval is on.

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
  /** CRM tab: the agency of an agency's own row (the CRM's agency_pending, relayed as a proposal). */
  agency_name?: string | null;
}

/** AA-1096 l23 (CIS 416): the operator's guide of a setting, one per language. */
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
  /** null = no guide yet (CI fails on it: CIS here, AA-Partner guides.json for CRM keys; TripPlanner may lack one). */
  guide?: SettingGuide | null;
}

/** AA-1096 (S280): an approved tenant value in force of a tenant-scoped key (the list's tenant_overrides). */
export interface TenantOverride {
  tenant_id: string;
  value: SettingValue;
  version: number;
  effective_from: string | null;
  /** CRM tab: the agency's name (the CRM's agency_overrides; its agencies are not CIS tenants). */
  agency_name?: string | null;
}

/** S280: an approved value whose effective_from is still ahead (the list's scheduled; every scope). */
export interface ScheduledValue {
  tenant_id: string | null;
  value: SettingValue;
  version: number;
  effective_from: string | null;
}

export interface SettingItem extends SettingDefinition {
  value: SettingValue;
  source: SettingSource;
  version: number | null;
  /** The approved platform version in force (0 = none), apart from the resolved `version` (null under an env
   *  override or a refused stored value). Absent from the CRM relay (approvedVersion falls back). */
  approved_version?: number;
  /** Approved changes not in force yet (absent from the CRM relay: scheduledOf reads the last change). */
  scheduled?: ScheduledValue[];
  overridden_by_env: boolean;
  env_ignored: string | null;
  refused: string | null;
  last_change: SettingRow | null;
  proposals: SettingRow[];
  /** The tenant values in force (a tenant-scoped key; [] otherwise; absent from an older API or the CRM relay). */
  tenant_overrides?: TenantOverride[];
}

export interface RegistryResp {
  app: SettingApp;
  areas: string[];
  settings: SettingItem[];
  two_person_approval: boolean;
  reader_error: string | null;
  /** CRM tab: may this staff member change CRM settings (the CRM's own answer; null / absent = not known). */
  can_write?: boolean | null;
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

/** The page's words for a value (messages.ts): on / off, "one of …", a unit in the page's language. English when
 *  omitted (the tests, a caller without a page). */
export interface ValueWords {
  on: string;
  off: string;
  oneOf: (list: string) => string;
  unit: (unit: string) => string;
}
export const EN_WORDS: ValueWords = { on: "on", off: "off", oneOf: (list) => `one of ${list}`, unit: (u) => u };

/** The unit the page writes after a number, list or table ("" = none; USD and % are written by the number). */
function unitAfter(d: Pick<SettingDefinition, "value_type" | "unit">, w: ValueWords): string {
  if (!d.unit || d.value_type === "usd" || d.value_type === "pct") return "";
  return ` ${w.unit(d.unit)}`;
}

function formatNumber(value: number, d: Pick<SettingDefinition, "value_type" | "unit">, w: ValueWords): string {
  if (d.value_type === "usd") return `$${value}`;
  if (d.value_type === "pct") return `${value} %`;
  // a 0-1 share also reads as a percentage (the guides talk in %)
  if (d.unit === "share") return `${value} (${Math.round(value * 1000) / 10} %)`;
  return `${value}${unitAfter(d, w)}`;
}

/** A value as one line of text: numbers as typed (USD with $, a share with its %), booleans in the page's on / off,
 *  a list or table as compact JSON followed by its unit. */
export function formatValue(value: SettingValue | undefined | null, d: Pick<SettingDefinition, "value_type" | "unit">,
  w: ValueWords = EN_WORDS): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "boolean") return value ? w.on : w.off;
  if (typeof value === "number") return formatNumber(value, d, w);
  if (typeof value === "string") return value;
  return `${JSON.stringify(value)}${unitAfter(d, w)}`;
}

/** The bounds as text ("1 – 1000 posts/day", "one of soft, hard"); "" when the setting has none. */
export function boundsText(d: Pick<SettingDefinition, "value_type" | "min_value" | "max_value" | "allowed_values" | "unit">,
  w: ValueWords = EN_WORDS): string {
  if (d.value_type === "enum" && d.allowed_values) return w.oneOf(d.allowed_values.join(", "));
  if (d.min_value == null && d.max_value == null) return "";
  const lo = d.min_value == null ? "…" : String(d.min_value);
  const hi = d.max_value == null ? "…" : String(d.max_value);
  return `${lo} – ${hi}${d.unit ? ` ${w.unit(d.unit)}` : ""}`;
}

/** Why a typed value or a form cannot be sent: a message id of the page (messages.ts `problem_<id>`) and its one
 *  argument. */
export type ProblemId = "wholeNoSep" | "whole" | "number" | "belowMin" | "aboveMax" | "entryBelowMin" |
  "entryAboveMax" | "onOff" | "chooseOne" | "json" | "jsonList" | "notAllowed" | "jsonObject" | "entryNumber" |
  "reasonShort" | "evidence" | "effectivePast";
export interface Problem { id: ProblemId; arg?: string }
export const PROBLEM_IDS: ProblemId[] = ["wholeNoSep", "whole", "number", "belowMin", "aboveMax", "entryBelowMin",
  "entryAboveMax", "onOff", "chooseOne", "json", "jsonList", "notAllowed", "jsonObject", "entryNumber", "reasonShort",
  "evidence", "effectivePast"];

function inBounds(d: SettingDefinition, n: number, entry = false): Problem | null {
  if (d.min_value != null && n < d.min_value) return { id: entry ? "entryBelowMin" : "belowMin", arg: String(d.min_value) };
  if (d.max_value != null && n > d.max_value) return { id: entry ? "entryAboveMax" : "aboveMax", arg: String(d.max_value) };
  return null;
}

/** A typed number: {value} or {error}. A decimal key takes "3.364" and "3,364" alike (one point or one comma as the
 *  decimal mark); a whole-number key takes digits only, so "2,000" is refused, never read as 2. No thousands
 *  separator. Pure. */
export function parseNumber(text: string, whole: boolean): { value?: number; error?: Problem } {
  const raw = text.trim();
  if (whole && /^-?\d+[.,]\d+$/.test(raw)) return { error: { id: "wholeNoSep" } };
  const ok = whole ? /^-?\d+$/.test(raw) : /^-?\d+([.,]\d+)?$/.test(raw);
  return ok ? { value: Number(raw.replace(",", ".")) } : { error: { id: whole ? "whole" : "number" } };
}

/** The value typed in the form, parsed and checked against the definition: {value} or {error}. Pure. */
export function parseDraft(d: SettingDefinition, text: string): { value?: SettingValue; error?: Problem } {
  const raw = text.trim();
  if (NUMERIC.includes(d.value_type)) {
    const num = parseNumber(raw, WHOLE.includes(d.value_type));
    if (num.value === undefined) return { error: num.error };
    const out = inBounds(d, num.value);
    return out ? { error: out } : { value: num.value };
  }
  if (d.value_type === "bool") {
    if (raw === "true" || raw === "on") return { value: true };
    if (raw === "false" || raw === "off") return { value: false };
    return { error: { id: "onOff" } };
  }
  if (d.value_type === "enum") {
    return (d.allowed_values ?? []).includes(raw) ? { value: raw }
      : { error: { id: "chooseOne", arg: (d.allowed_values ?? []).join(", ") } };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: { id: "json" } };
  }
  if (d.value_type === "list") {
    if (!Array.isArray(parsed)) return { error: { id: "jsonList" } };
    const bad = d.allowed_values ? parsed.find((x) => !(d.allowed_values ?? []).includes(x)) : undefined;
    return bad !== undefined ? { error: { id: "notAllowed", arg: JSON.stringify(bad) } } : { value: parsed };
  }
  if (parsed === null || typeof parsed !== "object") return { error: { id: "jsonObject" } };
  if (!Array.isArray(parsed)) {
    for (const v of Object.values(parsed as Record<string, unknown>)) {
      if (typeof v !== "number") {
        if (d.min_value != null || d.max_value != null) return { error: { id: "entryNumber" } };
        continue;
      }
      const out = inBounds(d, v, true);
      if (out) return { error: out };
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

/** How far in the past an "Effective from" may lie and still count as now (the API's PAST_GRACE_S: clock skew and
 *  the minute picker). */
export const PAST_GRACE_MS = 300_000;

/** Is a form's "Effective from" (datetime-local text, "" = when approved) in the past? Pure. */
export function effectiveInPast(text: string, now: Date = new Date()): boolean {
  if (!text) return false;
  const at = new Date(text).getTime();
  return Number.isFinite(at) && at < now.getTime() - PAST_GRACE_MS;
}

/** The `min` of a datetime-local input: now in local time, to the minute ("2026-11-14T09:05"). Pure. */
export function localMinute(now: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}T${p(now.getHours())}:${p(now.getMinutes())}`;
}

/** Why the form cannot be sent (null = it can), checked like the server: value, reason length, evidence, a date in the
 *  past. */
export function draftProblem(d: SettingDefinition, draft: ProposalDraft, now: Date = new Date()): Problem | null {
  const parsed = parseDraft(d, draft.text);
  if (parsed.error) return parsed.error;
  if (draft.reason.trim().length < REASON_MIN) return { id: "reasonShort", arg: String(REASON_MIN) };
  if (d.evidence_required && draft.evidence.trim().length < 3) return { id: "evidence" };
  if (effectiveInPast(draft.effectiveFrom, now)) return { id: "effectivePast" };
  return null;
}

/** The POST body of a proposal (the server re-checks everything). `item.version` is the approved version of the
 *  scope being changed (scopeTarget); a tenant value names its tenant (AA-1096, S280). */
export function proposalBody(d: SettingDefinition, item: Pick<SettingItem, "version">, draft: ProposalDraft,
  tenantId?: string | null) {
  const parsed = parseDraft(d, draft.text);
  return {
    value: parsed.value,
    reason: draft.reason.trim(),
    evidence_ref: draft.evidence.trim() || null,
    effective_from: draft.effectiveFrom ? new Date(draft.effectiveFrom).toISOString() : null,
    base_version: item.version ?? 0,
    apply: draft.apply,
    ...(tenantId ? { tenant_id: tenantId } : {}),
  };
}

/** AA-1096 (S280): where a change applies. A key whose definition allows a tenant value (scope_kind
 *  "platform+tenant") offers Platform or One tenant; any other key, and every CRM key (its agencies are the CRM's,
 *  not CIS tenants: they keep their values in the CRM), offers Platform only. */
export type SettingScope = "platform" | "tenant";
export function scopeOptions(d: Pick<SettingDefinition, "scope_kind" | "app">): SettingScope[] {
  return d.scope_kind === "platform+tenant" && d.app !== "crm" ? ["platform", "tenant"] : ["platform"];
}

export function tenantScoped(d: Pick<SettingDefinition, "scope_kind" | "app">): boolean {
  return scopeOptions(d).includes("tenant");
}

/** A CRM key an agency may keep its own value of: the page lists the agencies' values and open changes (named by the
 *  CRM, decided with the agency's id), while a change from this page stays a platform change. */
export function agencyScoped(d: Pick<SettingDefinition, "scope_kind" | "app">): boolean {
  return d.app === "crm" && d.scope_kind === "platform+tenant";
}

/** The agency names the CRM gave with its agencies' values and open changes (CRM tab), by agency id. Pure. */
export function agencyNames(item: Pick<SettingItem, "tenant_overrides" | "proposals">): Map<string, string> {
  const out = new Map<string, string>();
  for (const r of [...(item.tenant_overrides ?? []), ...item.proposals]) {
    if (r.tenant_id && r.agency_name && !out.has(r.tenant_id)) out.set(r.tenant_id, r.agency_name);
  }
  return out;
}

/** The scope picked in the change form: the platform, or one tenant ("" = none picked yet). */
export interface ScopeChoice { kind: SettingScope; tenantId: string }
export const PLATFORM_SCOPE: ScopeChoice = { kind: "platform", tenantId: "" };

/** The tenant a change names (null = the platform, or no tenant picked yet). */
export function scopeTenant(c: ScopeChoice): string | null {
  return c.kind === "tenant" && c.tenantId ? c.tenantId : null;
}

/** Whether the scope is complete: the platform, or a tenant picked. */
export function scopeReady(c: ScopeChoice): boolean {
  return c.kind === "platform" || c.tenantId !== "";
}

/** The value a change replaces at a scope and the version it names: the platform value in force, or a tenant's own
 *  value (else what the tenant inherits today: the platform value, version 0 = no tenant value yet). `inherited`
 *  tells the form the tenant has no value of its own. Pure. */
export function scopeTarget(item: VersionItem & Pick<SettingItem, "value" | "tenant_overrides">, tenantId: string | null):
  { value: SettingValue; version: number; inherited: boolean } {
  if (!tenantId) return { value: item.value, version: approvedVersion(item), inherited: false };
  const own = (item.tenant_overrides ?? []).find((o) => o.tenant_id === tenantId);
  return own ? { value: own.value, version: own.version, inherited: false }
    : { value: item.value, version: 0, inherited: true };
}

type VersionItem = Pick<SettingItem, "version"> & Partial<Pick<SettingItem, "approved_version" | "last_change">>;

function isAhead(at: string | null | undefined, now: Date): boolean {
  return !!at && new Date(at).getTime() > now.getTime();
}

/** The approved platform version in force, the base version of a platform change (the server's optimistic lock):
 *  the API's approved_version; else (the CRM relay) the resolved version; else the last change when it is an
 *  approved one in force; else 0. The resolved version alone is null under an env override or a refused stored value,
 *  which made every change fail as "changed since you opened it". Pure. */
export function approvedVersion(item: VersionItem, now: Date = new Date()): number {
  if (typeof item.approved_version === "number") return item.approved_version;
  if (item.version != null) return item.version;
  const last = item.last_change;
  return last && last.status === "approved" && !isAhead(last.effective_from, now) ? last.version : 0;
}

/** The approved changes of one scope (null = platform) not in force yet, soonest first: the list's scheduled, else
 *  (the CRM relay) the last change when it is approved and dated ahead. Pure. */
export function scheduledOf(item: Pick<SettingItem, "scheduled" | "last_change">, tenantId: string | null = null,
  now: Date = new Date()): ScheduledValue[] {
  const scope = (r: { tenant_id: string | null }) => (r.tenant_id ?? null) === tenantId;
  if (item.scheduled) return item.scheduled.filter(scope);
  const last = item.last_change;
  return last && last.status === "approved" && isAhead(last.effective_from, now) && scope(last) ? [last] : [];
}

/** The open proposal of one scope (null = platform), if any: a second one is refused (one per key and scope). */
export function openProposal(item: Pick<SettingItem, "proposals">, tenantId: string | null): SettingRow | undefined {
  return item.proposals.find((p) => (p.tenant_id ?? null) === tenantId);
}

/** The open proposals of tenants (the platform's own is the dialog's default). Pure. */
export function tenantProposals(item: Pick<SettingItem, "proposals">): SettingRow[] {
  return item.proposals.filter((p) => p.tenant_id);
}

/** May the change dialog approve at once? Not while two-person approval is on, unless the key needs one approval
 *  only (the server checks the second person only for maker_checker keys). */
export function canApplyAtOnce(item: Pick<SettingDefinition, "approval">, twoPerson: boolean): boolean {
  return !twoPerson || item.approval === "single";
}

/** What the last-change cell says beside its row: "rejected" (the CRM still lists rejections), "scheduled" (an
 *  approved change dated ahead) or null. Pure. */
export function lastChangeState(row: Pick<SettingRow, "status" | "effective_from">, now: Date = new Date()):
  "rejected" | "scheduled" | null {
  if (row.status === "rejected") return "rejected";
  return row.status === "approved" && isAhead(row.effective_from, now) ? "scheduled" : null;
}

/** Is a history row an approved change dated ahead? Pure. */
export function isScheduled(row: Pick<SettingRow, "status" | "effective_from">, now: Date = new Date()): boolean {
  return row.status === "approved" && isAhead(row.effective_from, now);
}

/** What History's "Effective from" cell shows. A change sent without a date is stored with its proposal time (the
 *  server's coalesce(now())), but it takes effect only once approved: an open one shows "when approved" (null), an
 *  approved or superseded one the later of its date and its approval, a rejected one nothing (null as well; the cell
 *  tells the two apart by status). Pure. */
export function effectiveShown(row: Pick<SettingRow, "status" | "effective_from" | "proposed_at" | "decided_at">):
  string | null {
  const at = row.effective_from;
  if (row.status === "rejected" || !at) return null;
  const t = new Date(at).getTime();
  const proposed = row.proposed_at ? new Date(row.proposed_at).getTime() : NaN;
  if (row.status === "proposed") return t - proposed <= 1000 ? null : at; // undated: stored as its proposal time
  const decided = row.decided_at ? new Date(row.decided_at).getTime() : NaN;
  return decided > t ? row.decided_at : at;
}

/** Why the list could not load, as the page words it: the registry is not set up yet (CIS 407 / crm-064 missing),
 *  the app (the CRM) or the database did not answer (retry), or another error. */
export type LoadProblem = "notMigrated" | "unavailable" | "other";
export function loadProblem(status: number | undefined, code: string | undefined): LoadProblem {
  if (status !== 503) return "other";
  return code === "not_migrated" || code === "settings_unavailable" ? "notMigrated" : "unavailable";
}

/** The tenant whose values the history dialog opens on: the tenant of the open proposal when only a tenant has one
 *  (the Review button), else the platform (null). Pure. */
export function historyTenant(item: Pick<SettingItem, "proposals">): string | null {
  if (item.proposals.some((p) => !p.tenant_id)) return null;
  return item.proposals.find((p) => p.tenant_id)?.tenant_id ?? null;
}

/** AA-1096 (S280): the page search over every area of the app: each word of the query must appear in the key, the
 *  label or the description (any case); "" = no search. Matches sorted by area, then label. Pure. */
export function searchSettings<T extends Pick<SettingDefinition, "key" | "label" | "description" | "area">>(
  items: T[], query: string): T[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const hit = (s: T) => {
    const text = `${s.key} ${s.label} ${s.description}`.toLowerCase();
    return words.every((w) => text.includes(w));
  };
  return items.filter(hit).sort((a, b) => a.area.localeCompare(b.area) || a.label.localeCompare(b.label));
}

/** The POST body of an approve / reject. A CRM row is decided in the CRM (AA-1096 L4: the CIS API relays it there),
 *  an agency's own row names its agency (the CRM sees that row only in the agency's context). */
export function decisionBody(item: Pick<SettingDefinition, "app">, row: Pick<SettingRow, "tenant_id">, note: string) {
  const body: { note: string | null; app?: "crm"; tenant_id?: string } = { note: note.trim() || null };
  if (item.app === "crm") {
    body.app = "crm";
    if (row.tenant_id) body.tenant_id = row.tenant_id;
  }
  return body;
}

/** The badges of one row, in display order. */
export type BadgeKey = "env" | "notEnforced" | "pending" | "scheduled" | "refused" | "evidence" | "changed";
export function badgesOf(item: SettingItem, now: Date = new Date()): BadgeKey[] {
  const out: BadgeKey[] = [];
  if (item.overridden_by_env) out.push("env");
  if (!item.enforced) out.push("notEnforced");
  if (item.proposals.length) out.push("pending");
  if (scheduledOf(item, null, now).length) out.push("scheduled");
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

/** The actor id of a row without its prefix ("admin:<id>" -> "<id>"; a CRM row's "platform:<sub>" -> "<sub>"). */
export function actorText(actor: string | null | undefined): string {
  if (!actor) return "—";
  return actor.replace(/^(admin|platform):/, "");
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
