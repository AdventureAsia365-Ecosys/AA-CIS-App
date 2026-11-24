"use client";
// app/admin/overview/_status/Services.tsx — AA-1140: the services and data grid. Every ECS service and health check the
// 5-minute sampler reads (acp_shared.ops_sample), its rollout, version, the uptime strip of the range and the uptime
// against the objective (cis.ops.status_uptime_slo_pct) with the error budget left; TripPlanner's Lambda functions and
// Vercel web are rows that say "not measured yet". Filter (all / needs attention / healthy / no sample), app filter,
// sortable columns; the filter and the sort live in the URL.

import Link from "next/link";
import { Badge, DataTable, K, SegmentedControl, type ColumnDef, type Tone } from "../../../_kit";
import {
  RUNTIME_FILTERS, fmtPct, inApps, levelTone, runtimeMatches, shortSha, sortingParam, tableSorting,
  withSort, type RuntimeFilter, type RuntimeRow, type Section, type View, type SetView,
} from "@/lib/opsStatus";
import { Card, LevelGlyph, WhenUp, rowWrap, small, useST, type ST } from "./ui";
import { useWords, type StatusWords } from "./words";

const CELL: Readonly<Record<string, string>> = { ok: "success", warn: "warning", degraded: "warning", down: "danger" };

function Strip({ cells }: { cells: string[] | undefined }) {
  const t = useST();
  if (!cells?.length) return <span style={small}>—</span>;
  // AA-1141 (WCAG 1.1.1 / 1.4.1): the strip is an image with its count of up, down and unsampled cells in words
  const up = cells.filter((s) => CELL[s] === "success").length;
  const down = cells.filter((s) => CELL[s] === "danger" || CELL[s] === "warning").length;
  const label = t("stripLabel", String(up), String(down), String(cells.length - up - down));
  return (
    <span role="img" aria-label={label} title={label}
      style={{ display: "inline-flex", gap: 1, alignItems: "stretch", height: 14, maxWidth: 220 }}>
      {cells.map((s, i) => {
        const tone = CELL[s];
        const bg = tone === "success" ? K.success : tone === "warning" ? K.amber : tone === "danger" ? K.danger
          : K.line;
        return <span key={i} style={{ flex: "1 1 0", minWidth: 1, background: bg, borderRadius: 1 }} />;
      })}
    </span>
  );
}

function stateCell(r: RuntimeRow, t: ST, w: StatusWords) {
  const label = r.state === "not_measured" ? t("notMeasured") : r.state.replace("_", " ");
  const reason = w.reason(r);
  return (
    <span title={reason ?? undefined} style={{ display: "grid", gap: 2 }}>
      <Badge tone={levelTone(r.state) as Tone}>{label}</Badge>
      {reason && r.state !== "ok" && <span style={{ ...small, overflowWrap: "anywhere" }}>{reason}</span>}
    </span>
  );
}

function columns(t: ST, w: StatusWords, slo: number): ColumnDef<RuntimeRow>[] {
  return [
    { id: "service", header: t("colService"), accessorFn: (r) => r.subject,
      cell: ({ row }) => (<span style={{ display: "grid" }}><b style={{ color: K.ink, overflowWrap: "anywhere" }}>
        {row.original.subject}</b><span style={small}>{row.original.role}</span></span>) },
    { id: "app", header: t("colApp"), accessorFn: (r) => w.app(r.app) },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state, cell: ({ row }) => stateCell(row.original, t, w) },
    { id: "tasks", header: t("colTasks"), accessorFn: (r) => r.running ?? null,
      cell: ({ row }) => row.original.desired != null ? `${row.original.running ?? 0}/${row.original.desired}` : "—" },
    { id: "version", header: t("colVersion"), accessorFn: (r) => r.version ?? "",
      cell: ({ row }) => <code>{shortSha(row.original.version)}</code> },
    { id: "uptime", header: t("colUptime"), accessorFn: (r) => r.uptime_pct ?? null, meta: { align: "right" },
      cell: ({ row }) => (<span style={{ color: row.original.below_slo ? K.danger : K.ink }}
        title={row.original.below_slo ? t("belowSlo") : undefined}>
        {row.original.below_slo && <LevelGlyph level="crit" />}{fmtPct(row.original.uptime_pct, 2)}</span>) },
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
  section: Section<{ slo_pct: number; rows: RuntimeRow[] }>; view: View; setView: SetView;
}) {
  const t = useST();
  const w = useWords();
  return (
    <Card title={t("servicesTitle")} testId="status-services" asOf={section.as_of} refreshS={section.refresh_s}
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
              <DataTable<RuntimeRow> tableId="status-services" data={rows} columns={columns(t, w, d.slo_pct)}
                getRowId={(r) => `${r.kind}:${r.subject}`} searchable={false} paginate={false} compact
                tableMinWidth={860} sorting={tableSorting(view.sort.svc)}
                onSortingChange={(s) => setView((v) => ({ sort: withSort(v.sort, "svc", sortingParam(s)) }))} />
            </>
          );
        }}
      </WhenUp>
    </Card>
  );
}
