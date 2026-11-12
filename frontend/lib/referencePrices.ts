// frontend/lib/referencePrices.ts — AA-1091: the structured reference price of the AA tours for the admin page
// app/admin/reference-prices (AA Catalogue › Reference prices). Types, the list URL, the inline-edit draft and its
// check, the import / export helpers and one call helper that turns a FastAPI error into one readable line.
// Backend: api/routers/admin_reference_prices.py (CIS 402); every call goes through the BFF /api/admin/reference-prices.
// Dependency-free (no React), so lib/referencePrices.test.mjs can import it.

export const BASE = "/api/admin/reference-prices";

export const STATUSES = ["missing", "suggested", "needs_review", "parsed", "confirmed"] as const;
export type RefStatus = (typeof STATUSES)[number];
export const FILTERS = ["no_price", "needs_review", "parsed", "confirmed", "all"] as const;
export type RefFilter = (typeof FILTERS)[number];
export const BASES = ["per_person", "per_group", "from"] as const;
export type Basis = (typeof BASES)[number];
// services/reference_price/parse.py CODES, the common ones first.
export const CURRENCIES = [
  "USD", "EUR", "GBP", "AUD", "SGD", "THB", "VND", "JPY", "INR", "NPR", "BTN", "LAK", "KHR", "MMK", "MYR", "IDR", "PHP",
  "LKR", "MVR", "KRW", "CNY", "HKD", "TWD", "MNT", "NZD", "CAD", "CHF", "BND", "AED",
] as const;
export const MAX_NOTE = 500;
export const MAX_IMPORT_BYTES = 5 * 1024 * 1024;

