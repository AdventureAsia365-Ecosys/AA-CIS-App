// frontend/lib/supplyContent.ts
// AA-835 — types, labels and pure helpers of CIS Content › Assets › Supply content: the review of the customer
// content CIS writes for AA-Booking hotels and services (root ADR 0004a). Booking reads the published text back
// through catalog.v_supply_content, whose `edit_url` is `/admin/supply-content/<entity id>` (CIS 278) — the detail
// page of this module, also Booking's "Edit in CIS" link.
//
// The shapes mirror the admin API exactly (api/routers/admin_supply_content.py → services/supply_content/review.py).
// Errors: the API answers `{detail: {error, message, …extra}}` for its own rules (404 not_found, 409 inactive /
// outdated / photo_rights / not_a_draft / not_published, 422 invalid / unknown_media) and FastAPI's
// `{detail: [{loc, msg}]}` for a body it refuses; `supplyCall` turns both into one readable message.
//
// Type-level TS only (no enums, no parameter properties), so lib/supplyContent.test.mjs runs it with node --test
// --experimental-strip-types.

export const SUPPLY_API = "/api/admin/supply-content";

// ── vocabulary (admin_supply_content.STATUSES, CIS 278 CHECKs) ──────────────────────────────────────────────────

export type ContentStatus = "draft" | "in_review" | "published" | "stale" | "unpublished";
export type EntityType = "hotel" | "service";
export type VersionStatus = "draft" | "approved" | "rejected" | "superseded";
export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export const CONTENT_STATUSES: readonly ContentStatus[] = ["draft", "in_review", "published", "stale", "unpublished"];
export const ENTITY_TYPES: readonly EntityType[] = ["hotel", "service"];

export const STATUS_LABEL: Record<ContentStatus, string> = {
  draft: "No content yet",
  in_review: "Draft to review",
  published: "Published",
  stale: "Stale (facts changed)",
  unpublished: "Unpublished",
};

/** What each content status means for Booking (the page explains it next to the badge). */
export const STATUS_HINT: Record<ContentStatus, string> = {
  draft: "Booking shows no CIS text. A draft is written when the facts arrive (or press Regenerate).",
  in_review: "A draft waits for review. Booking shows no CIS text until one is published.",
  published: "Booking shows the published version.",
  stale: "The facts changed in Booking after the last publish. Booking shows no CIS text until a new version is published.",
  unpublished: "Hidden from Booking. The approved version is kept and can be published again.",
};

export const STATUS_TONE: Record<ContentStatus, Tone> = {
  draft: "neutral", in_review: "warning", published: "success", stale: "danger", unpublished: "neutral",
};

export const TYPE_LABEL: Record<EntityType, string> = { hotel: "Hotel", service: "Service" };

export const VERSION_TONE: Record<VersionStatus, Tone> = {
  draft: "warning", approved: "success", rejected: "danger", superseded: "neutral",
};

// writer.py / review.py limits (the backend stays the authority and answers 422)
export const MAX_DESCRIPTION_CHARS = 2000;
export const MAX_HIGHLIGHTS = 5;
export const MAX_GOOD_TO_KNOW = 3;
export const MAX_ITEM_CHARS = 160;

// ── API shapes ────────────────────────────────────────────────────────────────────────────────────────────────────

export interface ListRow {
  id: string; source_id: string; entity_type: EntityType; service_type: string | null; title: string | null;
  country_code: string | null; applied_supply_version: number; active: boolean; content_status: ContentStatus;
  updated_at: string | null; open_drafts: number;
}

export interface ListPage { items: ListRow[]; total: number; by_status: Partial<Record<ContentStatus, number>> }

export interface FeedStatus {
  consumer: string; cursor: number; last_pull_at?: string | null; last_success_at?: string | null;
  last_error?: string | null; last_error_at?: string | null; feed_available: boolean; feed_error?: string;
  head?: number | null; pending?: number; oldest_pending_at?: string | null;
  draft_jobs_7d: Record<string, number>; photo_purge_jobs_7d?: Record<string, number>; llm_cost_today_usd: number;
}

export interface VersionPhoto { media_id: string; caption: string | null; alt: string | null }

export interface Version {
  id: string; content_version: number; supply_version: number; origin: "llm" | "human"; description: string;
  highlights: string[]; good_to_know: string[]; photos: VersionPhoto[]; status: VersionStatus;
  based_on_version_id: string | null; llm_job_id: string | null; created_by: string | null; created_at: string | null;
  reviewed_by: string | null; reviewed_at: string | null; review_note: string | null;
}

