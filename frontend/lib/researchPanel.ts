// frontend/lib/researchPanel.ts — AA-1122: the pure parts of the SEO Intelligence Research tab
// (app/admin/seo-intelligence/ResearchTab.tsx): money text, the month warning, the market rows and the run payload.

export interface MonthCap { cap_usd: number | null; spent_usd: number; warn_pct: number }
export interface MarketRow { market: string; stale: number; isNew: boolean }

/** "$0.123" under one dollar, "$4.20" above; "—" when unknown. */
export function usd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `$${n.toFixed(Math.abs(n) < 1 ? 3 : 2)}`;
}

/** The month spend reached the warning share of the cap (a 0 or missing cap never warns: nothing can be spent). */
export function monthWarn(m: MonthCap | null | undefined): boolean {
  if (!m || !m.cap_usd) return false;
  return m.spent_usd >= (m.cap_usd * m.warn_pct) / 100;
}

/** One row per targeted market: its stale places and whether it counts as a new market (bought by hand only). */
export function marketRows(stale: Record<string, number> | undefined, activation: string[] | undefined): MarketRow[] {
  const isNew = new Set(activation ?? []);
  return Object.entries(stale ?? {}).map(([market, n]) => ({ market, stale: n, isNew: isNew.has(market) }));
}

/** The body of a preview / run: upper-case market codes, no blanks; a positive cap or none (the server's default). */
export function runScope(markets: string, maxUsd: string): { markets: string[]; max_usd?: number } {
  const codes = markets.split(",").map((m) => m.trim().toUpperCase()).filter(Boolean);
  const cap = Number(maxUsd);
  return Number.isFinite(cap) && cap > 0 ? { markets: codes, max_usd: cap } : { markets: codes };
}
