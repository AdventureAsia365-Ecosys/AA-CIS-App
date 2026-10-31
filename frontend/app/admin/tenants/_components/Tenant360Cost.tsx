"use client";
// app/admin/tenants/_components/Tenant360Cost.tsx
// AA-766 — Tenant 360 Cost tab: month-to-date LLM spend per stage, $/piece (blog vs social),
// $/tour (T2 rewrite) and plan revenue vs LLM cost -> margin. GET /admin/tenants/{id}/cost, same
// cost definition as the Usage & Billing tab (AA-798: llm_call_log, Jev in, shadow out).
// AA-994: text in the UI language (tenant360.messages.ts); the API's cost definition stays as sent.
// AA-1016 UI (S262 C4): the tenant's monthly AI budget tile (Tenant360AiBudget: set / edit / remove, staff only).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { K, mono, Badge, DataTable, ErrorState, KpiTile, Spinner, apiGet, useT, type Translator } from "../../../_kit";
import { type TenantCost, pct, usd } from "./tenant360";
import { Tenant360AiBudget } from "./Tenant360AiBudget";
import { T360 } from "./tenant360.messages";

type T = Translator<typeof T360.en>;

type ChannelRow = TenantCost["by_channel"][number];
type StageRow = TenantCost["by_stage"][number] & { share: number | null };

const tabular: React.CSSProperties = { fontVariantNumeric: "tabular-nums" };
const KIND_TONE: Record<string, "accent" | "info" | "neutral"> = { blog: "accent", social: "info", other: "neutral" };

const channelColumns = (t: T): ColumnDef<ChannelRow, unknown>[] => [
  { accessorKey: "channel", header: t("colChannel"), cell: c => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{String(c.getValue())}</span> },
  { accessorKey: "kind", header: t("colKind"), cell: c => <Badge tone={KIND_TONE[String(c.getValue())] ?? "neutral"} dot={false}>{String(c.getValue())}</Badge> },
  { accessorKey: "pieces", header: t("colPieces"), cell: c => <span style={tabular}>{Number(c.getValue())}</span> },
  { accessorKey: "cost_usd", header: t("colCost"), cell: c => <span style={tabular}>{usd(c.getValue() as number, 4)}</span> },
  { accessorKey: "cost_per_piece_usd", header: t("colPerPiece"), cell: c => <strong style={tabular}>{usd(c.getValue() as number | null, 4)}</strong> },
];

const stageColumns = (t: T): ColumnDef<StageRow, unknown>[] => [
  { accessorKey: "stage", header: t("colStage"), cell: c => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{String(c.getValue())}</span> },
  { accessorKey: "calls", header: t("colCalls"), cell: c => <span style={tabular}>{Number(c.getValue()).toLocaleString()}</span> },
  { accessorKey: "cost_usd", header: t("colCost"), cell: c => <span style={tabular}>{usd(c.getValue() as number, 4)}</span> },
  { accessorKey: "share", header: t("colShare"), cell: c => <span style={{ ...tabular, color: K.muted }}>{pct(c.getValue() as number | null)}</span> },
];

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, margin: "18px 0 8px" }}>{children}</div>;
}

export function Tenant360Cost({ tenantId }: { tenantId: string }) {
  const t = useT(T360);
  const channelCols = useMemo(() => channelColumns(t), [t]);
  const stageCols = useMemo(() => stageColumns(t), [t]);
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "cost"],
    queryFn: () => apiGet<TenantCost>(`/api/admin/tenants/${tenantId}/cost`, { admin: true }),
  });
  const stageRows = useMemo<StageRow[]>(() => {
    const d = q.data;
    if (!d) return [];
    return d.by_stage.map(st => ({ ...st, share: d.total_cost_usd > 0 ? st.cost_usd / d.total_cost_usd : null }));
  }, [q.data]);
  if (q.isLoading) return <div style={{ fontSize: 12, color: K.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> {t("loadingCost")}</div>;
  if (q.isError || !q.data) return <ErrorState message={t("costLoadError", (q.error as Error)?.message ?? t("unknownError"))} onRetry={() => q.refetch()} />;
  const c = q.data;
  const mg = c.margin;
  const marginTone = mg.margin_usd == null ? "neutral" : mg.margin_usd < 0 ? "danger" : "success";
  const kind = (k: string) => c.by_kind.find(x => x.kind === k);
  const blog = kind("blog");
  const social = kind("social");

  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(150px, 100%), 1fr))", gap: 10 }}>
        <Tenant360AiBudget tenantId={tenantId} />
        <KpiTile size="sm" label={t("llmCostMtd", c.billing_month ?? t("thisMonth"))} value={usd(c.total_cost_usd)} />
        <KpiTile size="sm" label={t("planRevenue")} value={mg.plan_attached ? usd(mg.revenue_usd) : t("noPlan")}
          hint={mg.plan_attached ? t("planPlusOverage", usd(mg.plan_price_usd), usd(mg.overage_usd)) : undefined} />
        <KpiTile size="sm" label={t("llmMargin")} value={mg.margin_usd == null ? "—" : usd(mg.margin_usd)} valueTone={marginTone}
          hint={mg.margin_pct == null ? undefined : t("ofRevenue", mg.margin_pct.toFixed(1))} />
        <KpiTile size="sm" label={t("perTour")} value={usd(c.per_tour.cost_per_tour_usd, 4)}
          hint={t("overTours", usd(c.per_tour.t2_cost_usd), c.per_tour.tours_rewritten)} />
        <KpiTile size="sm" label={t("perBlog")} value={usd(blog?.cost_per_piece_usd, 4)}
          hint={blog ? t("pieces", blog.pieces) : t("noBlog")} />
        <KpiTile size="sm" label={t("perSocial")} value={usd(social?.cost_per_piece_usd, 4)}
          hint={social ? t("pieces", social.pieces) : t("noSocial")} />
      </div>

      <H>{t("perPieceByChannel")}</H>
      <DataTable<ChannelRow>
        tableId="tenant360-cost-channels" data={c.by_channel} columns={channelCols}
        getRowId={r => r.channel} searchable={false} pageSize={25} stickyHeader={false}
        emptyTitle={t("noPieceCalls")}
        emptyDescription={t("noPieceCallsBody")} />

      <H>{t("costByStage")}</H>
      <DataTable<StageRow>
        tableId="tenant360-cost-stages" columns={stageCols}
        data={stageRows}
        getRowId={r => r.stage} searchable={false} pageSize={25} stickyHeader={false} enableCsv
        csvFilename={`tenant-${tenantId}-cost-by-stage.csv`}
        emptyTitle={t("noSpend")} emptyDescription={t("noSpendBody")} />

      <div style={{ fontSize: 11, color: K.muted2, marginTop: 10, lineHeight: 1.5 }}>
        {c.definition} {t("costFootnote", usd(c.unattributed_to_piece_usd, 4))}
      </div>
    </div>
  );
}
