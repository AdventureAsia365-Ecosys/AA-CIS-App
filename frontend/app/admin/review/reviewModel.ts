// app/admin/review/reviewModel.ts
// AA-662 PR 2 — field model, gate-failure severity, and row mapping for the Review Queue.
// Extracted verbatim from the original page.tsx (AA-234/AA-240/AA-626/AA-717) so the migrated
// page keeps identical behaviour; only the data/table layer changed.

import { A } from "../_components/adminUi";
import { formatDate } from "../../_kit";

// ── Field model ───────────────────────────────────────────────────────────────
// Mirrors backend _ALLOWED_GC_FIELDS (11). og_tags is read-only in v1 (data empty).
export const TEXT_FIELDS = ["aa_name", "aa_subtitle", "seo_title"] as const; // single-line
export const AREA_FIELDS = ["aa_summary", "aa_description", "mobile_card_text", "seo_meta"] as const; // textarea
export const LIST_FIELDS = ["aa_highlights", "seo_keywords_used"] as const; // newline = item

export const FIELD_LABEL: Record<string, string> = {
  aa_name: "Name",
  aa_subtitle: "Subtitle",
  aa_summary: "Summary",
  aa_description: "Description",
  aa_highlights: "Highlights",
  aa_itineraries: "Itinerary",
  mobile_card_text: "Mobile card text",
  seo_title: "SEO title",
  seo_meta: "SEO meta",
  seo_keywords_used: "SEO keywords",
  og_tags: "OG tags",
};

export const SEO_META_MIN = 140;
export const SEO_META_MAX = 155;

// AA-626 — classify a gate failure code by nature.
const PRODUCT_TRUTH_CODES = new Set([
  "FACT_CHECK_MANUAL_CHECK",
  "UNSUPPORTED_NUMBER",
  "MISSING_FIELD",
  "ITINERARY_DAY_COUNT_MISMATCH",
  "ITINERARY_MEAL_TIME_INVENTED",
  "ITINERARY_STILL_COMPRESSED",
  "FABRICATED",
  "NOVEL_NUMERIC_CLAIM",
]);
const BRAND_STYLE_CODES = new Set([
  "FORBIDDEN_WORD",
  "META_TOO_SHORT",
  "SEO_META_TOO_LONG",
  "META_INCOMPLETE_SENTENCE",
  "HIGHLIGHTS_TOO_GENERIC",
  "HIGHLIGHTS_OPTIONAL_LANGUAGE",
  "DFS_INTENT_UNDERUSED",
  "BRAND_MANUAL_CHECK",
  "BRAND_FLAGGED",
]);

export type Severity = "red" | "amber" | "gray";

export function codeSeverity(code: string): Severity {
  const c = (code || "").toUpperCase();
  if (PRODUCT_TRUTH_CODES.has(c)) return "red";
  if (BRAND_STYLE_CODES.has(c)) return "amber";
  return "gray";
}

