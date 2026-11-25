"use client";
// app/admin/overview/_status/Agencies.tsx — AA-1142 (P2): which agency an AI spend, a failed job or a failed AI call
// comes from, over the page's time range. Without a filter: three rankings of at most cis.ops.status_tenant_top_n
// agencies (the API says how many) and the platform's own share; with the agency filter (URL key `tenant`): that
// agency's AI by app and stage, failed AI calls by class and failed jobs by kind (no error text: a job error may quote
// tenant content). Server errors per agency and the CRM's jobs per agency are "not measured yet" (no source).
// GET /api/admin/overview/status/tenants (ops.read). Kit only.

import { useQuery } from "@tanstack/react-query";
import { usePathname } from "next/navigation";
import { DataTable, K, Select, apiGet, useFormat, useLang, useUrlFilters, type ColumnDef } from "../../../_kit";
import { fmtNum, type View } from "@/lib/opsStatus";
import { agencyLabel, tenantParam, tenantsQuery, type AgencyRef, type TenantsResp } from "@/lib/opsStatusP2";
import { fmtUsd } from "@/lib/numberFormat";
import { Card, NotMeasured, rowWrap, small } from "./ui";
import { useP2 } from "./Slo";

type PT = ReturnType<typeof useP2>;
const grid = { display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))" };

function nameCell(a: AgencyRef) {
  return <b style={{ color: K.ink, overflowWrap: "anywhere" }}>{agencyLabel(a)}</b>;
}

function Ranking<T extends AgencyRef>({ title, rows, cols, testId, t }: {
  title: string; rows: T[]; cols: ColumnDef<T>[]; testId: string; t: PT;
}) {
  return (
    <div style={{ display: "grid", gap: 6, minWidth: 0 }} data-testid={testId}>
      <b style={{ color: K.ink, fontSize: 13 }}>{title}</b>
      {rows.length === 0 ? <span style={small}>{t("agEmpty")}</span> : (
        <DataTable<T> tableId={testId} data={rows} columns={cols} getRowId={(r) => r.tenant_id} searchable={false}
          paginate={false} compact tableMinWidth={280} />)}
    </div>
  );
}

function Rankings({ d, t }: { d: TenantsResp; t: PT }) {
  const lang = useLang();
  const { formatDateTime } = useFormat();
  const usd = (v: number) => fmtUsd(v, lang);
  type Spend = NonNullable<TenantsResp["ai_spend"]>[number];
  type Jobs = NonNullable<TenantsResp["failed_jobs"]>[number];
  type Errs = NonNullable<TenantsResp["ai_errors"]>[number];
  const spend: ColumnDef<Spend>[] = [
    { id: "a", header: t("colAgency"), accessorFn: (r) => agencyLabel(r), cell: ({ row }) => nameCell(row.original) },
    { id: "c", header: t("colCost"), accessorFn: (r) => r.cost_usd, cell: ({ row }) => usd(row.original.cost_usd) },
    { id: "n", header: t("colCalls"), accessorFn: (r) => r.requests },
  ];
  const jobs: ColumnDef<Jobs>[] = [
    { id: "a", header: t("colAgency"), accessorFn: (r) => agencyLabel(r), cell: ({ row }) => nameCell(row.original) },
    { id: "f", header: t("colFailed"), accessorFn: (r) => r.failed },
    { id: "l", header: t("colLast"), accessorFn: (r) => r.last ?? "",
      cell: ({ row }) => <span style={small}>{row.original.last ? formatDateTime(row.original.last) : "—"}</span> },
  ];
  const errs: ColumnDef<Errs>[] = [
    { id: "a", header: t("colAgency"), accessorFn: (r) => agencyLabel(r), cell: ({ row }) => nameCell(row.original) },
    { id: "f", header: t("colFailed"), accessorFn: (r) => r.failed },
    { id: "s", header: t("colShare"), accessorFn: (r) => r.failed_pct ?? null,
      cell: ({ row }) => (row.original.failed_pct == null ? "—" : `${fmtNum(row.original.failed_pct)} %`) },
  ];
  return (
    <>
      <div style={grid}>
        <Ranking title={t("agSpend")} rows={d.ai_spend ?? []} cols={spend} testId="status-agencies-spend" t={t} />
        <Ranking title={t("agJobs")} rows={d.failed_jobs ?? []} cols={jobs} testId="status-agencies-jobs" t={t} />
        <Ranking title={t("agErrors")} rows={d.ai_errors ?? []} cols={errs} testId="status-agencies-errors" t={t} />
      </div>
      {d.platform && <span style={small}>{t("platformLine", usd(d.platform.cost_usd), d.platform.requests,
        d.platform.failed)}</span>}
    </>
  );
}

