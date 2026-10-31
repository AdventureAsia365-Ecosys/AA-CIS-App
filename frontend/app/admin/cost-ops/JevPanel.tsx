"use client";
// app/admin/cost-ops/JevPanel.tsx — AA-665 C1: Cost Operations › Jev. The Jev (TypeSafe) spend and credit in one place,
// reusing the Jev Decisions endpoints: GET /api/admin/decisions/summary?days=N (billed calls and verdicts per stage;
// days 1–90) and GET /api/admin/decisions/jev-credit (top-ups and the estimated credit left; spend.read = admin).
// Question settings stay on /admin/decisions; top-ups are recorded in AI providers › Providers › Jev credit.
// AA-994: text in the UI language (panels.messages.ts).

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo } from "react";
import { DataTable, ErrorState, K, SkeletonBar, apiGet, mono, useFormat, useT, type ColumnDef } from "../../_kit";
import { aiGatewayHref } from "@/lib/aiGateway";
import { PanelTitle, Stat, int, noteStyle, numStyle, panelStyle, statGrid, usd } from "./costOpsUi";
import { PANELS } from "./panels.messages";

interface StageCalls { stage: string; calls: number; cost_usd: number; tokens_in: number }
interface StageVerdicts {
  stage: string; questions: number; verdicts: number; acted: number; errors: number; skipped: number; cached: number;
  cost_usd: number; last_used_at: string | null;
}
interface Summary {
  days: number;
  calls_by_stage: StageCalls[];
  stages: StageVerdicts[];
  total_cost_usd: number;
  total_calls: number;
}
interface Credit {
  total_topped_up_usd: number | null;
  first_topup_on: string | null;
  spent_since_first_topup_usd: number | null;
  estimated_left_usd: number | null;
  low_threshold_usd: number;
  below_low_threshold: boolean;
  last_low_alert_at: string | null;
  last_exhausted_alert_at: string | null;
}
interface StageRow extends StageVerdicts { calls: number; billed_usd: number }

const linkStyle = { color: K.accentDeep, fontWeight: 600 } as const;

export default function JevPanel({ days }: { days: number }) {
  const t = useT(PANELS);
  const { formatDate, formatDateTime } = useFormat();
  const d = Math.min(90, Math.max(1, Math.round(days)));
  const summary = useQuery({
    queryKey: ["cost-ops-jev-summary", d],
    queryFn: () => apiGet<Summary>(`/api/admin/decisions/summary?days=${d}`, { admin: true }),
  });
  const credit = useQuery({
    queryKey: ["cost-ops-jev-credit"],
    queryFn: () => apiGet<Credit>("/api/admin/decisions/jev-credit", { admin: true }),
  });

  const rows = useMemo<StageRow[]>(() => {
    const s = summary.data;
    if (!s) return [];
    const calls = new Map(s.calls_by_stage.map((c) => [c.stage, c]));
    const out = s.stages.map((st) => ({
      ...st, calls: calls.get(st.stage)?.calls ?? 0, billed_usd: calls.get(st.stage)?.cost_usd ?? 0,
    }));
    return out.sort((a, b) => b.billed_usd - a.billed_usd);
  }, [summary.data]);

  const columns = useMemo<ColumnDef<StageRow, unknown>[]>(() => [
    { accessorKey: "stage", header: t("colStage"), cell: (c) => <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.stage}</span> },
    {
      accessorKey: "billed_usd", header: t("colBilled"), meta: { align: "right" },
      cell: (c) => <span style={{ ...numStyle, fontWeight: 600 }}>{usd(c.row.original.billed_usd, 4)}</span>,
    },
    { accessorKey: "calls", header: t("colBilledCalls"), meta: { align: "right" }, cell: (c) => <span style={numStyle}>{int(c.row.original.calls)}</span> },
    { accessorKey: "verdicts", header: t("colVerdicts"), meta: { align: "right" }, cell: (c) => <span style={numStyle}>{int(c.row.original.verdicts)}</span> },
    { accessorKey: "cached", header: t("colFromCache"), meta: { align: "right" }, cell: (c) => <span style={numStyle}>{int(c.row.original.cached)}</span> },
    { accessorKey: "acted", header: t("colActedOn"), meta: { align: "right" }, cell: (c) => <span style={numStyle}>{int(c.row.original.acted)}</span> },
    {
      accessorKey: "errors", header: t("colErrors"), meta: { align: "right" },
      cell: (c) => <span style={{ ...numStyle, color: c.row.original.errors ? K.danger : undefined }}>{int(c.row.original.errors)}</span>,
    },
    { id: "last", header: t("colLastUsed"), cell: (c) => formatDateTime(c.row.original.last_used_at) },
  ], [t, formatDateTime]);

  const cr = credit.data;
  return (
    <div style={panelStyle} data-testid="cost-ops-jev">
      <PanelTitle aside={
        <span style={noteStyle}>
          <Link href="/admin/decisions" style={linkStyle}>{t("jevDecisions")}</Link>
          {" · "}
          <Link href={aiGatewayHref("providers", "jev-credit")} style={linkStyle}>{t("recordTopup")}</Link>
        </span>
      }>
        {t("jevTitle", d)}
      </PanelTitle>
      {d !== days && <div style={{ ...noteStyle, marginBottom: 8 }}>{t("jevMax90")}</div>}
      <div style={statGrid}>
        <Stat label={t("jevSpend")} value={summary.data ? usd(summary.data.total_cost_usd, 4) : "—"}
              sub={summary.data ? t("billedCalls", int(summary.data.total_calls)) : undefined} />
        <Stat label={t("creditLeft")} value={cr ? usd(cr.estimated_left_usd) : "—"}
              tone={cr?.below_low_threshold ? "danger" : "success"}
              sub={cr ? t("alertBelow", usd(cr.low_threshold_usd)) : undefined} />
        <Stat label={t("toppedUp")} value={cr ? usd(cr.total_topped_up_usd) : "—"}
              sub={cr?.first_topup_on ? t("since", formatDate(cr.first_topup_on)) : t("noTopup")} />
        <Stat label={t("spentSinceTopup")} value={cr ? usd(cr.spent_since_first_topup_usd, 4) : "—"}
              sub={cr?.last_low_alert_at ? t("lastLowAlert", formatDateTime(cr.last_low_alert_at)) : undefined} />
      </div>
      {credit.isError && <div style={{ ...noteStyle, color: K.danger, marginBottom: 8 }}>{t("creditLoadError")}</div>}
      {summary.isLoading && <div style={{ display: "flex", flexDirection: "column", gap: 8 }}><SkeletonBar /><SkeletonBar w="80%" /></div>}
      {summary.isError && (
        <ErrorState message={t("summaryLoadError", (summary.error as Error)?.message ?? "")}
                    onRetry={() => summary.refetch()} />
      )}
      {summary.data && (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)" }}>
          <DataTable<StageRow>
            tableId="cost-ops-jev-stages"
            data={rows}
            columns={columns}
            getRowId={(r) => r.stage}
            paginate={false}
            tableMinWidth={860}
            compact
            emptyTitle={t("noJevCalls")}
            emptyDescription={t("noJevDays", d)}
          />
        </div>
      )}
      <div style={{ ...noteStyle, marginTop: 10 }}>
        {t("jevFootnote")}
      </div>
    </div>
  );
}
