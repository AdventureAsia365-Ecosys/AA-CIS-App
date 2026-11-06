"use client";
// app/admin/ai-gateway/CallsTab.tsx — AA-804 (UI part 3): the Calls tab of AI providers.
//
// Every logged paid call, newest first, from GET /api/admin/ai-gateway/calls (backend
// api/services/ai_gateway_calls.py): each LLM attempt (ledger v2: a refused attempt is a $0 row with its class), each
// Jev verdict, each DataForSEO and geocoder call, and the CRM's own AI calls (crm.ops.v1 GET /llm/calls, read with
// the staff member's own sign-in; when it cannot be read the tab says why and shows the CRM's totals instead).
// Filters live in the address (kit useUrlFilters, `c_` keys from lib/aiGateway CALL_PARAM) so a filtered list can be
// shared; a job or tour filter is set from a row or a link, never typed. "Load older calls" follows the keyset cursor.
// A row with a job opens the page's Trace drawer. Kit only (FilterBar, KpiTile, LatencyStrip, DataTable, Badge).
// Accounts are named through lib/awsAccounts accountName (an AWS account ID or the vendor API's name), never by key.

import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  Badge, Button, DataTable, DateText, ErrorState, FilterBar, K, KpiTile, LatencyStrip, LinkButton, apiGet, mono,
  useT, useUrlFilters, type ColumnDef, type FilterSelect,
} from "../../_kit";
import {
  CALL_APPS, CALL_DRILL_KEYS, CALL_PARAM, CALL_PROVIDERS, CALL_SOURCES, CALL_STATUSES, budgetProviderLabel,
  callFiltersFrom,
  callTone, callsPath, usd, type CallRow, type CallsResp, type TraceTarget,
} from "@/lib/aiGateway";
import { accountName } from "@/lib/awsAccounts";
import type { RouteTarget } from "@/lib/routeTrace";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { APP, CALLS, WINDOW } from "./calls.messages";
import { OUTCOME, SOURCE } from "./messages";
import { useErrorClassText } from "./useErrorClassText";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const PAGE = 100;

function short(id: string): string {
  return id.slice(0, 8);
}

