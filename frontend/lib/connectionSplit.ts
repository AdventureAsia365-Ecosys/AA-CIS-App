// frontend/lib/connectionSplit.ts — AA-1113 (S284 tech lead redesign): AI providers › Budgets, "Connection budget
// split". Each LLM connection has one budget at its provider; CIS and the CRM each keep their OWN monthly cap on it
// (the CIS share = the connection's monthly budget, the CRM share = the CRM setting crm.ai.connection_monthly_cap_usd)
// and the shares together must stay under the provider budget (the CIS setting
// cis.spend.connection_provider_budget_usd). Backend: GET /admin/ai-gateway/connection-split
// (api/services/ai_gateway_split.py; the rule in api/services/llm_connection_split.py). Neither app reads the other's
// ledger: the CRM share comes through the CRM settings relay Platform › Settings already uses.

export const CONNECTION_SPLIT_API = "/api/admin/ai-gateway/connection-split";

export type SplitState = "ok" | "over_allocated" | "unbounded" | "crm_unknown" | "no_provider_budget";

export interface SplitRow {
  connection_key: string;
  label: string;
  kind: string;
  enabled: boolean;
  provider_budget_usd: number | null;
  cis_share_usd: number | null;
  /** null = no CRM cap on the connection, or (crm_known false) the CRM could not be asked. */
  crm_share_usd: number | null;
  crm_known: boolean;
  allocated_usd: number;
  headroom_usd: number | null;
  over_allocated: boolean;
  uncapped_apps: ("cis" | "crm")[];
  state: SplitState;
}

export interface SplitResp {
  rows: SplitRow[];
  /** Keys of the two settings that name no registry connection (a typo would otherwise go unseen). */
  unknown_keys: string[];
  over_allocated: string[];
  /** Why the CRM share is unknown (null = it was read). */
  crm_error: string | null;
  errors: { source: string; error: string }[];
  provider_budget_key: string;
  crm_cap_key: string;
}

export type SplitTone = "success" | "warning" | "danger" | "neutral";

/** Badge tone of a state: over-allocated is the only red one; a share missing or unknown is amber. */
export function splitTone(state: SplitState): SplitTone {
  switch (state) {
    case "over_allocated": return "danger";
    case "unbounded": case "crm_unknown": return "warning";
    case "ok": return "success";
    default: return "neutral";
  }
}

/** Rows that need a look first: over-allocated, then a missing / unknown share, then the rest, by key. */
export function splitOrder(rows: SplitRow[]): SplitRow[] {
  const rank: Record<SplitState, number> = { over_allocated: 0, crm_unknown: 1, unbounded: 1, ok: 2,
    no_provider_budget: 3 };
  return [...rows].sort((a, b) => rank[a.state] - rank[b.state] || a.connection_key.localeCompare(b.connection_key));
}
