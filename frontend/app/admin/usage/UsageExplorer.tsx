"use client";
// app/admin/usage/UsageExplorer.tsx — AA-1045: AI & spend › Usage explorer.
//
// Requests, tokens, cost, latency, error and fallback rates of every paid AI and data provider over any period, by one
// or two dimensions, from GET /api/admin/usage-explorer/query (backend api/services/usage_explorer.py: the hourly
// rollup of the CIS ledgers plus the CRM's daily totals). Every control lives in the address (kit useUrlFilters), so a
// view can be shared, saved under a name (the caller's own saved views, GET / POST / DELETE .../views) and exported
// as CSV. A bar of the chart or a row's "Calls" link opens the matching calls on the AI gateway Calls tab
// (lib/usageExplorer drillHref), whose rows open the Trace drawer. Times are Vietnam time (ICT); the data stays UTC.
// The invoice card puts each provider's ledger month next to its own figure (the nightly reconciliation).
// Kit only (PageFrame, FilterBar, Section, KpiTile, DataTable, Drawer, Modal, ChipButton, SegmentedControl).
// AI & spend roles only (lib/rbac.ts PAGE_RULES /admin/usage; backend spend.read).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Filter, RefreshCw, Save, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Badge, Button, CHART_TOOLTIP, ChipButton, DataTable, Drawer, ErrorState, Field, FilterBar, FilterField, Input,
  K, KpiTile, LinkButton, Modal, PageFrame, PageHeader, Section, SegmentedControl, Select, Switch, ToastProvider,
  apiGet, apiSend, downloadCsv, filterControlStyle, mono, toCsv, useT, useToast,
  useUrlFilters, type ColumnDef, type FilterSelect,
} from "../../_kit";
import { useChartColors } from "../../_kit/useTheme";
import {
  DEFAULTS, DIMENSIONS, GRANULARITIES, METRICS, NONE, RANGES, VIEWS_API, csvColumns, csvName, drillHref,
  filterValues, fmtIct, fmtInt, fmtMetric, fmtMs, fmtPct, fmtUsd, groupOf, invoicesPath, lastMonths, optionsPath,
  queryPath, readQuery, toggleValue, viewHref, type Dimension, type ExplorerResp, type ExplorerRow, type GroupKey,
  type InvoiceRow, type InvoicesResp, type Metric, type OptionsResp, type SavedView,
} from "@/lib/usageExplorer";
import { DIM, GRAN, M, MET, RANGE } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
type ChartMetric = "requests" | "tokens" | "cost_usd";
const SORTABLE = new Set<string>(METRICS);

