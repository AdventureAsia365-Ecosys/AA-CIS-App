// frontend/lib/tenantBudgetInForce.ts — S280 (AA-1096) review round: the budget the gateway applies to a tenant row of
// AI providers › Budgets (the thresholds, stop, use and state in force), apart from lib/aiGateway.ts (size ratchet).
// Pure.

import type { TenantBudgetRow, TenantBudgetState } from "./aiGateway.ts";

/** What the gateway applies to a Budgets-tab tenant row: its effective budget (S280: a Subscription override wins
 *  over the stored row, the plan applies without one), else (an older API, before the resolver) the row itself. The
 *  thresholds fall back to the row's when the effective block leaves them empty. Pure. */
export function tenantBudgetInForce(r: TenantBudgetRow): {
  monthly_usd: number | null; alert_pct: number; approval_pct: number; hard_stop: boolean; used_pct: number | null;
  state: TenantBudgetState;
} {
  const e = r.effective;
  if (!e) {
    return { monthly_usd: r.monthly_usd, alert_pct: r.alert_pct, approval_pct: r.approval_pct, hard_stop: r.hard_stop,
      used_pct: r.used_pct, state: r.state };
  }
  return { monthly_usd: e.monthly_usd, alert_pct: e.alert_pct ?? r.alert_pct, approval_pct: e.approval_pct ?? r.approval_pct,
    hard_stop: e.hard_stop ?? r.hard_stop, used_pct: e.used_pct, state: e.state };
}
