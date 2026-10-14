"use client";
// app/admin/tenants/_components/Tenant360Markets.tsx
// AA-766 — Tenant 360 Markets tab: the tenant's SEO markets (Brand Identity ->
// tenant_seo_config.target_market) with segment_research_log coverage over the places in the
// tenant's tours, the planning markets (Settings tab), and this tenant's unmapped_market_requests
// (read-only — adding a market stays a code change, AA-629 Tier 3). GET /admin/tenants/{id}/markets.

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { A, mono } from "../../_components/adminUi";
import { Badge, DataTable, ErrorState, Spinner, apiGet, formatDate, formatDateTime } from "../../../_kit";
import { type TenantMarkets } from "./tenant360";

type CoverageRow = TenantMarkets["coverage"][number];
type UnmappedRow = TenantMarkets["unmapped_requests"][number];
const tabular: React.CSSProperties = { fontVariantNumeric: "tabular-nums" };
const intCell = (c: { getValue: () => unknown }) => <span style={tabular}>{Number(c.getValue())}</span>;
function dateCell(fmt: (s: string) => string) {
  return function DateCell(c: { getValue: () => unknown }) {
    const v = c.getValue() as string | null;
    return <span style={{ color: A.muted }}>{v ? fmt(v) : "—"}</span>;
  };
}

const COVERAGE_COLUMNS: ColumnDef<CoverageRow, unknown>[] = [
  { accessorKey: "market", header: "Market", cell: c => <strong style={{ fontFamily: mono }}>{String(c.getValue())}</strong> },
  { accessorKey: "places_in_scope", header: "Places in tours", cell: intCell },
  { accessorKey: "researched", header: "Researched", cell: intCell },
  { accessorKey: "fresh", header: "Fresh", cell: intCell },
  {
    accessorKey: "fresh_pct", header: "Fresh %",
    cell: c => {
      const v = c.getValue() as number | null;
      return <strong style={{ ...tabular, color: v == null ? A.muted : v < 50 ? A.amber : A.body }}>{v == null ? "—" : `${v.toFixed(1)}%`}</strong>;
    },
  },
  { accessorKey: "last_researched", header: "Last researched", cell: dateCell(formatDate) },
];

const UNMAPPED_COLUMNS: ColumnDef<UnmappedRow, unknown>[] = [
  { accessorKey: "country_code", header: "Country", cell: c => <strong style={{ fontFamily: mono }}>{String(c.getValue())}</strong> },
  { accessorKey: "first_seen_at", header: "First seen", cell: dateCell(formatDateTime) },
  { accessorKey: "requested_at", header: "Last requested", cell: dateCell(formatDateTime) },
  {
    accessorKey: "resolved_at", header: "Status",
    cell: c => {
      const v = c.getValue() as string | null;
      return v ? <Badge tone="success" dot={false}>resolved {formatDate(v)}</Badge> : <Badge tone="warning" dot={false}>waiting</Badge>;
    },
  },
];

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, margin: "18px 0 8px" }}>{children}</div>;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: `1px solid ${A.line2}`, flexWrap: "wrap", alignItems: "center" }}>
      <div style={{ width: 170, fontSize: 12, fontWeight: 600, color: A.muted, flexShrink: 0 }}>{label}</div>
      <div style={{ display: "flex", gap: 5, flexWrap: "wrap", alignItems: "center", fontSize: 12.5, color: A.body, minWidth: 0 }}>{children}</div>
    </div>
  );
}

export function Tenant360Markets({ tenantId }: { tenantId: string }) {
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "markets"],
    queryFn: () => apiGet<TenantMarkets>(`/api/admin/tenants/${tenantId}/markets`, { admin: true }),
  });
  if (q.isLoading) return <div style={{ fontSize: 12, color: A.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> Loading markets…</div>;
  if (q.isError || !q.data) return <ErrorState message={`Failed to load markets: ${(q.error as Error)?.message ?? "unknown error"}`} onRetry={() => q.refetch()} />;
  const m = q.data;
  const open = m.unmapped_requests.filter(r => !r.resolved_at);

  return (
    <div style={{ minWidth: 0 }}>
      <Row label="Declared (Brand Identity)">
        {m.seo.declared.length ? m.seo.declared.map(d => <Badge key={d} tone="neutral" dot={false}>{d}</Badge>)
          : <span style={{ color: A.muted }}>{m.seo.configured ? "nothing declared" : "no SEO config row"}</span>}
      </Row>
      <Row label="SEO research markets">
        {m.seo.markets.map(c => <Badge key={c} tone="success" dot={false}>{c}</Badge>)}
        {m.seo.defaulted_to_us && <span style={{ fontSize: 11.5, color: A.amber }}>US default — no supported market declared</span>}
      </Row>
      {m.seo.unsupported.length > 0 && (
        <Row label="Not supported yet">
          {m.seo.unsupported.map(c => <Badge key={c} tone="warning" dot={false}>{c}</Badge>)}
        </Row>
      )}
      <Row label="Planning markets">
        {m.planning_markets ? m.planning_markets.map(c => <Badge key={c} tone="info" dot={false}>{c}</Badge>)
          : <span style={{ color: A.muted }}>not configured (planner uses US) — set it in Settings</span>}
      </Row>

      <H>Research coverage (fresh = researched in the last {m.fresh_days} days)</H>
      <DataTable<CoverageRow>
        tableId="tenant360-market-coverage" data={m.coverage} columns={COVERAGE_COLUMNS}
        getRowId={r => r.market} searchable={false} pageSize={25} stickyHeader={false} />
      {m.coverage.every(r => r.places_in_scope === 0) && (
        <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 6 }}>
          No places in scope: the tenant has not adopted any tour with segmented atoms yet.
        </div>
      )}

      <H>Unmapped market requests ({open.length} open)</H>
      <DataTable<UnmappedRow>
        tableId="tenant360-unmapped-markets" data={m.unmapped_requests} columns={UNMAPPED_COLUMNS}
        getRowId={r => `${r.country_code}-${r.requested_at}`} searchable={false} pageSize={25} stickyHeader={false}
        emptyTitle="No unmapped market requests" emptyDescription="Every market this tenant declared is supported." />
    </div>
  );
}
