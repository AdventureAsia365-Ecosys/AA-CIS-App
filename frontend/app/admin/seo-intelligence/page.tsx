"use client";
// app/admin/seo-intelligence/page.tsx
// AA-705 — SEO Intelligence: 4 tabs (Overview / Keywords / Questions / Gaps) merging
// search_demand + seo_context + published_tours. UI kit + react-query (AA-662).
// AA-1039 R6 rest (IA §6.2): the Spend tab (DataForSEO cost by endpoint) folded into Spend › DataForSEO, which shows
// the same dfs_call_log rollup with the balance, the runs by job and the markets; a link there replaces it for the
// roles that may open Spend (`?tab=spend` answers 307 there, lib/adminIa.ts).
// Data: GET /api/admin/seo-intelligence (BFF -> /admin/seo-intelligence).
// AA-994: text in the UI language (messages.ts); keywords, questions and markets from the API stay as sent.

import type { ColumnDef } from "@tanstack/react-table";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Wallet } from "lucide-react";
import {
  apiGet,
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  K,
  LoadingScreen,
  PageHeader,
  SkeletonStyle,
  Tabs, PageFrame,
  useT,
  type Translator,
  useAdminIdentity,
} from "../../_kit";
import { canSeePage } from "@/lib/rbac";
import { SEO } from "./messages";

type T = Translator<typeof SEO.en>;

/** Spend › AI & data spend › DataForSEO: cost by endpoint and tenant, balance, runs by job, markets. */
const DFS_SPEND_HREF = "/admin/spend?tab=spend&view=dfs";

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
}

const TABS = [
  { key: "overview", label: "tabOverview" },
  { key: "keywords", label: "tabKeywords" },
  { key: "questions", label: "tabQuestions" },
  { key: "gaps", label: "tabGaps" },
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
        <DataTable<DemandRow>
          tableId="seo-demand-by-market"
          data={d.demand_by_market}
          getRowId={(m) => m.market}
          searchable={false}
          paginate={false}
          compact
          emptyTitle={t("noDemand")}
          columns={[
            { id: "market", header: t("market"), accessorKey: "market",
              cell: ({ row }) => <span style={{ fontWeight: 600, color: K.ink }}>{row.original.market}</span> },
            { id: "keywords", header: t("keywords"), accessorKey: "keywords", meta: { align: "right" },
              cell: ({ row }) => row.original.keywords.toLocaleString() },
            { id: "withVolume", header: t("withVolumeCol"), accessorKey: "keywords_with_volume", meta: { align: "right" },
              cell: ({ row }) => row.original.keywords_with_volume.toLocaleString() },
            { id: "totalVolume", header: t("totalVolume"), accessorKey: "total_volume", meta: { align: "right" },
              cell: ({ row }) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{row.original.total_volume.toLocaleString()}</span> },
            { id: "bar", header: "", enableSorting: false, meta: { hideLabelOnCard: true }, cell: ({ row }) => (
              <div style={{ minWidth: 120, height: 8, background: K.line2, borderRadius: 4, overflow: "hidden" }}>
                <div style={{ width: `${(row.original.total_volume / maxVol) * 100}%`, height: "100%", background: K.accent }} />
              </div>
            ) },
          ]}
        />
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
          <DataTable<GapRow>
            tableId="seo-gaps"
            data={rows}
            getRowId={(g) => g.tour_id}
            searchable={false}
            paginate={false}
            compact
            columns={[
              { id: "tour", header: t("tour"), accessorKey: "name", cell: ({ row }) => (
                <a href={`/admin/master-content?tour=${row.original.tour_id}`} style={{ color: K.accentDeep, textDecoration: "none" }}>
                  {row.original.name}
                </a>
              ) },
              { id: "state", header: t("researchState"), accessorKey: "has_research", meta: { align: "right" }, cell: ({ row }) => (
                row.original.has_research
                  ? <Badge tone="warning" dot={false}>{t("researchedNoVolume")}</Badge>
                  : <Badge tone="danger" dot={false}>{t("neverResearched")}</Badge>
              ) },
            ]}
          />
        </div>
      ))}
    </div>
  );
}

export default function SeoIntelligencePage() {
  const t = useT(SEO);
  const kwCols = useMemo(() => kwColumns(t), [t]);
  const [tab, setTab] = useState("overview");
  // AA-1039 R6 rest: DataForSEO spend lives on Spend (AI & spend roles); content_lead, who also reads this page, has no
  // spend.read (root audit S265 §4.1), so the link shows only to a role that may open Spend.
  const seesSpend = canSeePage(useAdminIdentity().role, "/admin/spend");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["admin-seo-intelligence"],
    queryFn: () => apiGet<SeoData>("/api/admin/seo-intelligence", { admin: true }),
  });

  return (
    <>
      <SkeletonStyle />
      <PageFrame>
          <PageHeader
            title={t("title")}
            description={t("description")}
            actions={seesSpend ? (
              <Link href={DFS_SPEND_HREF} data-testid="seo-dfs-spend-link"
                style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: K.accentDeep }}>
                <Wallet size={14} /> {t("dfsSpendLink")}
              </Link>
            ) : undefined}
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
            </>
          )}
        </PageFrame>
    </>
  );
}
