"use client";
// app/admin/seo-intelligence/page.tsx
// AA-705 — SEO Intelligence: 5 tabs (Overview / Keywords / Questions / Gaps / Spend) merging
// search_demand + seo_context + published_tours + dfs_call_log. UI kit + react-query (AA-662).
// Data: GET /api/admin/seo-intelligence (BFF -> /admin/seo-intelligence).

import type { ColumnDef } from "@tanstack/react-table";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { BarChart3, HelpCircle, Search, TriangleAlert, Wallet } from "lucide-react";
import {
  apiGet,
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  K,
  LoadingScreen,
  mono,
  PageHeader,
  SkeletonStyle,
  Tabs,
} from "../../_kit";

interface DemandRow {
  market: string;
  keywords: number;
  keywords_with_volume: number;
  total_volume: number;
}
interface KeywordRow {
  keyword: string;
  market: string;
  search_volume: number;
  tours_using: number;
  used_in_content: boolean;
}
interface GapRow {
  tour_id: string;
  name: string;
  country: string;
  has_research: boolean;
}
interface SpendBranch {
  endpoint: string;
  tenant_label: string;
  call_count: number;
  live_count: number;
  cache_hit_count: number;
  total_cost_usd: number;
  keywords_total: number;
  cache_hit_rate: number | null;
}
interface SeoData {
  overview: {
    total_tours: number;
    seo_covered: number;
    coverage_pct: number;
    tours_with_volume: number;
    with_volume_pct: number;
    paa_total: number;
    demand_by_market: DemandRow[];
    freshness: {
      places_researched: number;
      places_expired: number;
      median_age_days: number | null;
      fresh_days: number;
      last_researched: string | null;
    };
  };
  keywords: KeywordRow[];
  questions: { question: string; markets: string[] }[];
  gaps: GapRow[];
  spend: {
    summary: {
      total_calls?: number;
      live_calls?: number;
      cache_hits?: number;
      total_cost_usd?: number;
      cache_hit_rate?: number | null;
    };
    branches: SpendBranch[];
  };
}

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "keywords", label: "Keywords" },
  { key: "questions", label: "Questions (PAA)" },
  { key: "gaps", label: "Gaps" },
  { key: "spend", label: "Spend & cache" },
];

