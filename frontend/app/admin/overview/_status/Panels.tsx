"use client";
// app/admin/overview/_status/Panels.tsx — AA-1140: the job queues (shared.job live, crm.job through the CRM ops
// contract, booking.job not measured yet), the deploy gate and the top failing kinds; the shared resources (RDS CPU,
// connections, free storage, busiest role, transaction ID age, ECS tasks) with their limits and headroom; the cost of
// the month (spent, forecast, budget); the AI gateway (today against each app's daily cap, the range by connection).

import Link from "next/link";
import { Line, LineChart, ReferenceLine, ResponsiveContainer, YAxis } from "recharts";
import { Badge, DataTable, K, useLang, type ColumnDef, type Tone } from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import {
  APP_LABEL, fmtNum, fmtPct, headroom, inApps, levelTone, sortingParam, tableSorting, withSort, yTop, type Ai,
  type AiConn, type Cost, type Jobs, type Queue, type ResourceTile, type Section, type View,
} from "@/lib/opsStatus";
import { fmtCount, fmtUsd } from "@/lib/numberFormat";
import { Card, NotMeasured, WhenUp, levelColor, num, rowWrap, small, useST, type ST } from "./ui";

type SetView = (v: Partial<View>) => void;
const link = { ...small, color: K.accentDeep, fontWeight: 600 } as const;

// ── jobs ────────────────────────────────────────────────────────────────────────────────────────────────────────

function queueColumns(t: ST): ColumnDef<Queue>[] {
  const n = (v: number | undefined) => (v == null ? "—" : fmtCount(v));
  return [
    { id: "queue", header: t("colQueue"), accessorFn: (q) => q.name,
      cell: ({ row }) => <span style={{ display: "grid" }}><code>{row.original.name}</code>
        <span style={small}>{APP_LABEL[row.original.app]}</span></span> },
    { id: "state", header: t("colState"), accessorFn: (q) => q.status, enableSorting: false,
      cell: ({ row }) => row.original.status === "ok" ? <Badge tone={levelTone(row.original.level) as Tone}>
        {row.original.level}</Badge> : <NotMeasured reason={row.original.reason} /> },
    { id: "queued", header: t("colQueued"), accessorFn: (q) => q.queued ?? null, cell: ({ row }) => n(row.original.queued) },
    { id: "running", header: t("colRunning"), accessorFn: (q) => q.running ?? null,
      cell: ({ row }) => n(row.original.running) },
    { id: "failed", header: t("colFailed"), accessorFn: (q) => q.failed ?? null, cell: ({ row }) => n(row.original.failed) },
    { id: "wait", header: t("colWait"), accessorFn: (q) => q.oldest_wait_min ?? null,
      cell: ({ row }) => <span style={{ color: levelColor(row.original.level) }}>
        {row.original.oldest_wait_min == null ? "—" : `${fmtNum(row.original.oldest_wait_min)} min`}</span> },
    { id: "workers", header: t("workers"), accessorFn: (q) => q.workers ?? null,
      cell: ({ row }) => row.original.workers ?? "—" },
  ];
}

function FailingKinds({ jobs, t }: { jobs: Jobs; t: ST }) {
  const kinds = jobs.failing_kinds;
  const max = Math.max(1, ...kinds.map((k) => k.n));
  if (kinds.length === 0) return <span style={small}>{t("noFailing")}</span>;
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid="status-failing-kinds">
      {kinds.map((k) => (
        <div key={k.kind} title={k.last_error ?? undefined} style={{ display: "grid", gap: 2 }}>
          <span style={{ ...rowWrap, ...small }}><code>{k.kind}</code><span style={num}>{k.n}</span></span>
          <span style={{ height: 6, borderRadius: 3, background: K.line }}>
            <span style={{ display: "block", height: 6, borderRadius: 3, width: `${(100 * k.n) / max}%`,
              background: K.danger }} /></span>
        </div>
      ))}
    </div>
  );
}

export function JobsCard({ section, view, setView }: { section: Section<Jobs>; view: View; setView: SetView }) {
  const t = useST();
  return (
    <Card title={t("jobsTitle")} testId="status-jobs" asOf={section.as_of}
      actions={<Link href="/admin/jobs" style={link}>{t("lJobs")}</Link>}>
      <WhenUp section={section}>
        {(jobs) => (
          <>
            <DataTable<Queue> tableId="status-queues" data={jobs.queues.filter((q) => inApps(q.app, view.apps))}
              columns={queueColumns(t)} getRowId={(q) => q.id} searchable={false} paginate={false} compact
              tableMinWidth={620} sorting={tableSorting(view.sort.q)}
              onSortingChange={(s) => setView({ sort: withSort(view.sort, "q", sortingParam(s)) })} />
            <div style={rowWrap} data-testid="status-gate">
              <span style={small}>{t("gate")}:</span>
              {jobs.deploy_gate.safe_to_deploy == null ? <NotMeasured reason={null} />
                : <Badge tone={jobs.deploy_gate.safe_to_deploy ? "success" : "warning"}>
                  {jobs.deploy_gate.safe_to_deploy ? t("gateOpen")
                    : t("gateBlocked", jobs.deploy_gate.blocked_by.join(", "))}</Badge>}
            </div>
            <div style={{ fontWeight: 600, fontSize: 12, color: K.ink }}>{t("topFailing")}</div>
            <FailingKinds jobs={jobs} t={t} />
          </>
        )}
      </WhenUp>
    </Card>
  );
}

