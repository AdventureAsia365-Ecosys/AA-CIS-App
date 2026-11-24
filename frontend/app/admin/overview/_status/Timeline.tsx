"use client";
// app/admin/overview/_status/Timeline.tsx — AA-1140: the event timeline on the charts' time axis (deploys, alarms,
// service drops, ops check bells, job failures; severity chips, app filter, search, sortable list), delivery (main
// branch per repo, nightly guards: a guard CIS cannot read says "not measured yet"), sources and freshness (each
// section's source, refresh time, as of; stale after cis.ops.status_stale_factor x its refresh) and the drill-down
// links. The cost tables, content counts, pipeline funnel and tenant quotas live on Spend, Data coverage and Tenants.

import Link from "next/link";
import { useState } from "react";
import { Badge, DataTable, Input, K, useFormat, type ColumnDef, type Tone } from "../../../_kit";
import {
  EVENT_KINDS, countBy, eventKind, filterEvents, inApps, levelTone, sortingParam, tableSorting, withSort,
  type Delivery, type EventRow, type PipelineRow, type RangeInfo, type Section, type SourceRow, type View,
  type SetView,
} from "@/lib/opsStatus";
import { Card, ChipFilter, ClampText, NotMeasured, WhenUp, rowWrap, small, useST, type ST } from "./ui";
import { useWords, type StatusWords } from "./words";

const LANES = ["deploy", "alarm", "service", "check", "job"] as const;

function laneDot(e: EventRow): string {
  const tone = levelTone(e.severity);
  if (tone === "danger") return K.danger;
  if (tone === "warning") return K.amber;
  return e.lane === "deploy" ? K.accent : K.muted;
}

function Lanes({ rows, range }: { rows: EventRow[]; range: RangeInfo }) {
  const w = useWords();
  const a = Date.parse(range.start);
  const span = Math.max(1, Date.parse(range.end) - a);
  const pos = (iso: string) => `${Math.min(100, Math.max(0, (100 * (Date.parse(iso) - a)) / span))}%`;
  return (
    <div aria-hidden style={{ display: "grid", gap: 4 }} data-testid="status-lanes">
      {LANES.map((lane) => (
        <div key={lane} style={{ display: "grid", gridTemplateColumns: "70px minmax(0, 1fr)", gap: 8,
          alignItems: "center" }}>
          <span style={{ ...small, fontSize: 11 }}>{lane}</span>
          <span style={{ position: "relative", height: 12, background: K.bg, borderRadius: 3 }}>
            {rows.filter((e) => e.lane === lane).map((e) => (
              <span key={e.id} title={w.text(e.msg, e.text)} style={{ position: "absolute", top: 2, height: 8, left: pos(e.t),
                minWidth: 3, width: e.t2 ? `calc(${pos(e.t2)} - ${pos(e.t)})` : 3, borderRadius: 2,
                background: laneDot(e) }} />
            ))}
          </span>
        </div>
      ))}
    </div>
  );
}

function EventText({ e, w }: { e: EventRow; w: StatusWords }) {
  return (
    <span style={{ display: "grid", gap: 2, overflowWrap: "anywhere", minWidth: 0 }}>
      {w.text(e.msg, e.text)}
      {e.detail && <ClampText text={e.detail} lines={2}
        label={e.detail_source ? w.source(SOURCE_OF[e.kind] ?? "") : null} />}
    </span>
  );
}

// the source of an event's outside detail text, by event kind (named in the "As written by ..." caption)
const SOURCE_OF: Readonly<Record<string, string>> = {
  alarm: "CloudWatch alarm", rollout_failed: "ECS", check: "Ops check", job_failures: "job",
};

function eventColumns(t: ST, w: StatusWords, formatDateTime: (d: string | null) => string): ColumnDef<EventRow>[] {
  return [
    { id: "t", header: t("colTime"), accessorFn: (e) => e.t, cell: ({ row }) => formatDateTime(row.original.t) },
    { id: "severity", header: t("severity"), accessorFn: (e) => eventKind(e),
      cell: ({ row }) => <Badge tone={levelTone(row.original.severity) as Tone}>{eventKind(row.original)}</Badge> },
    { id: "app", header: t("colApp"), accessorFn: (e) => w.app(e.app) },
    { id: "event", header: t("colEvent"), accessorFn: (e) => w.text(e.msg, e.text),
      cell: ({ row }) => <EventText e={row.original} w={w} /> },
    { id: "ends", header: t("colEnds"), accessorFn: (e) => e.t2 ?? "",
      cell: ({ row }) => row.original.t2 ? formatDateTime(row.original.t2)
        : row.original.open ? <Badge tone="warning">{t("stillOpen")}</Badge> : "—" },
  ];
}