export default function CallsTab({ onTrace, onRoute }: {
  onTrace: (t: TraceTarget) => void;
  /** AA-1042: open the row's gateway request on the Trace tab */
  onRoute?: (t: RouteTarget) => void;
}) {
  const t = useT(CALLS);
  const tw = useT(WINDOW);
  const ta = useT(APP);
  const tsrc = useT(SOURCE);
  const tout = useT(OUTCOME);
  const errorText = useErrorClassText();
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const { get, setMany } = useUrlFilters();
  const filters = callFiltersFrom(get);
  const key = JSON.stringify(filters);

  const list = useInfiniteQuery({
    queryKey: ["ai-gateway", "calls", key],
    queryFn: ({ pageParam }) => apiGet<CallsResp>(callsPath(filters, pageParam, PAGE), { admin: true }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    staleTime: 30_000,
  });
  const rows = useMemo(() => list.data?.pages.flatMap((p) => p.rows) ?? [], [list.data]);
  const first = list.data?.pages[0];
  const errors = useMemo(() => {
    const seen = new Map<string, string>();
    for (const p of list.data?.pages ?? []) for (const e of p.errors) seen.set(e.source, e.error);
    return [...seen.entries()];
  }, [list.data]);

  const selects: FilterSelect[] = [
    { key: CALL_PARAM.window, label: t("filterWindow"), allLabel: tw("24h"), width: 150,
      options: (["1h", "7d", "30d"] as const).map((w) => ({ value: w, label: tw(w) })) },
    { key: CALL_PARAM.source, label: t("filterSource"), allLabel: t("allSources"), width: 140,
      options: CALL_SOURCES.map((s) => ({ value: s, label: tsrc(s) })) },
    { key: CALL_PARAM.app, label: t("filterApp"), allLabel: t("allApps"), width: 140,
      options: CALL_APPS.map((a) => ({ value: a, label: ta(a) })) },
    { key: CALL_PARAM.provider, label: t("filterProvider"), allLabel: t("allProviders"), width: 170,
      options: CALL_PROVIDERS.map((p) => ({ value: p, label: budgetProviderLabel(p) })) },
    { key: CALL_PARAM.status, label: t("filterStatus"), allLabel: t("allStatuses"), width: 160,
      options: CALL_STATUSES.map((s) => ({ value: s, label: tout(s) })) },
  ];

  const columns = useMemo<ColumnDef<CallRow, unknown>[]>(() => [
    { id: "at", header: t("colWhen"), accessorKey: "at",
      cell: ({ row }) => <span style={{ whiteSpace: "nowrap" }}><DateText value={row.original.at} withTime /></span> },
    { id: "source", header: t("colLog"), accessorKey: "source",
      cell: ({ row }) => <Badge dot={false}>{tsrc(row.original.source)}</Badge> },
    { id: "app", header: t("colApp"), accessorFn: (r) => r.app ?? "",
      cell: ({ row }) => {
        const a = row.original.app;
        return <span>{a && a in APP.en ? ta(a as keyof typeof APP.en) : (a ?? "—")}</span>;
      } },
    { id: "stage", header: t("colStage"), accessorFn: (r) => r.stage ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.stage ?? "—"}</span> },
    { id: "what", header: t("colWhat"), accessorFn: (r) => r.what ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.what ?? "—"}</span> },
    { id: "model", header: t("colModel"), accessorFn: (r) => r.model ?? r.provider ?? "",
      cell: ({ row: { original: e } }) => {
        const who = e.account ? accountName(e.account, dir, accountText) : "";
        return (
          <span style={{ fontFamily: mono, fontSize: 12 }}>{e.model ?? e.provider ?? "—"}{who ? ` · ${who}` : ""}</span>
        );
      } },
    { id: "outcome", header: t("colOutcome"), accessorFn: (r) => r.outcome ?? "",
      cell: ({ row: { original: e } }) => {
        const known = e.outcome != null && e.outcome in OUTCOME.en;
        return (
          <span style={{ display: "grid", gap: 2, justifyItems: "start" }}>
            <Badge tone={e.expected_refusal ? "neutral" : callTone(e.outcome)} dot={false}>
              {known ? tout(e.outcome as keyof typeof OUTCOME.en) : (e.outcome ?? "—")}
            </Badge>
            {e.expected_refusal && <span style={small}>{t("expectedRefusal")}</span>}
            {e.error_class && <span style={small}>{errorText(e.error_class)}</span>}
          </span>
        );
      } },
    { id: "cost", header: t("colCost"), accessorFn: (r) => r.cost_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{usd(row.original.cost_usd)}</span> },
    { id: "latency", header: t("colLatency"), accessorFn: (r) => r.latency_ms ?? -1, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>
        {row.original.latency_ms == null ? "—" : `${row.original.latency_ms} ms`}</span> },
    { id: "tokens", header: t("colTokens"), accessorFn: (r) => (r.tokens_in ?? 0) + (r.tokens_out ?? 0),
      meta: { align: "right" },
      cell: ({ row: { original: e } }) => <span style={{ fontFamily: mono }}>
        {e.tokens_in == null && e.tokens_out == null ? "—" : `${e.tokens_in ?? 0} / ${e.tokens_out ?? 0}`}</span> },
    { id: "trace", header: t("colTrace"), enableSorting: false,
      cell: ({ row: { original: e } }) => {
        // AA-1042: the row's gateway request (every attempt it needed) on the Trace tab
        const rt = typeof e.detail?.route_trace === "string" ? e.detail.route_trace : null;
        const route = rt && onRoute ? (
          <span data-testid="ai-gateway-calls-route" style={{ display: "inline-flex" }}>
            <LinkButton tone="muted" onClick={() => onRoute({ id: rt, at: e.at })} title={t("openRouteTitle")}>
              {t("openRoute")}</LinkButton>
          </span>
        ) : null;
        return e.job_id ? (
        <span style={{ display: "inline-flex", gap: 10, whiteSpace: "nowrap" }}>
          <span data-testid="ai-gateway-calls-trace" style={{ display: "inline-flex" }}>
            <LinkButton onClick={() => onTrace({ kind: "job", id: e.job_id as string })}
              title={t("openTraceTitle")}>{t("openTrace")}</LinkButton>
          </span>
          {!filters.job_id && (
            <LinkButton tone="muted" onClick={() => setMany({ [CALL_PARAM.job_id]: e.job_id })}>
              {t("onlyThisJob")}</LinkButton>
          )}
          {route}
        </span>
      ) : route ?? <span style={small}>{t("noJob")}</span>;
      } },
  ], [t, ta, tsrc, tout, errorText, dir, accountText, onTrace, onRoute, filters.job_id, setMany]);

  const cost = rows.reduce((s, r) => s + (r.cost_usd ?? 0), 0);
  // AA-900: an expected refusal (a model with no access yet) is counted apart, never as a failure
  const expected = rows.filter((r) => r.expected_refusal).length;
  const failed = rows.filter((r) => (r.status === "refused" || r.status === "error") && !r.expected_refusal).length;
  const crm = first?.crm;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}
      data-testid={list.data || list.isError ? "ai-gateway-calls-ready" : "ai-gateway-calls"}>
      <div style={small}>{t("intro")}</div>
      <FilterBar search={{ key: CALL_PARAM.stage, placeholder: t("stageSearch") }} selects={selects}
        extraKeys={[CALL_PARAM.job_id, CALL_PARAM.tour_id, ...CALL_DRILL_KEYS.map((k) => CALL_PARAM[k])]}
        testId="ai-gateway-calls-filters" />
      {CALL_DRILL_KEYS.some((k) => filters[k]) && (
        // AA-1045: the usage explorer's drill-down (a cell's range and dimensions), cleared in one click
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}
          data-testid="ai-gateway-calls-drill">
          <span style={small}>{t("drillTitle")}</span>
          {filters.since && filters.until && (
            <Badge tone="accent" dot={false}>
              <DateText value={filters.since} withTime /> – <DateText value={filters.until} withTime />
            </Badge>
          )}
          {CALL_DRILL_KEYS.filter((k) => k !== "since" && k !== "until" && filters[k]).map((k) => (
            <Badge key={k} tone="accent" dot={false}>{t("drillDim", k, filters[k] as string)}</Badge>
          ))}
          <LinkButton tone="muted" onClick={() => setMany(Object.fromEntries(
            CALL_DRILL_KEYS.map((k) => [CALL_PARAM[k], null])))}>{t("clearFilter")}</LinkButton>
        </div>
      )}
      {(filters.job_id || filters.tour_id) && (
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          {filters.job_id && (
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <Badge tone="accent" dot={false}>{t("onlyJob", short(filters.job_id))}</Badge>
              <LinkButton tone="muted" onClick={() => setMany({ [CALL_PARAM.job_id]: null })}>
                {t("clearFilter")}</LinkButton>
            </span>
          )}
          {filters.tour_id && (
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <Badge tone="accent" dot={false}>{t("onlyTour", short(filters.tour_id))}</Badge>
              <LinkButton tone="muted" onClick={() => setMany({ [CALL_PARAM.tour_id]: null })}>
                {t("clearFilter")}</LinkButton>
            </span>
          )}
        </div>
      )}
      {list.isError ? (
        <ErrorState message={`${t("loadError")}: ${(list.error as Error).message}`} onRetry={() => void list.refetch()} />
      ) : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 170px), 1fr))",
            gap: 10 }}>
            <KpiTile size="sm" label={t("kpiShown")} info={t("kpiShownInfo")}
              value={list.isLoading ? undefined : rows.length} testId="ai-gateway-calls-kpi-shown" />
            <KpiTile size="sm" label={t("kpiCost")} value={list.isLoading ? undefined : usd(cost)} />
            <KpiTile size="sm" label={t("kpiFailed")} info={t("kpiFailedInfo")}
              value={list.isLoading ? undefined : failed} valueTone={failed ? "danger" : undefined} />
            <KpiTile size="sm" label={t("kpiExpected")} info={t("kpiExpectedInfo")}
              value={list.isLoading ? undefined : expected} testId="ai-gateway-calls-kpi-expected" />
          </div>
          {rows.length > 0 && (
            <div>
              <div style={{ ...small, marginBottom: 4 }}>{t("latencyTitle")}</div>
              <LatencyStrip testId="ai-gateway-calls-latency"
                points={[...rows].reverse().map((r) => ({ ms: r.latency_ms, tone: (r.status === "refused"
                  || r.status === "error") && !r.expected_refusal ? "danger" : "info" }))} />
            </div>
          )}
          {crm && (
            <div style={small} data-testid="ai-gateway-calls-crm">
              {crm.status === "ok" && t("crmOk", crm.rows ?? 0)}
              {crm.status === "not_asked" && t("crmNotAskedFiltered")}
              {crm.status !== "ok" && crm.status !== "not_asked" && (
                crm.reason === "cognito_session_required" ? t("crmNoSession")
                  : t("crmUnavailable", crm.reason ?? crm.status)
              )}
              {crm.aggregate && <> {t("crmAggregate", crm.aggregate.calls, usd(crm.aggregate.cost_usd),
                crm.aggregate.from, crm.aggregate.to)}</>}
            </div>
          )}
          {errors.map(([source, error]) => (
            <div key={source} role="alert" style={{ ...small, color: K.danger }}>
              {t("sourceError", source in SOURCE.en ? tsrc(source as keyof typeof SOURCE.en) : source, error)}
            </div>
          ))}
          <DataTable<CallRow>
            tableId="ai-gateway-calls" data={rows} columns={columns} getRowId={(r) => r.id}
            loading={list.isLoading} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={1100}
            emptyTitle={t("empty")}
          />
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {list.hasNextPage ? (
              <span data-testid="ai-gateway-calls-more" style={{ display: "inline-flex" }}>
                <Button variant="secondary" size="sm" disabled={list.isFetchingNextPage}
                  onClick={() => list.fetchNextPage()}>
                  {list.isFetchingNextPage ? t("loadingMore") : t("loadMore")}</Button>
              </span>
            ) : (!list.isLoading && rows.length > 0 && <span style={small}>{t("end")}</span>)}
          </div>
        </>
      )}
    </div>
  );
}