export const SEV_STYLE: Record<Severity, { bg: string; color: string; border: string }> = {
  red: { bg: A.redSoft, color: A.red, border: A.redBorder },
  amber: { bg: A.amberSoft, color: "var(--aa-amber-deep)", border: "var(--aa-amber-border)" },
  gray: { bg: "var(--aa-neutral-bg)", color: "var(--aa-neutral-fg)", border: "var(--aa-neutral-border)" },
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function asList(v: any): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === "string") {
    try {
      const p = JSON.parse(v);
      return Array.isArray(p) ? p.map(String) : [];
    } catch {
      return v.trim() ? [v] : [];
    }
  }
  return [];
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function toDraft(r: any): Record<string, string> {
  return {
    aa_name: r.aa_name || "",
    aa_subtitle: r.aa_subtitle || "",
    aa_summary: r.aa_summary || "",
    aa_description: r.aa_description || "",
    aa_highlights: asList(r.aa_highlights).join("\n"),
    aa_itineraries: r.aa_itineraries || "",
    mobile_card_text: r.mobile_card_text || "",
    seo_title: r.seo_title || "",
    seo_meta: r.seo_meta || "",
    seo_keywords_used: asList(r.seo_keywords_used).join("\n"),
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function draftToPatchValue(field: string, raw: string): any {
  if ((LIST_FIELDS as readonly string[]).includes(field)) {
    return raw.split("\n").map((s) => s.trim()).filter(Boolean);
  }
  return raw;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function failureMap(failures: any[]): Record<string, { code: string; reason: string }[]> {
  const m: Record<string, { code: string; reason: string }[]> = {};
  for (const f of failures || []) {
    if (!f?.field) continue;
    (m[f.field] ||= []).push({ code: f.code, reason: f.reason });
  }
  return m;
}

// AA-717 — fallback: parse overall verdict codes from failure_summary when no per-field failure.
function codesFromSummary(summary: string): string[] {
  const s = summary || "";
  const codes: string[] = [];
  const m = s.match(/codes=([A-Z0-9_,]+)/i);
  if (m) codes.push(...m[1].split(",").map((c) => c.trim()).filter(Boolean));
  if (/low_quality/i.test(s)) codes.push("LOW_QUALITY");
  if (/brand_audit\s*=\s*manual_check/i.test(s)) codes.push("BRAND_MANUAL_CHECK");
  if (/brand_audit\s*=\s*flagged/i.test(s)) codes.push("BRAND_FLAGGED");
  return [...new Set(codes)];
}

export type ReviewItem = {
  id: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  raw: any;
  name: string;
  country: string;
  score: number;
  date: string;
  created_at_ms: number;
  human_edited: boolean;
  edited_at: string | null;
  reviewed_by: string | null;
  revalidate_passed: boolean | null;
  failure_summary: string;
  version_num: number | null;
  brand_audit_status: string | null;
  codes: string[];
  // AA-739: why the row is not on Master (server-computed). The headline, ahead of the score.
  block: ReviewBlock | null;
  // AA-728: can Regenerate (a same-harness retry) fix this row, and what to do if not.
  failure_class: FailureClass | null;
  retryable: boolean;
  hint: string;
  // Client-only: set while a non-blocking regenerate job is in flight for this row (AA-719).
  regenerating?: boolean;
};

export type ReviewBlock = { kind: "needs_human" | "hard" | "low_quality" | "other"; label: string };

// AA-728 — the per-row actionability class the server computes alongside `block`.
export type FailureClass =
  | "raw_insufficient"
  | "needs_human"
  | "writer_tone"
  | "transient"
  | "hard"
  | "low_quality"
  | "other";

export const BLOCK_STYLE: Record<ReviewBlock["kind"], { bg: string; color: string; border: string }> = {
  needs_human: SEV_STYLE.red,
  hard: SEV_STYLE.red,
  low_quality: SEV_STYLE.amber,
  other: SEV_STYLE.gray,
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function mapRow(r: any): ReviewItem {
  const fieldCodes = [
    ...new Set(((r.failures || []) as any[]).map((f) => f?.code).filter(Boolean)), // eslint-disable-line @typescript-eslint/no-explicit-any
  ] as string[];
  const codes = fieldCodes.length ? fieldCodes : codesFromSummary(r.failure_summary || "");
  return {
    id: String(r.id),
    raw: r,
    name: r.aa_name || r.src_name || "Untitled tour",
    country: r.country || "Unknown",
    score: typeof r.score_overall === "number" ? r.score_overall : parseFloat(r.score_overall || "0"),
    date: r.created_at ? formatDate(r.created_at) : "",
    created_at_ms: r.created_at ? new Date(r.created_at).getTime() : 0,
    human_edited: !!r.human_edited,
    edited_at: r.edited_at || null,
    reviewed_by: r.reviewed_by || null,
    revalidate_passed:
      r.revalidate_passed === true ? true : r.revalidate_passed === false ? false : null,
    failure_summary: r.failure_summary || "",
    version_num: typeof r.version_num === "number" ? r.version_num : null,
    brand_audit_status: r.brand_audit_status || null,
    codes,
    block: r.block && r.block.kind ? (r.block as ReviewBlock) : null,
    // AA-728: default to retryable when the server omits the field (older payloads), so a missing
    // classifier never hides the Regenerate button on a row that used to show it.
    failure_class: (r.failure_class as FailureClass) || null,
    retryable: r.retryable !== false,
    hint: typeof r.hint === "string" ? r.hint : "",
  };
}