function evLabel(k: string, t: ST): string {
  if (k === "info") return t("evinfo");
  return k === "pass" ? t("evpass") : k.toUpperCase();
}

export function EventsCard({ section, range, view, setView }: {
  section: Section<EventRow[]>; range: RangeInfo; view: View; setView: SetView;
}) {
  const t = useST();
  const w = useWords();
  const { formatDateTime } = useFormat();
  const [search, setSearch] = useState("");
  return (
    <Card title={t("eventsTitle")} testId="status-events" asOf={section.as_of} refreshS={section.refresh_s}
      actions={<Link href="/admin/platform/alerts" style={{ ...small, color: K.accentDeep, fontWeight: 600 }}>
        {t("lAlerts")}</Link>}>
      <WhenUp section={section}>
        {(all) => {
          const scoped = all.filter((e) => inApps(e.app, view.apps));
          const counts = countBy(scoped, eventKind);
          const rows = filterEvents(scoped, view, search);
          return (
            <>
              <div style={rowWrap}>
                <ChipFilter label={t("severity")} testId="status-ev-filter" value={view.ev}
                  onChange={(f) => setView((v) => ({ ev: f(v.ev) }))}
                  options={EVENT_KINDS.map((k) => ({ value: k, label: evLabel(k, t), count: counts[k] ?? 0 }))} />
                <Input size="sm" type="search" aria-label={t("search")} placeholder={t("search")} value={search}
                  onChange={(e) => setSearch(e.target.value)} style={{ width: 220 }} />
              </div>
              <Lanes rows={rows} range={range} />
              <DataTable<EventRow> tableId="status-events" data={rows} columns={eventColumns(t, w, formatDateTime)}
                getRowId={(e) => e.id} searchable={false} compact pageSize={25} tableMinWidth={640}
                emptyTitle={t("noEvents")} sorting={tableSorting(view.sort.ev)}
                onSortingChange={(s) => setView((v) => ({ sort: withSort(v.sort, "ev", sortingParam(s)) }))} />
            </>
          );
        }}
      </WhenUp>
    </Card>
  );
}

export function DeliveryCard({ section }: { section: Section<Delivery> }) {
  const t = useST();
  return (
    <Card title={t("deliveryTitle")} testId="status-delivery" asOf={section.as_of} refreshS={section.refresh_s}
      actions={<Link href="/admin/platform/delivery" style={{ ...small, color: K.accentDeep, fontWeight: 600 }}>
        {t("lDelivery")}</Link>}>
      <WhenUp section={section}>
        {(d) => (
          <>
            <div style={rowWrap}>
              <span style={small}>{t("mainBranch")}:</span>
              {d.repos.length === 0 && <NotMeasured reason={d.repos_note} />}
              {d.repos.map((r) => <Badge key={r.id} tone={levelTone(r.main) as Tone}>{r.label}: {r.main}</Badge>)}
            </div>
            <div style={{ fontWeight: 600, fontSize: 12, color: K.ink }}>{t("guards")}</div>
            <div style={{ display: "grid", gap: 4 }} data-testid="status-guards">
              {d.guards.map((g) => (
                <span key={g.key} style={rowWrap}>
                  {g.state === "not_measured" ? <NotMeasured reason={null} />
                    : <Badge tone={levelTone(g.state) as Tone}>{g.state}</Badge>}
                  <span style={{ fontSize: 13, color: K.ink }}>{g.label}</span>
                  {g.detail && <span style={small}>{g.detail}</span>}
                </span>
              ))}
            </div>
          </>
        )}
      </WhenUp>
    </Card>
  );
}

