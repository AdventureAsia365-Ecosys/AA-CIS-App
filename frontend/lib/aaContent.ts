// frontend/lib/aaContent.ts
// AA-977 (CRM-B5, slice S3) — types, labels and pure helpers for CIS Content › Assets › AA content (CRM), the editor
// of AA-authored content (prep packs, starter templates, campaign blocks) that the partner CRM reads over
// catalog.http.v1 `/catalog/v1/aa-content` once published.
//
// The shapes mirror the admin API exactly (api/routers/admin_aa_content.py → services/aa_content/store.py). One item =
// one locale; the EN and VI copies of a piece share `group_key`. An item has numbered versions: at most one draft
// (always the newest) and published versions that never change (CIS 275 trigger). The field rules mirror
// services/aa_content/validate.py so the editor can warn before a save; the backend stays the authority and answers
// 422 `{detail, code, field}` on any rule it refuses.
//
// Type-level TS only (no enums, no parameter properties), so lib/aaContent.test.mjs runs it with node --test
// --experimental-strip-types.
//
// AA-994: labels and sentences here are English; the Vietnamese twins are in lib/aaContent.messages.ts and the page
// passes them (`AA_WORDS_MESSAGES[lang]`, the UI language for country names) as the optional last argument.

export const AA_CONTENT_API = "/api/admin/aa-content";

// ── vocabulary (validate.py KINDS / LOCALES, store.py STATUSES) ─────────────────────────────────────────────────

export type AaKind = "prep_pack" | "starter_template" | "campaign_block";
export type AaLocale = "en" | "vi";
export type AaState = "draft" | "published" | "retired";

export const KINDS: readonly AaKind[] = ["prep_pack", "starter_template", "campaign_block"];
export const LOCALES: readonly AaLocale[] = ["en", "vi"];
export const STATUSES: readonly AaState[] = ["draft", "published", "retired"];

export const KIND_LABEL: Record<AaKind, string> = {
  prep_pack: "Before you go pack",
  starter_template: "Starter template",
  campaign_block: "Campaign block",
};

export const LOCALE_LABEL: Record<AaLocale, string> = { en: "English", vi: "Vietnamese" };

/** The list's status filter, as the backend reads it (store.py _LIST_FROM). */
export const STATUS_FILTER_LABEL: Record<AaState, string> = {
  draft: "Has a draft",
  published: "Published",
  retired: "Retired",
};

// The editor's country quick picks (upper case, as applies_to stores them). The backend accepts any ISO 3166-1 code
// (services/aa_content/validate.py COUNTRY_CODES, AA-990); tests/unit/test_aa977_editor_mirror.py keeps every code
// here an ISO code with its shared.countries name.
export const AA_COUNTRIES: Record<string, string> = {
  BT: "Bhutan", KH: "Cambodia", CN: "China", IN: "India", ID: "Indonesia", JP: "Japan", KG: "Kyrgyzstan",
  LA: "Laos", MY: "Malaysia", MN: "Mongolia", MM: "Myanmar", NP: "Nepal", PK: "Pakistan", PH: "Philippines",
  KR: "South Korea", LK: "Sri Lanka", TW: "Taiwan", TH: "Thailand", UZ: "Uzbekistan", VN: "Vietnam",
};

/** A country code's display name: the quick-pick name, else the browser's English region name, else the code.
 *  Another UI language ("vi") names every code with the browser's region names in that language. */
export function countryLabel(code: string, lang: string = "en"): string {
  if (lang === "en" && AA_COUNTRIES[code]) return AA_COUNTRIES[code];
  try {
    return new Intl.DisplayNames([lang], { type: "region" }).of(code) ?? AA_COUNTRIES[code] ?? code;
  } catch {
    return AA_COUNTRIES[code] ?? code;
  }
}

/** Country codes sorted by country name, for the pickers: the quick picks plus any other ISO code the item already
 *  holds (the API accepts every ISO 3166-1 code, AA-990), so staff can see and remove it. */