// ── shared resources ────────────────────────────────────────────────────────────────────────────────────────────

function Spark({ tile }: { tile: ResourceTile }) {
  const c = useChartColors({ line: "--aa-accent", warn: "--aa-amber", crit: "--aa-red" });
  if (!tile.series?.length) return null;
  const rows = tile.series.map((v, i) => ({ i, v }));
  const top = yTop(tile.series, [tile.warn, tile.crit]);
  return (
    <div style={{ height: 44 }} aria-hidden>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 2, right: 2, left: 2, bottom: 2 }}>
          <YAxis hide domain={[0, top]} />
          {tile.warn != null && <ReferenceLine y={tile.warn} stroke={c.warn} strokeDasharray="4 3" />}
          {tile.crit != null && <ReferenceLine y={tile.crit} stroke={c.crit} />}
          <Line dataKey="v" stroke={c.line} strokeWidth={1.4} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function ResourceBox({ tile }: { tile: ResourceTile }) {
  const t = useST();
  const room = headroom(tile.latest, tile.warn ?? tile.crit, tile.dir);
  const down = tile.status === "unavailable";
  return (
    <div data-testid={`status-res-${tile.id}`} style={{ border: `1px solid ${K.line}`, borderRadius: 8,
      padding: "10px 12px", display: "grid", gap: 4, minWidth: 0, background: K.card }}>
      <span style={{ ...rowWrap, justifyContent: "space-between" }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: K.ink }}>{tile.label}</span>
        <Badge tone={levelTone(tile.level) as Tone}>{tile.level === "none" ? t("notMeasured") : tile.level}</Badge>
      </span>
      {down ? <NotMeasured reason={null} /> : (
        <span style={{ ...num, fontSize: 18, fontWeight: 600, color: levelColor(tile.level) }}>
          {fmtNum(tile.latest)} <span style={small}>{tile.unit}</span></span>)}
      <Spark tile={tile} />
      <span style={small}>
        {tile.worst != null && t("latestWorst", fmtNum(tile.latest), fmtNum(tile.worst))}
        {tile.warn != null && ` · ${t("warnLine", fmtNum(tile.warn))}`}
        {tile.crit != null && ` · ${t("critLine", fmtNum(tile.crit))}`}
        {room != null && ` · ${t("headroom", fmtNum(room))}`}
      </span>
      {tile.note && <span style={{ ...small, overflowWrap: "anywhere" }}>{tile.note}</span>}
    </div>
  );
}

export function ResourcesCard({ section }: { section: Section<{ tiles: ResourceTile[] }> }) {
  const t = useST();
  return (
    <Card title={t("resourcesTitle")} testId="status-resources" asOf={section.as_of}>
      <WhenUp section={section}>
        {(d) => (
          <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))" }}>
            {d.tiles.map((x) => <ResourceBox key={x.id} tile={x} />)}
          </div>
        )}
      </WhenUp>
    </Card>
  );
}

// ── cost ────────────────────────────────────────────────────────────────────────────────────────────────────────

function Meter({ c }: { c: Cost }) {
  const scale = Math.max(c.budget_usd ?? 0, c.forecast_usd, c.spent_usd, 1);
  const pct = (v: number) => `${Math.min(100, (100 * v) / scale)}%`;
  const over = c.budget_usd != null && c.forecast_usd > c.budget_usd;
  return (
    <div aria-hidden style={{ position: "relative", height: 10, borderRadius: 5, background: K.line }}>
      <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: pct(c.forecast_usd), borderRadius: 5,
        background: over ? K.dangerSoft : K.accentTint }} />
      <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: pct(c.spent_usd), borderRadius: 5,
        background: over ? K.danger : K.accent }} />
      {c.budget_usd != null && <span style={{ position: "absolute", top: -3, bottom: -3, width: 2,
        left: pct(c.budget_usd), background: K.ink }} />}
    </div>
  );
}