function refreshText(s: number, t: ST): string {
  return t("every", s >= 3600 ? `${s / 3600} h` : s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`);
}

function stateBadge(state: string, reason: string | null | undefined, t: ST) {
  const word = state === "stale" ? t("stale") : state === "unknown" ? t("stUnknown")
    : state === "not_measured" ? t("stNotMeasured") : state.replace("_", " ");
  return (
    <span title={reason ?? undefined}>
      <Badge tone={levelTone(state) as Tone}>{word}</Badge>
      {reason && state !== "ok" && <span style={{ ...small, display: "block", overflowWrap: "anywhere" }}>{reason}</span>}
    </span>
  );
}

/** AA-1141 (P1 feature 3): the background producers this page relies on (sampler, digest, probes, delivery summary,
 *  schema dump), each grey "stale" after cis.ops.status_stale_factor x its refresh time; never green without a run. */
function Pipelines({ rows }: { rows: PipelineRow[] }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  const columns: ColumnDef<PipelineRow>[] = [
    { id: "label", header: t("colProducer"), accessorFn: (r) => r.label },
    { id: "refresh", header: t("colRefresh"), accessorFn: (r) => r.refresh_s,
      cell: ({ row }) => refreshText(row.original.refresh_s, t) },
    { id: "asof", header: t("colLastRun"), accessorFn: (r) => r.as_of ?? "",
      cell: ({ row }) => formatDateTime(row.original.as_of) },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state,
      cell: ({ row }) => stateBadge(row.original.state, row.original.reason, t) },
  ];
  return (
    <div style={{ display: "grid", gap: 6 }} data-testid="status-pipelines">
      <div style={{ fontWeight: 600, fontSize: 13, color: K.ink }}>{t("pipelinesTitle")}</div>
      <span style={small}>{t("pipelinesNote")}</span>
      <DataTable<PipelineRow> tableId="status-pipelines" data={rows} columns={columns} getRowId={(r) => r.key}
        searchable={false} paginate={false} compact tableMinWidth={560} />
    </div>
  );
}

export function SourcesCard({ rows, staleFactor, pipelines = [] }: {
  rows: SourceRow[]; staleFactor: number | null; pipelines?: PipelineRow[];
}) {
  const t = useST();
  const { formatDateTime } = useFormat();
  const columns: ColumnDef<SourceRow>[] = [
    { id: "section", header: t("colSection"), accessorFn: (r) => r.section },
    { id: "source", header: t("colSource"), accessorFn: (r) => r.source, enableSorting: false,
      cell: ({ row }) => <span style={{ ...small, overflowWrap: "anywhere" }}>{row.original.source}</span> },
    { id: "refresh", header: t("colRefresh"), accessorFn: (r) => r.refresh_s,
      cell: ({ row }) => refreshText(row.original.refresh_s, t) },
    { id: "asof", header: t("colAsOf"), accessorFn: (r) => r.as_of ?? "", cell: ({ row }) => formatDateTime(row.original.as_of) },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state,
      cell: ({ row }) => stateBadge(row.original.state, null, t) },
  ];
  return (
    <Card title={t("sourcesTitle")} testId="status-sources">
      {staleFactor != null && <span style={small}>{t("sourcesNote", staleFactor)}</span>}
      <DataTable<SourceRow> tableId="status-sources" data={rows} columns={columns} getRowId={(r) => r.section}
        searchable={false} paginate={false} compact tableMinWidth={620} />
      {pipelines.length > 0 && <Pipelines rows={pipelines} />}
    </Card>
  );
}

const DRILL: { key: Parameters<ST>[0]; href: string }[] = [
  { key: "lAppHealth", href: "/admin/platform/apps" }, { key: "lJobs", href: "/admin/jobs" },
  { key: "lAi", href: "/admin/ai-gateway" }, { key: "lSpend", href: "/admin/spend" },
  { key: "lDelivery", href: "/admin/platform/delivery" }, { key: "lCoverage", href: "/admin/data-coverage" },
  { key: "lTenants", href: "/admin/tenants" }, { key: "lReports", href: "/admin/reports" },
  { key: "lAlerts", href: "/admin/platform/alerts" },
  { key: "lRecipients", href: "/admin/platform/alerts?tab=recipients" },
  { key: "lSettings", href: "/admin/platform/settings?app=cis&area=ops" },
];

export function DrillLinks() {
  const t = useST();
  return (
    <Card title={t("drillTitle")} testId="status-drill">
      <div style={rowWrap}>
        {DRILL.map((d) => (
          <Link key={d.href} href={d.href} style={{ ...small, color: K.accentDeep, fontWeight: 600 }}>
            {t(d.key) as string}</Link>
        ))}
      </div>
      <span style={small}>{t("movedNote")}</span>
    </Card>
  );
}
