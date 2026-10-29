"use client";
// app/admin/reports/ReportsBoard.tsx
// AA-1003 — Platform › Overview › Reports: the report register (every recurring report of the root report registry
// docs/reports/catalog.yaml, vendored in services/reports/registry/) with its last run and due state, and per report
// its catalogue entry, KPIs, runs and the output of a run (rendered Markdown).
//
// API (CIS BFF → api/routers/admin_reports.py; admin only: ops.read, "Generate now" ops.write):
//   GET  /api/admin/reports                       the whole register (63 rows: filtered here, lib/reports.ts)
//   GET  /api/admin/reports/{id}/runs?page=       one report + its runs, paged on the server (10 per page)
//   GET  /api/admin/reports/runs/{run_id}         one run with its output
//   POST /api/admin/reports/{id}/run              queue report_generate (EX-04, EX-06 today)
//
// No polling (S240): load on open + Refresh. Registry text can carry internal issue references: shown through
// stripIssueIds(). Filters live in the URL (kit FilterBar).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw } from "lucide-react";
import { Suspense, useMemo, useState } from "react";
import {
  AdminMain,
  Badge,
  Button,
  Checkbox,
  DataTable,
  Drawer,
  FilterBar,
  FilterField,
  K,
  KpiTile,
  PageHeader,
  apiGet,
  apiSend,
  mono,
  useAdminIdentity,
  useFormat,
  useLang,
  useT,
  useToast,
  useUrlFilters,
  type ColumnDef,
  type Translator,
} from "../../_kit";
import { MarkdownView } from "../../_kit/MarkdownView";
import { can } from "@/lib/rbac";
import { stripIssueIds } from "@/lib/issueIds";
import {
  DUE_TONE,
  LIST_PATH,
  PRIORITIES,
  PRIORITY_TONE,
  RUNS_PAGE_SIZE,
  RUN_TONE,
  defaultRun,
  families,
  filterReports,
  generatePath,
  periodText,
  runPath,
  runsPath,
  type ReportFilters,
  type ReportList,
  type ReportRow,
  type ReportRuns,
  type Run,
  type RunDetail,
  type RunStatus,
} from "@/lib/reports";
import { RM } from "./messages";

const LIST_KEY = ["admin-reports"] as const;
type T = Translator<typeof RM.en>;

function runLabel(t: T, status: RunStatus): string {
  return t(`run_${status}` as "run_queued");
}

export default function ReportsBoard() {
  return (
    <Suspense fallback={null}>
      <Board />
    </Suspense>
  );
}