export interface Media {
  media_id: string; usage_rights: boolean; active: boolean; content_type: string | null; width: number | null;
  height: number | null; url_600: string | null; url_1600: string | null; copied_at: string | null;
}

export interface Entity {
  id: string; source_system: string; source_id: string; entity_type: EntityType; service_type: string | null;
  title: string | null; country_code: string | null; facts: Record<string, unknown>; applied_supply_version: number;
  active: boolean; content_status: ContentStatus; published_version_id: string | null; created_at: string | null;
  updated_at: string | null;
}

export interface Detail {
  entity: Entity; versions: Version[]; current_draft: Version | null; published: Version | null;
  diff_base_version: number | null; diff: string[]; media: Media[]; edit_in_booking_path: string;
}

// ── helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────

export interface ListFilters { status: ContentStatus | ""; type: EntityType | "" }

/** One server page of the list (filters and paging run on the server, lesson S218). */
export function listUrl(f: ListFilters, pageIndex: number, pageSize: number): string {
  const q = new URLSearchParams();
  if (f.status) q.set("status", f.status);
  if (f.type) q.set("entity_type", f.type);
  q.set("limit", String(pageSize));
  q.set("offset", String(Math.max(0, pageIndex) * pageSize));
  return `${SUPPLY_API}?${q.toString()}`;
}

/** The detail page of an entity (the path catalog.v_supply_content.edit_url names). */
export function detailHref(entityId: string): string {
  return `/admin/supply-content/${encodeURIComponent(entityId)}`;
}

export type PhotoState = "ready" | "not_copied" | "no_rights" | "removed" | "unknown";

export const PHOTO_STATE: Record<PhotoState, { label: string; tone: Tone; hint: string }> = {
  ready: { label: "Cleared and copied", tone: "success", hint: "Usage rights in Booking and copied to the CIS photo store: it can be published." },
  not_copied: { label: "Not copied yet", tone: "warning", hint: "Usage rights are on, but the copy into the CIS photo store has not finished (or failed: see the draft job)." },
  no_rights: { label: "No usage rights", tone: "danger", hint: "Booking says AA may not use this photo. A version that shows it cannot be published." },
  removed: { label: "Removed in Booking", tone: "neutral", hint: "The photo is no longer in Booking's facts. A version that shows it cannot be published." },
  unknown: { label: "Unknown photo", tone: "danger", hint: "This photo is not one of the entity's photos any more." },
};

/** The rights state of one photo, the same rule publish checks (review._photo_problems). */
export function photoState(m: Media | undefined | null): PhotoState {
  if (!m) return "unknown";
  if (!m.active) return "removed";
  if (!m.usage_rights) return "no_rights";
  if (!m.url_1600) return "not_copied";
  return "ready";
}

/** Why publishing `v` would be refused now (empty = it can be published). Mirrors review.publish. */
export function publishBlockers(entity: Entity, v: Version, media: Media[]): string[] {
  const out: string[] = [];
  const republish = v.status === "approved" && v.id === entity.published_version_id
    && (entity.content_status === "unpublished" || entity.content_status === "stale");
  if (v.status !== "draft" && !republish) out.push(`This version is ${v.status}.`);
  if (!entity.active) out.push("The entity is inactive in Booking.");
  if (v.supply_version !== entity.applied_supply_version) {
    out.push(`Written for facts v${v.supply_version}; Booking is at v${entity.applied_supply_version}. Review the newer draft or write an edit.`);
  }
  const byId = new Map(media.map((m) => [m.media_id, m]));
  const bad = v.photos.filter((p) => photoState(byId.get(p.media_id)) !== "ready").length;
  if (bad) out.push(`${bad} photo${bad === 1 ? " is" : "s are"} not cleared for use (rights or copy).`);
  return out;
}

/** Can this version be published again after an unpublish / stale (the same approved one)? */
export function isRepublish(entity: Entity, v: Version): boolean {
  return v.status === "approved" && v.id === entity.published_version_id
    && (entity.content_status === "unpublished" || entity.content_status === "stale");
}

