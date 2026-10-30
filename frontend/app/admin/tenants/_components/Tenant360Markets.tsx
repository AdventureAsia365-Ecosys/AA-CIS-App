"use client";
// app/admin/tenants/_components/Tenant360Markets.tsx
// AA-766 — Tenant 360 Markets tab: the tenant's SEO markets (Brand Identity ->
// tenant_seo_config.target_market) with segment_research_log coverage over the places in the
// tenant's tours, the planning markets (Settings tab), and this tenant's unmapped_market_requests
// (read-only — adding a market stays a code change, AA-629 Tier 3). GET /admin/tenants/{id}/markets.
// AA-994: text in the UI language (tenant360.messages.ts); market codes stay as sent.

import { useMemo } from "react";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { K, mono, Badge, DataTable, ErrorState, Spinner, apiGet, useFormat, useT, type Translator } from "../../../_kit";
import { type TenantMarkets } from "./tenant360";
import { T360 } from "./tenant360.messages";

type T = Translator<typeof T360.en>;
type Fmt = (s: string) => string;

type CoverageRow = TenantMarkets["coverage"][number];
type UnmappedRow = TenantMarkets["unmapped_requests"][number];
const tabular: React.CSSProperties = { fontVariantNumeric: "tabular-nums" };
const intCell = (c: { getValue: () => unknown }) => <span style={tabular}>{Number(c.getValue())}</span>;
function dateCell(fmt: (s: string) => string) {
  return function DateCell(c: { getValue: () => unknown }) {
    const v = c.getValue() as string | null;
    return <span style={{ color: K.muted }}>{v ? fmt(v) : "—"}</span>;
  };
}

const coverageColumns = (t: T, formatDate: Fmt): ColumnDef<CoverageRow, unknown>[] => [
  { accessorKey: "market", header: t("colMarket"), cell: c => <strong style={{ fontFamily: mono }}>{String(c.getValue())}</strong> },
  { accessorKey: "places_in_scope", header: t("colPlacesInTours"), cell: intCell },
  { accessorKey: "researched", header: t("colResearched"), cell: intCell },
  { accessorKey: "fresh", header: t("colFresh"), cell: intCell },
  {
    accessorKey: "fresh_pct", header: t("colFreshPct"),
    cell: c => {
      const v = c.getValue() as number | null;
      return <strong style={{ ...tabular, color: v == null ? K.muted : v < 50 ? K.amber : K.body }}>{v == null ? "—" : `${v.toFixed(1)}%`}</strong>;
    },
  },
  { accessorKey: "last_researched", header: t("colLastResearched"), cell: dateCell(formatDate) },
];

const unmappedColumns = (t: T, formatDate: Fmt, formatDateTime: Fmt): ColumnDef<UnmappedRow, unknown>[] => [
  { accessorKey: "country_code", header: t("colCountry"), cell: c => <strong style={{ fontFamily: mono }}>{String(c.getValue())}</strong> },
  { accessorKey: "first_seen_at", header: t("colFirstSeen"), cell: dateCell(formatDateTime) },
  { accessorKey: "requested_at", header: t("colLastRequested"), cell: dateCell(formatDateTime) },
  {
    accessorKey: "resolved_at", header: t("colStatus"),
    cell: c => {
      const v = c.getValue() as string | null;
      return v ? <Badge tone="success" dot={false}>{t("resolved", formatDate(v))}</Badge> : <Badge tone="warning" dot={false}>{t("waiting")}</Badge>;
    },
  },
];

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, margin: "18px 0 8px" }}>{children}</div>;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: `1px solid ${K.line2}`, flexWrap: "wrap", alignItems: "center" }}>
      <div style={{ width: 170, fontSize: 12, fontWeight: 600, color: K.muted, flexShrink: 0 }}>{label}</div>
      <div style={{ display: "flex", gap: 5, flexWrap: "wrap", alignItems: "center", fontSize: 12.5, color: K.body, minWidth: 0 }}>{children}</div>
    </div>
  );
}

export function Tenant360Markets({ tenantId }: { tenantId: string }) {
  const t = useT(T360);
  const { formatDate, formatDateTime } = useFormat();
  const coverageCols = useMemo(() => coverageColumns(t, formatDate), [t, formatDate]);
  const unmappedCols = useMemo(() => unmappedColumns(t, formatDate, formatDateTime), [t, formatDate, formatDateTime]);
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "markets"],
    queryFn: () => apiGet<TenantMarkets>(`/api/admin/tenants/${tenantId}/markets`, { admin: true }),
  });
  if (q.isLoading) return <div style={{ fontSize: 12, color: K.muted, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> {t("loadingMarkets")}</div>;
  if (q.isError || !q.data) return <ErrorState message={t("marketsLoadError", (q.error as Error)?.message ?? t("unknownError"))} onRetry={() => q.refetch()} />;
  const m = q.data;
  const open = m.unmapped_requests.filter(r => !r.resolved_at);

  return (
    <div style={{ minWidth: 0 }}>
      <Row label={t("declared")}>
        {m.seo.declared.length ? m.seo.declared.map(d => <Badge key={d} tone="neutral" dot={false}>{d}</Badge>)
          : <span style={{ color: K.muted }}>{m.seo.configured ? t("nothingDeclared") : t("noSeoRow")}</span>}
      </Row>
      <Row label={t("seoMarkets")}>
        {m.seo.markets.map(c => <Badge key={c} tone="success" dot={false}>{c}</Badge>)}
        {m.seo.defaulted_to_us && <span style={{ fontSize: 11.5, color: K.amber }}>{t("usDefault")}</span>}
      </Row>
      {m.seo.unsupported.length > 0 && (
        <Row label={t("notSupported")}>
          {m.seo.unsupported.map(c => <Badge key={c} tone="warning" dot={false}>{c}</Badge>)}
        </Row>
      )}
      <Row label={t("planningMarkets")}>
        {m.planning_markets ? m.planning_markets.map(c => <Badge key={c} tone="info" dot={false}>{c}</Badge>)
          : <span style={{ color: K.muted }}>{t("planningUnset")}</span>}
      </Row>

      <H>{t("coverageTitle", m.fresh_days)}</H>
      <DataTable<CoverageRow>
        tableId="tenant360-market-coverage" data={m.coverage} columns={coverageCols}
        getRowId={r => r.market} searchable={false} pageSize={25} stickyHeader={false} />
      {m.coverage.every(r => r.places_in_scope === 0) && (
        <div style={{ fontSize: 11.5, color: K.muted2, marginTop: 6 }}>
          {t("noPlaces")}
        </div>
      )}

      <H>{t("unmappedTitle", open.length)}</H>
      <DataTable<UnmappedRow>
        tableId="tenant360-unmapped-markets" data={m.unmapped_requests} columns={unmappedCols}
        getRowId={r => `${r.country_code}-${r.requested_at}`} searchable={false} pageSize={25} stickyHeader={false}
        emptyTitle={t("noUnmapped")} emptyDescription={t("noUnmappedBody")} />
    </div>
  );
}
