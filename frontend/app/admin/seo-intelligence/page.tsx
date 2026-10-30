"use client";
// app/admin/seo-intelligence/page.tsx
// AA-705 — SEO Intelligence: 5 tabs (Overview / Keywords / Questions / Gaps / Spend) merging
// search_demand + seo_context + published_tours + dfs_call_log. UI kit + react-query (AA-662).
// Data: GET /api/admin/seo-intelligence (BFF -> /admin/seo-intelligence).
// AA-994: text in the UI language (messages.ts); keywords, questions and markets from the API stay as sent.

import type { ColumnDef } from "@tanstack/react-table";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
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
  Tabs, PageFrame, ScrollX,
  useT,
  type Translator,
} from "../../_kit";
import { SEO } from "./messages";

type T = Translator<typeof SEO.en>;

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
  { key: "overview", label: "tabOverview" },
  { key: "keywords", label: "tabKeywords" },
  { key: "questions", label: "tabQuestions" },
  { key: "gaps", label: "tabGaps" },
  { key: "spend", label: "tabSpend" },
] as const;

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
  const t = useT(SEO);
  const maxVol = Math.max(1, ...d.demand_by_market.map((m) => m.total_volume));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Stat label={t("seoCoverage")} value={`${d.coverage_pct}%`} sub={t("masters", d.seo_covered, d.total_tours)} color={K.accentDeep} />
        <Stat label={t("withVolume")} value={`${d.with_volume_pct}%`} sub={t("mastersCount", d.tours_with_volume)} color={K.success} />
        <Stat label={t("paaQuestions")} value={d.paa_total.toLocaleString()} />
        <Stat label={t("placesResearched")} value={d.freshness.places_researched.toLocaleString()}
          sub={d.freshness.places_expired > 0 ? t("expired", d.freshness.places_expired, d.freshness.fresh_days) : t("allFresh")}
          color={d.freshness.places_expired > 0 ? K.amber : K.ink} />
        <Stat label={t("medianAge")} value={d.freshness.median_age_days != null ? t("days", d.freshness.median_age_days) : "—"} />
      </div>
      <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "16px 18px" }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 12 }}>{t("demandByMarket")}</div>
        <ScrollX>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={TH}>{t("market")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("keywords")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("withVolumeCol")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("totalVolume")}</th>
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
              <tr><td colSpan={5} style={{ ...TD, color: K.muted }}>{t("noDemand")}</td></tr>
            )}
          </tbody>
        </table>
        </ScrollX>
      </div>
    </div>
  );
}

const kwColumns = (t: T): ColumnDef<KeywordRow, unknown>[] => [
  { accessorKey: "keyword", header: t("keyword") },
  { accessorKey: "market", header: t("market"), cell: (c) => <Badge tone="neutral" dot={false}>{String(c.getValue())}</Badge> },
  {
    accessorKey: "search_volume", header: t("volumeMo"),
    cell: (c) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{Number(c.getValue()).toLocaleString()}</span>,
  },
  { accessorKey: "tours_using", header: t("toursUsing") },
  {
    accessorKey: "used_in_content", header: t("inContent"),
    cell: (c) => (c.getValue() ? <Badge tone="success" dot={false}>{t("yes")}</Badge> : <span style={{ color: K.muted2 }}>{t("no")}</span>),
  },
];

function GapsTab({ gaps }: { gaps: GapRow[] }) {
  const t = useT(SEO);
  if (gaps.length === 0) {
    return <EmptyState title={t("noGapsTitle")} description={t("noGapsBody")} />;
  }
  const byCountry: Record<string, GapRow[]> = {};
  for (const g of gaps) (byCountry[g.country || t("unknownCountry")] ??= []).push(g);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ fontSize: 12, color: K.muted }}>
        {t("gapsIntro", gaps.length)}
      </div>
      {Object.entries(byCountry).map(([country, rows]) => (
        <div key={country} style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "14px 16px" }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 8 }}>
            {country} <span style={{ color: K.muted2, fontWeight: 400 }}>· {rows.length}</span>
          </div>
          <ScrollX>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr><th style={TH}>{t("tour")}</th><th style={{ ...TH, textAlign: "right" }}>{t("researchState")}</th></tr></thead>
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
                      ? <Badge tone="warning" dot={false}>{t("researchedNoVolume")}</Badge>
                      : <Badge tone="danger" dot={false}>{t("neverResearched")}</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </ScrollX>
        </div>
      ))}
    </div>
  );
}

function SpendTab({ spend }: { spend: SeoData["spend"] }) {
  const t = useT(SEO);
  const s = spend.summary;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <Stat label={t("dfsCost")} value={usd(s.total_cost_usd)} color={K.accentDeep} />
        <Stat label={t("calls")} value={(s.total_calls ?? 0).toLocaleString()} />
        <Stat label={t("liveCalls")} value={(s.live_calls ?? 0).toLocaleString()} />
        <Stat label={t("cacheHitRate")} value={pct(s.cache_hit_rate)} color={K.success} />
      </div>
      <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "16px 18px" }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, marginBottom: 10 }}>{t("spendByEndpoint")}</div>
        <ScrollX>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={TH}>{t("endpoint")}</th><th style={TH}>{t("tenant")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("calls")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("cacheHit")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("keywords")}</th>
              <th style={{ ...TH, textAlign: "right" }}>{t("cost")}</th>
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
              <tr><td colSpan={6} style={{ ...TD, color: K.muted }}>{t("noSpend")}</td></tr>
            )}
          </tbody>
        </table>
        </ScrollX>
      </div>
    </div>
  );
}

export default function SeoIntelligencePage() {
  const t = useT(SEO);
  const kwCols = useMemo(() => kwColumns(t), [t]);
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
      <PageFrame>
          <PageHeader
            title={t("title")}
            description={t("description")}
          />
          <div style={{ marginBottom: 20 }}>
            <Tabs tabs={TABS.map((x) => ({ key: x.key, label: t(x.label) }))} active={tab} onChange={setTab} />
          </div>

          {isLoading ? (
            <LoadingScreen message={t("loading")} />
          ) : isError ? (
            <ErrorState message={(error as Error)?.message || t("loadFailed")} onRetry={() => refetch()} />
          ) : !data ? null : (
            <>
              {tab === "overview" && <OverviewTab d={data.overview} />}
              {tab === "keywords" && (
                <DataTable<KeywordRow>
                  tableId="seo-keywords"
                  data={data.keywords}
                  columns={kwCols}
                  getRowId={(r) => `${r.keyword}:${r.market}`}
                  searchable
                  searchPlaceholder={t("searchKeywords")}
                  pageSize={25}
                />
              )}
              {tab === "questions" && (
                data.questions.length === 0 ? (
                  <EmptyState title={t("noPaaTitle")} description={t("noPaaBody")} />
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
        </PageFrame>
    </>
  );
}