function OneAgency({ d, t }: { d: TenantsResp; t: PT }) {
  const lang = useLang();
  const { formatDateTime } = useFormat();
  const a = d.agency;
  if (!a) return null;
  type Stage = (typeof a.by_stage)[number];
  type Cls = (typeof a.ai_errors)[number];
  type Kind = (typeof a.failed_jobs)[number];
  const box = { display: "grid", gap: 6, minWidth: 0 } as const;
  return (
    <>
      {!a.known && <span style={small}>{t("agUnknown")}</span>}
      <span style={{ fontSize: 13, color: K.ink }} data-testid="status-agency-totals">
        <b>{agencyLabel(a)}</b>{" · "}{t("agTotals", fmtUsd(a.cost_usd, lang), a.requests, a.ai_failed,
          a.jobs_failed)}
      </span>
      <div style={grid}>
        <div style={box}>
          <b style={{ color: K.ink, fontSize: 13 }}>{t("agSpend")}</b>
          {a.by_stage.length === 0 ? <span style={small}>{t("agEmpty")}</span> : (
            <DataTable<Stage> tableId="status-agency-stage" data={a.by_stage} searchable={false} paginate={false}
              compact tableMinWidth={280} getRowId={(r) => `${r.app}:${r.stage ?? ""}`} columns={[
                { id: "s", header: t("colStage"), accessorFn: (r) => `${r.app} · ${r.stage ?? "—"}` },
                { id: "c", header: t("colCost"), accessorFn: (r) => r.cost_usd,
                  cell: ({ row }) => fmtUsd(row.original.cost_usd, lang) },
                { id: "f", header: t("colFailed"), accessorFn: (r) => r.failed },
              ]} />)}
        </div>
        <div style={box}>
          <b style={{ color: K.ink, fontSize: 13 }}>{t("agErrors")}</b>
          {a.ai_errors.length === 0 ? <span style={small}>{t("agEmpty")}</span> : (
            <DataTable<Cls> tableId="status-agency-errors" data={a.ai_errors} searchable={false} paginate={false}
              compact tableMinWidth={240} getRowId={(r) => r.error_class} columns={[
                { id: "c", header: t("colClass"), accessorFn: (r) => r.error_class },
                { id: "n", header: t("colFailed"), accessorFn: (r) => r.requests },
              ]} />)}
        </div>
        <div style={box}>
          <b style={{ color: K.ink, fontSize: 13 }}>{t("agJobs")}</b>
          {a.failed_jobs.length === 0 ? <span style={small}>{t("agEmpty")}</span> : (
            <DataTable<Kind> tableId="status-agency-jobs" data={a.failed_jobs} searchable={false} paginate={false}
              compact tableMinWidth={260} getRowId={(r) => r.kind} columns={[
                { id: "k", header: t("colKind"), accessorFn: (r) => r.kind },
                { id: "f", header: t("colFailed"), accessorFn: (r) => r.failed },
                { id: "l", header: t("colLast"), accessorFn: (r) => r.last ?? "",
                  cell: ({ row }) => <span style={small}>{row.original.last ? formatDateTime(row.original.last)
                    : "—"}</span> },
              ]} />)}
        </div>
      </div>
    </>
  );
}

export function AgenciesCard({ view }: { view: View }) {
  const t = useP2();
  const { get } = useUrlFilters();
  const pathname = usePathname();
  const tenant = tenantParam(get("tenant"));
  const q = tenantsQuery(view.range, view.from, view.to, tenant);
  const res = useQuery({ queryKey: ["admin-ecosystem-agencies", q], refetchInterval: 60_000,
    queryFn: () => apiGet<TenantsResp>(`/api/admin/overview/status/tenants?${q}`, { admin: true }),
    placeholderData: (prev) => prev });
  const d = res.data;
  const choose = (v: string) => {
    const p = new URLSearchParams(window.location.search);
    if (v) p.set("tenant", v);
    else p.delete("tenant");
    const qs = p.toString();
    window.history.replaceState(null, "", qs ? `${pathname}?${qs}` : pathname);
  };
  return (
    <Card title={t("agTitle")} testId="status-agencies" asOf={d?.generated_at ?? null}
      actions={<span style={rowWrap}>
        <Select size="sm" aria-label={t("agFilter")} value={tenant ?? ""} onChange={(e) => choose(e.target.value)}
          style={{ width: "auto", maxWidth: 240 }} data-testid="status-agencies-filter"
          options={[{ value: "", label: t("agAll") }, ...(d?.options ?? []).map((o) => ({ value: o.tenant_id,
            label: agencyLabel(o) }))]} />
      </span>}>
      {res.isError ? <span style={small}>{t("loadFailed")}</span> : !d ? <span style={small}>…</span> : (
        <>
          {!tenant && <span style={small}>{t("agNote", d.top_n)}</span>}
          {tenant ? <OneAgency d={d} t={t} /> : <Rankings d={d} t={t} />}
          <NotMeasured reason={t("agNotMeasured")} testId="status-agencies-not-measured" />
        </>)}
    </Card>
  );
}
