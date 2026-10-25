// frontend/lib/writingReasons.ts
// AA-916 WM-07 — the reason chips an admin sets with one click when acting on writer output (app ADR 0010): a
// Content Quality sentence cut / rewrite / edit (T9 piece, T2 tour version) and an A1 review Reject / Dismiss /
// Regenerate. The codes are the CHECK set of shared.writing_signal.reason_code (CIS 277) and
// services/writing_memory/reason_codes.py REASON_CODES, in the same order; the API answers 422 on any other value.
//
// Preselection: a flagged sentence carries the API's `suggested_reason` (deterministic, from its first gate). An A1
// review row has failure codes instead (review_queue.failures / failure_summary); `reviewReason()` maps them here.
//
// Pure helpers with type-level TS only, so lib/writingReasons.test.mjs runs them with node --test
// --experimental-strip-types.

export type ReasonCode =
  | "fact_not_in_source" | "optional_as_included" | "wrong_place_or_time" | "voice_off_brand"
  | "ai_phrasing" | "format_channel" | "repair_residue" | "style_only";

export const REASON_CODES: readonly ReasonCode[] = [
  "fact_not_in_source", "optional_as_included", "wrong_place_or_time", "voice_off_brand",
  "ai_phrasing", "format_channel", "repair_residue", "style_only",
];

export const DEFAULT_REASON: ReasonCode = "style_only";

/** Short chip labels (English; the admin UI opens in English). */
export const REASON_LABEL: Record<ReasonCode, string> = {
  fact_not_in_source: "Fact not in source",
  optional_as_included: "Optional as included",
  wrong_place_or_time: "Wrong place / time",
  voice_off_brand: "Off-brand voice",
  ai_phrasing: "AI phrasing",
  format_channel: "Format / channel",
  repair_residue: "Repair leftover",
  style_only: "Style only",
};

/** The chip's tooltip: what the code means (same wording as services/writing_memory/mining.py _REASON_MEANING). */
export const REASON_HINT: Record<ReasonCode, string> = {
  fact_not_in_source: "a fact the source does not give",
  optional_as_included: "an optional item written as included",
  wrong_place_or_time: "a wrong place or time",
  voice_off_brand: "wording off the brand's voice",
  ai_phrasing: "generic AI phrasing",
  format_channel: "a format or length wrong for the channel",
  repair_residue: "leftovers of a repair step",
  style_only: "a style change",
};

export function isReasonCode(v: unknown): v is ReasonCode {
  return typeof v === "string" && (REASON_CODES as readonly string[]).includes(v);
}

/** The chip to preselect from the API's suggestion (`style_only` when it sent none or an unknown value). */
export function preselect(suggested: unknown): ReasonCode {
  return isReasonCode(suggested) ? suggested : DEFAULT_REASON;
}

// A1 review failure codes (frontend/app/admin/review/reviewModel.ts, services/content_generation) → chip. First
// matching code wins, in the row's order; anything unknown (LOW_QUALITY, DEGRADED_DETECTOR …) is style_only.
const A1_REASON: Record<string, ReasonCode> = {
  FACT_CHECK_MANUAL_CHECK: "fact_not_in_source",
  UNSUPPORTED_NUMBER: "fact_not_in_source",
  NOVEL_NUMERIC_CLAIM: "fact_not_in_source",
  FABRICATED: "fact_not_in_source",
  ITINERARY_MEAL_TIME_INVENTED: "fact_not_in_source",
  HIGHLIGHTS_OPTIONAL_LANGUAGE: "optional_as_included",
  ITINERARY_DAY_COUNT_MISMATCH: "wrong_place_or_time",
  DAY_SUBDAY_SPLIT: "wrong_place_or_time",
  DAY_OUTPUT_DUPLICATE: "wrong_place_or_time",
  DAY_OUTPUT_ORDER: "wrong_place_or_time",
  DAY_OUTPUT_GAP: "wrong_place_or_time",
  DAY_OUTPUT_EXTRA: "wrong_place_or_time",
  DAY_DEPARTURE_MIDTRIP: "wrong_place_or_time",
  DAY_COUNT_VS_DURATION: "wrong_place_or_time",
  FORBIDDEN_WORD: "voice_off_brand",
  BRAND_MANUAL_CHECK: "voice_off_brand",
  BRAND_FLAGGED: "voice_off_brand",
  HIGHLIGHTS_TOO_GENERIC: "ai_phrasing",
  META_TOO_SHORT: "format_channel",
  SEO_META_TOO_LONG: "format_channel",
  META_INCOMPLETE_SENTENCE: "format_channel",
  MISSING_FIELD: "format_channel",
  ITINERARY_STILL_COMPRESSED: "format_channel",
  DFS_INTENT_UNDERUSED: "format_channel",
};

/** The chip to preselect for an A1 review row from its failure codes. */
export function reviewReason(codes: readonly string[] | null | undefined): ReasonCode {
  for (const c of codes ?? []) {
    const hit = A1_REASON[(c || "").toUpperCase()];
    if (hit) return hit;
  }
  return DEFAULT_REASON;
}

/** The chip for several rows at once (bulk regenerate): the most common preselection, ties in REASON_CODES order. */
export function commonReason(codesPerRow: readonly (readonly string[])[]): ReasonCode {
  if (codesPerRow.length === 0) return DEFAULT_REASON;
  const n = new Map<ReasonCode, number>();
  for (const codes of codesPerRow) {
    const r = reviewReason(codes);
    n.set(r, (n.get(r) ?? 0) + 1);
  }
  let best: ReasonCode = DEFAULT_REASON;
  let top = -1;
  for (const r of REASON_CODES) {
    const k = n.get(r) ?? 0;
    if (k > top) { best = r; top = k; }
  }
  return best;
}

export type ExemplarPref = "use" | "never" | null;

/** The value a click on an example toggle sends: clicking the active one clears it. */
export function toggledPref(current: ExemplarPref | undefined, clicked: "use" | "never"): ExemplarPref {
  return current === clicked ? null : clicked;
}

/** The signal PATCH path of an admin edit, through the BFF (owner = the piece or tour version it belongs to). */
export function exemplarPath(owner: "pieces" | "tour-versions", ownerId: string, signalId: string): string {
  return `/api/admin/content-quality/${owner}/${encodeURIComponent(ownerId)}/signals/${encodeURIComponent(signalId)}`;
}
