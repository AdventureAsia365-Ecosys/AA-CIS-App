"use client";
// app/admin/tenants/_components/Tenant360Cost.tsx
// AA-766 — Tenant 360 Cost tab: month-to-date LLM spend per stage, $/piece (blog vs social),
// $/tour (T2 rewrite) and plan revenue vs LLM cost -> margin. GET /admin/tenants/{id}/cost, same
// cost definition as the Usage & Billing tab (AA-798: llm_call_log, Jev in, shadow out).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { A, mono, serif } from "../../_components/adminUi";
import { Badge, DataTable, ErrorState, Spinner, apiGet } from "../../../_kit";
import { type TenantCost, pct, usd } from "./tenant360";

type ChannelRow = TenantCost["by_channel"][number];
type StageRow = TenantCost["by_stage"][number] & { share: number | null };

const tabular: React.CSSProperties = { fontVariantNumeric: "tabular-nums" };
const KIND_TONE: Record<string, "accent" | "info" | "neutral"> = { blog: "accent", social: "info", other: "neutral" };

const CHANNEL_COLUMNS: ColumnDef<ChannelRow, unknown>[] = [
  { accessorKey: "channel", header: "Channel", cell: c => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{String(c.getValue())}</span> },
  { accessorKey: "kind", header: "Kind", cell: c => <Badge tone={KIND_TONE[String(c.getValue())] ?? "neutral"} dot={false}>{String(c.getValue())}</Badge> },
  { accessorKey: "pieces", header: "Pieces", cell: c => <span style={tabular}>{Number(c.getValue())}</span> },
  { accessorKey: "cost_usd", header: "Cost", cell: c => <span style={tabular}>{usd(c.getValue() as number, 4)}</span> },
  { accessorKey: "cost_per_piece_usd", header: "$ / piece", cell: c => <strong style={tabular}>{usd(c.getValue() as number | null, 4)}</strong> },
];

const STAGE_COLUMNS: ColumnDef<StageRow, unknown>[] = [
  { accessorKey: "stage", header: "Stage", cell: c => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{String(c.getValue())}</span> },
  { accessorKey: "calls", header: "Calls", cell: c => <span style={tabular}>{Number(c.getValue()).toLocaleString()}</span> },
  { accessorKey: "cost_usd", header: "Cost", cell: c => <span style={tabular}>{usd(c.getValue() as number, 4)}</span> },
  { accessorKey: "share", header: "Share", cell: c => <span style={{ ...tabular, color: A.muted }}>{pct(c.getValue() as number | null)}</span> },
];

function Kpi({ label, value, hint, color }: { label: string; value: string; hint?: string; color?: string }) {
  return (
    <div style={{ padding: "10px 14px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, minWidth: 0 }}>
      <div style={{ fontSize: 10, color: A.muted2, marginBottom: 3 }}>{label}</div>
      <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: color ?? A.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {hint && <div style={{ fontSize: 10.5, color: A.muted2, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, margin: "18px 0 8px" }}>{children}</div>;
}

export function Tenant360Cost({ tenantId }: { tenantId: string }) {
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "cost"],
    queryFn: () => apiGet<TenantCost>(`/api/admin/tenants/${tenantId}/cost`, { admin: true }),
  });
  const stageRows = useMemo<StageRow[]>(() => {
    const d = q.data;
    if (!d) return [];
    return d.by_stage.map(st => ({ ...st, share: d.total_cost_usd > 0 ? st.cost_usd / d.total_cost_usd : null }));
  }, [q.data]);
  if (q.isLoading) return <div style={{ fontSize: 12, color: A.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> Loading cost…</div>;
  if (q.isError || !q.data) return <ErrorState message={`Failed to load cost: ${(q.error as Error)?.message ?? "unknown error"}`} onRetry={() => q.refetch()} />;
  const c = q.data;
  const mg = c.margin;
  const marginColor = mg.margin_usd == null ? A.muted : mg.margin_usd < 0 ? A.red : A.green;
  const kind = (k: string) => c.by_kind.find(x => x.kind === k);
  const blog = kind("blog");
  const social = kind("social");

  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        <Kpi label={`LLM cost (${c.billing_month ?? "this month"}, MTD)`} value={usd(c.total_cost_usd)} />
        <Kpi label="Plan revenue (MTD)" value={mg.plan_attached ? usd(mg.revenue_usd) : "No plan attached"}
          hint={mg.plan_attached ? `${usd(mg.plan_price_usd)} plan + ${usd(mg.overage_usd)} overage` : undefined} />
        <Kpi label="LLM margin" value={mg.margin_usd == null ? "—" : usd(mg.margin_usd)} color={marginColor}
          hint={mg.margin_pct == null ? undefined : `${mg.margin_pct.toFixed(1)}% of revenue`} />
        <Kpi label="$ / tour (T2 rewrite)" value={usd(c.per_tour.cost_per_tour_usd, 4)}
          hint={`${usd(c.per_tour.t2_cost_usd)} over ${c.per_tour.tours_rewritten} tour${c.per_tour.tours_rewritten === 1 ? "" : "s"}`} />
        <Kpi label="$ / blog piece" value={usd(blog?.cost_per_piece_usd, 4)}
          hint={blog ? `${blog.pieces} piece${blog.pieces === 1 ? "" : "s"}` : "no blog pieces"} />
        <Kpi label="$ / social piece" value={usd(social?.cost_per_piece_usd, 4)}
          hint={social ? `${social.pieces} piece${social.pieces === 1 ? "" : "s"}` : "no social pieces"} />
      </div>

      <H>$ per piece by channel</H>
      <DataTable<ChannelRow>
        tableId="tenant360-cost-channels" data={c.by_channel} columns={CHANNEL_COLUMNS}
        getRowId={r => r.channel} searchable={false} pageSize={25} stickyHeader={false}
        emptyTitle="No piece-linked LLM calls this month"
        emptyDescription="Nothing in llm_call_log is linked to a T9/T10 content piece since the 1st." />

      <H>Cost by stage</H>
      <DataTable<StageRow>
        tableId="tenant360-cost-stages" columns={STAGE_COLUMNS}
        data={stageRows}
        getRowId={r => r.stage} searchable={false} pageSize={25} stickyHeader={false} enableCsv
        csvFilename={`tenant-${tenantId}-cost-by-stage.csv`}
        emptyTitle="No LLM spend this month" emptyDescription="Nothing in llm_call_log for this tenant since the 1st." />

      <div style={{ fontSize: 11, color: A.muted2, marginTop: 10, lineHeight: 1.5 }}>
        {c.definition} {usd(c.unattributed_to_piece_usd, 4)} this month is not linked to a content piece
        (T2 rewrites, planning, angle generation). Revenue = the plan&apos;s full monthly price + overage, not prorated;
        margin is revenue minus LLM cost only.
      </div>
    </div>
  );
}