function Board() {
  const t = useT(RM);
  const lang = useLang();
  const { formatDate, formatDateTime } = useFormat();
  const qc = useQueryClient();
  const { get } = useUrlFilters();
  const [open, setOpen] = useState<ReportRow | null>(null);

  const list = useQuery({ queryKey: LIST_KEY, queryFn: () => apiGet<ReportList>(LIST_PATH), retry: false });
  const filters: ReportFilters = {
    family: get("family"), priority: get("priority"), generatedOnly: get("generated") === "1", q: get("q"),
  };
  const rows = useMemo(() => filterReports(list.data?.reports ?? [], filters),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [list.data, filters.family, filters.priority, filters.generatedOnly, filters.q]);
  const filtered = !!(filters.family || filters.priority || filters.generatedOnly || filters.q.trim());

  const columns = useMemo<ColumnDef<ReportRow, unknown>[]>(() => [
    {
      accessorKey: "id", header: t("colReport"),
      cell: (c) => {
        const r = c.row.original;
        return (
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>
              <span style={{ fontFamily: mono, color: K.muted, marginRight: 6 }}>{r.id}</span>
              {stripIssueIds(lang === "vi" ? r.name_vi : r.name_en)}
            </span>
            <span style={{ fontSize: 11.5, color: K.muted }}>{stripIssueIds(r.owner_role)}</span>
          </span>
        );
      },
    },
    { accessorKey: "family", header: t("colFamily") },
    {
      accessorKey: "priority", header: t("colPriority"),
      cell: (c) => <Badge tone={PRIORITY_TONE[c.row.original.priority] ?? "neutral"}>{c.row.original.priority}</Badge>,
    },
    { accessorKey: "schedule", header: t("colSchedule") },
    { accessorKey: "automation", header: t("colAutomation") },
    { accessorKey: "status", header: t("colToday") },
    {
      id: "last_run", header: t("colLastRun"),
      accessorFn: (r) => r.last_run?.created_at ?? "",
      cell: (c) => {
        const run = c.row.original.last_run;
        if (!run) return <span style={{ color: K.muted }}>{t("never")}</span>;
        return (
          <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <Badge tone={RUN_TONE[run.status] ?? "neutral"}>{runLabel(t, run.status)}</Badge>
            <span style={{ fontSize: 11.5, color: K.muted }}>{periodText(run.period_start, run.period_end)}</span>
          </span>
        );
      },
    },
    {
      id: "due", header: t("colDue"), accessorFn: (r) => r.due.state,
      cell: (c) => {
        const s = c.row.original.due.state;
        return <Badge tone={DUE_TONE[s]}>{t(`due_${s}` as "due_ok")}</Badge>;
      },
    },
  ], [t, lang]);

  const refresh = () => { void qc.invalidateQueries({ queryKey: LIST_KEY }); };
  const data = list.data;
  const listError = list.error ? (list.error as Error).message || t("listError") : null;
  const failed7d = data ? data.reports.reduce((n, r) => n + r.failed_7d, 0) : undefined;

  return (
    <AdminMain>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("platform") }, { label: t("overview") }, { label: t("title") }]}
        description={t("description")}
        actions={
          <Button variant="secondary" size="sm" onClick={refresh} disabled={list.isFetching}>
            <RefreshCw size={13} /> {list.isFetching ? t("loading") : t("refresh")}
          </Button>
        }
      />

      <div data-testid="admin-reports-tiles" style={{
        display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12,
        marginBottom: 16,
      }}>
        <KpiTile size="sm" label={t("tileReports")} value={data ? data.registry.reports : undefined} />
        <KpiTile size="sm" label={t("tileGenerated")}
          value={data ? data.reports.filter((r) => r.runnable).length : undefined} />
        <KpiTile size="sm" label={t("tileOverdue")} value={data ? data.states.overdue ?? 0 : undefined}
          valueTone={data?.states.overdue ? "danger" : undefined} />
        <KpiTile size="sm" label={t("tileFailed")} value={failed7d}
          valueTone={failed7d ? "danger" : undefined} />
        <KpiTile size="sm" label={t("tileKpiDay")}
          value={data ? (data.latest_kpi_day ? formatDate(data.latest_kpi_day) : t("none")) : undefined}
          hint={data ? t("tileKpiDayHint", formatDate(data.snapshot_day)) : undefined} />
      </div>

      <DataTable<ReportRow>
        tableId="admin-reports"
        data={rows}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={listError}
        onRetry={refresh}
        searchable={false}
        toolbarExtra={
          <FilterBar layout="toolbar" testId="admin-reports-filters"
            search={{ placeholder: t("search") }}
            selects={[
              { key: "family", label: t("colFamily"), allLabel: t("allFamilies"),
                options: families(data?.reports ?? []).map((f) => ({ value: f, label: f })), width: 160 },
              { key: "priority", label: t("colPriority"), allLabel: t("allPriorities"),
                options: PRIORITIES.map((p) => ({ value: p, label: p })) },
            ]}
            extraKeys={["generated"]}>
            <GeneratedOnly label={t("generatedOnly")} />
          </FilterBar>
        }
        emptyTitle={filtered ? t("emptyFiltered") : t("emptyNone")}
        emptyDescription={filtered ? t("emptyFilteredDesc") : undefined}
        onRowClick={(r) => setOpen(r)}
        tableMinWidth={960}
      />
      {data && (
        <div style={{ fontSize: 12, color: K.muted, marginTop: 10 }}>{formatDateTime(data.generated_at)}</div>
      )}

      {open && <ReportDrawer key={open.id} report={open} onClose={() => setOpen(null)} onQueued={refresh} />}
    </AdminMain>
  );
}

