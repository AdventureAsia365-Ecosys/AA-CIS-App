// frontend/lib/spendRules.ts — AA-1126 (S283 review fix): what an app-wide AI cap does when it is reached, as the API
// says it (api/services/spend_rules_view.py cap_rule): "stops_ai" under the dev spend rules (the setting
// <app>.spend.environment = dev, the default), "alert_only" under prod. The App profiles card and the TripPlanner caps
// block show it next to each cap, so an operator never reads a hard stop that the code no longer applies.

export type CapRule = "alert_only" | "stops_ai";

/** The rule from an API value; null when the API did not say (an older backend, the rules unknown). */
export function capRuleOf(value: unknown): CapRule | null {
  return value === "alert_only" || value === "stops_ai" ? value : null;
}

/** The kit Badge tone of a rule: a hard stop is a warning, an alert-only cap is informational. */
export function capRuleTone(rule: CapRule): "warning" | "info" {
  return rule === "stops_ai" ? "warning" : "info";
}

/** The rule of an App profiles row (GET /admin/ai-gateway/app-profiles `budget_rule`, AA-1126; TripPlanner and the
 *  CRM only). Read off the row so the AppProfile type stays as it is (an older backend sends no such field). */
export function budgetRuleOf(row: object): CapRule | null {
  return capRuleOf((row as { budget_rule?: unknown }).budget_rule);
}
