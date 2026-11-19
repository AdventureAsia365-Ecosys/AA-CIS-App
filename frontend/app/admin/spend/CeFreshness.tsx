"use client";
// app/admin/spend/CeFreshness.tsx — AA-1127 (C2 "spend truth", tech lead decision S285 E2): the freshness badge of the
// stored AWS Cost Explorer figures, shown by the forecast (ForecastPanel) and the By app tab (CostByApp).
//
// API: the `ce_freshness` block of GET /api/admin/cost-ops/forecast and GET /api/admin/platform/cost (backend
// api/services/cost_explorer_daily.py freshness): the older of the two newest fetches, its age, the stale window
// (setting cis.spend.ce_stale_after_hours) and the daily check (cis.spend.ce_daily_refresh_enabled / _hour_utc). A
// failed read (`stale: null`) or an older API without the block shows nothing.

import { ceFreshnessView } from "@/lib/ceFreshness";
import type { CeFreshness } from "@/lib/platform";
import { Badge, K, useT } from "../../_kit";
import { CE_FRESHNESS } from "./ceFreshness.messages";

export type { CeFreshness };

/** The badge plus one short line, or nothing when the API sent no usable block (lib/ceFreshness.ts). */
export function CeFreshnessBadge({ f }: { f?: CeFreshness | null }) {
  const t = useT(CE_FRESHNESS);
  const v = ceFreshnessView(f);
  if (!v) return null;
  const line = v.parts.map((p) => ("arg" in p ? t(p.key, p.arg) : t(p.key))).join(" · ");
  return (
    <span data-testid="ce-freshness" data-stale={v.stale ? "true" : "false"} title={t("title")}
          style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap", minWidth: 0 }}>
      <Badge tone={v.tone}>{v.stale ? t("stale") : t("fresh")}</Badge>
      <span style={{ fontSize: 12, color: v.stale ? K.amber : K.muted, overflowWrap: "anywhere" }}>{line}</span>
    </span>
  );
}