function GeneratedOnly({ label }: { label: string }) {
  const { get, set } = useUrlFilters();
  return (
    <FilterField label={label} as="div">
      <Checkbox aria-label={label} checked={get("generated") === "1"}
        onChange={(e) => set("generated", e.target.checked ? "1" : null)} />
    </FilterField>
  );
}

// ── the drawer ──────────────────────────────────────────────────────────────────────────────────────────────────

const labelStyle: React.CSSProperties = { fontSize: 11.5, fontWeight: 600, color: K.muted, textTransform: "uppercase",
  letterSpacing: 0.3, margin: "16px 0 6px" };
const textStyle: React.CSSProperties = { fontSize: 13, color: K.ink, lineHeight: 1.5, overflowWrap: "anywhere" };

function ReportDrawer({ report, onClose, onQueued }: { report: ReportRow; onClose: () => void; onQueued: () => void }) {
  const t = useT(RM);
  const lang = useLang();
  const toast = useToast();
  const qc = useQueryClient();
  const { formatDateTime } = useFormat();
  const role = useAdminIdentity().role;
  const canRun = can(role, "ops.write");
  const [page, setPage] = useState(1);
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const runs = useQuery({
    queryKey: ["admin-report-runs", report.id, page],
    queryFn: () => apiGet<ReportRuns>(runsPath(report.id, page)),
    retry: false,
  });
  const selectedId = runId ?? defaultRun(runs.data?.runs ?? [])?.id ?? null;
  const run = useQuery({
    queryKey: ["admin-report-run", selectedId],
    queryFn: () => apiGet<RunDetail>(runPath(selectedId as string)),
    enabled: !!selectedId,
    retry: false,
  });
  const detail = runs.data?.report;
  const runColumns = useMemo<ColumnDef<Run, unknown>[]>(() => [
    {
      accessorKey: "status", header: t("colLastRun"),
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <Badge tone={RUN_TONE[c.row.original.status] ?? "neutral"}>{runLabel(t, c.row.original.status)}</Badge>
          {c.row.original.id === selectedId && <span style={{ color: K.accentDeep, fontSize: 11.5 }}>●</span>}
        </span>
      ),
    },
    { id: "period", header: t("period"), accessorFn: (r) => periodText(r.period_start, r.period_end) },
    { accessorKey: "trigger", header: t("trigger") },
    {
      accessorKey: "created_at", header: t("created"),
      cell: (c) => (c.row.original.created_at ? formatDateTime(c.row.original.created_at) : ""),
    },
  ], [t, formatDateTime, selectedId]);

  const generate = async () => {
    setBusy(true);
    try {
      await apiSend(generatePath(report.id), { method: "POST", body: {} });
      toast.success(t("generated"));
      setRunId(null);
      setPage(1);
      void qc.invalidateQueries({ queryKey: ["admin-report-runs", report.id] });
      onQueued();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open onClose={onClose} width={760}
      title={<span><span style={{ fontFamily: mono, color: K.muted, marginRight: 8 }}>{report.id}</span>
        {stripIssueIds(lang === "vi" ? report.name_vi : report.name_en)}</span>}
      footer={report.runnable ? (
        canRun ? (
          <Button variant="primary" size="sm" onClick={() => void generate()} disabled={busy}>
            <Play size={13} /> {busy ? t("generating") : t("generateNow")}
          </Button>
        ) : <span style={{ fontSize: 12.5, color: K.muted }}>{t("adminOnly")}</span>
      ) : <span style={{ fontSize: 12.5, color: K.muted }}>{t("notGeneratedHere")}</span>}>
      <div data-testid="admin-report-drawer">
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <Badge tone={PRIORITY_TONE[report.priority] ?? "neutral"}>{report.priority}</Badge>
          <Badge tone={DUE_TONE[report.due.state]}>{t(`due_${report.due.state}` as "due_ok")}</Badge>
          <Badge tone="neutral">{report.family}</Badge>
        </div>
        {detail && (
          <>
            <div style={labelStyle}>{t("decision")}</div>
            <div style={textStyle}>{stripIssueIds(detail.decision)}</div>
            <dl style={{ display: "grid", gridTemplateColumns: "max-content minmax(0, 1fr)", gap: "6px 14px",
              margin: "12px 0 0", fontSize: 13 }}>
              <dt style={{ color: K.muted }}>{t("owner")}</dt><dd style={{ margin: 0 }}>{stripIssueIds(detail.owner_role)}</dd>
              <dt style={{ color: K.muted }}>{t("audience")}</dt><dd style={{ margin: 0 }}>{stripIssueIds(detail.audience)}</dd>
              <dt style={{ color: K.muted }}>{t("cadence")}</dt><dd style={{ margin: 0 }}>{stripIssueIds(detail.cadence)}</dd>
              <dt style={{ color: K.muted }}>{t("channel")}</dt><dd style={{ margin: 0 }}>{stripIssueIds(detail.channel)}</dd>
              <dt style={{ color: K.muted }}>{t("template")}</dt>
              <dd style={{ margin: 0, fontFamily: detail.template ? mono : undefined, fontSize: 12 }}>
                {detail.template ?? t("noTemplate")}
              </dd>
              {detail.covered_to && (<>
                <dt style={{ color: K.muted }}>{t("coveredTo")}</dt><dd style={{ margin: 0 }}>{detail.covered_to}</dd>
              </>)}
            </dl>
            <div style={labelStyle}>{t("sections")}</div>
            <ul style={{ margin: 0, paddingLeft: 18, ...textStyle }}>
              {detail.sections.map((s) => <li key={s}>{stripIssueIds(s)}</li>)}
            </ul>
            <div style={labelStyle}>{t("kpis")}</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {detail.kpi_details.map((k) => (
                <div key={k.id} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px" }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span style={{ fontFamily: mono, fontSize: 12, color: K.muted }}>{k.id}</span>
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{stripIssueIds(k.name)}</span>
                    <Badge tone={k.snapshotted ? "success" : "neutral"}>
                      {k.snapshotted ? t("measuredDaily") : t("notMeasured")}
                    </Badge>
                  </div>
                  <div style={{ ...textStyle, fontSize: 12.5, marginTop: 4 }}>{stripIssueIds(k.definition)}</div>
                  <div style={{ fontSize: 12, color: K.muted, marginTop: 4 }}>
                    {t("threshold")}: {stripIssueIds(k.threshold)}
                    {k.sources.length > 0 && <> · {t("sources")}: <span style={{ fontFamily: mono }}>{k.sources.join(", ")}</span></>}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}

        <div style={labelStyle}>{t("runs")}</div>
        <DataTable<Run>
          tableId="admin-report-runs"
          data={runs.data?.runs ?? []}
          columns={runColumns}
          getRowId={(r) => r.id}
          loading={runs.isLoading}
          error={runs.error ? (runs.error as Error).message || t("loadRunsError") : null}
          onRetry={() => void runs.refetch()}
          searchable={false}
          compact
          pageSizeOptions={[RUNS_PAGE_SIZE]}
          serverPagination={{
            pageIndex: page - 1, pageSize: RUNS_PAGE_SIZE, total: runs.data?.total ?? 0,
            onChange: (index) => { setPage(index + 1); setRunId(null); },
          }}
          emptyTitle={t("noRuns")}
          onRowClick={(r) => setRunId(r.id)}
        />

        {run.data && (
          <div data-testid="admin-report-output">
            <div style={labelStyle}>{t("outputOf", periodText(run.data.period_start, run.data.period_end))}</div>
            {run.data.error && (
              <div style={{ color: K.danger, fontSize: 12.5, marginBottom: 8, overflowWrap: "anywhere" }}>
                {t("runError")}: {run.data.error}
              </div>
            )}
            {run.data.markdown ? (
              <MarkdownView markdown={run.data.markdown} resolveHref={() => ({ kind: "text" })}
                slugForLine={() => undefined} />
            ) : !run.data.error && <div style={{ color: K.muted, fontSize: 13 }}>{t("noOutput")}</div>}
          </div>
        )}
      </div>
    </Drawer>
  );
}
