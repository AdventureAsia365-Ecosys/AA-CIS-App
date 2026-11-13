// S279 thresholds lane L2 — the quality values the admin pages show (pass line, score colour bands, SEO bands), read
// from the backend at runtime (GET /admin/quality/thresholds, the platform settings registry). They used to be a
// generated copy (quality-registry.generated.ts SCORE_BANDS / SEO_BANDS / PASS_LINE); a copy cannot follow an approved
// change, so the pages read the values in force and show neutral colours until they load (no fallback numbers here:
// a literal would be a mirror of a setting).

export type ScoreBands = { top: number; pass: number; low: number };
export type SeoBands = { meta: { min: number; max: number }; title: { min: number; max: number }; slug: { max: number } };
export type QualityThresholds = {
  pass_line: number;
  score_bands: ScoreBands;
  seo_bands: SeoBands;
  writing_policy_sha?: string;
  settings_fp?: string;
};

export const QUALITY_THRESHOLDS_PATH = "/api/admin/quality/thresholds";

/** Where a score sits: "top" (>= top band), "pass" (>= pass line), "fail" (under it); null without a score or bands. */
export type ScoreLevel = "top" | "pass" | "fail";
export function scoreLevel(s: number | null | undefined, bands: ScoreBands | null | undefined): ScoreLevel | null {
  if (s == null || !bands) return null;
  if (s >= bands.top) return "top";
  if (s >= bands.pass) return "pass";
  return "fail";
}

/** The kit tone of a score: success / warning / danger; undefined while it cannot be told. */
export function scoreTone(
  s: number | null | undefined,
  bands: ScoreBands | null | undefined,
): "success" | "warning" | "danger" | undefined {
  const level = scoreLevel(s, bands);
  return level === "top" ? "success" : level === "pass" ? "warning" : level === "fail" ? "danger" : undefined;
}

/** The Review Queue's colour of a score: pass line and up / the critical-low boundary and up / under it. */
export type ReviewLevel = "ok" | "low" | "critical";
export function reviewLevel(s: number | null | undefined, bands: ScoreBands | null | undefined): ReviewLevel | null {
  if (s == null || !bands) return null;
  if (s >= bands.pass) return "ok";
  if (s >= bands.low) return "low";
  return "critical";
}

/** Is a meta description of `n` characters inside the meta band? null while the band is not loaded. */
export function inMetaBand(n: number, seo: SeoBands | null | undefined): boolean | null {
  if (!seo) return null;
  return n >= seo.meta.min && n <= seo.meta.max;
}

/** The Review Queue score filter labels' numbers ("—" until the thresholds load). */
export function scoreFilterNumbers(
  bands: ScoreBands | null | undefined,
  passLine: number | null | undefined,
): { low: string; belowPass: string; pass: string } {
  return {
    low: bands ? bands.low.toFixed(1) : "—",
    belowPass: passLine != null ? (passLine - 0.1).toFixed(1) : "—",
    pass: passLine != null ? passLine.toFixed(1) : "—",
  };
}

/** Is an SEO field of `n` characters over its band maximum? false while the band is not loaded. */
export function overSeoMax(n: number, seo: SeoBands | null | undefined, field: "title" | "meta"): boolean {
  return !!seo && n > seo[field].max;
}

/** The band maximum of an SEO field as text ("—" until it loads). */
export function seoMaxLabel(seo: SeoBands | null | undefined, field: "title" | "meta"): string {
  return seo ? String(seo[field].max) : "—";
}
