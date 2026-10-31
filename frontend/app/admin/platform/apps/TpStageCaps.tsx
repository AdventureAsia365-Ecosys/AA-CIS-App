"use client";
// app/admin/platform/apps/TpStageCaps.tsx — AA-1015 (Done-when 3): TripPlanner's AI spend caps, per stage.
//
// TripPlanner (B2C) stops its customer-facing AI for the rest of the UTC day at a daily cap: the combined cap over its
// online stages (TP_DAILY_SPEND_CAP_USD, 5 USD) and, when set, a cap per stage (narration / search). It warns at
// 80 % ("near cap"). CIS reads today's spend from the shared LLM log and mirrors the caps (api/core/app_registry.py
// TP_STAGE_CAPS_USD; no stage cap is set today, so each stage row says the combined cap applies). The figures come
// from GET /api/admin/platform/apps (the TripPlanner card's spend_today, api/services/platform_apps.tp_spend_today).
//
//   TpStageCaps            the near-cap state and one row per stage (Apps board card, the section below);
//   TripPlannerCapsSection the same with the combined figure, as a section of Cost Operations › AI & data spend.
// Kit only (Section, KpiTile, DataTable, Badge).

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Badge, DataTable, ErrorState, K, KpiTile, Section, apiGet, mono, useT, type ColumnDef, type Tone,
} from "../../../_kit";
import type { AppsResp, TpSpendToday, TpStageCap, TripPlannerCard } from "@/lib/platform";
import { TP_CAPS } from "./tpCaps.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

const STATE: Record<TpStageCap["level"], { tone: Tone; label: "stateOk" | "stateWarn" | "stateCap" }> = {
  ok: { tone: "success", label: "stateOk" },
  warn: { tone: "warning", label: "stateWarn" },
  cap_reached: { tone: "danger", label: "stateCap" },
};

function usd(v: number | null | undefined): string {
  return v == null ? "—" : `$${v.toFixed(v >= 1 || v === 0 ? 2 : 4)}`;
}

/** The rows to show: the API's per-stage caps, or (a payload cached before they existed) the stages with no cap. */
export function stageCapRows(s: TpSpendToday): TpStageCap[] {
  if (s.stage_caps?.length) return s.stage_caps;
  const spent = new Map(s.by_stage.map((b) => [b.stage, b]));
  return s.stages.map((stage) => ({
    stage, calls: spent.get(stage)?.calls ?? 0, cost_usd: spent.get(stage)?.cost_usd ?? 0, cap_usd: null,
    pct_of_cap: null, near_cap: false, level: "ok" as const,
  }));
}

export function TpStageCaps({ spend, testId = "tp-stage-caps" }: { spend: TpSpendToday; testId?: string }) {
  const t = useT(TP_CAPS);
  const rows = useMemo(() => stageCapRows(spend), [spend]);
  const pct = spend.near_cap_pct ?? 80;
  const columns = useMemo<ColumnDef<TpStageCap, unknown>[]>(() => [
    { id: "stage", header: t("colStage"), accessorKey: "stage",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.stage}</span> },
    { id: "spent", header: t("colSpent"), accessorKey: "cost_usd", meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{usd(row.original.cost_usd)}</span> },
    { id: "calls", header: t("colCalls"), accessorKey: "calls", meta: { align: "right" } },
    { id: "cap", header: t("colCap"), accessorFn: (r) => r.cap_usd ?? -1,
      cell: ({ row }) => row.original.cap_usd == null
        ? <span style={small}>{t("noStageCap")}</span>
        : <span style={{ fontFamily: mono }}>{usd(row.original.cap_usd)}</span> },
    { id: "used", header: t("colUsed"), accessorFn: (r) => r.pct_of_cap ?? -1,
      cell: ({ row: { original: r } }) => r.cap_usd == null ? <span style={small}>—</span> : (
        <Badge tone={STATE[r.level]?.tone ?? "neutral"} dot={false}>
          {r.pct_of_cap == null ? "—" : `${r.pct_of_cap} %`} · {t(STATE[r.level]?.label ?? "stateOk")}
        </Badge>
      ) },
  ], [t]);
  return (
    <div data-testid={testId} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 8 }}>
      <span data-testid={`${testId}-near`} style={{ display: "inline-flex" }}>
        <Badge tone={spend.near_cap ? "warning" : "success"} dot={false}>
          {spend.near_cap ? t("nearCap", pct) : t("notNear")}
        </Badge>
      </span>
      <DataTable<TpStageCap> tableId={`${testId}-table`} data={rows} columns={columns} getRowId={(r) => r.stage}
        searchable={false} paginate={false} compact tableMinWidth={440} />
      <div style={small}>{spend.stage_caps_set ? t("mirroredSet", pct) : t("mirrored", pct)}</div>
    </div>
  );
}

export function TripPlannerCapsSection() {
  const t = useT(TP_CAPS);
  const q = useQuery({
    queryKey: ["platform-apps"],
    queryFn: () => apiGet<AppsResp>("/api/admin/platform/apps"),
    staleTime: 60_000,
  });
  const card = q.data?.apps.find((a): a is TripPlannerCard => a.id === "tripplanner");
  const s = card?.spend_today ?? null;
  return (
    <Section title={t("title")} description={t("description")} testId="cost-tp-caps"
      actions={<a href="/admin/platform/apps" style={{ fontSize: 12, color: K.accent }}>{t("open")}</a>}
      bodyStyle={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      {q.isError && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => void q.refetch()} />}
      {!q.isError && (q.isLoading || s) && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 10 }}>
          <KpiTile size="sm" label={t("combined")} value={q.isLoading ? undefined : usd(s?.cost_usd)}
            hint={s ? `${t("combinedOf", usd(s.cap_usd))} · ${s.pct_of_cap ?? 0} %` : undefined}
            hintTone={s?.near_cap ? "warning" : "neutral"} testId="cost-tp-caps-combined" />
        </div>
      )}
      {s && <TpStageCaps spend={s} testId="cost-tp-stage-caps" />}
      {!q.isLoading && !q.isError && card && !s && <div style={{ ...small, color: K.danger }}>{t("unreadable")}</div>}
    </Section>
  );
}
