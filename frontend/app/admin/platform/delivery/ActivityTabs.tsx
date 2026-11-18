"use client";
// app/admin/platform/delivery/ActivityTabs.tsx — AA-1125: the Merge queue and Git map tabs of Platform › Delivery.
//
// Merge queue: one card per repo with a GitHub merge queue (queue wait p95, ejection rate, flaky rate coloured with the
// settings cis.delivery.queue_*, n next to each value), then per repo: wait and enqueue-to-merge times, throughput per
// day, ejection reasons, merge_group CI minutes, recent entries and flaky signals, each linking to its pull request or
// run on GitHub. Git map: commit-activity calendar per repo, author kinds, churn hotspots by directory with a
// drill-down, top files, merges by branch prefix and the recent commit graph with pull request links.
//
// API: GET /api/admin/platform/delivery?section=queue|git (admin only, lib/rbac.ts). Pure helpers:
// lib/deliveryActivity.ts. Kit only; the repo, period and directory live in the URL (?repo=, ?period=, ?dir=).
// AA-1135: both tabs follow the page's range picker (?range=, ?from=, ?to=; the API serves the range's queue section
// and git window), the queue cards sort (?sort=), the queue repo view adds the wait histogram, ejection reason bars and
// weekly ejection rate, and the git map adds the churn treemap (RangeCharts.tsx).

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, GitCommitHorizontal } from "lucide-react";
import { useMemo } from "react";
import { Bar, BarChart, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Badge, Button, CHART_TOOLTIP, DataTable, EmptyState, ErrorState, K, KpiTile, Section, SegmentedControl,
  apiGet, mono, useFormat, useT, useUrlFilters, type ColumnDef,
} from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import {
  COMMIT_KINDS, GIT_PATH, QUEUE_METRICS, QUEUE_OUTCOMES, QUEUE_PATH, attemptLabel, calendarWeeks, fmtLines,
  gitWindow, kindRows, kindShares, outcomeTone, pickDir, queueN, queueValue, weeklyCommits, windowKeys, worstQueueStatus,
  type ChurnRow, type CommitKind, type GitApp, type GitNode, type GitResp, type GitWindow, type QueueApp,
  type QueueEntry, type QueueMetric, type QueueReason, type QueueResp, type QueueWorkflow,
} from "@/lib/deliveryActivity";
import { fmtHours, fmtNum, fmtRatio, fmtSeconds, repoUrl, safeGithubUrl, shortSha, statusTone } from "@/lib/deliveryMetrics";
import { parseRange, rangeQuery, sortCards, statusRank, withRange } from "@/lib/deliveryRange";
import { ACTIVITY } from "./activity.messages";
import { ChurnTreemap, QueueCharts, SortPicker, WindowLabel, useCardSort } from "./RangeCharts";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const linkStyle = { color: K.accentDeep, display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12 } as const;
const grid = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 } as const;
const tiles = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 170px), 1fr))", gap: 10 } as const;
const cards = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 340px), 1fr))", gap: 14 } as const;

function useActivityT() {
  return useT(ACTIVITY);
}
type AT = ReturnType<typeof useActivityT>;
type Key = Parameters<AT>[0];

function Ext({ href, children }: { href: string | null; children: React.ReactNode }) {
  if (!href) return <>{children}</>;
  return <a href={href} target="_blank" rel="noreferrer" style={linkStyle}>{children}<ExternalLink size={11} /></a>;
}

function fmtQueue(q: QueueApp["queue"], m: QueueMetric): string {
  const v = queueValue(q, m);
  return m === "queue_wait" ? fmtHours(v) : fmtRatio(v);
}

/** The repo picker shared by both tabs ("All repos" + one chip per repo). */
function RepoPicker({ apps, active, t, onPick }: {
  apps: { id: string; name: string }[]; active: string; t: AT; onPick: (id: string) => void;
}) {
  return (
    <SegmentedControl<string> label={t("repoPicker")} appearance="chips" size="sm" testId="delivery-repo-picker"
      value={active} onChange={onPick}
      options={[{ value: "all", label: t("allRepos") }, ...apps.map((a) => ({ value: a.id, label: a.name }))]} />
  );
}