export function countryOptions(selected: readonly string[] = [], lang: string = "en"): { code: string; name: string }[] {
  const extra = selected.filter((code, i) => !AA_COUNTRIES[code] && selected.indexOf(code) === i);
  return [
    ...Object.keys(AA_COUNTRIES).map((code) => ({ code, name: countryLabel(code, lang) })),
    ...extra.map((code) => ({ code, name: countryLabel(code, lang) })),
  ].sort((a, b) => a.name.localeCompare(b.name, lang === "en" ? undefined : lang));
}

/** The words the helpers below return (English); the Vietnamese twin is lib/aaContent.messages.ts. */
export const AA_WORDS = {
  retired: "Retired",
  publishedV: (v: string) => `Published v${v}`,
  draft: "Draft",
  draftWaiting: (v: number) => `Draft v${v} waiting`,
  titleRequired: "A title is required.",
  titleLong: (max: number) => `The title has more than ${max} characters.`,
  textLong: (max: number) => `The text has more than ${max} characters.`,
  tourIdLong: (max: number) => `A tour id has more than ${max} characters.`,
  network: "Network error: nothing was sent. Try again.",
  stale: "Someone else saved this item since you opened it. Reload to see their changes, then edit again.",
  roleAdminOnly: "Your role cannot make this change (publish, retire and restore are admin only).",
  roleCannot: "Your role cannot make this change.",
  unavailable: "AA content storage is not available yet. Try again in a moment.",
  refusedShape: "The request was refused: a field is missing or has the wrong shape.",
  requestFailed: (status: number) => `Request failed (${status}).`,
};
export type AaWords = typeof AA_WORDS;

export const TITLE_MAX = 300;
export const BODY_MAX = 20000;
export const SOURCE_ID_MAX = 128;
export const GROUP_KEY_RE = /^[a-z0-9][a-z0-9-]{1,79}$/;

// ── responses ───────────────────────────────────────────────────────────────────────────────────────────────────

export interface AaTourKey {
  source_system: "cis";
  source_id: string;
}

export interface AaAppliesTo {
  country_codes?: string[];
  tours?: AaTourKey[];
}

export interface AaVersion {
  version: number;
  status: "draft" | "published";
  title: string;
  body_markdown: string;
  applies_to: AaAppliesTo;
  content_hash: string | null;
  published_at: string | null;
  published_by: string | null;
  created_at: string | null;
  created_by: string | null;
  updated_at: string | null;
}

export interface AaItem {
  content_id: string;
  kind: AaKind;
  locale: AaLocale;
  group_key: string;
  state: AaState;
  published_version: number | null;
  draft_version: number | null;
  retired_at: string | null;
  retired_by: string | null;
  created_at: string | null;
  created_by: string | null;
  /** The concurrency token: every write sends it back as expected_updated_at (stale → 409). */
  updated_at: string;
  /** Newest first. */
  versions: AaVersion[];
}

export interface AaListRow {
  content_id: string;
  kind: AaKind;
  locale: AaLocale;
  group_key: string;
  title: string | null;
  state: AaState;
  published_version: number | null;
  draft_version: number | null;
  country_codes: string[];
  updated_at: string;
}

export interface AaListPage {
  items: AaListRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface AaGroup {
  group_key: string;
  items: AaItem[];
}

export interface AaPublishResult {
  item: AaItem;
  published: Record<string, unknown>;
}

// ── list query ──────────────────────────────────────────────────────────────────────────────────────────────────

export interface AaListFilters {
  kind: AaKind | "";
  locale: AaLocale | "";
  status: AaState | "";
  country: string;
}

export const EMPTY_FILTERS: AaListFilters = { kind: "", locale: "", status: "", country: "" };

/** The admin list URL for one server page (filters and paging run on the server; lesson S218). */
export function listUrl(filters: AaListFilters, pageIndex: number, pageSize: number): string {
  const q = new URLSearchParams();
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.locale) q.set("locale", filters.locale);
  if (filters.status) q.set("status", filters.status);
  if (filters.country) q.set("country", filters.country);
  q.set("limit", String(pageSize));
  q.set("offset", String(Math.max(0, pageIndex) * pageSize));
  return `${AA_CONTENT_API}?${q.toString()}`;
}

