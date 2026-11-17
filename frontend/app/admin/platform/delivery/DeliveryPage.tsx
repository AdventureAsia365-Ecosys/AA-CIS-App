"use client";
// app/admin/platform/delivery/DeliveryPage.tsx — AA-1108: Platform › Delivery (CI/CD and DORA per app).
//
// Overview: one card per app with the five DORA keys (colour from the settings cis.delivery.*, n next to each value,
// a 12-week sparkline), the slowest CI p95 and the main branch state. One tab per app: deploy timeline and table,
// the stacked lead-time segments, CI health per workflow, failed deploys with recovery time, flaky jobs, merge queue
// and change counts, bot pull requests; every run, commit and pull request links to GitHub. "Dev numbers" label on the
// header: few events, never compared with prod benchmarks.
//
// AA-1125: two more tabs, Merge queue and Git map (ActivityTabs.tsx), each loading its own section of the summary.
//
// API (backend api/routers/admin_platform_delivery.py; BFF /api/admin/platform/delivery, admin only — lib/rbac.ts):
//   GET /api/admin/platform/delivery[?refresh=1][&section=queue|git]
// Pure helpers: lib/deliveryMetrics.ts, lib/deliveryActivity.ts. Kit only. The tab lives in the URL (?app=<id>, or
// ?app=merge-queue / ?app=git-map).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, ExternalLink, GitBranch, RefreshCw, SlidersHorizontal } from "lucide-react";
import { useMemo } from "react";
import { Bar, BarChart, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Badge, Button, CHART_TOOLTIP, DataTable, EmptyState, ErrorState, InfoTip, K, KpiTile, PageFrame, PageHeader, Section,
  SkeletonStyle, Tabs, apiGet, mono, useFormat, useT, useUrlFilters, type ColumnDef, type Tone,
} from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import {
  DELIVERY_PATH, DORA_KEYS, GUIDE_PATH, SEGMENTS, SETTINGS_HREF, commitUrl, docHref, doraN, doraValue, fmtHours,
  fmtNum, fmtRatio, fmtSeconds, prUrl, repoUrl, safeGithubUrl, segmentRows, shortSha, slowestSegment, sparkSeries,
  statusTone, worstStatus,
  type ChangeRow, type DeliveryApp, type DeliveryResp, type DeployRow, type FlakyRow, type MetricKey, type MetricStatus,
  type WorkflowRow,
} from "@/lib/deliveryMetrics";
import { GitMapTab, MergeQueueTab } from "./ActivityTabs";
import { ACTIVITY } from "./activity.messages";
import { DELIVERY } from "./messages";

const KEY = ["platform-delivery"];
const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const linkStyle = { color: K.accentDeep, display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12 } as const;
const grid = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 } as const;
const CARD_METRICS: MetricKey[] = [...DORA_KEYS, "ci_p95"];

function useDeliveryT() {
  return useT(DELIVERY);
}
type DT = ReturnType<typeof useDeliveryT>;

function Ext({ href, children }: { href: string | null; children: React.ReactNode }) {
  if (!href) return <>{children}</>;
  return <a href={href} target="_blank" rel="noreferrer" style={linkStyle}>{children}<ExternalLink size={11} /></a>;
}

function StatusDot({ status }: { status: MetricStatus | undefined }) {
  const tone = statusTone(status);
  const color = tone === "success" ? K.success : tone === "warning" ? K.amber : tone === "danger" ? K.danger : K.muted2;
  return <span aria-hidden style={{ width: 8, height: 8, borderRadius: 4, background: color, flexShrink: 0 }} />;
}

function Spark({ app, metric }: { app: DeliveryApp; metric: MetricKey }) {
  const c = useChartColors({ line: "--aa-accent" });
  const data = sparkSeries(app, metric);
  if (!data.some((d) => d.v !== null)) return <span style={{ width: 72 }} />;
  return (
    <span style={{ width: 72, height: 22, display: "inline-block" }} aria-hidden>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
          <Line type="monotone" dataKey="v" stroke={c.line} strokeWidth={1.5} dot={false} connectNulls
            isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </span>
  );
}

