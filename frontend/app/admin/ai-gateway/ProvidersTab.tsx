"use client";
// app/admin/ai-gateway/ProvidersTab.tsx — AA-804 (UI part 1): the Providers tab of AI providers.
//
// One row per paid source from GET /api/admin/ai-gateway/providers (backend api/services/ai_gateway.py): every LLM
// connection, Jev, DataForSEO and Geoapify, in three kit Sections (LLM connections, Decisions, Data providers) above
// three summary tiles. Each row: status (+ why), health, spend today / this month, the budget (monthly per
// connection, the provider-wide daily cap), and the balance or today's credits. The overview is read-only.
// AA-804 UI part 2: two more views of the tab (`?view=`): Connections (ConnectionsPanel: switch a connection on or
// off, limits, monthly budget, test call; secret name and base URL read-only) and Jev credit (JevCreditPanel: the
// top-up ledger), both moved here from Settings. A Bedrock connection is named by its AWS account ID
// (lib/llmConnections connectionName + useAwsAccounts), never by a key.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Badge, DataTable, DateText, ErrorState, K, KpiTile, Section, Tabs, apiGet, mono, useT, type ColumnDef,
} from "../../_kit";
import {
  PROVIDERS_API, PROVIDER_VIEWS, groupRows, needsLook, pct, statusTone, usd,
  type ProviderRow, type ProviderView, type ProvidersResp,
} from "@/lib/aiGateway";
import { connectionName } from "@/lib/llmConnections";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { ConnectionsPanel } from "./ConnectionsPanel";
import { JevCreditPanel } from "./JevCreditPanel";
import { M, REASON, STATUS } from "./messages";

export const PROVIDERS_KEY = ["ai-gateway", "providers"] as const;

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function Lines({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "grid", gap: 2, minWidth: 0 }}>{children}</div>;
}

function ProviderCell({ row }: { row: ProviderRow }) {
  const t = useT(M);
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const sub = row.group === "llm" && row.connection_key
    ? connectionName({ connection_key: row.connection_key, kind: row.provider, account_id: row.account_id },
      dir, accountText)
    : null;
  return (
    <Lines>
      <span style={{ fontWeight: 600, color: K.ink }}>{row.label}</span>
      {sub && <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>{sub}</span>}
      <span style={small}>
        {row.data_scope === "aws_only" ? t("scopeAws") : row.data_scope === "vendor_direct" ? t("scopeVendor") : ""}
        {!row.enabled ? `${row.data_scope ? " · " : ""}${t("off")}` : ""}
      </span>
    </Lines>
  );
}

function StatusCell({ row }: { row: ProviderRow }) {
  const ts = useT(STATUS);
  const tr = useT(REASON);
  return (
    <Lines>
      <span><Badge tone={statusTone(row.status)}>{ts(row.status)}</Badge></span>
      {row.status_reason && row.status_reason in REASON.en && (
        <span style={small}>{tr(row.status_reason as keyof typeof REASON.en)}</span>
      )}
      {row.error && <span style={{ ...small, color: K.danger }}>{row.error}</span>}
    </Lines>
  );
}

function HealthCell({ row }: { row: ProviderRow }) {
  const t = useT(M);
  const h = row.health;
  if (!h) return <span style={small}>—</span>;
  if (row.group === "decide") {
    return (
      <Lines>
        <span style={small}>{t("jevCalls", h.calls ?? 0, h.errors ?? 0)}</span>
        {h.mean_latency_ms != null && <span style={small}>{t("meanLatency", h.mean_latency_ms)}</span>}
      </Lines>
    );
  }
  return (
    <Lines>
      {h.last_ok_at
        ? <span style={small}>{t("lastAnswer")} <DateText value={h.last_ok_at} relative /></span>
        : <span style={small}>{t("noCalls")}</span>}
      {h.last_error_class && <span style={{ ...small, color: K.amber }}>{t("lastError", h.last_error_class)}</span>}
      {!!h.open_models && <span style={{ ...small, color: K.danger }}>{t("openModels", h.open_models)}</span>}
      {!h.open_models && !!h.failing_models && (
        <span style={{ ...small, color: K.amber }}>{t("failingModels", h.failing_models)}</span>
      )}
    </Lines>
  );
}