function Stat({ label, value, sub, color = K.ink }: {
  label: string; value: React.ReactNode; sub?: string; color?: string;
}) {
  return (
    <div style={{ background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, padding: "12px 14px" }}>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase", color: K.muted2 }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 600, color, fontVariantNumeric: "tabular-nums", marginTop: 3 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 11, color: K.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const TH: React.CSSProperties = {
  textAlign: "left", fontSize: 10, fontWeight: 700, letterSpacing: "0.05em",
  textTransform: "uppercase", color: K.muted2, padding: "6px 10px", borderBottom: `1px solid ${K.line}`,
};
const TD: React.CSSProperties = { fontSize: 13, color: K.body, padding: "7px 10px", borderBottom: `1px solid ${K.line2}` };

const usd = (n: number | undefined | null) =>
  n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
const pct = (n: number | null | undefined) => (n == null ? "—" : `${Math.round(n * 100)}%`);

function OverviewTab({ d }: { d: SeoData["overview"] }) {
  const maxVol = Math.max(1, ...d.demand_by_market.map((m) => m.total_volume));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Stat label="SEO coverage" value={`${d.coverage_pct}%`} sub={`${d.seo_covered}/${d.total_tours} masters`} color={K.accentDeep} />
        <Stat label="With real volume" value={`${d.with_volume_pct}%`} sub={`${d.tours_with_volume} masters`} color={K.success} />
        <Stat label="PAA questions" value={d.paa_total.toLocaleString()} />
        <Stat label="Places researched" value={d.freshness.places_researched.toLocaleString()}
          sub={d.freshness.places_expired > 0 ? `${d.freshness.places_expired} expired (>${d.freshness.fresh_days}d)` : "all fresh"}
          color={d.freshness.places_expired > 0 ? K.amber : K.ink} />
        <Stat label="Median data age" value={d.freshness.median_age_days != null ? `${d.freshness.median_age_days}d` : "—"} />
      </div>
      <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "16px 18px" }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 12 }}>Demand by market</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={TH}>Market</th>
              <th style={{ ...TH, textAlign: "right" }}>Keywords</th>
              <th style={{ ...TH, textAlign: "right" }}>With volume</th>
              <th style={{ ...TH, textAlign: "right" }}>Total monthly volume</th>
              <th style={TH}></th>
            </tr>
          </thead>
          <tbody>
            {d.demand_by_market.map((m) => (
              <tr key={m.market}>
                <td style={{ ...TD, fontWeight: 600, color: K.ink }}>{m.market}</td>
                <td style={{ ...TD, textAlign: "right" }}>{m.keywords.toLocaleString()}</td>
                <td style={{ ...TD, textAlign: "right" }}>{m.keywords_with_volume.toLocaleString()}</td>
                <td style={{ ...TD, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                  {m.total_volume.toLocaleString()}
                </td>
                <td style={{ ...TD, width: "30%" }}>
                  <div style={{ height: 8, background: K.line2, borderRadius: 4, overflow: "hidden" }}>
                    <div style={{ width: `${(m.total_volume / maxVol) * 100}%`, height: "100%", background: K.accent }} />
                  </div>
                </td>
              </tr>
            ))}
            {d.demand_by_market.length === 0 && (
              <tr><td colSpan={5} style={{ ...TD, color: K.muted }}>No research demand data yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const KW_COLUMNS: ColumnDef<KeywordRow, unknown>[] = [
  { accessorKey: "keyword", header: "Keyword" },
  { accessorKey: "market", header: "Market", cell: (c) => <Badge tone="neutral" dot={false}>{String(c.getValue())}</Badge> },
  {
    accessorKey: "search_volume", header: "Volume / mo",
    cell: (c) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{Number(c.getValue()).toLocaleString()}</span>,
  },
  { accessorKey: "tours_using", header: "Tours using" },
  {
    accessorKey: "used_in_content", header: "In content",
    cell: (c) => (c.getValue() ? <Badge tone="success" dot={false}>yes</Badge> : <span style={{ color: K.muted2 }}>no</span>),
  },
];

function GapsTab({ gaps }: { gaps: GapRow[] }) {
  if (gaps.length === 0) {
    return <EmptyState title="No gaps" description="Every published master has at least one keyword with measured search volume." />;
  }
  const byCountry: Record<string, GapRow[]> = {};
  for (const g of gaps) (byCountry[g.country || "Unknown"] ??= []).push(g);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ fontSize: 12, color: K.muted }}>
        {gaps.length} published masters have no keyword with measured volume. Each links to its tour for research.
      </div>
      {Object.entries(byCountry).map(([country, rows]) => (
        <div key={country} style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "14px 16px" }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 8 }}>
            {country} <span style={{ color: K.muted2, fontWeight: 400 }}>· {rows.length}</span>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr><th style={TH}>Tour</th><th style={{ ...TH, textAlign: "right" }}>Research state</th></tr></thead>
            <tbody>
              {rows.map((g) => (
                <tr key={g.tour_id}>
                  <td style={TD}>
                    <a href={`/admin/master-content?tour=${g.tour_id}`} style={{ color: K.accentDeep, textDecoration: "none" }}>
                      {g.name}
                    </a>
                  </td>
                  <td style={{ ...TD, textAlign: "right" }}>
                    {g.has_research
                      ? <Badge tone="warning" dot={false}>researched, no volume</Badge>
                      : <Badge tone="danger" dot={false}>never researched</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

function SpendTab({ spend }: { spend: SeoData["spend"] }) {
  const s = spend.summary;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <Stat label="DFS cost (window)" value={usd(s.total_cost_usd)} color={K.accentDeep} />
        <Stat label="Calls" value={(s.total_calls ?? 0).toLocaleString()} />
        <Stat label="Live calls" value={(s.live_calls ?? 0).toLocaleString()} />
        <Stat label="Cache hit rate" value={pct(s.cache_hit_rate)} color={K.success} />
      </div>
      <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "16px 18px" }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 10 }}>DataForSEO spend by endpoint</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={TH}>Endpoint</th><th style={TH}>Tenant</th>
              <th style={{ ...TH, textAlign: "right" }}>Calls</th>
              <th style={{ ...TH, textAlign: "right" }}>Cache hit</th>
              <th style={{ ...TH, textAlign: "right" }}>Keywords</th>
              <th style={{ ...TH, textAlign: "right" }}>Cost</th>
            </tr>
          </thead>
          <tbody>
            {spend.branches.map((b, i) => (
              <tr key={i}>
                <td style={{ ...TD, fontFamily: mono, fontSize: 12 }}>{b.endpoint}</td>
                <td style={TD}>{b.tenant_label}</td>
                <td style={{ ...TD, textAlign: "right" }}>{b.call_count.toLocaleString()}</td>
                <td style={{ ...TD, textAlign: "right" }}>{pct(b.cache_hit_rate)}</td>
                <td style={{ ...TD, textAlign: "right" }}>{b.keywords_total.toLocaleString()}</td>
                <td style={{ ...TD, textAlign: "right" }}>{usd(b.total_cost_usd)}</td>
              </tr>
            ))}
            {spend.branches.length === 0 && (
              <tr><td colSpan={6} style={{ ...TD, color: K.muted }}>No DataForSEO spend in this window.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function SeoIntelligencePage() {
  const [tab, setTab] = useState("overview");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["admin-seo-intelligence"],
    queryFn: () => apiGet<SeoData>("/api/admin/seo-intelligence?days=30", { admin: true }),
  });

  const icon: Record<string, React.ReactNode> = {
    overview: <BarChart3 size={14} />, keywords: <Search size={14} />,
    questions: <HelpCircle size={14} />, gaps: <TriangleAlert size={14} />, spend: <Wallet size={14} />,
  };

  return (
    <>
      <SkeletonStyle />
      <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
          <PageHeader
            title="SEO Intelligence"
            description="Search demand, keywords, PAA questions, coverage gaps and research spend — merged from Segment research, S1 SEO context and DataForSEO."
          />
          <div style={{ marginBottom: 20 }}>
            <Tabs tabs={TABS.map((t) => ({ key: t.key, label: t.label }))} active={tab} onChange={setTab} />
          </div>

          {isLoading ? (
            <LoadingScreen message="Loading SEO intelligence…" />
          ) : isError ? (
            <ErrorState message={(error as Error)?.message || "Failed to load."} onRetry={() => refetch()} />
          ) : !data ? null : (
            <>
              {tab === "overview" && <OverviewTab d={data.overview} />}
              {tab === "keywords" && (
                <DataTable<KeywordRow>
                  tableId="seo-keywords"
                  data={data.keywords}
                  columns={KW_COLUMNS}
                  getRowId={(r) => `${r.keyword}:${r.market}`}
                  searchable
                  searchPlaceholder="Search keywords…"
                  pageSize={25}
                />
              )}
              {tab === "questions" && (
                data.questions.length === 0 ? (
                  <EmptyState title="No PAA questions" description="No People-Also-Ask questions have been researched yet." />
                ) : (
                  <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "8px 4px" }}>
                    {data.questions.map((q, i) => (
                      <div key={i} style={{
                        display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12,
                        padding: "9px 14px", borderBottom: i < data.questions.length - 1 ? `1px solid ${K.line2}` : "none",
                      }}>
                        <span style={{ fontSize: 13, color: K.body }}>{q.question}</span>
                        <span style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                          {q.markets.map((m) => <Badge key={m} tone="neutral" dot={false}>{m}</Badge>)}
                        </span>
                      </div>
                    ))}
                  </div>
                )
              )}
              {tab === "gaps" && <GapsTab gaps={data.gaps} />}
              {tab === "spend" && <SpendTab spend={data.spend} />}
            </>
          )}
        </main>
    </>
  );
}
