"use client";
// app/admin/ai-gateway/ShadowReport.tsx
// AA-686 — the shadow comparison report (AI providers › Models & trials since AA-804 UI part 2; was Settings › LLM
// Models › Shadow A/B): a kit DataTable, one row per (stage, primary, shadow) group
// from GET /admin/llm-shadow/report?days=. Shows agreement %, mean |Δscore|, cost per call primary
// vs shadow, latency p50/p95, repeat-scoring variance, and n / unparsed / errors. Every rate shows
// its sample size next to it so a rate from 3 rows does not read like one from 3,000. A days
// selector (7/30/90) drives the query. Dead stages (HIDDEN_STAGES) are not shown.
// AA-994: text in the UI language (models.messages.ts).

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { K, mono, type ColumnDef, DataTable, SegmentedControl, apiGet, useT } from "../../_kit";
import { HIDDEN_STAGES, type ShadowGroup } from "./llmShared";
import { MODELS } from "./models.messages";

interface ShadowReportResp {
  days: number;
  groups: ShadowGroup[];
}

const DAYS_OPTIONS = [7, 30, 90];

const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const usd = (v: number | null) => (v == null ? "—" : `$${v.toFixed(5)}`);
const ms = (v: number | null) => (v == null ? "—" : `${Math.round(v)}ms`);
const dev = (v: number | null) => (v == null ? "—" : v.toFixed(3));

/** A rate value with its sample size underneath — so a rate over 3 rows is not read as 3,000. */
function RateWithSample({ value, sample }: { value: React.ReactNode; sample: number }) {
  return (
    <div>
      <div style={{ fontFamily: mono, color: K.ink }}>{value}</div>
      <div style={{ fontSize: 10.5, color: K.muted2 }}>n={sample.toLocaleString()}</div>
    </div>
  );
}

export function ShadowReport() {
  const t = useT(MODELS);
  const [days, setDays] = useState(30);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["llm-shadow-report", days],
    queryFn: () => apiGet<ShadowReportResp>(`/api/admin/llm-shadow/report?days=${days}`, { admin: true }),
  });

  // Hide dead stages (contract): currently none — HIDDEN_STAGES is empty since the N7 removal.
  const groups = useMemo(
    () => (data?.groups ?? []).filter((g) => !HIDDEN_STAGES.has(g.stage)),
    [data],
  );

  const columns = useMemo<ColumnDef<ShadowGroup, unknown>[]>(() => [
    {
      accessorKey: "stage",
      header: t("colStage"),
      cell: (c) => <span style={{ fontFamily: mono, fontWeight: 600, color: K.ink }}>{c.row.original.stage}</span>,
    },
    {
      id: "pair",
      header: t("colPair"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ fontSize: 12 }}>
            <div style={{ color: K.body }}>{r.primary_model}</div>
            <div style={{ color: K.muted }}>→ {r.shadow_model}</div>
          </div>
        );
      },
    },
    {
      accessorKey: "n",
      header: "n",
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.n.toLocaleString()}</span>,
    },
    {
      accessorKey: "agreement_rate",
      header: t("colAgreement"),
      cell: (c) => <RateWithSample value={pct(c.row.original.agreement_rate)} sample={c.row.original.agreement_sample} />,
    },
    {
      accessorKey: "mean_abs_score_delta",
      header: t("colDelta"),
      cell: (c) => <RateWithSample value={dev(c.row.original.mean_abs_score_delta)} sample={c.row.original.score_delta_sample} />,
    },
    {
      id: "cost",
      header: t("colCost"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ fontFamily: mono, fontSize: 12 }}>
            <div style={{ color: K.body }}>{usd(r.primary_cost_per_call)}</div>
            <div style={{ color: K.muted }}>{usd(r.shadow_cost_per_call)}</div>
          </div>
        );
      },
    },
    {
      id: "latency",
      header: t("colLatency"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ fontFamily: mono, fontSize: 12 }}>
            {ms(r.shadow_latency_p50_ms)} / {ms(r.shadow_latency_p95_ms)}
          </span>
        );
      },
    },
    {
      id: "variance",
      header: t("colRepeat"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ fontFamily: mono, fontSize: 12 }}>
            {dev(r.primary_repeat_score_stddev)} / {dev(r.shadow_repeat_score_stddev)}
          </span>
        );
      },
    },
    {
      id: "health",
      header: t("colHealth"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ fontFamily: mono, fontSize: 12, color: r.shadow_error > 0 ? K.danger : K.muted }}>
            {r.unparsed.toLocaleString()} / {r.shadow_error.toLocaleString()}
          </span>
        );
      },
    },
  ], [t]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, color: K.muted }}>{t("window")}</span>
        <SegmentedControl<string>
          label={t("window")}
          appearance="chips"
          size="sm"
          testId="shadow-days"
          value={String(days)}
          onChange={(v) => setDays(Number(v))}
          options={DAYS_OPTIONS.map((d) => ({ value: String(d), label: t("daysShort", d) }))}
        />
      </div>

      <DataTable<ShadowGroup>
        data={groups}
        columns={columns}
        tableId="admin-llm-shadow"
        getRowId={(r) => `${r.stage}:${r.primary_model}:${r.shadow_model}`}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : t("loadShadowError")) : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder={t("searchStages")}
        enableCsv
        csvFilename="llm-shadow-report"
        pageSize={25}
        pageSizeOptions={[25, 50, 100]}
        tableMinWidth={1080}
        emptyTitle={t("noShadowTitle")}
        emptyDescription={t("noShadowBody")}
      />
    </div>
  );
}
