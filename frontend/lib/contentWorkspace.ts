// frontend/lib/contentWorkspace.ts
// AA-1148 — the content workspace (AA Catalogue › Content workspace): hand-made tours tied to an AA supplier of
// Booking, and the report of old tours whose supplier is only the sheet's text. Plain data and pure helpers (no
// React), shared by the pages under app/admin/content-workspace/ and lib/contentWorkspace.test.mjs.
//
// API (CIS BFF /api/admin → api/routers/admin_content_workspace.py): options, suppliers, tours (list, create, read,
// save facts), supplier-links (report, link, apply suggestions). The wording of a hand-made tour is edited with the
// tour draft routes of AA-1146 (app/admin/_components/TourDraftEditor.tsx).

export const CW_API = "/api/admin/content-workspace";
export const CW_PAGE = "/admin/content-workspace";

export type Meal = "breakfast" | "lunch" | "dinner";
export const MEALS: readonly Meal[] = ["breakfast", "lunch", "dinner"];
/** A tour type code of the setting cis.content.manual_tour.tour_types (admin data, not a code list). */
export type TourType = string;
export interface TourTypeOption { code: string; label: string; label_vi: string }

export interface DayForm {
  title: string;
  places: string[];
  meals: Meal[];
  overnight: string;
  description: string;
}

export interface FactsForm {
  name: string;
  country: string;            // ISO 3166-1 alpha-2, "" when not chosen
  supplier_id: string;        // "" when not chosen
  summary: string;
  nights: string;             // a text box: "" = not said yet
  days: DayForm[];
  inclusions: string[];
  exclusions: string[];
  group_min: string;
  group_max: string;
  tour_type: TourType;        // "" when not chosen
  guide_languages: string[];
  best_time_to_go: string;
}

export interface Limits { max_days: number; min_inclusions: number; min_exclusions: number; bulk_max: number }
export interface Supplier {
  id: string; code: string | null; company_name: string | null; categories: string[];
  country_code: string | null; status: string;
}
export interface Options {
  countries: { iso2: string; name: string }[];
  limits: Limits;
  // Business option lists an administrator edits in Platform > Settings (CIS 478).
  tour_types: TourTypeOption[];
  quick_inclusions: string[];
  quick_exclusions: string[];
  suppliers_available: boolean;
  two_person_publish: boolean;
}
export interface CheckItem {
  key: string; ok: boolean; required: boolean; message: string; params: Record<string, unknown>;
}
export type TourState = "draft" | "live" | "live_changes" | "paused" | "retired";
export interface StoredFacts {
  name?: string; summary?: string; nights?: number | null; days?: Partial<DayForm>[]; inclusions?: string[];
  exclusions?: string[]; group_min?: number | null; group_max?: number | null; tour_type?: TourType | null;
  tour_type_label?: string | null;
  guide_languages?: string[]; best_time_to_go?: string; country?: string | null; supplier_id?: string | null;
  saved_at?: string;
}
export interface TourView {
  tour_id: string;
  entry_origin: "sheet" | "manual";
  state: TourState;
  editable: boolean;
  facts_two_person: boolean;  // published + two-person publishing on: facts are read-only here
  reason_required: boolean;
  facts: StoredFacts;
  country_name: string | null;
  provider: string | null;
  supplier: Supplier | null;
  checklist: CheckItem[];
  completeness: number;
  has_draft: boolean;
  published_at: string | null;
  locked: boolean;
  from_price: { amount: string; currency: string; basis: string; pax: number; season: string | null } | null;
  limits: Limits;
}
export interface TourRow {
  tour_id: string; name: string; country: string | null; supplier_id: string | null; supplier_name: string | null;
  state: TourState; days: number; created_at: string | null;
}
export interface UnlinkedRow {
  tour_id: string; name: string; provider: string | null; country: string | null; country_iso2: string | null;
  live: boolean; entry_origin: "sheet" | "manual";
  suggestion: { id: string; company_name: string; code: string | null } | null;
}

export const emptyDay = (): DayForm => ({ title: "", places: [], meals: [], overnight: "", description: "" });

export function emptyForm(): FactsForm {
  return {
    name: "", country: "", supplier_id: "", summary: "", nights: "", days: [emptyDay()], inclusions: [],
    exclusions: [], group_min: "", group_max: "", tour_type: "", guide_languages: [], best_time_to_go: "",
  };
}

const num = (v: number | null | undefined): string => (v === null || v === undefined ? "" : String(v));
const str = (v: string | null | undefined): string => v ?? "";
const list = <T,>(v: readonly T[] | null | undefined): T[] => [...(v ?? [])];

function dayOf(d: Partial<DayForm>): DayForm {
  return {
    title: str(d.title), places: list(d.places), meals: list(d.meals), overnight: str(d.overnight),
    description: str(d.description),
  };
}

/** The form of a stored tour (GET tours/{id} facts). */
export function formOf(f: StoredFacts): FactsForm {
  return {
    name: str(f.name), country: str(f.country), supplier_id: str(f.supplier_id), summary: str(f.summary),
    nights: num(f.nights), days: list(f.days).map(dayOf), inclusions: list(f.inclusions),
    exclusions: list(f.exclusions), group_min: num(f.group_min), group_max: num(f.group_max),
    tour_type: f.tour_type ?? "", guide_languages: list(f.guide_languages), best_time_to_go: str(f.best_time_to_go),
  };
}