// ── state labels ────────────────────────────────────────────────────────────────────────────────────────────────

export type BadgeTone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "purple";

/** The status badge of a row or an item: Draft / Published vN / Retired. */
export function stateBadge(
  row: Pick<AaListRow, "state" | "published_version">, w: AaWords = AA_WORDS,
): { label: string; tone: BadgeTone } {
  if (row.state === "retired") return { label: w.retired, tone: "neutral" };
  if (row.state === "published") return { label: w.publishedV(String(row.published_version ?? "?")), tone: "success" };
  return { label: w.draft, tone: "warning" };
}

/** "Draft v3 waiting" next to a published item that has a newer draft; null otherwise. */
export function pendingDraftLabel(
  row: Pick<AaListRow, "state" | "draft_version" | "published_version">, w: AaWords = AA_WORDS,
): string | null {
  if (row.draft_version == null || row.published_version == null) return null;
  return w.draftWaiting(row.draft_version);
}

// ── editor baseline ─────────────────────────────────────────────────────────────────────────────────────────────

export interface AaForm {
  title: string;
  body_markdown: string;
  country_codes: string[];
  /** One CIS tour source_id per line. */
  tours_text: string;
}

/** The version the editor opens with: the draft when there is one, else the newest published version (saving it
 * starts draft n+1 with what is sent; store.save_draft). */
export function baselineVersion(item: AaItem): AaVersion | null {
  return item.versions.find((v) => v.status === "draft") ?? item.versions[0] ?? null;
}

export function toursText(applies: AaAppliesTo | null | undefined): string {
  return (applies?.tours ?? []).map((t) => t.source_id).join("\n");
}

export function formFromVersion(v: AaVersion | null): AaForm {
  return {
    title: v?.title ?? "",
    body_markdown: v?.body_markdown ?? "",
    country_codes: [...(v?.applies_to?.country_codes ?? [])],
    tours_text: toursText(v?.applies_to),
  };
}

/** One tour key per non-empty line, trimmed, duplicates dropped (validate.applies_to does the same). */
export function parseTours(text: string): AaTourKey[] {
  const out: AaTourKey[] = [];
  for (const line of text.split(/\r?\n/)) {
    const id = line.trim();
    if (id && !out.some((t) => t.source_id === id)) out.push({ source_system: "cis", source_id: id });
  }
  return out;
}

/** applies_to as the draft PUT sends it: only the keys that hold something (the backend stores {} for none). */
export function appliesFromForm(form: Pick<AaForm, "country_codes" | "tours_text">): AaAppliesTo {
  const out: AaAppliesTo = {};
  const codes = form.country_codes.filter((c, i, all) => all.indexOf(c) === i);
  if (codes.length) out.country_codes = codes;
  const tours = parseTours(form.tours_text);
  if (tours.length) out.tours = tours;
  return out;
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** Has the editor changed anything since `base`? (tour lines compared after parsing, so whitespace is no change) */
export function isDirty(form: AaForm, base: AaForm): boolean {
  return form.title !== base.title
    || form.body_markdown !== base.body_markdown
    || !sameList(form.country_codes, base.country_codes)
    || !sameList(parseTours(form.tours_text).map((t) => t.source_id), parseTours(base.tours_text).map((t) => t.source_id));
}

/** Field problems the editor can show before a save (validate.py title / body / tours). Empty = nothing to say. */
export function formProblems(
  form: AaForm, w: AaWords = AA_WORDS,
): Partial<Record<"title" | "body_markdown" | "applies_to.tours", string>> {
  const out: Partial<Record<"title" | "body_markdown" | "applies_to.tours", string>> = {};
  const title = form.title.trim();
  if (!title) out.title = w.titleRequired;
  else if (title.length > TITLE_MAX) out.title = w.titleLong(TITLE_MAX);
  if (form.body_markdown.length > BODY_MAX) out.body_markdown = w.textLong(BODY_MAX);
  const long = parseTours(form.tours_text).find((t) => t.source_id.length > SOURCE_ID_MAX);
  if (long) out["applies_to.tours"] = w.tourIdLong(SOURCE_ID_MAX);
  return out;
}

// ── the prep-pack visa rule (validate.py visa_sections_without_link, mirrored as guidance) ──────────────────────────

// Python's \b is Unicode-aware; JS \b is ASCII-only, so the word edges are spelled with \p{L}\p{N}_ lookarounds.
const W = "[\\p{L}\\p{N}_]";
const VISA_RE = new RegExp(`(?<!${W})e-?visas?(?!${W})|(?<!${W})visas?(?!${W})|thị\\s+thực`, "iu");
const VISA_CARD_RE = new RegExp(
  `(?<!${W})visa\\s+(?:card|cards|debit|credit|or\\s+mastercard)(?!${W})|(?<!${W})visa\\s*/\\s*mastercard(?!${W})`,
  "giu",
);
const HTTPS_LINK_RE = /https:\/\/[^\s)\]>]+/i;
const BLOCK_SPLIT_RE = /\n\s*\n/;