export function CostCard({ section }: { section: Section<Cost> }) {
  const t = useST();
  const lang = useLang();
  const usd = (v: number | null | undefined) => (v == null ? "—" : fmtUsd(v, lang));
  return (
    <Card title={t("costTitle")} testId="status-cost" asOf={section.as_of}
      actions={<Link href="/admin/spend?tab=apps" style={link}>{t("lSpend")}</Link>}>
      <WhenUp section={section}>
        {(c) => (
          <>
            <div style={{ ...num, fontSize: 26, fontWeight: 700, color: K.ink }}>{usd(c.spent_usd)}</div>
            <div style={small}>{t("costSplit", usd(c.aws_usd), usd(c.ai_usd))}</div>
            <Meter c={c} />
            <div style={{ ...rowWrap, ...small }}>
              <span>{t("forecast", usd(c.forecast_usd))}{c.forecast_pct_of_budget != null
                ? ` (${fmtPct(c.forecast_pct_of_budget, 0)})` : ""}</span>
              <span>{c.budget_usd == null ? t("noBudget") : t("budget", usd(c.budget_usd))}</span>
              {c.headroom_usd != null && <span style={{ color: c.headroom_usd < 0 ? K.danger : K.muted }}>
                {t("headroom", usd(c.headroom_usd))}</span>}
            </div>
          </>
        )}
      </WhenUp>
    </Card>
  );
}

// ── AI gateway ──────────────────────────────────────────────────────────────────────────────────────────────────

function CapBar({ a, near, t, lang }: { a: Ai["apps"][number]; near: number; t: ST; lang: "en" | "vi" }) {
  const used = Math.min(100, a.used_pct ?? 0);
  return (
    <div style={{ display: "grid", gap: 3 }} data-testid={`status-ai-${a.app}`}>
      <span style={{ ...rowWrap, ...small }}>
        <b style={{ color: K.ink }}>{APP_LABEL[a.app] ?? a.app}</b>
        <span style={num}>{a.spent_usd == null ? t("notMeasured") : fmtUsd(a.spent_usd, lang)}</span>
        <span>/ {a.cap_usd == null ? t("noCap") : fmtUsd(a.cap_usd, lang)}</span>
        {a.used_pct != null && <span style={{ color: levelColor(a.level) }}>{fmtPct(a.used_pct, 0)}</span>}
      </span>
      <span style={{ position: "relative", height: 8, borderRadius: 4, background: K.line }}>
        <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${used}%`, borderRadius: 4,
          background: levelColor(a.level) === K.ink ? K.accent : levelColor(a.level) }} />
        {a.cap_usd != null && <span style={{ position: "absolute", top: -2, bottom: -2, width: 2, left: `${near}%`,
          background: K.amber }} title={t("warnLine", fmtPct(near, 0))} />}
      </span>
    </div>
  );
}

function connColumns(t: ST, lang: "en" | "vi"): ColumnDef<AiConn>[] {
  return [
    { id: "key", header: t("colConn"), accessorFn: (c) => c.key, cell: ({ row }) => <code>{row.original.key}</code> },
    { id: "cost", header: t("colCost"), accessorFn: (c) => c.cost, cell: ({ row }) => fmtUsd(row.original.cost, lang) },
    { id: "requests", header: t("colReq"), accessorFn: (c) => c.requests,
      cell: ({ row }) => fmtCount(row.original.requests, lang) },
    { id: "errors", header: t("colErr"), accessorFn: (c) => c.error_pct ?? null,
      cell: ({ row }) => fmtPct(row.original.error_pct, 1) },
  ];
}

export function AiCard({ section, view, setView }: { section: Section<Ai>; view: View; setView: SetView }) {
  const t = useST();
  const lang = useLang();
  return (
    <Card title={t("aiTitle")} testId="status-ai" asOf={section.as_of}
      actions={<Link href="/admin/ai-gateway" style={link}>{t("lAi")}</Link>}>
      <WhenUp section={section}>
        {(ai) => (
          <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))" }}>
            <div style={{ display: "grid", gap: 8, alignContent: "start" }}>
              <div style={{ fontWeight: 600, fontSize: 12, color: K.ink }}>{t("aiByApp")}</div>
              {ai.apps.filter((a) => inApps(a.app, view.apps)).map((a) => (
                <CapBar key={a.app} a={a} near={ai.near_pct} t={t} lang={lang} />))}
              {ai.crm_note && <NotMeasured reason={`${APP_LABEL.crm}: ${ai.crm_note}`} />}
            </div>
            <div style={{ display: "grid", gap: 8, alignContent: "start", minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 12, color: K.ink }}>{t("aiByConn")}</div>
              <DataTable<AiConn> tableId="status-ai-conn" data={ai.connections} columns={connColumns(t, lang)}
                getRowId={(c) => c.key} searchable={false} paginate={false} compact
                sorting={tableSorting(view.sort.conn)}
                onSortingChange={(s) => setView({ sort: withSort(view.sort, "conn", sortingParam(s)) })} />
            </div>
          </div>
        )}
      </WhenUp>
    </Card>
  );
}