function MetricLine({ app, metric, t }: { app: DeliveryApp; metric: MetricKey; t: DT }) {
  const status = app.status?.[metric];
  const unit = metric === "deployment_frequency" ? ` ${t("perWeek")}` : "";
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto auto", gap: 8, alignItems: "center" }}
      data-testid={`delivery-${app.id}-${metric}`}>
      {/* S282: the label wraps and the n note sits under the value, so a long note never pushes the value over the
          label (the Recovery p50 line overlapped on the CIS and Booking cards). */}
      <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 12.5,
        color: K.ink2, minWidth: 0, overflowWrap: "anywhere" }}>
        <StatusDot status={status} /> {t(metric)} <InfoTip text={t(`tip${metric}` as Parameters<DT>[0])} />
      </span>
      <span style={{ fontSize: 13, fontWeight: 600, color: K.ink, textAlign: "right", minWidth: 0 }}>
        <span style={{ whiteSpace: "nowrap" }}>{doraValue(app, metric)}{unit}</span>
        <span style={{ ...small, fontWeight: 400, display: "block" }}>
          ({t("n", doraN(app, metric))}{status === "few" ? ` · ${t("few")}` : ""})
        </span>
      </span>
      <Spark app={app} metric={metric} />
    </div>
  );
}

function MainBadge({ app, t }: { app: DeliveryApp; t: DT }) {
  const s = app.status?.main;
  const m = app.ci.main;
  const label = m.state === "green" ? t("mainGreen") : m.state === "red" ? t("mainRed", fmtHours(m.red_hours))
    : t("mainUnknown");
  const badge = <Badge tone={statusTone(s)}>{t("main")}: {label}</Badge>;
  return m.url ? <Ext href={safeGithubUrl(m.url)}>{badge}</Ext> : badge;
}

function AppCard({ app, t, open }: { app: DeliveryApp; t: DT; open: (id: string) => void }) {
  const worst = worstStatus(app);
  const tone = statusTone(worst);
  const border = tone === "danger" ? K.danger : tone === "warning" ? K.amber : K.line;
  return (
    <Section testId={`delivery-card-${app.id}`} style={{ borderColor: border }}
      title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>{app.name}</span>}
      description={<span style={small}><Ext href={repoUrl(app.repo)}>{app.repo}</Ext>
        {app.complete_since && <> · {t("historyFrom", app.complete_since)}</>}</span>}
      actions={<Button size="sm" variant="secondary" onClick={() => open(app.id)} testId={`delivery-open-${app.id}`}>
        {t("viewApp")}</Button>}>
      <div style={{ display: "grid", gap: 8 }}>
        {CARD_METRICS.map((m) => <MetricLine key={m} app={app} metric={m} t={t} />)}
        <div style={{ paddingTop: 4 }}><MainBadge app={app} t={t} /></div>
      </div>
    </Section>
  );
}

function Overview({ data, t, open }: { data: DeliveryResp; t: DT; open: (id: string) => void }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 340px), 1fr))", gap: 14 }}>
      {data.apps.map((a) => <AppCard key={a.id} app={a} t={t} open={open} />)}
    </div>
  );
}

// ── app tab ──────────────────────────────────────────────────────────────────────────────────────────────────────

function Kpis({ app, t }: { app: DeliveryApp; t: DT }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 170px), 1fr))", gap: 10 }}>
      {[...CARD_METRICS, "main" as MetricKey].map((m) => (
        <KpiTile key={m} size="sm" label={t(m)} testId={`delivery-kpi-${m}`}
          value={m === "main" ? (app.ci.main.state === "red" ? t("mainRed", fmtHours(app.ci.main.red_hours))
            : app.ci.main.state === "green" ? t("mainGreen") : t("mainUnknown")) : doraValue(app, m)}
          hint={`${t("n", doraN(app, m))}${app.status?.[m] === "few" ? ` · ${t("few")}` : ""}`}
          hintTone={statusTone(app.status?.[m])} info={t(`tip${m}` as Parameters<DT>[0])} />
      ))}
    </div>
  );
}

const OUTCOME_TONE: Record<string, Tone> = {
  success: "success", verify_failed: "danger", deploy_failed: "danger", unclassified: "neutral",
};

