"use client";
// app/admin/data-coverage/ContentPipeline.tsx — AA-1140 (S289): the content pipeline funnel per country and the content
// counts (atoms, segments, routes, hubs, score rows, researched places) moved here from the old Overview when
// /admin/overview became the Ecosystem status page (S285 advisory: content inventory and the pipeline funnel are not
// ops status). Same data as before: GET /api/admin/overview (backend /admin/overview, cached 60 s, ops.read = admin);
// the wording and the "where does this number come from" texts stay in app/admin/overview/messages.ts and
// _components/sources.ts (AA-858 guard tests).

import { useQuery } from "@tanstack/react-query";
import { DataTable, ErrorState, K, KpiTile, Section, SkeletonBar, apiGet, useT } from "../../_kit";
import { OVERVIEW } from "../overview/messages";
import { hintColumn } from "../overview/_components/opsUi";
import { useSources } from "../overview/_components/sources";

interface FunnelRow {
  country: string; raw_active: number; raw_7d: number; generated: number; generated_7d: number; in_review: number;
  published: number; published_7d: number; atomized: number;
}
export interface OverviewContent {
  pipeline_funnel: FunnelRow[];
  intelligence: {
    atom_count: number; segment_count: number; route_count: number; hub_count: number; score_count: number;
    research: { places_researched: number; places_stale: number };
  };
  tenants: {
    active_tenants: number; rewrites_7d: number; pieces_7d: number; published_pieces_7d: number;
    quota_outliers: { tenant_name: string; plan_tier: string; quota_tours_pct: number; quota_calls_pct: number }[];
  };
}

/** The old Overview read, shared by Data coverage (this file) and Tenants (TenantActivity.tsx): one cached query. */
export function useOverviewContent() {
  return useQuery({
    queryKey: ["admin-overview"],
    queryFn: () => apiGet<OverviewContent>("/api/admin/overview", { admin: true }),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

function delta(n: number) {
  if (!n) return null;
  return <span style={{ fontSize: 11, color: K.success, marginLeft: 6, fontWeight: 600 }}>+{n}/7d</span>;
}

function Funnel({ rows }: { rows: FunnelRow[] }) {
  const t = useT(OVERVIEW);
  const { PIPELINE_SRC } = useSources();
  return (
    <DataTable<FunnelRow> tableId="coverage-pipeline" data={rows} getRowId={(r) => r.country || "unknown"}
      searchable={false} paginate={false} compact tableMinWidth={560}
      columns={[
        { id: t("country"), header: t("country"), enableSorting: false,
          cell: ({ row }) => <span style={{ fontWeight: 600, color: K.ink }}>{row.original.country || t("unknown")}</span> },
        { ...hintColumn(t("raw"), PIPELINE_SRC.raw), cell: ({ row }) => <>{row.original.raw_active}{delta(row.original.raw_7d)}</> },
        { ...hintColumn(t("generated"), PIPELINE_SRC.generated),
          cell: ({ row }) => <>{row.original.generated}{delta(row.original.generated_7d)}</> },
        { ...hintColumn(t("inReview"), PIPELINE_SRC.review),
          cell: ({ row }) => <span style={{ color: row.original.in_review > 0 ? K.amber : K.muted2 }}>
            {row.original.in_review}</span> },
        { ...hintColumn(t("published"), PIPELINE_SRC.published),
          cell: ({ row }) => <b>{row.original.published}{delta(row.original.published_7d)}</b> },
        { ...hintColumn(t("atomized"), PIPELINE_SRC.atomized), cell: ({ row }) => row.original.atomized },
      ]} />
  );
}

function Counts({ intel }: { intel: OverviewContent["intelligence"] }) {
  const t = useT(OVERVIEW);
  const { INTEL_SRC } = useSources();
  const stale = intel.research.places_stale;
  const tile = (label: string, value: number, source: string) => (
    <KpiTile key={label} label={label} value={value.toLocaleString()} size="sm"
      info={source} />
  );
  return (
    <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))" }}>
      {tile(t("atoms"), intel.atom_count, INTEL_SRC.atoms)}
      {tile(t("segments"), intel.segment_count, INTEL_SRC.segments)}
      {tile(t("routes"), intel.route_count, INTEL_SRC.routes)}
      {tile(t("hubs"), intel.hub_count, INTEL_SRC.hubs)}
      {tile(t("scoreRows"), intel.score_count, INTEL_SRC.scores)}
      <KpiTile label={t("researchPlaces")} value={intel.research.places_researched.toLocaleString()} size="sm"
        info={INTEL_SRC.research}
        hint={stale > 0 ? t("stale", stale) : t("allFresh")} hintTone={stale > 0 ? "warning" : "neutral"} />
    </div>
  );
}

/** Data coverage › Content pipeline: the funnel per country and the content counts. */
export function ContentPipeline() {
  const t = useT(OVERVIEW);
  const { data, isLoading, isError, error, refetch } = useOverviewContent();
  return (
    <div style={{ display: "grid", gap: 18, marginTop: 22 }} data-testid="coverage-content-pipeline">
      <Section title={t("pipelineTitle")} testId="coverage-pipeline-section">
        {isLoading && <SkeletonBar w="100%" h={80} />}
        {isError && <ErrorState message={(error as Error)?.message || t("loadFailed")} onRetry={() => refetch()} />}
        {data && <Funnel rows={data.pipeline_funnel} />}
      </Section>
      <Section title={t("intelligence")} testId="coverage-intelligence-section">
        {data && <Counts intel={data.intelligence} />}
      </Section>
    </div>
  );
}