function BudgetCell({ row }: { row: ProviderRow }) {
  const t = useT(M);
  const m = row.monthly_budget;
  const d = row.daily_cap;
  if (!m && !d) return <span style={small}>{t("noCap")}</span>;
  return (
    <Lines>
      {m && (
        <span style={small}>
          {t("monthly")} <strong style={{ color: K.ink }}>{usd(m.month_spent_usd)}</strong>{" "}
          {t("ofCap", usd(m.monthly_budget_usd))} · {pct(m.used_pct)}
        </span>
      )}
      {d && (
        <span style={small}>
          {t("daily")} <strong style={{ color: K.ink }}>{usd(d.spent_today_usd)}</strong>{" "}
          {t("ofCap", usd(d.per_day_usd))} · {pct(d.used_pct)} · {d.hard_stop ? t("hardStop") : t("alertOnly")}
        </span>
      )}
    </Lines>
  );
}

function BalanceCell({ row }: { row: ProviderRow }) {
  const t = useT(M);
  const b = row.balance;
  const c = row.credits;
  if (c) {
    return (
      <Lines>
        <span style={{ fontFamily: mono, color: K.ink }}>{t("creditsToday", c.used_today, c.daily_limit)}</span>
        <span style={small}>{pct(c.used_pct)}</span>
      </Lines>
    );
  }
  if (!b) return <span style={small}>—</span>;
  if (b.usd == null) return <span style={small}>{t("notRead")}</span>;
  return (
    <Lines>
      <span>
        <strong style={{ fontFamily: mono, color: b.below_threshold ? K.danger : K.ink }}>{usd(b.usd, 2)}</strong>{" "}
        <span style={small}>{t("left")}{b.estimated ? ` (${t("estimated")})` : ""}</span>
      </span>
      <span style={small}>{t("threshold", usd(b.threshold_usd, 2))}</span>
      {b.as_of && <span style={small}>{t("asOf")} <DateText value={b.as_of} relative /></span>}
    </Lines>
  );
}

function SpendCell({ row, which }: { row: ProviderRow; which: "today" | "month" }) {
  const t = useT(M);
  const s = row.spend;
  if (!s) return <span style={small}>—</span>;
  if (which === "today") return <span style={{ fontFamily: mono }}>{usd(s.today_usd)}</span>;
  const live = row.live_calls_month;
  return (
    <Lines>
      <span style={{ fontFamily: mono, color: K.ink }}>{usd(s.month_usd)}</span>
      <span style={small}>{t("calls", s.month_calls)}{live != null ? ` · ${t("liveCalls", live)}` : ""}</span>
    </Lines>
  );
}

function ProviderTable({ id, rows, loading, empty }: {
  id: string; rows: ProviderRow[]; loading: boolean; empty?: string;
}) {
  const t = useT(M);
  const columns = useMemo<ColumnDef<ProviderRow, unknown>[]>(() => [
    { id: "provider", header: t("colProvider"), accessorKey: "label", cell: ({ row }) => <ProviderCell row={row.original} /> },
    { id: "status", header: t("colStatus"), accessorKey: "status", cell: ({ row }) => <StatusCell row={row.original} /> },
    { id: "health", header: t("colHealth"), enableSorting: false, cell: ({ row }) => <HealthCell row={row.original} /> },
    { id: "today", header: t("colToday"), accessorFn: (r) => r.spend?.today_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <SpendCell row={row.original} which="today" /> },
    { id: "month", header: t("colMonth"), accessorFn: (r) => r.spend?.month_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <SpendCell row={row.original} which="month" /> },
    { id: "budget", header: t("colBudget"), enableSorting: false, cell: ({ row }) => <BudgetCell row={row.original} /> },
    { id: "balance", header: t("colBalance"), enableSorting: false, cell: ({ row }) => <BalanceCell row={row.original} /> },
  ], [t]);
  return (
    <DataTable<ProviderRow>
      tableId={id} data={rows} columns={columns} getRowId={(r) => r.key} loading={loading}
      searchable={false} paginate={false} stickyHeader={false} tableMinWidth={980}
      emptyTitle={empty ?? "—"}
    />
  );
}