function useSection<T>(path: string, key: string) {
  // AA-1135: the page's range applies to this section too (its own cache entry per range)
  const { get } = useUrlFilters();
  const choice = parseRange(get);
  return useQuery({ queryKey: ["platform-delivery", key, rangeQuery(choice)], queryFn: () => apiGet<T>(withRange(path, choice)),
    staleTime: 60_000, retry: false });
}

/** Error, loading or "nothing published yet" of a tab's section; null once there are apps to show. */
function TabState({ q, empty, t }: {
  q: { error: unknown; isLoading: boolean; data?: { apps: unknown[] }; refetch: () => unknown }; empty: string; t: AT;
}) {
  const error = q.error as Error | null;
  if (error) return <ErrorState message={`${t("loadError")}: ${error.message}`} onRetry={() => void q.refetch()} />;
  if (q.isLoading) return <KpiTile label={t("entries")} value={undefined} />;
  if (q.data && !q.data.apps.length) return <EmptyState title={empty} />;
  return null;
}

// ── merge queue ──────────────────────────────────────────────────────────────────────────────────────────────────

function QueueMetricTile({ app, m, t }: { app: QueueApp; m: QueueMetric; t: AT }) {
  const status = app.queue_status?.[m];
  return (
    <KpiTile size="sm" label={t(m)} value={fmtQueue(app.queue, m)} info={t(`tip${m}` as Key)}
      testId={`queue-${app.id}-${m}`} hintTone={statusTone(status)}
      hint={`${t("n", queueN(app.queue, m))}${status === "few" ? ` · ${t("few")}` : ""}`} />
  );
}

function QueueCard({ app, t, open }: { app: QueueApp; t: AT; open: (id: string) => void }) {
  const q = app.queue;
  const tone = statusTone(worstQueueStatus(app.queue_status));
  const border = tone === "danger" ? K.danger : tone === "warning" ? K.amber : K.line;
  return (
    <Section testId={`queue-card-${app.id}`} style={{ borderColor: border }} title={app.name}
      description={<span style={small}><Ext href={repoUrl(app.repo)}>{app.repo}</Ext>
        <WindowLabel window={app.window} clipOnly testId={`queue-card-window-${app.id}`} /></span>}
      actions={<Button size="sm" variant="secondary" onClick={() => open(app.id)} testId={`queue-open-${app.id}`}>
        {t("details")}</Button>}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 140px), 1fr))", gap: 8 }}>
        {QUEUE_METRICS.map((m) => <QueueMetricTile key={m} app={app} m={m} t={t} />)}
        <KpiTile size="sm" label={t("entries")} value={fmtNum(q.entries)}
          hint={t("entriesHint", q.merged, q.ejected, q.removed)} />
        <KpiTile size="sm" label={t("ciMinutes")} value={fmtNum(q.ci.minutes)}
          hint={t("ciMinutesHint", q.ci.runs, q.ci.reruns)} />
        <KpiTile size="sm" label={t("inQueue")} value={fmtNum(q.in_queue)} />
      </div>
    </Section>
  );
}

function ThroughputChart({ app, t }: { app: QueueApp; t: AT }) {
  const c = useChartColors({ merged: "--aa-green", ejected: "--aa-red", removed: "--aa-muted", axis: "--aa-muted" });
  const rows = app.queue.throughput.map((d) => ({ ...d, label: d.date.slice(5) }));
  const colors: Record<string, string> = { merged: c.merged, ejected: c.ejected, removed: c.removed };
  return (
    <Section title={t("secThroughput")} description={t("secThroughputDesc")} testId="queue-throughput">
      <div style={{ width: "100%", height: 200 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: -16 }}>
            <XAxis dataKey="label" tick={{ fontSize: 10, fill: c.axis }} interval="preserveStartEnd" minTickGap={12} />
            <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: c.axis }} width={36} />
            <Tooltip {...CHART_TOOLTIP} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {QUEUE_OUTCOMES.map((o) => <Bar key={o} dataKey={o} name={t(`outcome${o}` as Key)} stackId="q"
              fill={colors[o]} isAnimationActive={false} />)}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Section>
  );
}