function Timeline({ app, t }: { app: DeliveryApp; t: DT }) {
  const { formatDateTime } = useFormat();
  const rows = [...app.deploys].reverse();
  if (!rows.length) return <span style={small}>{t("noDeploys")}</span>;
  return (
    <div role="list" style={{ display: "flex", flexWrap: "wrap", gap: 4 }} data-testid="delivery-timeline">
      {rows.map((d) => {
        const bad = d.failed.length > 0 || d.outcome !== "success";
        const color = bad ? K.danger : d.rework.length ? K.amber : K.success;
        const title = `${d.started_at ? formatDateTime(d.started_at) : "—"} · ${shortSha(d.sha)} · `
          + `${t(`outcome${d.outcome}` as Parameters<DT>[0])}${d.failed.length ? ` (${d.failed.join(", ")})` : ""}`;
        return <span role="listitem" key={`${d.sha}-${d.started_at}`} title={title}
          style={{ width: 12, height: 18, borderRadius: 3, background: color, opacity: d.redeploy ? 0.5 : 1 }} />;
      })}
    </div>
  );
}

function reasons(d: DeployRow, t: DT): string {
  const f = d.failed.map((r) => t(`reason${r}` as Parameters<DT>[0]));
  const w = d.rework.map((r) => t(`rework${r}` as Parameters<DT>[0]));
  return [...f, ...w, ...(d.redeploy ? [t("redeploy")] : [])].join(", ") || "—";
}

function useDeployColumns(app: DeliveryApp, t: DT): ColumnDef<DeployRow, unknown>[] {
  const { formatDateTime } = useFormat();
  return useMemo(() => [
    { id: "started", header: t("colStarted"), accessorKey: "started_at",
      cell: ({ row: { original: d } }) => <span style={small}>{d.started_at ? formatDateTime(d.started_at) : "—"}</span> },
    { id: "commit", header: t("colCommit"), accessorKey: "sha",
      cell: ({ row: { original: d } }) => <Ext href={commitUrl(app.repo, d.sha)}>
        <span style={{ fontFamily: mono }}>{shortSha(d.sha)}</span></Ext> },
    { id: "outcome", header: t("colOutcome"), accessorKey: "outcome",
      cell: ({ row: { original: d } }) => <Badge tone={d.failed.length ? "danger" : OUTCOME_TONE[d.outcome] ?? "neutral"}>
        {t(`outcome${d.outcome}` as Parameters<DT>[0])}</Badge> },
    { id: "why", header: t("colWhy"), cell: ({ row: { original: d } }) => <span style={small}>{reasons(d, t)}</span> },
    { id: "recovery", header: t("colRecovery"), accessorKey: "recovery_h",
      cell: ({ row: { original: d } }) => <span style={small}>{d.failed.length
        ? (d.recovery_h === null ? t("recoveryOpen") : fmtHours(d.recovery_h)) : "—"}</span> },
    { id: "runs", header: t("colRuns"), cell: ({ row: { original: d } }) => (
      <span style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {d.runs.map((r) => <Ext key={r.id} href={safeGithubUrl(r.url)}>{r.workflow}</Ext>)}</span>) },
    { id: "changes", header: t("colChanges"), cell: ({ row: { original: d } }) => (
      <span style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {d.changes.slice(0, 8).map((n) => <Ext key={n} href={prUrl(app.repo, n)}>#{n}</Ext>)}
        {d.changes.length > 8 && <span style={small}>+{d.changes.length - 8}</span>}</span>) },
  ], [app.repo, t, formatDateTime]);
}

function DeploysSection({ app, t }: { app: DeliveryApp; t: DT }) {
  const columns = useDeployColumns(app, t);
  return (
    <Section title={t("secTimeline")} description={t("secTimelineDesc")} testId="delivery-deploys">
      <div style={grid}>
        <Timeline app={app} t={t} />
        <DataTable<DeployRow> tableId={`delivery-deploys-${app.id}`} data={app.deploys} columns={columns}
          getRowId={(d) => `${d.sha}-${d.started_at}`} paginate={false} searchable={false} tableMinWidth={860}
          emptyTitle={t("noDeploys")} />
      </div>
    </Section>
  );
}

function SegmentsSection({ app, t }: { app: DeliveryApp; t: DT }) {
  const c = useChartColors({ s1: "--aa-info", s2: "--aa-purple", s3: "--aa-kit-amber", s4: "--aa-accent",
    s5: "--aa-green", axis: "--aa-muted" });
  const colors = [c.s1, c.s2, c.s3, c.s4, c.s5];
  const rows = segmentRows(app, t("medianRow"));
  const slow = slowestSegment(app);
  const has = app.lead_time_segments.some((s) => s.n > 0);
  return (
    <Section title={t("secSegments")} description={has && slow ? t("secSegmentsDesc", t(slow)) : undefined}
      testId="delivery-segments">
      {!has ? <span style={small}>{t("secSegmentsNone")}</span> : (
        <div style={{ width: "100%", height: Math.max(140, rows.length * 28 + 40) }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 12, bottom: 4, left: 8 }}>
              <XAxis type="number" tick={{ fontSize: 11, fill: c.axis }} unit=" h" />
              <YAxis type="category" dataKey="label" width={70} tick={{ fontSize: 11, fill: c.axis }} />
              <Tooltip {...CHART_TOOLTIP} formatter={(v, name) => [fmtHours(typeof v === "number" ? v : null), String(name)]} />
              {SEGMENTS.map((s, i) => <Bar key={s} dataKey={s} name={t(s)} stackId="lt" fill={colors[i]}
                isAnimationActive={false} />)}
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
      {has && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 8 }}>
          {SEGMENTS.map((s, i) => {
            const stat = app.lead_time_segments.find((x) => x.segment === s);
            return <span key={s} style={{ ...small, display: "inline-flex", gap: 6, alignItems: "center" }}>
              <span aria-hidden style={{ width: 10, height: 10, borderRadius: 2, background: colors[i] }} />
              {t(s)}: {fmtHours(stat?.p50_h)} ({t("n", stat?.n ?? 0)})</span>;
          })}
        </div>
      )}
    </Section>
  );
}