function ProvidersOverview() {
  const t = useT(M);
  const q = useQuery({
    queryKey: PROVIDERS_KEY,
    queryFn: () => apiGet<ProvidersResp>(PROVIDERS_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
  const data = q.data;
  const groups = useMemo(() => groupRows(data?.providers ?? []), [data]);
  const look = data ? needsLook(data.providers) : undefined;

  if (q.error) {
    return <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />;
  }

  // minmax(0, 1fr): a DataTable with tableMinWidth must not widen the column (lessons: Frontend, S254).
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}
      data-testid={data ? "ai-gateway-ready" : undefined}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(220px, 100%), 1fr))", gap: 12 }}>
        <KpiTile label={t("tileMonth")} value={data ? usd(data.totals.month_usd, 2) : undefined}
          hint={data ? t("tileMonthHint", data.month_start.slice(0, 10)) : undefined} />
        <KpiTile label={t("tileToday")} value={data ? usd(data.totals.today_usd, 2) : undefined}
          hint={t("tileTodayHint")} />
        <KpiTile label={t("tileLook")} value={look}
          valueTone={look ? "warning" : undefined}
          hint={data ? (look ? t("tileLookHint", data.providers.length) : t("tileAllOk")) : undefined} />
      </div>
      {data && data.errors.length > 0 && (
        <div role="status" style={{ ...small, color: K.danger }}>{t("sourceErrors")}</div>
      )}
      <Section title={t("groupLlm")} description={t("groupLlmHelp")} padded={false}>
        <ProviderTable id="ai-gateway-llm" rows={groups.llm} loading={!data} empty={t("emptyLlm")} />
        {data?.unattributed_llm && data.unattributed_llm.month_calls > 0 && (
          <div style={{ ...small, padding: "10px 16px", borderTop: `1px solid ${K.line}` }}>
            <strong style={{ color: K.ink }}>{t("unattributed")}</strong>{" "}
            <span style={{ fontFamily: mono }}>
              {usd(data.unattributed_llm.today_usd)} · {usd(data.unattributed_llm.month_usd)}
            </span>{" "}
            · {t("calls", data.unattributed_llm.month_calls)}. {t("unattributedHelp")}
          </div>
        )}
      </Section>
      <Section title={t("groupDecide")} description={t("groupDecideHelp")} padded={false}>
        <ProviderTable id="ai-gateway-decide" rows={groups.decide} loading={!data} />
      </Section>
      <Section title={t("groupData")} description={t("groupDataHelp")} padded={false}>
        <ProviderTable id="ai-gateway-data" rows={groups.data} loading={!data} />
      </Section>
      <div style={small}>{t("traceHint")}</div>
    </div>
  );
}

const VIEW_LABEL = { overview: "viewOverview", connections: "viewConnections", "jev-credit": "viewJevCredit" } as const;

/** The Providers tab: the overview (default) or one of its two editing views, picked with underline tabs. */
export default function ProvidersTab({ view, onView }: { view: ProviderView; onView: (v: ProviderView) => void }) {
  const t = useT(M);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <Tabs variant="underline" ariaLabel={t("viewsLabel")} testId="ai-gateway-provider-views"
        tabs={PROVIDER_VIEWS.map((v) => ({ key: v, label: t(VIEW_LABEL[v]) }))}
        active={view} onChange={(k) => onView(k as ProviderView)} />
      {view === "overview" && <ProvidersOverview />}
      {view === "connections" && <ConnectionsPanel />}
      {view === "jev-credit" && <JevCreditPanel />}
    </div>
  );
}
