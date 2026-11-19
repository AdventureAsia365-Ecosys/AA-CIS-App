// frontend/lib/ceFreshness.ts — AA-1127 (C2 "spend truth"): what the Spend page's "AWS figures fresh / stale" badge says,
// from the `ce_freshness` block of the two cost reads (api/services/cost_explorer_daily.py freshness). Pure, so
// lib/aa1127CeFreshness.test.mjs can check it; app/admin/spend/CeFreshness.tsx renders it with the kit Badge.

import type { CeFreshness } from "./platform";

export type CeFreshnessPart =
  | { key: "noData" }
  | { key: "age"; arg: string }
  | { key: "staleAfter"; arg: string }
  | { key: "dailyAt"; arg: string }
  | { key: "dailyOff" };

export interface CeFreshnessView {
  stale: boolean;
  tone: "warning" | "success";
  parts: CeFreshnessPart[];
}

/** Whole hours as "36", others with one decimal ("7.5"). */
export function hoursText(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

/** null when there is nothing to show: no block (an older API) or a failed read (`stale: null`). */
export function ceFreshnessView(f: CeFreshness | null | undefined): CeFreshnessView | null {
  if (!f || f.stale == null) return null;
  const parts: CeFreshnessPart[] = [
    f.age_hours == null ? { key: "noData" } : { key: "age", arg: hoursText(f.age_hours) },
  ];
  if (f.stale_after_hours != null) parts.push({ key: "staleAfter", arg: hoursText(f.stale_after_hours) });
  if (f.daily_refresh) {
    parts.push(f.daily_refresh.enabled
      ? { key: "dailyAt", arg: String(f.daily_refresh.hour_utc).padStart(2, "0") }
      : { key: "dailyOff" });
  }
  return { stale: f.stale, tone: f.stale ? "warning" : "success", parts };
}