function useCiColumns(t: DT): ColumnDef<WorkflowRow, unknown>[] {
  return useMemo(() => [
    { id: "name", header: t("colWorkflow"), accessorKey: "name",
      cell: ({ row: { original: w } }) => <span style={{ fontSize: 13 }}>{w.name}</span> },
    { id: "kind", header: t("colKind"), accessorKey: "kind",
      cell: ({ row: { original: w } }) => <span style={small}>{t(`kind${w.kind}` as Parameters<DT>[0])}</span> },
    { id: "runs", header: t("colRunsN"), accessorKey: "runs" },
    { id: "fail", header: t("colFailRate"), accessorKey: "failure_rate",
      cell: ({ row: { original: w } }) => <span style={{ color: (w.failure_rate ?? 0) > 0 ? K.danger : K.ink2 }}>
        {fmtRatio(w.failure_rate)} ({w.failures})</span> },
    { id: "p50", header: t("colP50"), accessorKey: "p50_s", cell: ({ row: { original: w } }) => fmtSeconds(w.p50_s) },
    { id: "p95", header: t("colP95"), accessorKey: "p95_s", cell: ({ row: { original: w } }) => fmtSeconds(w.p95_s) },
    { id: "reruns", header: t("colReruns"), accessorKey: "reruns" },
    { id: "minutes", header: t("colMinutes"), accessorKey: "minutes" },
    { id: "last", header: t("colLast"), cell: ({ row: { original: w } }) => (
      <Ext href={safeGithubUrl(w.last_url)}>{w.last_result ?? "—"}</Ext>) },
  ], [t]);
}

function CiSection({ app, t }: { app: DeliveryApp; t: DT }) {
  const columns = useCiColumns(t);
  return (
    <Section title={t("secCi")} description={t("secCiDesc")} testId="delivery-ci">
      <DataTable<WorkflowRow> tableId={`delivery-ci-${app.id}`} data={app.ci.workflows} columns={columns}
        getRowId={(w) => w.name} paginate={false} searchable={false} tableMinWidth={820} enableCsv
        csvFilename={`ci-health-${app.id}.csv`} emptyTitle={t("noValue")} />
    </Section>
  );
}

function FailedSection({ app, t }: { app: DeliveryApp; t: DT }) {
  const columns = useDeployColumns(app, t);
  return (
    <Section title={t("secFailed")} description={t("secFailedDesc")} testId="delivery-failed">
      <DataTable<DeployRow> tableId={`delivery-failed-${app.id}`} data={app.failed_deploys} columns={columns}
        getRowId={(d) => `${d.sha}-${d.started_at}`} paginate={false} searchable={false} tableMinWidth={860}
        emptyTitle={t("noFailed")} />
    </Section>
  );
}