export interface SavedPrice {
  amount: string;
  currency: string;
  basis: Basis;
  origin: "parsed" | "staff" | "import";
  note: string | null;
  needs_review: boolean;
  review_reasons: string[];
  price_raw_seen: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

export interface Suggestion {
  amount: string | null;
  currency: string | null;
  basis: Basis | null;
  reasons: string[];
  needs_review: boolean;
}

export interface RefItem {
  source_key: string;
  tour_id: string | null;
  name: string | null;
  country: string | null;
  duration: string | null;
  live: boolean;
  price_raw: string | null;
  status: RefStatus;
  text_changed: boolean;
  saved: SavedPrice | null;
  suggestion: Suggestion | null;
  parse_reasons: string[];
}

export type Counts = Record<RefStatus | "no_price" | "all", number>;

export interface RefPage {
  items: RefItem[];
  total: number;
  page: number;
  page_size: number;
  counts: Counts;
  countries: string[];
}

export interface RefFilters {
  status: RefFilter;
  country: string;
  q: string;
  liveOnly: boolean;
}

export const EMPTY_FILTERS: RefFilters = { status: "no_price", country: "", q: "", liveOnly: true };

/** The query string of a filter (and page) — shared by the list and the Excel export. */
export function filterQuery(f: RefFilters, page?: { index: number; size: number }): string {
  const p = new URLSearchParams();
  p.set("status", f.status);
  if (f.country) p.set("country", f.country);
  if (f.q.trim()) p.set("q", f.q.trim());
  if (!f.liveOnly) p.set("live_only", "false");
  if (page) {
    p.set("page", String(page.index + 1));
    p.set("page_size", String(page.size));
  }
  return p.toString();
}

export function listUrl(f: RefFilters, page: { index: number; size: number }): string {
  return `${BASE}?${filterQuery(f, page)}`;
}

export function exportUrl(f: RefFilters): string {
  return `${BASE}/export?${filterQuery(f)}`;
}

export interface Draft {
  amount: string;
  currency: string;
  basis: Basis | "";
  note: string;
}

/** The inline editor's start values: the saved price, else the parser's suggestion, else empty (USD, per person). */
export function draftOf(item: RefItem): Draft {
  if (item.saved) {
    return { amount: trimZeros(item.saved.amount), currency: item.saved.currency, basis: item.saved.basis,
      note: item.saved.note ?? "" };
  }
  const s = item.suggestion;
  return { amount: s?.amount ? trimZeros(s.amount) : "", currency: s?.currency ?? "USD",
    basis: s?.basis ?? "per_person", note: "" };
}

/** "1450.00" -> "1450", "1450.50" -> "1450.50". */
export function trimZeros(amount: string): string {
  return amount.endsWith(".00") ? amount.slice(0, -3) : amount;
}

/** The amount a person typed ("1,450", "1450.5", "1.450,50") as a number, or null. */
export function amountOf(text: string): number | null {
  let t = text.trim().replace(/\s/g, "");
  if (!t) return null;
  if (t.includes(",") && t.includes(".")) {
    t = t.lastIndexOf(",") > t.lastIndexOf(".") ? t.replace(/\./g, "").replace(",", ".") : t.replace(/,/g, "");
  } else if (t.includes(",")) {
    const parts = t.split(",");
    t = parts.slice(1).every((p) => p.length === 3) ? t.replace(/,/g, "") : t.replace(",", ".");
  }
  if (!/^\d+(\.\d+)?$/.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** The first problem of a draft (a message key), or null. */
export function draftProblem(d: Draft): "errAmount" | "errCurrency" | "errBasis" | "errNote" | null {
  const n = amountOf(d.amount);
  if (n === null || n <= 0 || n > 9999999999.99) return "errAmount";
  if (!/^[A-Z]{3}$/.test(d.currency.trim().toUpperCase())) return "errCurrency";
  if (!(BASES as readonly string[]).includes(d.basis)) return "errBasis";
  if (d.note.length > MAX_NOTE) return "errNote";
  return null;
}

/** Has the draft changed from what the row shows as saved? (a row with no saved price always counts as changed) */
export function draftChanged(item: RefItem, d: Draft): boolean {
  const s = item.saved;
  if (!s) return true;
  const n = amountOf(d.amount);
  return n !== Number(s.amount) || d.currency.toUpperCase() !== s.currency || d.basis !== s.basis
    || (d.note.trim() || null) !== (s.note || null);
}

export function saveBody(item: RefItem, d: Draft) {
  return {
    amount: String(amountOf(d.amount)), currency: d.currency.trim().toUpperCase(), basis: d.basis,
    note: d.note.trim() || null,
    ...(item.saved?.updated_at ? { expected_updated_at: item.saved.updated_at } : {}),
  };
}

/** May "Accept" be offered on this row? (a suggestion, an unconfirmed parse, or a price to review) */
export function canAccept(item: RefItem): boolean {
  return item.status === "suggested" || item.status === "parsed" || item.status === "needs_review";
}

export interface ImportLine {
  line: number;
  source_key: string;
  /** confirm: Confirm = yes on a row, its saved price / the parser's suggestion is confirmed as it is. */
  action: "create" | "update" | "confirm" | "unchanged" | "skip" | "error";
  /** On an unchanged line: the price's status, which a re-import never changes (e.g. still "parsed"). */
  status?: RefStatus;
  name?: string | null;
  error?: string;
  before?: Record<string, unknown> | null;
  after?: { amount: string; currency: string; basis: Basis; note: string | null };
}

export interface ImportResult {
  dry_run: boolean;
  applied: number;
  summary: { rows: number; errors: number; changes: number; actions: Partial<Record<ImportLine["action"], number>> };
  rows: ImportLine[];
}

/** The import actions listed after a DRY RUN (the others are only counted). */
export const SHOWN_ACTIONS: readonly ImportLine["action"][] = ["error", "create", "update", "confirm"];

/** The lines worth showing after a DRY RUN: errors first, then the changes (unchanged / skipped lines are counted). */
export function linesToShow(rows: ImportLine[], max = 200): ImportLine[] {
  const order = { error: 0, create: 1, update: 2, confirm: 3, unchanged: 4, skip: 5 } as const;
  return rows.filter((r) => SHOWN_ACTIONS.includes(r.action))
    .sort((a, b) => order[a.action] - order[b.action] || a.line - b.line)
    .slice(0, max);
}

/** base64 of a file's bytes (the REST API Gateway in front of the API is not binary safe: JSON + base64, AA-741). */
export function toBase64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

export function fromBase64(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** A FastAPI error body as one line: a string detail, the first 422 item, or {message} of the import refusal. */
export function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    const d = detail[0] as { msg?: string; loc?: unknown[] };
    const field = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body").join(".") : "";
    return field ? `${field}: ${d.msg ?? "invalid"}` : d.msg ?? `Request failed (${status})`;
  }
  if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  return `Request failed (${status})`;
}

export type CallResult<T> = { ok: true; body: T } | { ok: false; status: number; message: string; detail?: unknown };

export async function refCall<T>(method: string, path: string, body?: unknown): Promise<CallResult<T>> {
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
    const detail = (json as { detail?: unknown } | null)?.detail;
    return { ok: false, status: res.status, message: errorMessage(detail, res.status), detail };
  }
  return { ok: true, body: json as T };
}