/** Blank-line separated blocks; a block of headings only joins the block after it ('## Visa' + its paragraph). */
export function sections(text: string): string[] {
  const out: string[] = [];
  let pending = "";
  for (const block of text.split(BLOCK_SPLIT_RE)) {
    if (!block.trim()) continue;
    const lines = block.split(/\r?\n/).filter((l) => l.trim());
    if (lines.every((l) => l.trimStart().startsWith("#"))) {
      pending = pending ? `${pending}\n${block}` : block;
      continue;
    }
    out.push(pending ? `${pending}\n${block}` : block);
    pending = "";
  }
  if (pending) out.push(pending);
  return out;
}

/** The sections that mention a visa without an https link (first 80 characters of each). A publish of a prep pack
 * with any of them is refused with 422 visa_needs_official_link. */
export function visaSectionsWithoutLink(text: string): string[] {
  const nfc = text.normalize("NFC");
  return sections(nfc)
    .filter((s) => VISA_RE.test(s.replace(VISA_CARD_RE, " ")) && !HTTPS_LINK_RE.test(s))
    .map((s) => s.trim().slice(0, 80));
}

// ── group keys ──────────────────────────────────────────────────────────────────────────────────────────────────

export function isGroupKey(value: string): boolean {
  return GROUP_KEY_RE.test(value);
}

/** A group_key suggestion from a title: lower case ASCII, '-' between words, at most 80 characters. */
export function suggestGroupKey(title: string): string {
  const slug = title
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/đ/gi, "d")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80)
    .replace(/-+$/g, "");
  return slug;
}

// ── calls ───────────────────────────────────────────────────────────────────────────────────────────────────────

export type AaResult<T> =
  | { ok: true; status: number; body: T }
  | { ok: false; status: number; message: string; code: string | null; field: string | null };

/** A readable message for a refused call. The backend's body is {detail, code[, field]} (admin_aa_content.py). */
export function aaErrorText(status: number, detail: unknown, code: unknown, w: AaWords = AA_WORDS): string {
  if (status === 0) return w.network;
  if (code === "stale") return w.stale;
  if (status === 403) {
    if (typeof detail === "string" && detail.startsWith("Role '")) return w.roleAdminOnly;
    return typeof detail === "string" && detail ? detail : w.roleCannot;
  }
  if (status === 503) return w.unavailable;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) return w.refusedShape;
  return w.requestFailed(status);
}

/** Call the admin API through the BFF and never throw: the editor shows the refusal next to the right field. */
export async function aaCall<T>(
  method: "GET" | "POST" | "PUT" | "DELETE", path: string, body?: unknown, w: AaWords = AA_WORDS,
): Promise<AaResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${AA_CONTENT_API}${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, status: 0, message: aaErrorText(0, null, null, w), code: null, field: null };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { ok: true, status: res.status, body: data as T };
  return {
    ok: false,
    status: res.status,
    message: aaErrorText(res.status, data.detail, data.code, w),
    code: typeof data.code === "string" ? data.code : null,
    field: typeof data.field === "string" ? data.field : null,
  };
}
