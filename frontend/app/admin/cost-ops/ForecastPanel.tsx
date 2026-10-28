"use client";
// app/admin/cost-ops/ForecastPanel.tsx — AA-665 C3: Cost Operations › Overview, month-end forecast vs budget.
//
// API: GET /api/admin/cost-ops/forecast (backend api/services/cost_ops.py cost_forecast; spend.read = admin). DB only:
// the AWS rows read the STORED Cost Explorer snapshots (AWS bill tab › "Refresh from AWS" stores new days), never a
// live CE call. Always the current UTC month; the page's date window does not apply.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Badge, DataTable, ErrorState, K, SkeletonBar, apiGet, formatDate, formatDateTime, type ColumnDef,
} from "../../_kit";
import { PanelTitle, STATUS_META, noteStyle, numStyle, panelStyle, usd, type BudgetStatus } from "./costOpsUi";

interface Projection {
  mtd_usd: number | null;
  run_rate_usd_per_day: number | null;
  days_left: number | null;
  forecast_usd: number | null;
  budget_usd: number | null;
  budget_source: string | null;
  alert_pct: number | null;
  status: BudgetStatus;
  forecast_pct_of_budget: number | null;
}
interface ProviderRow extends Projection {
  key: string;
  label: string;
  accounts?: ({ account: string; label: string } & Pick<Projection, "mtd_usd" | "run_rate_usd_per_day" | "forecast_usd">)[];
}
interface AwsRow extends Projection {
  account: string;
  label: string;
  covered_from: string | null;
  covered_to: string | null;
  fetched_at: string | null;
  days_missing: number | null;
}
interface ForecastResp {
  month: string;
  days_in_month: number;
  complete_days: number;
  run_rate_days: number;
  is_current_month: boolean;
  providers: ProviderRow[];
  providers_total: { mtd_usd: number; forecast_usd: number };
  aws_accounts: AwsRow[];
  generated_at: string;
  sources: Record<string, string>;
}

function StatusCell({ r }: { r: Projection }) {
  const m = STATUS_META[r.status] ?? STATUS_META.no_data;
  return (
    <span title={r.budget_source ?? undefined} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <Badge tone={m.tone}>{m.label}</Badge>
      {r.forecast_pct_of_budget != null && (
        <span style={{ ...numStyle, fontSize: 12, color: K.muted }}>{Math.round(r.forecast_pct_of_budget)}%</span>
      )}
    </span>
  );
}

function money<T>(key: keyof Projection, header: string): ColumnDef<T, unknown> {
  return {
    id: String(key), header, meta: { align: "right" },
    accessorFn: (r) => (r as unknown as Projection)[key],
    cell: (c) => <span style={numStyle}>{usd(c.getValue() as number | null)}</span>,
  };
}

function useColumns() {
  const providers = useMemo<ColumnDef<ProviderRow, unknown>[]>(() => [
    {
      accessorKey: "label", header: "Provider",
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{r.label}</div>
            {r.accounts && r.accounts.length > 0 && (
              <div style={{ fontSize: 11.5, color: K.muted }}>
                {r.accounts.map((a) => `${a.label} ${usd(a.forecast_usd)}`).join(" · ")}
              </div>
            )}
          </div>
        );
      },
    },
    money<ProviderRow>("mtd_usd", "Month to date"),
    money<ProviderRow>("run_rate_usd_per_day", "Per day (7-day rate)"),
    money<ProviderRow>("forecast_usd", "Month-end forecast"),
    money<ProviderRow>("budget_usd", "Budget"),
    { id: "status", header: "Status", cell: (c) => <StatusCell r={c.row.original} /> },
  ], []);
  const aws = useMemo<ColumnDef<AwsRow, unknown>[]>(() => [
    {
      accessorKey: "label", header: "AWS account",
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{r.label}</div>
            <div style={{ fontSize: 11.5, color: r.days_missing ? K.amber : K.muted }}>
              {r.covered_to
                ? `Stored to ${formatDate(r.covered_to)}${r.days_missing ? ` · ${r.days_missing} day(s) missing` : ""}`
                : "No stored Cost Explorer days this month"}
            </div>
          </div>
        );
      },
    },
    money<AwsRow>("mtd_usd", "Month to date"),
    money<AwsRow>("run_rate_usd_per_day", "Per day (7-day rate)"),
    money<AwsRow>("forecast_usd", "Month-end forecast"),
    money<AwsRow>("budget_usd", "Budget"),
    { id: "status", header: "Status", cell: (c) => <StatusCell r={c.row.original} /> },
  ], []);
  return { providers, aws };
}

export default function ForecastPanel() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["cost-ops-forecast"],
    queryFn: () => apiGet<ForecastResp>("/api/admin/cost-ops/forecast", { admin: true }),
  });
  const cols = useColumns();

  if (isLoading) {
    return (
      <div style={{ ...panelStyle, display: "flex", flexDirection: "column", gap: 10 }}>
        <SkeletonBar w="40%" h={16} />
        <SkeletonBar />
        <SkeletonBar w="85%" />
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div style={panelStyle}>
        <ErrorState message={`Could not load the forecast. ${(error as Error)?.message ?? ""}`}
                    onRetry={() => refetch()} />
      </div>
    );
  }
  const over = [...data.providers, ...data.aws_accounts].filter((r) => r.status === "over");
  return (
    <div style={panelStyle} data-testid="cost-ops-forecast">
      <PanelTitle aside={<span style={noteStyle}>As of {formatDateTime(data.generated_at)}</span>}>
        Month-end forecast · {data.month}
      </PanelTitle>
      <div style={{ ...noteStyle, marginBottom: 12 }}>
        {data.complete_days} of {data.days_in_month} days complete. Forecast = spend of the complete days + the mean of
        the last {data.run_rate_days} complete days × the days left.
        {over.length > 0 && (
          <span style={{ color: K.danger, fontWeight: 600 }}>
            {" "}Over budget at this rate: {over.map((r) => r.label).join(", ")}.
          </span>
        )}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
        <DataTable<ProviderRow>
          tableId="cost-ops-forecast-providers"
          data={data.providers}
          columns={cols.providers}
          getRowId={(r) => r.key}
          paginate={false}
          tableMinWidth={720}
          compact
          emptyTitle="No spend this month"
        />
        <DataTable<AwsRow>
          tableId="cost-ops-forecast-aws"
          data={data.aws_accounts}
          columns={cols.aws}
          getRowId={(r) => r.account}
          paginate={false}
          tableMinWidth={720}
          compact
          emptyTitle="No AWS accounts"
        />
      </div>
      <div style={{ ...noteStyle, marginTop: 10 }}>
        AI providers: gateway estimates (llm_call_log, llm_call_daily) and dfs_call_log; budget = the provider&apos;s
        daily cap on the Budgets tab × days in the month. AWS accounts: the stored Cost Explorer days (AWS bill tab), which
        lag 1–2 days; budget = the account&apos;s AWS Budget.
      </div>
    </div>
  );
}
