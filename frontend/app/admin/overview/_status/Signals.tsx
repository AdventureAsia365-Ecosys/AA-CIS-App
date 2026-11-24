"use client";
// app/admin/overview/_status/Signals.tsx — AA-1140: the golden-signal charts on one time axis (requests per minute,
// 5xx %, p95 latency, oldest job waiting): a thin line per app, dashed previous period when compare is on, the warn
// (dashed) and critical (solid, shaded above) lines from the API's thresholds (cis.ops.status_* settings), deploy
// ticks and alarm bands from the sampler's events, a headroom line under each chart, legend tick boxes per chart
// (show / hide, only this one, show all) kept in the URL, and a table view of every value per step.

import { useMemo } from "react";
import {
  CartesianGrid, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { CHART_TOOLTIP, Checkbox, DataTable, K, LinkButton, useLang, type ColumnDef } from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import {
  chartRows, fmtMs, fmtNum, fmtPct, headroom, inApps, latest, onlyHidden, peak, queueSeries, shownSeries,
  signalSeries, tickLabel, toggleHidden, unmeasuredApps, yTop, APP_LABEL, type ChartId, type Jobs, type Limit,
  type Series, type Signals, type StatusResp, type View,
} from "@/lib/opsStatus";
import { Card, NotMeasured, rowWrap, small, useST, type ST } from "./ui";

const COLOR_VARS = { cis: "--aa-accent", tripplanner: "--aa-info", booking: "--aa-purple", crm: "--aa-green",
  shared: "--aa-accent", warn: "--aa-amber", crit: "--aa-red", axis: "--aa-muted", grid: "--aa-line",
  deploy: "--aa-slate" } as const;
type Colors = Record<keyof typeof COLOR_VARS, string>;

interface ChartSpec {
  id: ChartId; title: string; sub?: string; series: Series[]; limit?: Limit | null; extra?: { y: number; label: string };
  fmt: (v: number | null | undefined) => string; missing: string[];
}

function seriesColor(c: Colors, id: string): string {
  return (c as Record<string, string>)[id] ?? c.deploy;
}

function Legend({ spec, hidden, onHidden }: { spec: ChartSpec; hidden: string[]; onHidden: (h: string[]) => void }) {
  const t = useST();
  const all = spec.series.map((s) => s.id);
  return (
    <div style={rowWrap} role="group" aria-label={spec.title} data-testid={`status-legend-${spec.id}`}>
      {spec.series.map((s) => (
        <span key={s.id} style={{ ...rowWrap, gap: 4 }}>
          <Checkbox checked={!hidden.includes(s.id)} onChange={() => onHidden(toggleHidden(hidden, s.id))}
            label={s.label} />
          <LinkButton size="sm" tone="muted" onClick={() => onHidden(onlyHidden(all, hidden, s.id))}
            ariaLabel={`${t("only")} ${s.label}`}>{t("only")}</LinkButton>
        </span>
      ))}
      {hidden.length > 0 && <LinkButton size="sm" onClick={() => onHidden([])}>{t("showAll")}</LinkButton>}
      {spec.missing.map((a) => <NotMeasured key={a} reason={APP_LABEL[a]} />)}
    </div>
  );
}

/** "warn x · critical y · <extra>" of one chart. */
function limitText(spec: ChartSpec, t: ST): string {
  const parts: string[] = [];
  if (spec.limit?.warn != null) parts.push(t("warnLine", spec.fmt(spec.limit.warn)));
  if (spec.limit?.crit != null) parts.push(t("critLine", spec.fmt(spec.limit.crit)));
  if (spec.extra) parts.push(spec.extra.label);
  return parts.map((p) => ` · ${p}`).join("");
}

function HeadroomLine({ spec, shown, t }: { spec: ChartSpec; shown: Series[]; t: ST }) {
  const values = shown.flatMap((s) => [...s.values]);
  const last = Math.max(...shown.map((s) => latest(s.values) ?? -Infinity));
  const top = peak(values);
  const room = headroom(top, spec.limit?.warn ?? spec.limit?.crit, spec.limit?.dir);
  return (
    <div style={small} data-testid={`status-headroom-${spec.id}`}>
      {t("peakNow", spec.fmt(Number.isFinite(last) ? last : null), spec.fmt(top))}
      {" · "}{room == null ? t("noHeadroom") : t("headroom", spec.fmt(room))}
      {limitText(spec, t)}
    </div>
  );
}

function Marks({ sig, apps, c, top }: { sig: Signals; apps: string[]; c: Colors; top: number }) {
  const deploys = (sig.deploys ?? []).filter((d) => inApps(d.app, apps));
  const bands = (sig.alarm_bands ?? []).filter((b) => inApps(b.app, apps));
  return (
    <>
      {bands.map((b) => (
        <ReferenceArea key={`b${b.start}${b.text}`} x1={Date.parse(b.start)} x2={b.end ? Date.parse(b.end) : undefined}
          y1={0} y2={top} fill={c.crit} fillOpacity={0.08} ifOverflow="hidden" />
      ))}
      {deploys.map((d) => (
        <ReferenceLine key={`d${d.t}${d.app}`} x={Date.parse(d.t)} stroke={seriesColor(c, d.app)}
          strokeDasharray="2 3" ifOverflow="hidden" />
      ))}
    </>
  );
}

/** The warn (dashed) and critical (solid, shaded above) lines of a chart, and an extra limit (dotted). */
function Limits({ spec, c, top }: { spec: ChartSpec; c: Colors; top: number }) {
  const lim = spec.limit;
  return (
    <>
      {lim?.crit != null && <ReferenceArea y1={lim.crit} y2={top} fill={c.crit} fillOpacity={0.06} />}
      {lim?.crit != null && <ReferenceLine y={lim.crit} stroke={c.crit} strokeWidth={1.2} />}
      {lim?.warn != null && <ReferenceLine y={lim.warn} stroke={c.warn} strokeDasharray="5 4" />}
      {spec.extra && <ReferenceLine y={spec.extra.y} stroke={c.warn} strokeDasharray="1 4" />}
    </>
  );
}

function chartTop(spec: ChartSpec, shown: Series[]): number {
  return yTop(shown.flatMap((s) => [...s.values, ...(s.previous ?? [])]),
    [spec.limit?.warn, spec.limit?.crit, spec.extra?.y]);
}

function Chart({ spec, rows, hidden, onHidden, sig, view, c, spanS }: {
  spec: ChartSpec; rows: Record<string, number | null>[]; hidden: string[]; onHidden: (h: string[]) => void;
  sig: Signals; view: View; c: Colors; spanS: number;
}) {
  const t = useST();
  const lang = useLang();
  const shown = shownSeries(spec.series, hidden);
  const top = chartTop(spec, shown);
  const prev = view.compare ? shown.filter((s) => s.previous) : [];
  return (
    <div style={{ display: "grid", gap: 6, minWidth: 0 }} data-testid={`status-chart-${spec.id}`}>
      <div style={{ fontWeight: 600, color: K.ink, fontSize: 13 }}>{spec.title}</div>
      {spec.sub && <div style={small}>{spec.sub}</div>}
      <Legend spec={spec} hidden={hidden} onHidden={onHidden} />
      <div style={{ height: 200 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={c.grid} strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tick={{ fontSize: 10,
              fill: c.axis }} tickFormatter={(v: number) => tickLabel(v, spanS, lang)} minTickGap={40} />
            <YAxis width={44} tick={{ fontSize: 10, fill: c.axis }} domain={[0, top]}
              tickFormatter={(v: number) => fmtNum(v, 1)} />
            <Tooltip {...CHART_TOOLTIP} labelFormatter={(v) => tickLabel(Number(v), spanS, lang)}
              formatter={(v) => spec.fmt(typeof v === "number" ? v : null)} />
            <Marks sig={sig} apps={view.apps} c={c} top={top} />
            <Limits spec={spec} c={c} top={top} />
            {shown.map((s) => (
              <Line key={s.id} dataKey={s.id} name={s.label} stroke={seriesColor(c, s.id)} strokeWidth={1.5}
                dot={false} connectNulls={false} isAnimationActive={false} />
            ))}
            {prev.map((s) => (
              <Line key={`${s.id}p`} dataKey={`${s.id}__prev`} name={`${s.label} (${t("vsPrevious")})`}
                stroke={seriesColor(c, s.id)} strokeOpacity={0.45} strokeDasharray="3 3" strokeWidth={1.2}
                dot={false} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {spec.series.length === 0 && <span style={small}>{t("noSeries")}</span>}
      <HeadroomLine spec={spec} shown={shown} t={t} />
    </div>
  );
}

function specs(data: StatusResp, view: View, t: ST): ChartSpec[] {
  const sig = data.sections.signals;
  const th = data.thresholds;
  const jobs = data.sections.jobs.data as Jobs | undefined;
  const missing = unmeasuredApps(sig, view.apps);
  const lam = th.lambda_p95_limit_ms?.warn;
  return [
    { id: "traffic", title: t("trafficTitle"), sub: t("trafficSub"), series: signalSeries(sig, "rate_per_min", view.apps),
      limit: null, fmt: (v) => fmtNum(v, 1), missing },
    { id: "errors", title: t("errorsTitle"), series: signalSeries(sig, "error_pct", view.apps),
      limit: thresholds(th, "error_rate_pct"), fmt: (v) => fmtPct(v, 2), missing },
    { id: "latency", title: t("latencyTitle"), series: signalSeries(sig, "p95_ms", view.apps),
      limit: thresholds(th, "p95_ms"), fmt: fmtMs, missing,
      extra: lam != null && inApps("tripplanner", view.apps) ? { y: lam, label: t("lambdaLimit", fmtMs(lam)) } : undefined },
    { id: "queue", title: t("queueTitle"), series: queueSeries(jobs, view.apps), limit: thresholds(th, "queue_wait_min"),
      fmt: (v) => fmtNum(v, 1), missing: (jobs?.queues ?? []).filter((q) => !q.series_wait_min && inApps(q.app, view.apps))
        .map((q) => q.app) },
  ];
}

/** The page's limit of one chart (thresholds.error_rate_pct, thresholds.p95_ms, thresholds.queue_wait_min). */
function thresholds(th: StatusResp["thresholds"], key: "error_rate_pct" | "p95_ms" | "queue_wait_min"): Limit | null {
  return th[key] ?? null;
}

function TableView({ data, all }: { data: StatusResp; all: ChartSpec[] }) {
  const t = useST();
  const lang = useLang();
  const rows = useMemo(() => chartRows(data.buckets, all.flatMap((s) => s.series.map((x) => ({ ...x,
    id: `${s.id}:${x.id}`, previous: undefined })))), [data.buckets, all]);
  const columns: ColumnDef<Record<string, number | null>>[] = [
    { id: "t", header: t("time"), accessorFn: (r) => r.t,
      cell: ({ row }) => tickLabel(row.original.t ?? 0, data.range.span_s, lang) },
    ...all.flatMap((s) => s.series.map((x) => ({ id: `${s.id}:${x.id}`, header: `${s.title} · ${x.label}`,
      accessorFn: (r: Record<string, number | null>) => r[`${s.id}:${x.id}`],
      cell: ({ getValue }: { getValue: () => unknown }) => s.fmt(getValue() as number | null) }))),
  ];
  return (
    <details data-testid="status-signals-table">
      <summary style={{ ...small, cursor: "pointer" }}>{t("tableView")}</summary>
      <DataTable tableId="status-signals" data={rows} columns={columns} getRowId={(r) => String(r.t)}
        searchable={false} compact pageSize={50} tableMinWidth={720} />
    </details>
  );
}

export function SignalsCard({ data, view, setView }: {
  data: StatusResp; view: View; setView: (v: Partial<View>) => void;
}) {
  const t = useST();
  const c = useChartColors(COLOR_VARS) as Colors;
  const sig = data.sections.signals;
  const all = specs(data, view, t);
  const setHidden = (id: ChartId, h: string[]) => setView({ hide: { ...view.hide, [id]: h } });
  return (
    <Card title={t("signalsTitle")} testId="status-signals" asOf={sig.as_of}>
      <div style={small}>{t("signalsNote")}</div>
      {sig.status !== "ok" && <NotMeasured reason={sig.message ?? sig.reason} testId="status-signals-down" />}
      <div style={{ display: "grid", gap: 18, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))" }}>
        {all.map((spec) => (
          <Chart key={spec.id} spec={spec} hidden={view.hide[spec.id] ?? []} onHidden={(h) => setHidden(spec.id, h)}
            rows={chartRows(data.buckets, spec.series)} sig={sig} view={view} c={c} spanS={data.range.span_s} />
        ))}
      </div>
      <TableView data={data} all={all} />
    </Card>
  );
}