function FlakySection({ app, t }: { app: DeliveryApp; t: DT }) {
  const { formatDateTime } = useFormat();
  const columns = useMemo<ColumnDef<FlakyRow, unknown>[]>(() => [
    { id: "workflow", header: t("colWorkflow"), accessorKey: "workflow" },
    { id: "job", header: t("colJob"), accessorKey: "job" },
    { id: "flips", header: t("colFlips"), accessorKey: "flips" },
    { id: "last", header: t("colLast"), cell: ({ row: { original: f } }) => (
      <Ext href={safeGithubUrl(f.url)}>{f.last_at ? formatDateTime(f.last_at) : "—"}</Ext>) },
  ], [t, formatDateTime]);
  return (
    <Section title={t("secFlaky")} description={t("secFlakyDesc")} testId="delivery-flaky">
      <DataTable<FlakyRow> tableId={`delivery-flaky-${app.id}`} data={app.flaky} columns={columns}
        getRowId={(f) => `${f.workflow}-${f.job}`} paginate={false} searchable={false} tableMinWidth={600}
        emptyTitle={t("noFlaky")} />
    </Section>
  );
}

function QueueSection({ app, t }: { app: DeliveryApp; t: DT }) {
  const { formatDateTime } = useFormat();
  const q = app.merge_queue;
  const ch = app.changes;
  const columns = useMemo<ColumnDef<ChangeRow, unknown>[]>(() => [
    { id: "pr", header: t("colPr"), cell: ({ row: { original: c } }) => (
      <Ext href={safeGithubUrl(c.url)}>#{c.number} {c.title}</Ext>) },
    { id: "merged", header: t("colMerged"), cell: ({ row: { original: c } }) => (
      <span style={small}>{c.merged_at ? formatDateTime(c.merged_at) : "—"}</span>) },
  ], [t, formatDateTime]);
  const tiles: [string, string, string?][] = [
    [t("queueWait"), fmtHours(q.wait_p50_h), t("n", q.n)],
    [t("queueWaitP95"), fmtHours(q.wait_p95_h), t("n", q.n)],
    [t("ejections"), fmtNum(q.ejections)],
    [t("merged"), fmtNum(ch.merged), t("byKind", ch.by_kind.agent ?? 0, ch.by_kind.human ?? 0, ch.by_kind.bot ?? 0)],
    [t("docsOnly"), fmtNum(ch.docs_only)],
    [t("awaiting"), fmtNum(ch.awaiting_deploy)],
    [t("verifyPass"), fmtRatio(app.verify.pass_rate), t("n", app.verify.n)],
    [t("minutesTotal"), fmtNum(app.ci.minutes.total), t("minutesCoverage", fmtRatio(app.ci.minutes.coverage))],
    [t("reruns"), fmtNum(app.ci.reruns)],
  ];
  return (
    <Section title={t("secQueue")} testId="delivery-queue">
      <div style={grid}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 170px), 1fr))", gap: 10 }}>
          {tiles.map(([label, value, hint]) => <KpiTile key={label} size="sm" label={label} value={value} hint={hint} />)}
        </div>
        <div style={{ ...small, fontWeight: 600 }}>{t("secBots")}</div>
        <DataTable<ChangeRow> tableId={`delivery-bots-${app.id}`} data={ch.bot} columns={columns}
          getRowId={(c) => String(c.number)} paginate={false} searchable={false} tableMinWidth={520}
          emptyTitle={t("noValue")} />
      </div>
    </Section>
  );
}

function AppTab({ app, t }: { app: DeliveryApp; t: DT }) {
  return (
    <div style={grid} data-testid={`delivery-app-${app.id}`}>
      <div style={{ ...small, display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
        <Ext href={repoUrl(app.repo)}><GitBranch size={12} /> {app.repo}</Ext>
        <span>{t("window", app.window.days)}</span>
        {app.complete_since && <span>{t("historyFrom", app.complete_since)}</span>}
      </div>
      <Kpis app={app} t={t} />
      <DeploysSection app={app} t={t} />
      <SegmentsSection app={app} t={t} />
      <CiSection app={app} t={t} />
      <FailedSection app={app} t={t} />
      <FlakySection app={app} t={t} />
      <QueueSection app={app} t={t} />
    </div>
  );
}

// ── page ─────────────────────────────────────────────────────────────────────────────────────────────────────────

function useDelivery() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: KEY, queryFn: () => apiGetDelivery(false), staleTime: 60_000 });
  const refresh = useMutation({
    mutationFn: () => apiGetDelivery(true),
    onSuccess: (d) => {
      qc.setQueryData(KEY, d);
      // AA-1125: the Merge queue and Git map tabs read the same (now re-read) summary
      void qc.invalidateQueries({ queryKey: [...KEY, "queue"] });
      void qc.invalidateQueries({ queryKey: [...KEY, "git"] });
    },
  });
  const error = (refresh.error as Error | null) ?? (q.error as Error | null);
  return { q, refresh, error, busy: q.isFetching || refresh.isPending };
}