function ReasonsSection({ app, t }: { app: QueueApp; t: AT }) {
  const { formatDateTime } = useFormat();
  const columns = useMemo<ColumnDef<QueueReason, unknown>[]>(() => [
    { id: "reason", header: t("colReason"), accessorKey: "reason",
      cell: ({ row: { original: r } }) => <span style={{ fontSize: 13 }}>{t(`reason${r.reason}` as Key)}</span> },
    { id: "n", header: t("colCount"), accessorKey: "n" },
    { id: "wf", header: t("colWorkflows"), cell: ({ row: { original: r } }) => (
      <span style={small}>{Object.entries(r.workflows).map(([w, n]) => `${w} (${n})`).join(", ") || "—"}</span>) },
    { id: "last", header: t("colLast"), cell: ({ row: { original: r } }) => (
      <Ext href={safeGithubUrl(r.last_url)}>{r.last_pr ? `#${r.last_pr}` : "—"}
        {r.last_at ? ` · ${formatDateTime(r.last_at)}` : ""}</Ext>) },
  ], [t, formatDateTime]);
  return (
    <Section title={t("secReasons")} description={t("secReasonsDesc")} testId="queue-reasons">
      <DataTable<QueueReason> tableId={`queue-reasons-${app.id}`} data={app.queue.reasons} columns={columns}
        getRowId={(r) => r.reason} paginate={false} searchable={false} tableMinWidth={560}
        emptyTitle={t("noReasons")} />
    </Section>
  );
}

function QueueCiSection({ app, t }: { app: QueueApp; t: AT }) {
  const ci = app.queue.ci;
  const columns = useMemo<ColumnDef<QueueWorkflow, unknown>[]>(() => [
    { id: "name", header: t("colWorkflow"), accessorKey: "name" },
    { id: "runs", header: t("colRuns"), accessorKey: "runs" },
    { id: "failures", header: t("colFailures"), accessorKey: "failures",
      cell: ({ row: { original: w } }) => <span style={{ color: w.failures ? K.danger : K.ink2 }}>{w.failures}</span> },
    { id: "reruns", header: t("colReruns"), accessorKey: "reruns" },
    { id: "minutes", header: t("colMinutes"), accessorKey: "minutes" },
  ], [t]);
  return (
    <Section title={t("secQueueCi")} description={t("secQueueCiDesc")} testId="queue-ci">
      <div style={grid}>
        <div style={tiles}>
          <KpiTile size="sm" label={t("ciMinutes")} value={fmtNum(ci.minutes)} hint={t("ciMinutesHint", ci.runs, ci.reruns)} />
          <KpiTile size="sm" label={t("ciFailRate")} value={fmtRatio(ci.failure_rate)} hint={t("n", ci.runs)} />
          <KpiTile size="sm" label={t("ciP95")} value={fmtSeconds(ci.p95_s)} hint={t("n", ci.runs)} />
        </div>
        <DataTable<QueueWorkflow> tableId={`queue-ci-${app.id}`} data={ci.workflows} columns={columns}
          getRowId={(w) => w.name} paginate={false} searchable={false} tableMinWidth={480}
          emptyTitle={t("noEntries")} />
      </div>
    </Section>
  );
}

