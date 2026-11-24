"use client";
// app/admin/overview/_status/Services.tsx — AA-1140: the services and data grid. Every ECS service and health check the
// 5-minute sampler reads (acp_shared.ops_sample), its rollout, version, the uptime strip of the range and the uptime
// against the objective (cis.ops.status_uptime_slo_pct) with the error budget left; TripPlanner's Lambda functions and
// Vercel web are rows that say "not measured yet". Filter (all / needs attention / healthy / no sample), app filter,
// sortable columns; the filter and the sort live in the URL.

import Link from "next/link";
import { Badge, DataTable, K, SegmentedControl, type ColumnDef, type Tone } from "../../../_kit";
import {
  APP_LABEL, RUNTIME_FILTERS, fmtPct, inApps, levelTone, runtimeMatches, shortSha, sortingParam, tableSorting,
  withSort, type RuntimeFilter, type RuntimeRow, type Section, type View,
} from "@/lib/opsStatus";
import { Card, WhenUp, rowWrap, small, useST, type ST } from "./ui";

const CELL: Readonly<Record<string, string>> = { ok: "success", warn: "warning", degraded: "warning", down: "danger" };

function Strip({ cells }: { cells: string[] | undefined }) {
  if (!cells?.length) return <span style={small}>—</span>;
  return (
    <span aria-hidden style={{ display: "inline-flex", gap: 1, alignItems: "stretch", height: 14, maxWidth: 220 }}>
      {cells.map((s, i) => {
        const tone = CELL[s];
        const bg = tone === "success" ? K.success : tone === "warning" ? K.amber : tone === "danger" ? K.danger
          : K.line;
        return <span key={i} style={{ flex: "1 1 0", minWidth: 1, background: bg, borderRadius: 1 }} />;
      })}
    </span>
  );
}

function stateCell(r: RuntimeRow, t: ST) {
  const label = r.state === "not_measured" ? t("notMeasured") : r.state.replace("_", " ");
  return (
    <span title={r.reason ?? undefined} style={{ display: "grid", gap: 2 }}>
      <Badge tone={levelTone(r.state) as Tone}>{label}</Badge>
      {r.reason && r.state !== "ok" && <span style={{ ...small, overflowWrap: "anywhere" }}>{r.reason}</span>}
    </span>
  );
}

function columns(t: ST, slo: number): ColumnDef<RuntimeRow>[] {
  return [
    { id: "service", header: t("colService"), accessorFn: (r) => r.subject,
      cell: ({ row }) => (<span style={{ display: "grid" }}><b style={{ color: K.ink, overflowWrap: "anywhere" }}>
        {row.original.subject}</b><span style={small}>{row.original.role}</span></span>) },
    { id: "app", header: t("colApp"), accessorFn: (r) => APP_LABEL[r.app] ?? r.app },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state, cell: ({ row }) => stateCell(row.original, t) },
    { id: "tasks", header: t("colTasks"), accessorFn: (r) => r.running ?? null,
      cell: ({ row }) => row.original.desired != null ? `${row.original.running ?? 0}/${row.original.desired}` : "—" },
    { id: "version", header: t("colVersion"), accessorFn: (r) => r.version ?? "",
      cell: ({ row }) => <code>{shortSha(row.original.version)}</code> },
    { id: "uptime", header: t("colUptime"), accessorFn: (r) => r.uptime_pct ?? null, meta: { align: "right" },
      cell: ({ row }) => (<span style={{ color: row.original.below_slo ? K.danger : K.ink }}>
        {fmtPct(row.original.uptime_pct, 2)}</span>) },
    { id: "budget", header: t("colBudget"), accessorFn: (r) => r.budget_left_pct ?? null, meta: { align: "right" },
      cell: ({ row }) => fmtPct(row.original.budget_left_pct, 0) },
    { id: "strip", header: `${t("colStrip")} · ${t("slo", fmtPct(slo, 1))}`, enableSorting: false,
      cell: ({ row }) => <Strip cells={row.original.cells} /> },
  ];
}

const FILTER_KEY: Readonly<Record<RuntimeFilter, "svcAll" | "svcAttention" | "svcHealthy" | "svcNoSample">> = {
  all: "svcAll", attention: "svcAttention", healthy: "svcHealthy", no_sample: "svcNoSample",
};

export function ServicesCard({ section, view, setView }: {
  section: Section<{ slo_pct: number; rows: RuntimeRow[] }>; view: View; setView: (v: Partial<View>) => void;
}) {
  const t = useST();
  return (
    <Card title={t("servicesTitle")} testId="status-services" asOf={section.as_of}
      actions={<Link href="/admin/platform/apps" style={{ ...small, color: K.accentDeep, fontWeight: 600 }}>
        {t("lAppHealth")}</Link>}>
      <WhenUp section={section.data ? { ...section, status: "ok" } : section}>
        {(d) => {
          const scoped = d.rows.filter((r) => inApps(r.app, view.apps));
          const rows = scoped.filter((r) => runtimeMatches(r, view.svc));
          const count = (f: RuntimeFilter) => scoped.filter((r) => runtimeMatches(r, f)).length;
          return (
            <>
              {section.status !== "ok" && <span style={small}>{section.message}</span>}
              <div style={rowWrap}>
                <SegmentedControl<RuntimeFilter> label={t("servicesTitle")} size="sm" appearance="chips"
                  value={view.svc} onChange={(f) => setView({ svc: f })} testId="status-svc-filter"
                  options={RUNTIME_FILTERS.map((f) => ({ value: f, label: t(FILTER_KEY[f]), count: count(f) }))} />
              </div>
              <DataTable<RuntimeRow> tableId="status-services" data={rows} columns={columns(t, d.slo_pct)}
                getRowId={(r) => `${r.kind}:${r.subject}`} searchable={false} paginate={false} compact
                tableMinWidth={860} sorting={tableSorting(view.sort.svc)}
                onSortingChange={(s) => setView({ sort: withSort(view.sort, "svc", sortingParam(s)) })} />
            </>
          );
        }}
      </WhenUp>
    </Card>
  );
}