const intOrNull = (s: string): number | null => {
  const t = s.trim();
  if (!/^\d+$/.test(t)) return null;
  return Number(t);
};

/** The API body of a form (POST tours, PUT tours/{id}/facts). */
export function bodyOf(form: FactsForm): Record<string, unknown> {
  return {
    name: form.name, country: form.country || null, supplier_id: form.supplier_id || null, summary: form.summary,
    nights: intOrNull(form.nights), days: form.days, inclusions: form.inclusions, exclusions: form.exclusions,
    group_min: intOrNull(form.group_min), group_max: intOrNull(form.group_max), tour_type: form.tour_type || null,
    guide_languages: form.guide_languages, best_time_to_go: form.best_time_to_go,
  };
}

/** The tour type choices of the form: the options in force, plus the type the tour already holds when an
 * administrator has removed it since (shown with the label it was saved with), so the form never loses it. */
export function tourTypeChoices(options: TourTypeOption[], current: string, currentLabel: string | null | undefined,
  lang: string): { value: string; label: string }[] {
  const out = options.map((o) => ({ value: o.code, label: lang === "vi" ? o.label_vi || o.label : o.label }));
  if (current && !options.some((o) => o.code === current)) out.push({ value: current, label: currentLabel || current });
  return out;
}

/** The label a tour's type was saved with (shown when an administrator has removed the type since). */
export const savedTypeLabel = (view: TourView | null): string | null => view?.facts.tour_type_label ?? null;

export type NoticeKey = "readOnlySheet" | "readOnlyRole" | "liveNote" | "factsTwoPerson";

/** The plain notes above the editor, in order: a sheet tour is read-only here, the role only reads, a published tour's
 * facts go live on save (with a reason), or (two-person publishing on) a published tour's facts are read-only. */
export function noticeKeys(view: TourView | null, writer: boolean): NoticeKey[] {
  const out: NoticeKey[] = [];
  if (view?.entry_origin === "sheet") out.push("readOnlySheet");
  if (!writer) return [...out, "readOnlyRole"];
  if (view?.facts_two_person) out.push("factsTwoPerson");
  else if (view?.reason_required && view.editable) out.push("liveNote");
  return out;
}

/** A stable text of the form, to tell whether it changed since the last save. */
export const formKey = (form: FactsForm): string => JSON.stringify(bodyOf(form));

/** Split a typed line into list items ("Water, guide; tickets" → three), trimmed, empty ones dropped. */
export function splitItems(text: string): string[] {
  return text.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);
}

/** Add items to a list once each (case-blind). */
export function addItems(list: string[], items: string[]): string[] {
  const seen = new Set(list.map((x) => x.toLowerCase()));
  const out = [...list];
  for (const it of items) {
    if (!seen.has(it.toLowerCase())) {
      seen.add(it.toLowerCase());
      out.push(it);
    }
  }
  return out;
}

/** Move day `from` to `to` (both 0-based, clamped). */
export function moveDay(days: DayForm[], from: number, to: number): DayForm[] {
  const t = Math.max(0, Math.min(days.length - 1, to));
  if (from === t || from < 0 || from >= days.length) return days;
  const out = [...days];
  const [d] = out.splice(from, 1);
  out.splice(t, 0, d);
  return out;
}

/** A copy of day `i` right after it. */
export function duplicateDay(days: DayForm[], i: number): DayForm[] {
  const d = days[i];
  if (!d) return days;
  const copy: DayForm = { ...d, places: [...d.places], meals: [...d.meals] };
  return [...days.slice(0, i + 1), copy, ...days.slice(i + 1)];
}

/** "3 days, 2 nights" hint in the form: the usual nights for the days typed. */
export const usualNights = (days: number): number => Math.max(days - 1, 0);

export function stateTone(state: TourState): "neutral" | "success" | "warning" | "danger" | "info" {
  switch (state) {
    case "live": return "success";
    case "live_changes": return "warning";
    case "paused": return "warning";
    case "retired": return "danger";
    default: return "info";
  }
}

/** The page of one tour (the editor). */
export const tourHref = (tourId: string): string => `${CW_PAGE}/${encodeURIComponent(tourId)}`;

/** The unlinked report's API URL with its filters. */
export function unlinkedUrl(p: { country?: string; q?: string; onlySuggested?: boolean; limit: number; offset: number }): string {
  const s = new URLSearchParams();
  if (p.country) s.set("country", p.country);
  if (p.q?.trim()) s.set("q", p.q.trim());
  if (p.onlySuggested) s.set("only_suggested", "true");
  s.set("limit", String(p.limit));
  s.set("offset", String(p.offset));
  return `${CW_API}/supplier-links?${s.toString()}`;
}

/** The required items that are not done, in list order. */
export const missingItems = (items: CheckItem[]): CheckItem[] => items.filter((i) => i.required && !i.ok);