function useEntryColumns(t: AT): ColumnDef<QueueEntry, unknown>[] {
  const { formatDateTime } = useFormat();
  return useMemo(() => [
    { id: "pr", header: t("colPr"), cell: ({ row: { original: e } }) => (
      <Ext href={safeGithubUrl(e.url)}>#{e.pr}{e.title ? ` ${e.title}` : ""}</Ext>) },
    { id: "outcome", header: t("colOutcome"), accessorKey: "outcome",
      cell: ({ row: { original: e } }) => <Badge tone={outcomeTone(e.outcome)}>{t(`outcome${e.outcome}` as Key)}</Badge> },
    { id: "reason", header: t("colReason"), cell: ({ row: { original: e } }) => (
      <span style={small}>{e.reason ? t(`reason${e.reason}` as Key) : "—"}
        {e.failed_workflows.length ? ` (${e.failed_workflows.join(", ")})` : ""}</span>) },
    { id: "wait", header: t("colWait"), accessorKey: "wait_h",
      cell: ({ row: { original: e } }) => <span style={small}>{fmtHours(e.wait_h)}</span> },
    { id: "attempt", header: t("colAttempt"), cell: ({ row: { original: e } }) => (
      <span style={small}>{attemptLabel(e)}{e.new_commit ? ` · ${t("newCommit")}` : ""}</span>) },
    { id: "runs", header: t("colRuns"), cell: ({ row: { original: e } }) => (
      <Ext href={safeGithubUrl(e.run_url)}>{e.runs}{e.reruns ? ` (+${e.reruns})` : ""}</Ext>) },
    { id: "minutes", header: t("colMinutes"), accessorKey: "minutes" },
    { id: "flaky", header: t("colFlaky"), cell: ({ row: { original: e } }) => (
      e.flaky ? <Badge tone="warning">{t(`flaky${e.flaky}` as Key)}</Badge> : <span style={small}>—</span>) },
    { id: "left", header: t("colLeft"), cell: ({ row: { original: e } }) => (
      <span style={small}>{e.left_at ? formatDateTime(e.left_at) : "—"}</span>) },
  ], [t, formatDateTime]);
}

function EntriesSection({ app, t, flaky }: { app: QueueApp; t: AT; flaky?: boolean }) {
  const columns = useEntryColumns(t);
  const rows = flaky ? app.queue.flaky_entries : app.queue.recent;
  return (
    <Section title={t(flaky ? "secFlakyEntries" : "secRecent")} description={t(flaky ? "secFlakyEntriesDesc" : "secRecentDesc")}
      testId={flaky ? "queue-flaky" : "queue-recent"}>
      <DataTable<QueueEntry> tableId={`queue-${flaky ? "flaky" : "recent"}-${app.id}`} data={rows} columns={columns}
        getRowId={(e) => `${e.pr}-${e.attempt}`} paginate={false} searchable={false} tableMinWidth={980}
        enableCsv csvFilename={`merge-queue-${app.id}.csv`} emptyTitle={t(flaky ? "noFlaky" : "noEntries")} />
    </Section>
  );
}

function QueueRepo({ app, t }: { app: QueueApp; t: AT }) {
  const q = app.queue;
  return (
    <div style={grid} data-testid={`queue-app-${app.id}`}>
      <span style={small}><Ext href={repoUrl(app.repo)}>{app.repo}</Ext> · <WindowLabel window={app.window}
        testId={`queue-window-${app.id}`} />{q.complete_since && <> · {t("historyFrom", q.complete_since)}</>}</span>
      <div style={tiles}>
        {QUEUE_METRICS.map((m) => <QueueMetricTile key={m} app={app} m={m} t={t} />)}
        <KpiTile size="sm" label={t("waitP50")} value={fmtHours(q.wait.p50_h)} hint={t("n", q.wait.n)} />
        <KpiTile size="sm" label={t("toMerge")} value={fmtHours(q.to_merge.p50_h)} hint={t("n", q.to_merge.n)}
          info={t("tipToMerge")} />
        <KpiTile size="sm" label={t("toMergeP95")} value={fmtHours(q.to_merge.p95_h)} hint={t("n", q.to_merge.n)}
          info={t("tipToMerge")} />
        <KpiTile size="sm" label={t("entries")} value={fmtNum(q.entries)} hint={t("entriesHint", q.merged, q.ejected, q.removed)} />
        <KpiTile size="sm" label={t("inQueue")} value={fmtNum(q.in_queue)} />
      </div>
      <QueueCharts queue={q} reasonLabel={(r) => t(`reason${r.reason}` as Key)} />
      <ThroughputChart app={app} t={t} />
      <ReasonsSection app={app} t={t} />
      <QueueCiSection app={app} t={t} />
      <EntriesSection app={app} t={t} flaky />
      <EntriesSection app={app} t={t} />
    </div>
  );
}

export function MergeQueueTab() {
  const t = useActivityT();
  const { get, set } = useUrlFilters();
  const q = useSection<QueueResp>(QUEUE_PATH, "queue");
  const apps = q.data?.apps ?? [];
  const asked = get("repo");
  const app = apps.find((a) => a.id === asked);
  const [sort, setSort] = useCardSort(QUEUE_METRICS);
  const sorted = sortCards(apps, sort, (a) => statusRank(worstQueueStatus(a.queue_status)),
    (a, m) => queueValue(a.queue, m as QueueMetric), () => false);
  return (
    <div style={grid} data-testid="delivery-queue-tab">
      <span style={small}>{t("queueIntro")}</span>
      <TabState q={q} empty={t("noQueueApps")} t={t} />
      {apps.length > 0 && <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
        <RepoPicker apps={apps} active={app?.id ?? "all"} t={t}
          onPick={(id) => set("repo", id === "all" ? null : id)} />
        {!app && <SortPicker value={sort} onChange={setSort}
          metrics={QUEUE_METRICS.map((m) => ({ key: m, label: t(m) }))} />}
      </div>}
      {app ? <QueueRepo app={app} t={t} /> : apps.length > 0 && (
        <div style={cards}>{sorted.map((a) => <QueueCard key={a.id} app={a} t={t} open={(id) => set("repo", id)} />)}</div>
      )}
    </div>
  );
}

// ── git map ──────────────────────────────────────────────────────────────────────────────────────────────────────

const HEAT_OPACITY = [0, 0.3, 0.5, 0.75, 1];

function Heatmap({ app, t, cell = 11 }: { app: GitApp; t: AT; cell?: number }) {
  const weeks = useMemo(() => calendarWeeks(app.git.daily), [app.git.daily]);
  return (
    <div style={{ overflowX: "auto", maxWidth: "100%" }}>
      <div role="img" aria-label={`${t("secHeatmap")}: ${app.name}`} data-testid={`git-heatmap-${app.id}`}
        style={{ display: "inline-grid", gridAutoFlow: "column", gridTemplateRows: `repeat(7, ${cell}px)`,
          gridAutoColumns: `${cell}px`, gap: 2 }}>
        {weeks.flatMap((col, w) => col.map((d, i) => (
          <span key={`${w}-${i}`} title={d ? t("heatDay", d.date, d.n) : undefined}
            style={{ width: cell, height: cell, borderRadius: 2, position: "relative",
              background: d ? K.line2 : "transparent" }}>
            {d && d.level > 0 && <span style={{ position: "absolute", inset: 0, borderRadius: 2, background: K.accent,
              opacity: HEAT_OPACITY[d.level] }} />}
          </span>
        )))}
      </div>
    </div>
  );
}

function HeatLegend({ t }: { t: AT }) {
  return (
    <span style={{ ...small, display: "inline-flex", gap: 4, alignItems: "center" }}>
      {t("less")}
      {HEAT_OPACITY.map((o, i) => (
        <span key={i} aria-hidden style={{ width: 10, height: 10, borderRadius: 2, position: "relative", background: K.line2 }}>
          {o > 0 && <span style={{ position: "absolute", inset: 0, borderRadius: 2, background: K.accent, opacity: o }} />}
        </span>
      ))}
      {t("more")}
    </span>
  );
}

const KIND_VAR: Record<CommitKind, string> = {
  agent: "--aa-accent", ci_bot: "--aa-info", release_please: "--aa-purple", dependabot: "--aa-green",
  bot: "--aa-muted", human: "--aa-slate",
};

function useKindColors(): Record<CommitKind, string> & { axis: string } {
  return useChartColors({ ...KIND_VAR, axis: "--aa-muted" }) as Record<CommitKind, string> & { axis: string };
}

function KindBar({ win, t }: { win: GitWindow | undefined; t: AT }) {
  const colors = useKindColors();
  const shares = kindShares(win?.by_kind);
  if (!shares.length) return <span style={small}>{t("noCommits")}</span>;
  return (
    <div style={{ display: "grid", gap: 6 }}>
      <div aria-hidden style={{ display: "flex", height: 10, borderRadius: 5, overflow: "hidden", background: K.line2 }}>
        {shares.map((s) => <span key={s.kind} style={{ width: `${s.share * 100}%`, background: colors[s.kind] }} />)}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        {shares.map((s) => (
          <span key={s.kind} style={{ ...small, display: "inline-flex", gap: 6, alignItems: "center" }}>
            <span aria-hidden style={{ width: 10, height: 10, borderRadius: 2, background: colors[s.kind] }} />
            {t(`kind${s.kind}` as Key)}: {s.n} ({fmtRatio(s.share)})
          </span>
        ))}
      </div>
    </div>
  );
}

function KindChart({ apps, period, t }: { apps: GitApp[]; period: string; t: AT }) {
  const colors = useKindColors();
  const rows = kindRows(apps, period);
  return (
    <Section title={t("secKinds")} description={t("secKindsDesc")} testId="git-kinds">
      <div style={{ width: "100%", height: Math.max(160, rows.length * 34 + 60) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 12, bottom: 4, left: 8 }}>
            <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11, fill: colors.axis }} />
            <YAxis type="category" dataKey="name" width={96} tick={{ fontSize: 11, fill: colors.axis }} />
            <Tooltip {...CHART_TOOLTIP} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {COMMIT_KINDS.map((k) => <Bar key={k} dataKey={k} name={t(`kind${k}` as Key)} stackId="k" fill={colors[k]}
              isAnimationActive={false} />)}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Section>
  );
}

