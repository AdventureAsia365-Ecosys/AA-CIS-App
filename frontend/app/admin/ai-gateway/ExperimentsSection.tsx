"use client";
// app/admin/ai-gateway/ExperimentsSection.tsx — AA-900 UI (S262 slice C4): the read-only Experiments section of
// AI providers › Models & trials. One read, GET /api/admin/ai-gateway/experiments (backend
// api/services/ai_gateway_experiments.py): the prompt register (shared.prompt_version, CIS migration 321; "not
// ready" before it) and the newest evaluation runs (shared.prompt_eval_runs). The shadow comparisons sit above it in
// the same tab (ShadowReport). Nothing here starts a run or writes anything. Kit only.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Badge, DataTable, DateText, ErrorState, K, Section, apiGet, mono, useT, type ColumnDef,
} from "../../_kit";
import {
  EXPERIMENTS_API, usd, verdictTone, type EvalRunRow, type ExperimentsResp, type PromptVersionRow,
} from "@/lib/aiGateway";
import { EXPERIMENTS } from "./experiments.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function PromptVersions({ data, loading }: { data?: ExperimentsResp["prompt_versions"]; loading: boolean }) {
  const t = useT(EXPERIMENTS);
  const columns = useMemo<ColumnDef<PromptVersionRow, unknown>[]>(() => [
    { accessorKey: "stage", header: t("colStage"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12, color: K.ink }}>{c.row.original.stage}</span> },
    { accessorKey: "version", header: t("colVersion"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.version}</span> },
    { id: "sha", header: t("colFingerprint"), enableSorting: false,
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12, color: K.muted }}>{c.row.original.sha_short}</span> },
    { id: "created", header: t("colRecorded"), accessorFn: (r) => r.created_at ?? "",
      cell: (c) => <DateText value={c.row.original.created_at} relative /> },
    { id: "notes", header: t("colNotes"), enableSorting: false,
      cell: (c) => <span style={small}>{c.row.original.notes ?? "—"}</span> },
  ], [t]);
  if (data && !data.ready) return <div style={small}>{data.error ?? t("promptsNotReady")}</div>;
  return (
    <DataTable<PromptVersionRow>
      tableId="ai-gateway-prompt-versions" data={data?.rows ?? []} columns={columns}
      getRowId={(r) => `${r.stage}:${r.version}`} loading={loading} searchable={false} pageSize={25}
      pageSizeOptions={[25, 50, 100]} stickyHeader={false} tableMinWidth={640}
      emptyTitle={t("promptsEmpty")} emptyDescription={t("promptsEmptyBody")}
    />
  );
}

function EvalRuns({ data, loading }: { data?: ExperimentsResp["eval_runs"]; loading: boolean }) {
  const t = useT(EXPERIMENTS);
  const columns = useMemo<ColumnDef<EvalRunRow, unknown>[]>(() => [
    { id: "run", header: t("colRun"), accessorFn: (r) => `${r.pipeline} ${r.run_kind}`,
      cell: ({ row: { original: r } }) => (
        <div style={{ display: "grid", gap: 2, minWidth: 140 }}>
          <span style={{ fontFamily: mono, fontSize: 12, color: K.ink }}>{r.pipeline}</span>
          <span style={small}>{r.run_kind}{r.writer ? ` · ${r.writer}` : ""}</span>
        </div>
      ) },
    { id: "compared", header: t("colCompared"), accessorFn: (r) => r.prompt_version,
      cell: ({ row: { original: r } }) => (
        <div style={{ display: "grid", gap: 2, minWidth: 120 }}>
          <span style={{ fontFamily: mono, fontSize: 12 }}>{r.prompt_version}</span>
          {r.baseline_prompt_version && <span style={small}>{t("vsBaseline", r.baseline_prompt_version)}</span>}
        </div>
      ) },
    { accessorKey: "tour_count", header: t("colTours"), meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.tour_count}</span> },
    { id: "score", header: t("colScore"), accessorFn: (r) => r.avg_quality_score ?? -1, meta: { align: "right" },
      cell: ({ row: { original: r } }) => (
        <span style={{ fontFamily: mono }}>
          {r.avg_quality_score == null ? "—" : r.avg_quality_score.toFixed(2)}
          {r.baseline_avg_quality_score != null && (
            <span style={{ color: K.muted }}> / {r.baseline_avg_quality_score.toFixed(2)}</span>
          )}
        </span>
      ) },
    { id: "gates", header: t("colGates"), enableSorting: false, meta: { align: "right" },
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono }}>
        {r.gate_pass_count ?? "—"} / {r.gate_fail_count ?? "—"}</span> },
    { id: "verdict", header: t("colVerdict"), accessorFn: (r) => r.verdict ?? "",
      cell: ({ row: { original: r } }) => (
        <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
          <Badge tone={verdictTone(r.verdict, r.regression_detected)} dot={false} style={{ whiteSpace: "nowrap" }}>
            {r.verdict ?? (r.finished_at ? "—" : t("running"))}
          </Badge>
          {r.regression_detected && (
            <Badge tone="danger" dot={false} style={{ whiteSpace: "nowrap" }}>{t("regression")}</Badge>
          )}
        </span>
      ) },
    { id: "cost", header: t("colCost"), accessorFn: (r) => r.cost_usd ?? -1, meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.cost_usd)}</span> },
    { id: "when", header: t("colWhen"), accessorFn: (r) => r.created_at ?? "",
      cell: (c) => <span style={{ whiteSpace: "nowrap" }}><DateText value={c.row.original.created_at} relative /></span> },
  ], [t]);
  if (data && !data.ready) return <div style={{ ...small, color: K.danger }}>{data.error ?? t("runsNotReady")}</div>;
  return (
    <DataTable<EvalRunRow>
      tableId="ai-gateway-eval-runs" data={data?.rows ?? []} columns={columns} getRowId={(r) => r.id}
      loading={loading} searchable={false} pageSize={25} pageSizeOptions={[25, 50]} stickyHeader={false}
      tableMinWidth={860} emptyTitle={t("runsEmpty")}
    />
  );
}

export function ExperimentsSection() {
  const t = useT(EXPERIMENTS);
  const q = useQuery({
    queryKey: ["ai-gateway", "experiments"],
    queryFn: () => apiGet<ExperimentsResp>(EXPERIMENTS_API, { admin: true }),
  });
  return (
    <Section title={t("title")} description={t("intro")} testId="ai-gateway-experiments">
      {q.error ? (
        <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 18 }}>
          <div style={{ display: "grid", gap: 8, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("promptsTitle")}</div>
            <div style={small}>{t("promptsHelp")}</div>
            <PromptVersions data={q.data?.prompt_versions} loading={q.isLoading} />
          </div>
          <div style={{ display: "grid", gap: 8, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("runsTitle")}</div>
            <div style={small}>{t("runsHelp", q.data?.eval_runs.limit ?? 50)}</div>
            <EvalRuns data={q.data?.eval_runs} loading={q.isLoading} />
          </div>
        </div>
      )}
    </Section>
  );
}
