"use client";
// app/admin/cost-ops/ForecastPanel.tsx — AA-665 C3: Cost Operations › Overview, month-end forecast vs budget.
//
// API: GET /api/admin/cost-ops/forecast (backend api/services/cost_ops.py cost_forecast; spend.read = admin). DB only:
// the AWS rows read the STORED Cost Explorer snapshots (AWS bill tab › "Refresh from AWS" stores new days), never a
// live CE call. Always the current UTC month; the page's date window does not apply.
// AA-994: text in the UI language (panels.messages.ts); provider and account labels come from the API.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Badge, DataTable, ErrorState, K, SkeletonBar, apiGet, useFormat, useT, type ColumnDef, type Translator,
} from "../../_kit";
import { PanelTitle, STATUS_META, noteStyle, numStyle, panelStyle, usd, type BudgetStatus } from "./costOpsUi";
import { PANELS } from "./panels.messages";

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
  const t = useT(PANELS);
  const m = STATUS_META[r.status] ?? STATUS_META.no_data;
  return (
    <span title={r.budget_source ?? undefined} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <Badge tone={m.tone}>{t(m.label)}</Badge>
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

function useColumns(t: Translator<typeof PANELS.en>) {
  const { formatDate } = useFormat();
  const providers = useMemo<ColumnDef<ProviderRow, unknown>[]>(() => [
    {
      accessorKey: "label", header: t("colProvider"),
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
    money<ProviderRow>("mtd_usd", t("colMtd")),
    money<ProviderRow>("run_rate_usd_per_day", t("colPerDay")),
    money<ProviderRow>("forecast_usd", t("colForecast")),
    money<ProviderRow>("budget_usd", t("colBudget")),
    { id: "status", header: t("colStatus"), cell: (c) => <StatusCell r={c.row.original} /> },
  ], [t]);
  const aws = useMemo<ColumnDef<AwsRow, unknown>[]>(() => [
    {
      accessorKey: "label", header: t("colAwsAccount"),
      cell: (c) => {
        const r = c.row.original;
        return (
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{r.label}</div>
            <div style={{ fontSize: 11.5, color: r.days_missing ? K.amber : K.muted }}>
              {r.covered_to ? t("storedTo", formatDate(r.covered_to), r.days_missing ?? 0) : t("noStoredDays")}
            </div>
          </div>
        );
      },
    },
    money<AwsRow>("mtd_usd", t("colMtd")),
    money<AwsRow>("run_rate_usd_per_day", t("colPerDay")),
    money<AwsRow>("forecast_usd", t("colForecast")),
    money<AwsRow>("budget_usd", t("colBudget")),
    { id: "status", header: t("colStatus"), cell: (c) => <StatusCell r={c.row.original} /> },
  ], [t, formatDate]);
  return { providers, aws };
}

export default function ForecastPanel() {
  const t = useT(PANELS);
  const { formatDateTime } = useFormat();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["cost-ops-forecast"],
    queryFn: () => apiGet<ForecastResp>("/api/admin/cost-ops/forecast", { admin: true }),
  });
  const cols = useColumns(t);

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
        <ErrorState message={t("forecastLoadError", (error as Error)?.message ?? "")}
                    onRetry={() => refetch()} />
      </div>
    );
  }
  const over = [...data.providers, ...data.aws_accounts].filter((r) => r.status === "over");
  return (
    <div style={panelStyle} data-testid="cost-ops-forecast">
      <PanelTitle aside={<span style={noteStyle}>{t("asOf", formatDateTime(data.generated_at))}</span>}>
        {t("forecastTitle", data.month)}
      </PanelTitle>
      <div style={{ ...noteStyle, marginBottom: 12 }}>
        {t("forecastNote", data.complete_days, data.days_in_month, data.run_rate_days)}
        {over.length > 0 && (
          <span style={{ color: K.danger, fontWeight: 600 }}>
            {" "}{t("overAtRate", over.map((r) => r.label).join(", "))}
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
          emptyTitle={t("noSpendMonth")}
        />
        <DataTable<AwsRow>
          tableId="cost-ops-forecast-aws"
          data={data.aws_accounts}
          columns={cols.aws}
          getRowId={(r) => r.account}
          paginate={false}
          tableMinWidth={720}
          compact
          emptyTitle={t("noAwsAccounts")}
        />
      </div>
      <div style={{ ...noteStyle, marginTop: 10 }}>
        {t("forecastFootnote")}
      </div>
    </div>
  );
}