function ApiError(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function UsageExplorer() {
  return (
    <ToastProvider>
      <Explorer />
    </ToastProvider>
  );
}

function Explorer() {
  const t = useT(M);
  const tr = useT(RANGE);
  const tg = useT(GRAN);
  const td = useT(DIM);
  const tm = useT(MET);
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();
  const { get, set, setMany } = useUrlFilters();
  const q = readQuery(get);
  const key = JSON.stringify(q);
  const group = groupOf(q);
  const range = q.range || DEFAULTS.range;
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [viewName, setViewName] = useState("");
  const [chartMetric, setChartMetric] = useState<ChartMetric>("cost_usd");
  const [month, setMonth] = useState(() => lastMonths()[1]);
  const chart = useChartColors({ accent: "--aa-accent", line: "--aa-line", line2: "--aa-line2", muted: "--aa-muted" });

  const data = useQuery({
    queryKey: ["usage-explorer", "query", key],
    queryFn: () => apiGet<ExplorerResp>(queryPath(q), { admin: true }),
    staleTime: 60_000,
  });
  const options = useQuery({
    queryKey: ["usage-explorer", "options", q.range ?? "", q.from ?? "", q.to ?? ""],
    queryFn: () => apiGet<OptionsResp>(optionsPath(q), { admin: true }),
    staleTime: 300_000,
    enabled: filtersOpen,
  });
  const views = useQuery({
    queryKey: ["usage-explorer", "views"],
    queryFn: () => apiGet<{ views: SavedView[]; limit: number }>(VIEWS_API, { admin: true }),
    staleTime: 300_000,
  });
  const invoices = useQuery({
    queryKey: ["usage-explorer", "invoices", month],
    queryFn: () => apiGet<InvoicesResp>(invoicesPath(month), { admin: true }),
    staleTime: 300_000,
  });

  const saveView = useMutation({
    mutationFn: (name: string) => apiSend<SavedView>(VIEWS_API, { body: { name, query: q }, admin: true }),
    onSuccess: (v) => {
      toast.success(t("viewSaved", v.name));
      setSaveOpen(false);
      void qc.invalidateQueries({ queryKey: ["usage-explorer", "views"] });
    },
    onError: (e) => toast.error(t("viewError", ApiError(e))),
  });
  const deleteView = useMutation({
    mutationFn: (id: string) => apiSend<{ deleted: boolean }>(`${VIEWS_API}/${id}`, { method: "DELETE", admin: true }),
    onSuccess: () => {
      toast.success(t("viewDeleted"));
      void qc.invalidateQueries({ queryKey: ["usage-explorer", "views"] });
    },
    onError: (e) => toast.error(ApiError(e)),
  });

  const resp = data.data;
  const tenantNames = useMemo(() => ({ ...(options.data?.labels.tenant ?? {}), ...(resp?.labels.tenant ?? {}) }),
    [options.data, resp]);
  const filters = resp?.filters ?? {};
  const activeView = views.data?.views.find((v) => viewHref(v.query) === viewHref(q));

  const dimText = (d: GroupKey, v: string | undefined) => {
    if (v == null || v === "" || v === NONE) return t("noneValue");
    if (d === "tenant") return tenantNames[v] ?? v.slice(0, 8);
    return v;
  };

  const drill = (row: Pick<ExplorerRow, "dims" | "bucket">) =>
    resp ? drillHref(resp.range, row.dims, filters, row.bucket) : null;

  // ── controls ──
  const dimOptions = (withTime: boolean) => [
    ...(withTime ? [{ value: "time", label: td("time") }] : []),
    ...DIMENSIONS.map((d) => ({ value: d, label: td(d) })),
  ];
  const selects: FilterSelect[] = [
    { key: "range", label: t("range"), allLabel: tr("7d"), width: 160,
      options: RANGES.filter((r) => r !== "7d").map((r) => ({ value: r, label: tr(r) })) },
    { key: "granularity", label: t("granularity"), allLabel: tg("day"), width: 110,
      options: GRANULARITIES.filter((g) => g !== "day").map((g) => ({ value: g, label: tg(g) })) },
  ];
  const setGroup = (i: 0 | 1, value: string) => {
    const next = [...group];
    if (i === 0) next[0] = (value || DEFAULTS.group) as GroupKey;
    else if (value) next[1] = value as GroupKey;
    else next.splice(1);
    const uniq = [...new Set(next)];
    const joined = uniq.join(",");
    const sortKey = q.sort && !SORTABLE.has(q.sort) && !uniq.includes(q.sort as GroupKey) ? null : (q.sort ?? null);
    setMany({ group: joined === DEFAULTS.group ? null : joined, sort: sortKey });
  };

  // ── table ──
  const groupKey = group.join(",");
  const perUnit = q.per_unit ?? "";
  const sorting = [{ id: q.sort || DEFAULTS.sort, desc: (q.dir || DEFAULTS.dir) === "desc" }];
  const metricCols: Metric[] = [
    "requests", "tokens_in", "tokens_out", "cache_read_tokens", "reasoning_tokens", "cost_usd", "avg_latency_ms",
    "p95_latency_ms", "error_rate", "fallback_rate", ...(q.per_unit ? (["cost_per_tour", "cost_per_piece"] as Metric[])
      : []),
  ];
  const columns = useMemo<ColumnDef<ExplorerRow, unknown>[]>(() => [
    ...group.map((g): ColumnDef<ExplorerRow, unknown> => ({
      id: g, header: td(g), accessorFn: (r) => r.dims[g] ?? "",
      cell: ({ row }) => (
        <span style={{ fontFamily: g === "tenant" || g === "time" ? undefined : mono, fontSize: 12 }}
          title={row.original.dims[g] || undefined}>
          {dimText(g, row.original.dims[g])}
        </span>
      ),
    })),
    ...metricCols.map((m): ColumnDef<ExplorerRow, unknown> => ({
      id: m, header: tm(m), accessorFn: (r) => r[m] ?? -1, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{fmtMetric(m, row.original[m])}</span>,
    })),
    { id: "calls", header: t("calls"), enableSorting: false,
      cell: ({ row }) => {
        const link = drill(row.original);
        if (!link) return null;
        const lost = link.lost.map((d) => td(d as Dimension)).join(", ");
        return (
          <span data-testid="usage-explorer-drill" style={{ display: "inline-flex" }}>
            <LinkButton size="sm" onClick={() => router.push(link.href)}
              title={lost ? `${t("callsTitle")}. ${t("callsLost", lost)}` : t("callsTitle")}>
              {t("calls")}{lost ? " *" : ""}</LinkButton>
          </span>
        );
      } },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [groupKey, perUnit, resp, tenantNames, t, td, tm]);

  const exportCsv = () => {
    if (!resp) return;
    downloadCsv(csvName(q), toCsv(resp.rows, csvColumns(group, tenantNames)));
  };

  const series = resp?.series ?? [];
  const chartValue = (p: (typeof series)[number]) => chartMetric === "cost_usd" ? p.cost_usd
    : chartMetric === "tokens" ? p.tokens : p.requests;
  const chartFmt = (v: number) => chartMetric === "cost_usd" ? fmtUsd(v) : fmtInt(v);

  const crmLine = (() => {
    const c = resp?.crm;
    if (!c) return null;
    if (c.status === "ok") return t("crmMerged", c.rows ?? 0);
    if (c.reason === "hour_granularity") return t("crmHour");
    if (c.reason?.startsWith("filter_")) return t("crmFilter");
    if (c.reason === "cognito_session_required") return t("crmNoSession");
    if (c.status === "not_asked") return null;
    return t("crmUnavailable", c.reason ?? c.status);
  })();
  const needsMigration = data.isError && ApiError(data.error).includes("372");

  const activeFilters = DIMENSIONS.flatMap((d) => filterValues(q, d).map((v) => ({ d, v })));

  return (
    <PageFrame>
      <PageHeader title={t("title")}
        breadcrumbs={[{ label: t("crumbGroup") }, { label: t("title") }]}
        description={t("description")}
        actions={
          <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
            <Button variant="ghost" onClick={() => void qc.invalidateQueries({ queryKey: ["usage-explorer"] })}>
              <RefreshCw size={14} /> {t("refresh")}</Button>
            <span data-testid="usage-explorer-csv" style={{ display: "inline-flex" }}>
              <Button variant="secondary" disabled={!resp || resp.rows.length === 0} onClick={exportCsv}>
                <Download size={14} /> {t("exportCsv")}</Button>
            </span>
          </span>
        } />

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}
        data-testid={resp || data.isError ? "usage-explorer-ready" : "usage-explorer"}>
        <FilterBar selects={selects} testId="usage-explorer-filters"
          dateRange={range === "custom" ? { fromKey: "from", toKey: "to", label: t("customRange") } : undefined}
          extraKeys={["group", "sort", "dir", "per_unit", ...DIMENSIONS.map((d) => `f_${d}`)]}
          tools={
            <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Select size="sm" aria-label={t("views")} value={activeView?.id ?? ""} style={{ width: 190 }}
                placeholder={t("noView")}
                options={(views.data?.views ?? []).map((v) => ({ value: v.id, label: v.name }))}
                onChange={(e) => {
                  const v = views.data?.views.find((x) => x.id === e.target.value);
                  if (v) router.replace(viewHref(v.query), { scroll: false });
                }} />
              <span data-testid="usage-explorer-save-view" style={{ display: "inline-flex" }}>
                <Button size="sm" variant="secondary"
                  onClick={() => { setViewName(activeView?.name ?? ""); setSaveOpen(true); }}>
                  <Save size={13} /> {t("saveView")}</Button>
              </span>
              {activeView && (
                <Button size="sm" variant="ghost" title={t("deleteView")} ariaLabel={t("deleteView")}
                  disabled={deleteView.isPending} onClick={() => deleteView.mutate(activeView.id)}>
                  <Trash2 size={13} /></Button>
              )}
            </span>
          }>
          <FilterField label={t("group1")}>
            <Select size="sm" value={group[0]} options={dimOptions(true)} style={{ width: 180 }}
              onChange={(e) => setGroup(0, e.target.value)} />
          </FilterField>
          <FilterField label={t("group2")}>
            <Select size="sm" value={group[1] ?? ""} placeholder={t("noGroup2")} style={{ width: 180 }}
              options={dimOptions(true).filter((o) => o.value !== group[0])}
              onChange={(e) => setGroup(1, e.target.value)} />
          </FilterField>
          <span data-testid="usage-explorer-open-filters" style={{ display: "inline-flex" }}>
            <Button size="sm" variant="secondary" onClick={() => setFiltersOpen(true)}>
              <Filter size={13} /> {t("filters")}{activeFilters.length ? ` (${activeFilters.length})` : ""}</Button>
          </span>
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }} title={t("perUnitHint")}>
            <Switch checked={!!q.per_unit} label={t("perUnit")} onChange={(on) => set("per_unit", on ? "1" : null)} />
            <span style={small}>{t("perUnit")}</span>
          </span>
        </FilterBar>

        {activeFilters.length > 0 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}
            data-testid="usage-explorer-active-filters">
            {activeFilters.map(({ d, v }) => (
              <ChipButton key={`${d}:${v}`} size="sm" selected
                onClick={() => set(`f_${d}`, toggleValue(q, d, v))} title={t("clear")}>
                {td(d)}: {dimText(d, v)} ×
              </ChipButton>
            ))}
            <LinkButton tone="muted" size="sm"
              onClick={() => setMany(Object.fromEntries(DIMENSIONS.map((d) => [`f_${d}`, null])))}>
              {t("clearAll")}</LinkButton>
          </div>
        )}

        {data.isError ? (
          needsMigration ? <div style={small} role="status">{t("needsMigration")}</div>
            : <ErrorState message={`${t("loadError")}: ${ApiError(data.error)}`} onRetry={() => void data.refetch()} />
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 170px), 1fr))",
              gap: 10 }} data-testid="usage-explorer-kpis">
              <KpiTile size="sm" label={t("kpiRequests")} value={resp ? fmtInt(resp.totals.requests) : undefined} />
              <KpiTile size="sm" label={t("kpiTokens")}
                value={resp ? `${fmtInt(resp.totals.tokens_in)} / ${fmtInt(resp.totals.tokens_out)}` : undefined} />
              <KpiTile size="sm" label={t("kpiCost")} value={resp ? fmtUsd(resp.totals.cost_usd) : undefined} />
              <KpiTile size="sm" label={t("kpiLatency")} value={resp
                ? `${fmtMs(resp.totals.avg_latency_ms)} / ${fmtMs(resp.totals.p95_latency_ms)}` : undefined} />
              <KpiTile size="sm" label={t("kpiErrors")} value={resp ? fmtPct(resp.totals.error_rate) : undefined}
                valueTone={resp && (resp.totals.error_rate ?? 0) > 0.05 ? "danger" : undefined} />
              <KpiTile size="sm" label={t("kpiFallback")} info={t("kpiFallbackInfo")}
                value={resp ? fmtPct(resp.totals.fallback_rate) : undefined} />
            </div>

            <div style={small} data-testid="usage-explorer-freshness">
              {resp && (resp.rollup ? t("rolledUp", fmtIct(resp.rollup.finished_at)) : t("neverRolled"))}
              {resp && <> {fmtIct(resp.range.from)} – {fmtIct(resp.range.to)}.</>}
              {crmLine && <> {crmLine}</>}
              {resp?.notes.includes("per_unit_outside_raw_window") && <> {t("perUnitOutside")}</>}
              {resp?.notes.includes("per_unit_llm_only") && <> {t("perUnitLlmOnly")}</>}
            </div>

            <Section title={t("chartTitle")} testId="usage-explorer-chart"
              actions={
                <SegmentedControl<ChartMetric> label={t("chartTitle")} size="sm" value={chartMetric}
                  onChange={setChartMetric} options={[
                    { value: "requests", label: t("chartRequests") },
                    { value: "tokens", label: t("chartTokens") },
                    { value: "cost_usd", label: t("chartCost") },
                  ]} />
              }>
              <div style={{ ...small, marginBottom: 6 }}>{t("chartHint")}</div>
              <div style={{ width: "100%", height: 240 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={series.map((p) => ({ ...p, value: chartValue(p) }))}
                    margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={chart.line2} vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 11, fill: chart.muted }} stroke={chart.line}
                      minTickGap={16} />
                    <YAxis tick={{ fontSize: 11, fill: chart.muted }} stroke={chart.line} width={64}
                      tickFormatter={(v) => chartFmt(Number(v) || 0)} />
                    <Tooltip {...CHART_TOOLTIP} formatter={(v) => chartFmt(Number(v) || 0)} />
                    <Bar dataKey="value" fill={chart.accent} radius={[3, 3, 0, 0]} cursor="pointer"
                      name={chartMetric === "cost_usd" ? t("chartCost") : chartMetric === "tokens" ? t("chartTokens")
                        : t("chartRequests")}
                      onClick={(p) => {
                        type Point = { start?: string; end?: string; label?: string };
                        const item = p as unknown as Point & { payload?: Point };
                        const point = item?.payload ?? item;
                        if (!point?.start || !point.end) return;
                        const link = drill({ dims: {}, bucket: { start: point.start, end: point.end,
                          label: point.label ?? "" } });
                        if (link) router.push(link.href);
                      }} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Section>

            <Section title={t("tableTitle")} padded={false} testId="usage-explorer-table"
              description={resp ? t("rowsInfo", resp.rows.length, resp.groups) : undefined}>
              {resp?.truncated && <div style={{ ...small, padding: "8px 14px" }}>{t("truncated")}</div>}
              <DataTable<ExplorerRow>
                tableId="usage-explorer" data={resp?.rows ?? []} columns={columns} getRowId={(r) => r.key}
                loading={data.isLoading} searchable={false} paginate={false} stickyHeader={false}
                tableMinWidth={1100} emptyTitle={t("empty")} manualSorting sorting={sorting}
                onSortingChange={(next) => {
                  const s = next[0];
                  if (!s) return setMany({ sort: null, dir: null });
                  setMany({ sort: s.id === DEFAULTS.sort ? null : s.id, dir: s.desc ? null : "asc" });
                }} />
            </Section>
          </>
        )}

        <InvoiceCard month={month} onMonth={setMonth} data={invoices.data} loading={invoices.isLoading}
          error={invoices.isError ? ApiError(invoices.error) : null} />
      </div>

      <Drawer open={filtersOpen} onClose={() => setFiltersOpen(false)} title={t("filtersTitle")} width={520}
        footer={
          <span style={{ display: "flex", justifyContent: "space-between", width: "100%" }}>
            <LinkButton tone="muted" onClick={() => setMany(Object.fromEntries(DIMENSIONS.map((d) => [`f_${d}`,
              null])))}>{t("clearAll")}</LinkButton>
            <Button variant="primary" onClick={() => setFiltersOpen(false)}>{t("done")}</Button>
          </span>
        }>
        <div style={{ display: "grid", gap: 16 }} data-testid="usage-explorer-filter-drawer">
          <div style={small}>{t("filtersHint")}</div>
          {options.isError && <ErrorState message={ApiError(options.error)} onRetry={() => void options.refetch()} />}
          {DIMENSIONS.map((d) => {
            const chosen = filterValues(q, d);
            const values = [...new Set([...(options.data?.values[d] ?? []), ...chosen])];
            return (
              <div key={d} style={{ display: "grid", gap: 6 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: K.ink3 }}>{td(d)}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} role="group" aria-label={td(d)}>
                  {values.length === 0 && <span style={small}>{options.isLoading ? "…" : t("filtersNone")}</span>}
                  {values.map((v) => (
                    <ChipButton key={v} size="sm" role="checkbox" selected={chosen.includes(v)}
                      onClick={() => set(`f_${d}`, toggleValue(q, d, v))} title={v}>
                      {dimText(d, v)}
                    </ChipButton>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </Drawer>

      <Modal open={saveOpen} onClose={() => setSaveOpen(false)} title={t("saveView")} testId="usage-explorer-save-modal"
        footer={
          <>
            <Button variant="ghost" onClick={() => setSaveOpen(false)}>{t("cancel")}</Button>
            <Button variant="primary" disabled={!viewName.trim() || saveView.isPending}
              onClick={() => saveView.mutate(viewName.trim())}>{t("save")}</Button>
          </>
        }>
        <Field label={t("viewName")} hint={t("viewNameHint")} required>
          <Input value={viewName} maxLength={80} onChange={(e) => setViewName(e.target.value)}
            style={{ width: "100%" }} />
        </Field>
      </Modal>
    </PageFrame>
  );
}

function InvoiceCard({ month, onMonth, data, loading, error }: {
  month: string; onMonth: (m: string) => void; data?: InvoicesResp; loading: boolean; error: string | null;
}) {
  const t = useT(M);
  const columns = useMemo<ColumnDef<InvoiceRow, unknown>[]>(() => [
    { id: "provider", header: t("colProvider"), accessorKey: "provider",
      cell: ({ row }) => <Badge dot={false}>{row.original.provider}</Badge> },
    { id: "account", header: t("colAccount"), accessorFn: (r) => r.account ?? "",
      cell: ({ row }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{row.original.account ?? "—"}</span> },
    { id: "requests", header: t("colRequests"), accessorKey: "requests", meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{fmtInt(row.original.requests)}</span> },
    { id: "ledger", header: t("colLedger"), accessorKey: "ledger_usd", meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{fmtUsd(row.original.ledger_usd)}</span> },
    { id: "provider_usd", header: t("colProviderFigure"), accessorFn: (r) => r.provider_usd ?? -1,
      meta: { align: "right" },
      cell: ({ row: { original: r } }) => (
        <span style={{ fontFamily: mono }} title={r.provider_source.join(", ") || undefined}>
          {r.provider_usd == null ? "—" : `${fmtUsd(r.provider_usd)} (${r.provider_days} d)`}</span>
      ) },
    { id: "invoice", header: t("colInvoice"), accessorFn: (r) => r.invoice_usd ?? -1, meta: { align: "right" },
      cell: ({ row }) => <span style={{ fontFamily: mono }}>{fmtUsd(row.original.invoice_usd)}</span> },
  ], [t]);
  return (
    <Section title={t("invoiceTitle")} description={t("invoiceHint")} padded={false} testId="usage-explorer-invoices"
      actions={
        <Select size="sm" aria-label={t("month")} value={month} style={{ ...filterControlStyle, width: 120 }}
          options={lastMonths().map((m) => ({ value: m, label: m }))} onChange={(e) => onMonth(e.target.value)} />
      }>
      {error ? <ErrorState message={error} /> : (
        <>
          <DataTable<InvoiceRow> tableId="usage-explorer-invoices" data={data?.rows ?? []} columns={columns}
            getRowId={(r) => `${r.provider}:${r.account ?? ""}`} loading={loading} searchable={false}
            paginate={false} stickyHeader={false} tableMinWidth={720} emptyTitle={t("empty")} />
          <div style={{ ...small, padding: "8px 14px" }}>{t("invoiceMissing")}</div>
        </>
      )}
    </Section>
  );
}