function useChurnColumns(t: AT, withKinds: boolean): ColumnDef<ChurnRow, unknown>[] {
  const { formatDateTime } = useFormat();
  return useMemo(() => {
    const cols: ColumnDef<ChurnRow, unknown>[] = [
      { id: "path", header: t("colPath"), accessorKey: "path",
        cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12, overflowWrap: "anywhere" }}>
          {r.path}</span> },
      { id: "churn", header: t("colChurn"), accessorKey: "churn" },
      { id: "add", header: t("colAdded"), accessorKey: "additions" },
      { id: "del", header: t("colDeleted"), accessorKey: "deletions" },
      { id: "commits", header: t("colCommits"), accessorKey: "commits" },
    ];
    if (withKinds) {
      cols.push({ id: "files", header: t("colFiles"), accessorKey: "files" });
      cols.push({ id: "kinds", header: t("colKinds"), cell: ({ row: { original: r } }) => (
        <span style={small}>{kindShares(r.by_kind).map((s) => `${t(`kind${s.kind}` as Key)} ${s.n}`).join(" · ") || "—"}
        </span>) });
    } else {
      cols.push({ id: "last", header: t("colLastChange"), cell: ({ row: { original: r } }) => (
        <span style={small}>{r.last_at ? formatDateTime(r.last_at) : "—"}</span>) });
    }
    return cols;
  }, [t, withKinds, formatDateTime]);
}