/** The Booking staff admin link of the entity (path from the API, origin from lib/apps.ts). */
export function bookingHref(bookingOrigin: string, path: string): string {
  return `${bookingOrigin.replace(/\/+$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
}

/** A facts value for display (nested objects as compact JSON, long text cut). */
export function factText(v: unknown, max = 300): string {
  let s: string;
  if (v === null || v === undefined || v === "") s = "—";
  else if (Array.isArray(v)) s = v.every((x) => typeof x !== "object" || x === null) ? v.join(", ") : JSON.stringify(v);
  else if (typeof v === "object") s = JSON.stringify(v);
  else s = String(v);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** The facts keys worth showing first (the rest follow in key order). Media are shown in the Photos section. */
const FACT_FIRST = ["name", "title", "code", "city", "country_code", "destination", "star_rating", "category", "address"];
export function factRows(facts: Record<string, unknown>): [string, unknown][] {
  const keys = Object.keys(facts || {}).filter((k) => k !== "media" && k !== "photos");
  keys.sort((a, b) => {
    const ia = FACT_FIRST.indexOf(a), ib = FACT_FIRST.indexOf(b);
    if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    return a.localeCompare(b);
  });
  return keys.map((k) => [k, facts[k]]);
}

export interface EditForm { description: string; highlights: string; goodToKnow: string; photoIds: string[] }

/** The editor's starting values: a copy of `base` (the open draft, else the published, else empty). */
export function formFrom(base: Version | null): EditForm {
  return {
    description: base?.description ?? "",
    highlights: (base?.highlights ?? []).join("\n"),
    goodToKnow: (base?.good_to_know ?? []).join("\n"),
    photoIds: (base?.photos ?? []).map((p) => p.media_id),
  };
}

const lines = (s: string) => s.split("\n").map((x) => x.split(/\s+/).filter(Boolean).join(" ")).filter(Boolean);

/** The problems the API would answer 422 for (shown before Save; the backend re-checks). */
export function formProblems(f: EditForm): string[] {
  const out: string[] = [];
  const d = f.description.trim();
  if (!d) out.push("The description is empty.");
  if (d.length > MAX_DESCRIPTION_CHARS) out.push(`The description is over ${MAX_DESCRIPTION_CHARS} characters.`);
  const h = lines(f.highlights), g = lines(f.goodToKnow);
  if (h.length > MAX_HIGHLIGHTS) out.push(`At most ${MAX_HIGHLIGHTS} highlights (one per line).`);
  if (g.length > MAX_GOOD_TO_KNOW) out.push(`At most ${MAX_GOOD_TO_KNOW} "good to know" lines.`);
  if ([...h, ...g].some((x) => x.length > MAX_ITEM_CHARS)) out.push(`A highlight or "good to know" line is over ${MAX_ITEM_CHARS} characters.`);
  return out;
}

/** The POST /versions body: photos keep the caption / alt they had in `base`. */
export function versionBody(f: EditForm, base: Version | null) {
  const prev = new Map((base?.photos ?? []).map((p) => [p.media_id, p]));
  return {
    description: f.description.trim(),
    highlights: lines(f.highlights),
    good_to_know: lines(f.goodToKnow),
    photos: f.photoIds.map((id) => ({ media_id: id, caption: prev.get(id)?.caption ?? null, alt: prev.get(id)?.alt ?? null })),
    based_on_version_id: base?.id ?? null,
  };
}

/** A readable message from an API error body (`{detail: {error, message, …}}`, a FastAPI 422 list, or text). */
export function supplyErrorText(status: number, body: unknown): { code: string | null; message: string } {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { error?: unknown; message?: unknown; media_ids?: unknown };
    const code = typeof d.error === "string" ? d.error : null;
    let message = typeof d.message === "string" && d.message ? d.message : `Request failed (${status})`;
    if (code === "photo_rights" && Array.isArray(d.media_ids) && d.media_ids.length) {
      message += ` (${d.media_ids.length} photo${d.media_ids.length === 1 ? "" : "s"})`;
    }
    return { code, message };
  }
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown[]; msg?: unknown };
    const where = Array.isArray(first.loc) ? first.loc.filter((x) => x !== "body").join(".") : "";
    return { code: "invalid", message: `${where ? `${where}: ` : ""}${String(first.msg ?? "invalid value")}` };
  }
  if (typeof detail === "string" && detail) return { code: null, message: detail };
  if (status === 403) return { code: "forbidden", message: "Your role cannot do this." };
  return { code: null, message: `Request failed (${status})` };
}

export type CallResult<T> = { ok: true; body: T } | { ok: false; status: number; code: string | null; message: string };

/** fetch through the BFF (`/api/admin/supply-content…`), never throwing on an API refusal. */
export async function supplyCall<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<CallResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${SUPPLY_API}${path}`, {
      method,
      headers: { ...headers, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, status: 0, code: "network", message: "Network error — try again." };
  }
  let parsed: unknown = null;
  try { parsed = await res.json(); } catch { /* empty / non-JSON body */ }
  if (!res.ok) return { ok: false, status: res.status, ...supplyErrorText(res.status, parsed) };
  return { ok: true, body: parsed as T };
}
