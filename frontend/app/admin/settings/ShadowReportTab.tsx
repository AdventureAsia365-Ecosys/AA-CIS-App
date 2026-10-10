"use client";
// app/admin/settings/ShadowReportTab.tsx
// AA-686 — the Shadow A/B sub-tab: a kit DataTable, one row per (stage, primary, shadow) group
// from GET /admin/llm-shadow/report?days=. Shows agreement %, mean |Δscore|, cost per call primary
// vs shadow, latency p50/p95, repeat-scoring variance, and n / unparsed / errors. Every rate shows
// its sample size next to it so a rate from 3 rows does not read like one from 3,000. A days
// selector (7/30/90) drives the query. Dead stages (HIDDEN_STAGES) are not shown.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { A, mono } from "../_components/adminUi";
import { type ColumnDef, DataTable, apiGet } from "../../_kit";
import { HIDDEN_STAGES, type ShadowGroup } from "./llmShared";

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
      <div style={{ fontFamily: mono, color: A.ink }}>{value}</div>
      <div style={{ fontSize: 10.5, color: A.muted2 }}>n={sample.toLocaleString()}</div>
    </div>
  );
}

export function ShadowReportTab() {
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
      header: "Stage",
      cell: (c) => <span style={{ fontFamily: mono, fontWeight: 600, color: A.ink }}>{c.row.original.stage}</span>,
    },
    {
      id: "pair",
      header: "Primary → Shadow",
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ fontSize: 12 }}>
            <div style={{ color: A.body }}>{r.primary_model}</div>
            <div style={{ color: A.muted }}>→ {r.shadow_model}</div>
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
      header: "Agreement",
      cell: (c) => <RateWithSample value={pct(c.row.original.agreement_rate)} sample={c.row.original.agreement_sample} />,
    },
    {
      accessorKey: "mean_abs_score_delta",
      header: "Mean |Δscore|",
      cell: (c) => <RateWithSample value={dev(c.row.original.mean_abs_score_delta)} sample={c.row.original.score_delta_sample} />,
    },
    {
      id: "cost",
      header: "Cost / call (P vs S)",
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ fontFamily: mono, fontSize: 12 }}>
            <div style={{ color: A.body }}>{usd(r.primary_cost_per_call)}</div>
            <div style={{ color: A.muted }}>{usd(r.shadow_cost_per_call)}</div>
          </div>
        );
      },
    },
    {
      id: "latency",
      header: "Shadow latency p50 / p95",
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
      header: "Repeat σ (P / S)",
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
      header: "Unparsed / errors",
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ fontFamily: mono, fontSize: 12, color: r.shadow_error > 0 ? A.red : A.muted }}>
            {r.unparsed.toLocaleString()} / {r.shadow_error.toLocaleString()}
          </span>
        );
      },
    },
  ], []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <p style={{ fontSize: 12, color: A.muted2, margin: 0 }}>
        Each row compares a stage&apos;s primary model against its shadow over the window. Agreement is
        the share of calls where both reach the same pass/fail; mean |Δscore| is the average absolute
        score gap. Sample sizes are shown next to each rate — a rate over a handful of calls is not
        the same as one over thousands.
      </p>

      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 12, color: A.muted }}>Window</span>
        {DAYS_OPTIONS.map((d) => (
          <button
            key={d}
            data-testid={`shadow-days-${d}`}
            onClick={() => setDays(d)}
            style={{
              padding: "5px 12px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer",
              border: `1px solid ${days === d ? A.accentBorder : A.line}`,
              background: days === d ? A.accentTint : A.card,
              color: days === d ? A.accentDeep : A.muted,
            }}
          >
            {d}d
          </button>
        ))}
      </div>

      <DataTable<ShadowGroup>
        data={groups}
        columns={columns}
        tableId="admin-llm-shadow"
        getRowId={(r) => `${r.stage}:${r.primary_model}:${r.shadow_model}`}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : "Could not load the shadow report") : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder="Search stages…"
        enableCsv
        csvFilename="llm-shadow-report"
        pageSize={25}
        pageSizeOptions={[25, 50, 100]}
        tableMinWidth={1080}
        emptyTitle="No shadow comparisons in this window"
        emptyDescription="Turn on a stage's shadow in the Stages sub-tab, then come back after it has run on some calls."
      />
    </div>
  );
}