function HotspotSection({ app, win, t }: { app: GitApp; win: GitWindow | undefined; t: AT }) {
  const { get, set } = useUrlFilters();
  const dir = pickDir(win, get("dir") || null);
  const dirCols = useChurnColumns(t, true);
  const childCols = useChurnColumns(t, false);
  return (
    <>
    <ChurnTreemap win={win} dir={dir} onPick={(path) => set("dir", path)} />
    <Section title={dir ? t("inDir", dir.path) : t("secHotspots")} description={t("secHotspotsDesc")}
      testId="git-hotspots"
      actions={dir ? <Button size="sm" variant="secondary" onClick={() => set("dir", null)} testId="git-hotspots-back">
        {t("backToDirs")}</Button> : undefined}>
      {dir ? (
        <DataTable<ChurnRow> tableId={`git-dir-${app.id}`} data={dir.children ?? []} columns={childCols}
          getRowId={(r) => r.path} paginate={false} searchable={false} tableMinWidth={640}
          emptyTitle={t("noCommits")} />
      ) : (
        <DataTable<ChurnRow> tableId={`git-dirs-${app.id}`} data={win?.dirs ?? []} columns={dirCols}
          getRowId={(r) => r.path} paginate={false} searchable={false} tableMinWidth={760}
          onRowClick={(r) => set("dir", r.path)} enableCsv csvFilename={`churn-${app.id}.csv`}
          emptyTitle={t("noCommits")} />
      )}
    </Section>
    </>
  );
}

function FilesSection({ app, win, t }: { app: GitApp; win: GitWindow | undefined; t: AT }) {
  const columns = useChurnColumns(t, false);
  return (
    <Section title={t("secFiles")} testId="git-files">
      <DataTable<ChurnRow> tableId={`git-files-${app.id}`} data={win?.files ?? []} columns={columns}
        getRowId={(r) => r.path} paginate={false} searchable={false} tableMinWidth={720} enableCsv
        csvFilename={`hot-files-${app.id}.csv`} emptyTitle={t("noCommits")} />
    </Section>
  );
}

