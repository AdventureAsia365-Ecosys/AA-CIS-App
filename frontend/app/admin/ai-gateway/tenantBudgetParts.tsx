"use client";
// app/admin/ai-gateway/tenantBudgetParts.tsx — S280 (AA-1096) review round: the cells of the tenant tier of AI
// providers › Budgets that read the budget in force (lib/tenantBudgetInForce: an override's thresholds, stop and use
// when the override wins over the stored row). Kit only.

import { K, useT } from "../../_kit";
import { pct } from "@/lib/aiGateway";
import type { tenantBudgetInForce } from "@/lib/tenantBudgetInForce";
import { BUDGETS } from "./budgets.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

/** " · 42 % of the cap" after the call count (nothing without a cap). */
export function UsedOfCap({ used }: { used: number | null }) {
  const t = useT(BUDGETS);
  return used != null ? <>{` · ${t("ofCap", pct(used))}`}</> : null;
}

/** The alert / approval levels and whether jobs are held, as the gateway applies them ("—" without a budget). */
export function LevelsCell({ r }: { r: ReturnType<typeof tenantBudgetInForce> }) {
  const t = useT(BUDGETS);
  if (r.monthly_usd == null) return <span style={small}>—</span>;
  return (
    <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
      <span style={small}>{t("levels", r.alert_pct, r.approval_pct)}</span>
      <span style={small}>{r.hard_stop ? t("holdsJobs") : t("alertsOnly")}</span>
    </div>
  );
}
