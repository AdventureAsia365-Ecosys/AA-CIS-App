"use client";
// app/admin/ai-gateway/StageCoverageCard.tsx
// AA-1026 (S267) — "does every stage have a model that answers right now?" on AI providers › Routing, under the LLM
// availability card. Reads `stage_coverage` of GET /api/admin/llm-availability (same query as LlmAvailabilityCard, so
// one request): per active stage the live model (answered within the proof window, no refusal or open circuit since)
// or NO LIVE MODEL, with why. A stage without one also raises the ops check bell + e-mail (shared/ops_checks.py).
// Built on the kit only (Section, Badge, DataTable, tokens).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Badge, DataTable, K, Section, Spinner, apiGet, mono, useT, type ColumnDef, type Translator,
} from "../../_kit";
import { MODELS } from "./models.messages";
import { type CoverageStage, type LlmAvailability, coverageReasons, coverageRows } from "@/lib/llmAvailability";

type T = Translator<typeof MODELS.en>;

const num = { fontFamily: mono, fontSize: 12.5, color: K.ink } as const;

function columns(t: T): ColumnDef<CoverageStage, unknown>[] {
  return [
    { accessorKey: "stage", header: t("coverageStage"), cell: (c) => <span style={num}>{c.row.original.stage}</span> },
    {
      accessorKey: "role",
      header: t("coverageRole"),
      cell: (c) => <span style={{ fontSize: 12.5, color: K.body }}>{c.row.original.role ?? "—"}</span>,
    },
    {
      id: "live",
      accessorFn: (r) => (r.status === "live" ? `${r.live_model}@${r.live_connection}` : ""),
      header: t("coverageLiveNow"),
      cell: (c) => {
        const r = c.row.original;
        return r.status === "live" ? (
          <span style={{ ...num, overflowWrap: "anywhere" }}>{r.live_model}@{r.live_connection}</span>
        ) : (
          <Badge tone="danger">{t("coverageNoLiveModel")}</Badge>
        );
      },
    },
    {
      id: "why",
      header: t("coverageNotes"),
      enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        const reasons = r.status === "live" ? [] : coverageReasons(r);
        return (
          <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>
            {reasons.length > 0 && <span style={{ fontFamily: mono }}>{reasons.join(" · ")}</span>}
            {reasons.length > 0 && (r.unproven ?? 0) > 0 && " · "}
            {(r.unproven ?? 0) > 0 && t("coverageUnproven", r.unproven ?? 0)}
          </span>
        );
      },
    },
  ];
}

export function StageCoverageCard() {
  const t = useT(MODELS);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["llm-availability", 24],
    queryFn: () => apiGet<LlmAvailability>("/api/admin/llm-availability?hours=24", { admin: true }),
    refetchInterval: 60_000,
  });
  const cols = useMemo(() => columns(t), [t]);
  const report = data?.stage_coverage;
  const rows = useMemo(() => coverageRows(report), [report]);

  if (isLoading && !data) {
    return <Section title={t("coverageTitle")} testId="llm-stage-coverage"><Spinner /></Section>;
  }
  if (isError || !data) {
    return (
      <Section title={t("coverageTitle")} testId="llm-stage-coverage">
        <div style={{ fontSize: 12.5, color: K.danger }}>{t("availLoadError")}</div>
      </Section>
    );
  }
  if (!report || report.ready === false) {
    return (
      <Section title={t("coverageTitle")} testId="llm-stage-coverage">
        <div style={{ fontSize: 12.5, color: K.muted }}>{t("coverageNotReady")}</div>
      </Section>
    );
  }
  const missing = rows.filter((r) => r.status !== "live").length;
  const badge = missing > 0
    ? <Badge tone="danger">{t("coverageMissing", missing)}</Badge>
    : <Badge tone="success">{t("coverageAllLive", rows.length)}</Badge>;
  return (
    <Section
      testId="llm-stage-coverage"
      title={t("coverageTitle")}
      description={t("coverageIntro", report.proof_max_age_hours ?? 36)}
      actions={<span data-testid="llm-stage-coverage-status" style={{ display: "inline-flex" }}>{badge}</span>}
    >
      <DataTable
        tableId="llm-stage-coverage"
        data={rows}
        columns={cols}
        getRowId={(r) => r.stage}
        paginate={false}
        compact
        tableMinWidth={560}
        emptyTitle={t("coverageEmpty")}
      />
    </Section>
  );
}
