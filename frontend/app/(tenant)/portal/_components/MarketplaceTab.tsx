"use client";
// app/(tenant)/portal/_components/MarketplaceTab.tsx — AA-444 (Marketplace, tenant-facing)
//
// Per ADR-2026-038 §0.3 (22/08/2026): Marketplace is NOT its own pipeline stage — it is a
// read-only rollup of what T1-T4 (tenant_tour_versions) and T5-T6 (tour_atoms, owner_scope)
// already produced for this tenant. No T-number route on purpose (see implementation notes).
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token> -> backend resolves
// tenant_id from the JWT, api/routers/v1_marketplace.py — no tenant_id is ever sent by this
// component):
//   GET /api/tenant/v1/marketplace
//
// Deliberately no action buttons — the task's own framing (and the ADR's) is that selecting a
// tour (T1) and curating atoms (T6) already have their own pages; this is a summary, not a
// third place to mutate the same state. Styled off AtomsTab.tsx (T6) — same ui.tsx tokens,
// same small-tool sizing (a tenant reviewing a handful of tours, not a staff catalog browser).

import { useState, useEffect, useCallback } from "react";
import { T, serif, sans, mono, Card, Badge, LoadingScreen, EmptyState, statusVariant } from "./ui";

interface MarketplaceTour {
  version_id: string;
  version_number: number;
  status: string;
  quality_score: number | null;
  qa_status: string;
  qa_auto_passed: boolean;
  published_tour_id: string;
  tour_id: string;
  name: string;
  country: string | null;
  duration: string | null;
  atom_count: number;
  high_atom_count: number;
  price_usd: number | null;
  price_available: boolean;
  runway_months: number | null;
}

interface MarketplaceResponse {
  tenant_id: string;
  posts_per_week: number | null;
  tours: MarketplaceTour[];
  total_tours: number;
  total_atoms: number;
}

export default function MarketplaceTab() {
  const [data, setData] = useState<MarketplaceResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(false);
    fetch("/api/tenant/v1/marketplace")
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(d => setData(d))
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) return <LoadingScreen message="Loading your marketplace…" />;

  if (error || !data) {
    return (
      <EmptyState icon="⚠️" title="Couldn't load your marketplace"
        sub="Something went wrong reaching your tour and atom data. Try refreshing the page." />
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
          Marketplace
        </h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Every tour you've written and the content atoms curated from it, in one place — a
          rollup of My Catalog Tours and Atom Curation, read-only. To select a new tour or curate
          atoms, use Browse Tours or Atom Curation.
        </p>
      </div>

      <div style={{ display: "flex", gap: 22, marginBottom: 22, flexWrap: "wrap" }}>
        <StatBlock label="Tours" value={data.total_tours} />
        <StatBlock label="Total atoms" value={data.total_atoms} />
        <StatBlock label="Posts / week" value={data.posts_per_week ?? "—"} />
      </div>

      {data.tours.length === 0 ? (
        <EmptyState icon="🗂️" title="Nothing here yet"
          sub="Once you write your first tour from Browse Tours, it'll show up here alongside its curated atoms." />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {data.tours.map(t => (
            <Card key={t.published_tour_id} style={{ padding: "14px 18px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
                <div style={{ flex: 1, minWidth: 220 }}>
                  <div style={{ fontSize: 11, color: T.muted2, marginBottom: 4, fontFamily: mono }}>
                    {[t.country, t.duration].filter(Boolean).join(" · ") || "—"}
                  </div>
                  <div style={{ fontSize: 14.5, fontWeight: 600, color: T.ink, lineHeight: 1.4 }}>{t.name}</div>
                  <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                    <Badge variant={statusVariant(t.status)}>{t.status.replace("_", " ")}</Badge>
                    {t.qa_auto_passed && <Badge>auto-passed</Badge>}
                    <Badge variant="info">v{t.version_number}</Badge>
                  </div>
                </div>

                <div style={{ display: "flex", gap: 20, flexShrink: 0, textAlign: "right" }}>
                  <Metric label="Atoms" value={t.atom_count} sub={`${t.high_atom_count} high`} />
                  <Metric label="Price"
                    value={t.price_available && t.price_usd != null ? `$${t.price_usd.toFixed(0)}` : "—"}
                    sub={t.price_available ? "estimated" : "on request"} />
                  <Metric label="Runway"
                    value={t.runway_months != null ? `${t.runway_months} mo` : "—"}
                    sub={t.runway_months != null ? "at current cadence" : "n/a"} />
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function StatBlock({ label, value }: { label: string; value: number | string }) {
  return (
    <div style={{ minWidth: 100 }}>
      <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 3 }}>{label}</div>
      <div style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink }}>{value}</div>
    </div>
  );
}

function Metric({ label, value, sub }: { label: string; value: string | number; sub: string }) {
  return (
    <div style={{ minWidth: 78 }}>
      <div style={{ fontSize: 10, color: T.muted2, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 2, fontFamily: sans }}>{label}</div>
      <div style={{ fontFamily: mono, fontSize: 14, fontWeight: 600, color: T.ink }}>{value}</div>
      <div style={{ fontSize: 10.5, color: T.muted2, marginTop: 1 }}>{sub}</div>
    </div>
  );
}