function CommitGraph({ app, t }: { app: GitApp; t: AT }) {
  const { formatDateTime } = useFormat();
  const colors = useKindColors();
  const nodes = app.git.graph ?? [];
  if (!nodes.length) return <Section title={t("secGraph")}><span style={small}>{t("noCommits")}</span></Section>;
  return (
    <Section title={t("secGraph")} description={t("secGraphDesc")} testId="git-graph">
      <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {nodes.map((c: GitNode, i) => (
          <li key={c.sha} style={{ display: "grid", gridTemplateColumns: "18px minmax(0, 1fr)", gap: 8 }}>
            <span aria-hidden style={{ position: "relative", display: "block" }}>
              {i < nodes.length - 1 && <span style={{ position: "absolute", left: 8, top: 14, bottom: -2, width: 2,
                background: K.line }} />}
              <span style={{ position: "absolute", left: 4, top: 6, width: 10, height: 10,
                borderRadius: c.parents.length > 1 ? 2 : 5, background: colors[c.kind ?? "human"] ?? K.muted,
                border: `2px solid ${K.card}` }} />
            </span>
            <span style={{ display: "grid", gap: 2, padding: "4px 0 10px", minWidth: 0 }}>
              <span style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{c.title ?? "—"}</span>
              <span style={{ ...small, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                <Ext href={safeGithubUrl(c.url)}><span style={{ fontFamily: mono }}>{shortSha(c.sha)}</span></Ext>
                {c.pr && <Ext href={safeGithubUrl(c.pr_url)}>#{c.pr}</Ext>}
                <span>{t(`kind${c.kind ?? "human"}` as Key)}</span>
                <span>{fmtLines(c.additions, c.deletions)}</span>
                {c.files !== null && <span>{t("filesN", c.files)}</span>}
                {c.at && <span>{formatDateTime(c.at)}</span>}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </Section>
  );
}

function WeeklyChart({ app, t }: { app: GitApp; t: AT }) {
  const c = useChartColors({ bar: "--aa-accent", axis: "--aa-muted" });
  const rows = useMemo(() => weeklyCommits(app.git.daily).map((w) => ({ ...w, label: w.week.slice(5) })),
    [app.git.daily]);
  if (!rows.length) return null;
  return (
    <div style={{ width: "100%", height: 140 }} data-testid="git-weekly">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: -16 }}>
          <XAxis dataKey="label" tick={{ fontSize: 10, fill: c.axis }} interval="preserveStartEnd" minTickGap={12} />
          <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: c.axis }} width={36} />
          <Tooltip {...CHART_TOOLTIP} />
          <Bar dataKey="n" name={t("commitsPerWeek")} fill={c.bar} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function GitTiles({ app, win, days, t }: { app: GitApp; win: GitWindow | undefined; days: number; t: AT }) {
  return (
    <div style={tiles}>
      <KpiTile size="sm" label={t("commits")} value={fmtNum(win?.commits)} hint={t("commitsHint", days)} />
      <KpiTile size="sm" label={t("lines")} value={fmtLines(win?.additions, win?.deletions)}
        hint={t("n", win?.files_read ?? 0)} />
      <KpiTile size="sm" label={t("merges")} value={fmtNum(win?.merges)} />
      <KpiTile size="sm" label={t("coverage")} value={fmtRatio(app.git.files_coverage)} info={t("tipCoverage")} />
    </div>
  );
}

function PrefixSection({ win, t }: { win: GitWindow | undefined; t: AT }) {
  const prefixes = Object.entries(win?.merges_by_prefix ?? {});
  return (
    <Section title={t("secPrefixes")} testId="git-prefixes">
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {prefixes.map(([p, n]) => <Badge key={p} tone="neutral" dot={false}>{p}: {n}</Badge>)}
        {!prefixes.length && <span style={small}>{t("noCommits")}</span>}
      </div>
    </Section>
  );
}

/** Days of the window shown: the served range's, else the fixed window's (d30 / d90). */
function windowDays(app: GitApp, period: string): number {
  const r = app.git.range;
  if (r?.start && r?.end) return Math.round((Date.parse(r.end) - Date.parse(r.start)) / 86_400_000);
  return Number(period.slice(1)) || app.git.days;
}

function GitRepo({ app, period, t }: { app: GitApp; period: string; t: AT }) {
  const win = gitWindow(app.git, period);
  return (
    <div style={grid} data-testid={`git-app-${app.id}`}>
      {app.git.range && <span style={small}><Ext href={repoUrl(app.repo)}>{app.repo}</Ext> · <WindowLabel
        window={app.git.range} completeSince={app.git.range.complete_since} testId={`git-window-${app.id}`} />
        {app.git.range.complete_since && <> · {t("historyFrom", app.git.range.complete_since)}</>}</span>}
      <GitTiles app={app} win={win} days={windowDays(app, period)} t={t} />
      <Section title={t("secHeatmap")} description={t("secHeatmapDesc")} testId="git-heatmap">
        <div style={{ display: "grid", gap: 8 }}><Heatmap app={app} t={t} cell={13} /><HeatLegend t={t} />
          <WeeklyChart app={app} t={t} /></div>
      </Section>
      <Section title={t("secKinds")} description={t("secKindsDesc")} testId="git-kinds">
        <KindBar win={win} t={t} />
      </Section>
      <PrefixSection win={win} t={t} />
      <HotspotSection app={app} win={win} t={t} />
      <FilesSection app={app} win={win} t={t} />
      <CommitGraph app={app} t={t} />
    </div>
  );
}

function GitOverview({ apps, period, t, open }: { apps: GitApp[]; period: string; t: AT; open: (id: string) => void }) {
  return (
    <div style={grid}>
      <div style={cards}>
        {apps.map((a) => {
          const win = gitWindow(a.git, period);
          return (
            <Section key={a.id} testId={`git-card-${a.id}`} title={a.name}
              description={<span style={small}><Ext href={repoUrl(a.repo)}>{a.repo}</Ext>
                {a.git.complete_since && <> · {t("historyFrom", a.git.complete_since)}</>}
                {a.git.range && <WindowLabel window={a.git.range} completeSince={a.git.range.complete_since} clipOnly
                  testId={`git-card-window-${a.id}`} />}</span>}
              actions={<Button size="sm" variant="secondary" onClick={() => open(a.id)} testId={`git-open-${a.id}`}>
                {t("details")}</Button>}>
              <div style={{ display: "grid", gap: 10 }}>
                <Heatmap app={a} t={t} />
                <span style={{ ...small, display: "inline-flex", gap: 6, alignItems: "center" }}>
                  <GitCommitHorizontal size={13} /> {t("commits")}: {fmtNum(win?.commits)} ·
                  {" "}{fmtLines(win?.additions, win?.deletions)} · {t("merges")}: {fmtNum(win?.merges)}
                </span>
                <KindBar win={win} t={t} />
              </div>
            </Section>
          );
        })}
      </div>
      <KindChart apps={apps} period={period} t={t} />
    </div>
  );
}

function GitPickers({ apps, active, keys, period, t }: {
  apps: GitApp[]; active: string; keys: string[]; period: string; t: AT;
}) {
  const { set, setMany } = useUrlFilters();
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
      <RepoPicker apps={apps} active={active} t={t}
        onPick={(id) => setMany({ repo: id === "all" ? null : id, dir: null })} />
      {keys.length > 1 && !apps[0]?.git.range && <SegmentedControl<string> label={t("windowPicker")} size="sm" value={period}
        testId="git-period" onChange={(v) => set("period", v)}
        options={keys.map((k) => ({ value: k, label: t("lastDays", Number(k.slice(1))) }))} />}
    </div>
  );
}

/** The period in the URL when the summary has it, else the shortest window. */
function pickPeriod(keys: string[], asked: string): string {
  return keys.includes(asked) ? asked : keys[0] ?? "d30";
}

export function GitMapTab() {
  const t = useActivityT();
  const { get, setMany } = useUrlFilters();
  const q = useSection<GitResp>(GIT_PATH, "git");
  const apps = q.data?.apps ?? [];
  const app = apps.find((a) => a.id === get("repo"));
  const keys = windowKeys(apps[0]?.git);
  const period = pickPeriod(keys, get("period"));
  const has = apps.length > 0;
  return (
    <div style={grid} data-testid="delivery-git-tab">
      <span style={small}>{t("gitIntro")}</span>
      <TabState q={q} empty={t("noGitApps")} t={t} />
      {has && <GitPickers apps={apps} active={app?.id ?? "all"} keys={keys} period={period} t={t} />}
      {app ? <GitRepo app={app} period={period} t={t} />
        : has && <GitOverview apps={apps} period={period} t={t} open={(id) => setMany({ repo: id, dir: null })} />}
    </div>
  );
}
