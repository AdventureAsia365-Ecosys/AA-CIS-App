"use client";
// portal/app/portal/_components/MarketplaceTab.tsx — AA-444 (Tour overview, tenant-facing; the nav
// called it "Marketplace" until AA-760)
//
// Per ADR-2026-038 §0.3 (22/08/2026): this is NOT its own pipeline stage — it is a read-only rollup
// of what T1-T4 (tenant_tour_versions) already produced for this tenant. No T-number route on
// purpose (see implementation notes).
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token> -> backend resolves
// tenant_id from the JWT, api/routers/v1_marketplace.py — no tenant_id is ever sent by this
// component):
//   GET /api/tenant/v1/marketplace
//
// Deliberately no action buttons: selecting a tour (T1) has its own page; this is a summary.
//
// AA-760 (screenshot audit):
//   - tenant sees content only: the atom count, "Total atoms" and the runway months (content-pipeline
//     terms, always 0 for tenants) are not shown; the API still returns them for the admin views;
//   - one status vocabulary with My Tours: the badge is the shared work status read from the same
//     version in the ["my-versions"] query (job-backed), not the raw marketplace state — a version
//     read "REJECTED" here and "READY" on My Tours;
//   - kit PageHeader on the route page, skeleton while loading, "$1,225" prices, one duration format.

import { useMemo } from "react";
import { AlertTriangle, FolderOpen } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/app/_kit";
import { T, sans, Card, Badge, CtaLink, EmptyState, fmtDuration, fmtUsd } from "./ui";
import { ListSkeleton } from "./Skeleton";
import { WorkStatusBadge, tourWorkStatus, type WorkStatus } from "./workStatus";
import { useCatalog } from "../_shell/useShellData";

interface MarketplaceTour {
  version_id: string;
  version_number: number;
  status: string;
  published_tour_id: string;
  tour_id: string;
  name: string;
  country: string | null;
  duration: string | null;
  price_usd: number | null;
  price_available: boolean;
}

interface MarketplaceResponse {
  tenant_id: string;
  posts_per_week: number | null;
  tours: MarketplaceTour[];
  total_tours: number;
}

/** The marketplace state (`tenant_version_state`) in work-status words, for a version not in
 *  ["my-versions"] yet (the two lists are read separately). */
function fromMarketplaceState(s: string): WorkStatus {
  if (s === "writing") return "writing";
  if (s === "failed") return "failed";
  return "done";
}

export default function MarketplaceTab() {
  // AA-760 — react-query (was a fetch in an effect with three useStates).
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ["tenant-marketplace"],
    queryFn: () => apiGet<MarketplaceResponse>("/api/tenant/v1/marketplace"),
  });
  const { versions } = useCatalog();
  const byVersion = useMemo(() => new Map((versions ?? []).map(v => [v.id, v])), [versions]);

  if (isPending) return <ListSkeleton rows={4} label="Loading your tours" />;

  if (isError || !data) {
    return (
      <EmptyState icon={<AlertTriangle size={32} strokeWidth={1.5} color={T.gold} />} title="Couldn't load your tours"
        sub="Something went wrong reaching your tour data."
        action={<button onClick={() => refetch()} style={{ background: "none", border: `1px solid ${T.line}`, borderRadius: 999, padding: "6px 14px", cursor: "pointer", fontSize: 12.5, color: T.ink3, fontFamily: sans }}>Try again</button>} />
    );
  }

  // AA-760 — an empty overview is the empty state alone, with its next step (no "Tours 0" stat above it).
  const empty = data.tours.length === 0;
  return (
    <div>
      {!empty && (
        <div style={{ display: "flex", gap: 22, marginBottom: 22, flexWrap: "wrap" }}>
          <StatBlock label="Tours" value={data.total_tours} />
          {data.posts_per_week != null && data.posts_per_week > 0 && (
            <StatBlock label="Posts planned / week" value={data.posts_per_week} />
          )}
        </div>
      )}

      {empty ? (
        <EmptyState icon={<FolderOpen size={32} strokeWidth={1.5} color={T.gold} />} title="Nothing here yet"
          sub="Once you rewrite your first tour from Browse Tours, it shows up here."
          action={<CtaLink href="/portal/tours">Browse tours</CtaLink>} />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {data.tours.map(t => {
            const v = byVersion.get(t.version_id);
            const status = v ? tourWorkStatus(v) : fromMarketplaceState(t.status);
            const meta = [t.country, fmtDuration(t.duration)].filter(Boolean).join(" · ");
            return (
              <Card key={t.published_tour_id} style={{ padding: "14px 18px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
                  <div style={{ flex: 1, minWidth: 220 }}>
                    {meta && <div style={{ fontSize: 12, color: T.muted, marginBottom: 4, fontFamily: sans }}>{meta}</div>}
                    <div style={{ fontSize: 14.5, fontWeight: 600, color: T.ink, lineHeight: 1.4 }}>{t.name}</div>
                    <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                      <WorkStatusBadge status={status} />
                      <Badge variant="info">v{t.version_number}</Badge>
                    </div>
                  </div>

                  <div style={{ flexShrink: 0, textAlign: "right" }}>
                    <Metric label="Price"
                      value={t.price_available && t.price_usd != null ? fmtUsd(t.price_usd) : "—"}
                      sub={t.price_available ? "estimated" : "on request"} />
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StatBlock({ label, value }: { label: string; value: number | string }) {
  return (
    <div style={{ minWidth: 100 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: T.muted, textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 4 }}>{label}</div>
      <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 22, fontWeight: 600, color: T.ink }}>{value}</div>
    </div>
  );
}

function Metric({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div style={{ minWidth: 78 }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: T.muted, textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 2, fontFamily: sans }}>{label}</div>
      <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 15, fontWeight: 600, color: T.ink }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: T.muted, marginTop: 1 }}>{sub}</div>}
    </div>
  );
}
