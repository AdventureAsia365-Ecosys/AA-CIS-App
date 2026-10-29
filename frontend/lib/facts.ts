// frontend/lib/facts.ts — AA-814: the platform Facts Entries (acp_shared.facts, scope 'platform') for the admin page
// app/admin/facts. Types, the kind list (CIS 302 facts_kind_check) and one call helper that turns a FastAPI 422 list
// into one readable line. Backend: api/routers/admin_facts.py; every call goes through the BFF /api/admin/facts.
// Dependency-free (no React), so lib/facts.test.mjs can import it.

export const FACT_KINDS = ["price", "season", "visa_entry", "transfer_time", "booking_terms", "other"] as const;
export type FactKind = (typeof FACT_KINDS)[number];

// Field limits (services/acp_shared/facts_store.py).
export const MAX_TITLE = 200;
export const MAX_BODY = 1200;
export const MAX_SOURCE = 500;
export const MAX_COUNTRY = 80;

export interface Fact {
  fact_id: string;
  scope: "platform" | "tenant";
  kind: FactKind;
  title: string;
  body: string;
  provenance: string;
  country: string | null;
  stated_on: string | null;
  valid_until: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  expired: boolean;
}

export interface FactList {
  facts: Fact[];
  kinds: FactKind[];
  count: number;
}

export interface FactHistoryRow {
  action: "fact.created" | "fact.updated" | "fact.deleted" | string;
  actor: string;
  details: { changes?: Record<string, [unknown, unknown]>; after?: Record<string, unknown>; before?: Record<string, unknown> };
  created_at: string | null;
}

export interface FactFilters {
  kind: FactKind | "";
  q: string;
  includeExpired: boolean;
}

export const EMPTY_FILTERS: FactFilters = { kind: "", q: "", includeExpired: true };

export const BASE = "/api/admin/facts";

export function listUrl(f: FactFilters): string {
  const p = new URLSearchParams();
  if (f.kind) p.set("kind", f.kind);
  if (f.q.trim()) p.set("q", f.q.trim());
  if (!f.includeExpired) p.set("include_expired", "false");
  const qs = p.toString();
  return qs ? `${BASE}?${qs}` : BASE;
}

/** The editable fields as the form holds them (strings; "" = no value). */
export interface FactForm {
  kind: FactKind;
  title: string;
  body: string;
  provenance: string;
  country: string;
  stated_on: string;
  valid_until: string;
}

export const EMPTY_FORM: FactForm = {
  kind: "other", title: "", body: "", provenance: "", country: "", stated_on: "", valid_until: "",
};

export function formOf(f: Fact): FactForm {
  return {
    kind: f.kind, title: f.title, body: f.body, provenance: f.provenance, country: f.country ?? "",
    stated_on: f.stated_on ?? "", valid_until: f.valid_until ?? "",
  };
}

const clean = (s: string) => s.replace(/\s+/g, " ").trim();
const orNull = (s: string) => (clean(s) ? clean(s) : null);

/** The body of a create. */
export function createBody(form: FactForm): Record<string, unknown> {
  return {
    kind: form.kind, title: clean(form.title), body: clean(form.body), provenance: clean(form.provenance),
    country: orNull(form.country), stated_on: form.stated_on || null, valid_until: form.valid_until || null,
  };
}

/** The fields that differ from the loaded fact (a PATCH sends only these, plus expected_updated_at). */
export function changedFields(fact: Fact, form: FactForm): Record<string, unknown> {
  const next = createBody(form);
  const before = createBody(formOf(fact));
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(next)) if (next[k] !== before[k]) out[k] = next[k];
  return out;
}

/** Required text present and the end date not before the statement date. */
export function formProblem(form: FactForm): "required" | "dates" | null {
  if (!clean(form.title) || !clean(form.body) || !clean(form.provenance)) return "required";
  if (form.stated_on && form.valid_until && form.valid_until < form.stated_on) return "dates";
  return null;
}

export type CallResult<T> = { ok: true; body: T } | { ok: false; status: number; message: string };

/** FastAPI errors: `detail` is a string, or (422 from validation) a list of {loc, msg}. */
export function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
      return loc ? `${loc}: ${d?.msg ?? ""}` : String(d?.msg ?? "");
    }).filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return `Request failed (${status})`;
}

export async function factCall<T>(method: string, path: string, body?: unknown): Promise<CallResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    return { ok: false, status: 0, message: (e as Error).message || "Network error" };
  }
  let json: unknown = null;
  try { json = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    return { ok: false, status: res.status, message: errorMessage((json as { detail?: unknown } | null)?.detail, res.status) };
  }
  return { ok: true, body: json as T };
}