function apiGetDelivery(refresh: boolean): Promise<DeliveryResp> {
  return apiGet<DeliveryResp>(refresh ? `${DELIVERY_PATH}?refresh=1` : DELIVERY_PATH);
}

function Description({ data, t }: { data: DeliveryResp | undefined; t: DT }) {
  const { formatDateTime } = useFormat();
  return (
    <>
      {t("description")}
      <br />
      <span style={small}>{t("devNote")}</span>
      {data?.generated_at && <><br /><span style={small}>
        {t("source", formatDateTime(data.generated_at), (data.source?.sha ?? "").slice(0, 12) || "—")}</span></>}
      {data?.collection?.stopped && <><br /><span style={{ ...small, color: K.amber }}>{t("stopped")}</span></>}
    </>
  );
}

function HeaderActions({ t, busy, onRefresh }: { t: DT; busy: boolean; onRefresh: () => void }) {
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
      <a href={docHref(GUIDE_PATH)} style={linkStyle}><BookOpen size={13} /> {t("guide")}</a>
      <a href={SETTINGS_HREF} style={linkStyle}><SlidersHorizontal size={13} /> {t("thresholds")}</a>
      <span title={t("refreshHint")}>
        <Button size="sm" variant="secondary" onClick={onRefresh} disabled={busy} testId="delivery-refresh">
          <RefreshCw size={13} /> {t("refresh")}</Button>
      </span>
    </span>
  );
}

function LoadError({ error, t, onRetry }: { error: Error; t: DT; onRetry: () => void }) {
  const status = (error as { status?: number }).status;
  return status === 404
    ? <EmptyState icon={<ExternalLink size={18} />} title={t("notPublished")} />
    : <ErrorState message={`${t("loadError")}: ${error.message}`} onRetry={onRetry} />;
}

// AA-1125: the two activity tabs sit after Overview; their keys are never an app id.
const QUEUE_TAB = "merge-queue";
const GIT_TAB = "git-map";

function Content({ data, t }: { data: DeliveryResp; t: DT }) {
  const ta = useT(ACTIVITY);
  const { get, setMany } = useUrlFilters();
  const asked = get("app");
  const active = asked === QUEUE_TAB || asked === GIT_TAB || data.apps.some((a) => a.id === asked) ? asked : "overview";
  const app = data.apps.find((a) => a.id === active);
  // a tab change drops the inner choices of the activity tabs (repo, period, directory)
  const open = (id: string) => setMany({ app: id === "overview" ? null : id, repo: null, period: null, dir: null });
  return (
    <div style={grid}>
      <Tabs ariaLabel={t("title")} active={active} onChange={open} testId="delivery-tabs" tabs={[
        { key: "overview", label: t("tabOverview") },
        { key: QUEUE_TAB, label: ta("tabQueue") },
        { key: GIT_TAB, label: ta("tabGit") },
        ...data.apps.map((a) => ({ key: a.id, label: a.name })),
      ]} />
      {active === QUEUE_TAB ? <MergeQueueTab /> : active === GIT_TAB ? <GitMapTab />
        : app ? <AppTab app={app} t={t} /> : <Overview data={data} t={t} open={open} />}
    </div>
  );
}

export default function DeliveryPage() {
  const t = useDeliveryT();
  const { q, refresh, error, busy } = useDelivery();
  const data = q.data;
  return (
    <PageFrame>
      <SkeletonStyle />
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <GitBranch size={18} /> {t("title")} <Badge tone="warning">{t("devNumbers")}</Badge></span>}
        description={<Description data={data} t={t} />}
        actions={<HeaderActions t={t} busy={busy} onRefresh={() => refresh.mutate()} />}
      />
      {error && <LoadError error={error} t={t} onRetry={() => void q.refetch()} />}
      {!data && !error && <KpiTile label={t("deployment_frequency")} value={undefined} />}
      {data && <Content data={data} t={t} />}
    </PageFrame>
  );
}
