"use client";
// app/admin/platform/compliance/CalendarTab.tsx — AA-1184: the Calendar tab of Platform › Compliance (read-only).
//
// The root repo's ops calendar (docs/ops/calendar.yaml) as its daily compliance collector published it in the check
// ops_calendar_overdue: the check's state (red the day a line is overdue), the KPI tiles (overdue, due soon, open,
// lines), a filter (needs attention / every dated line / all; in the URL as ?cal=) and the table: state, due date with
// "in N days" / "N days ago", the duty (title and, on a line that needs attention, its note), kind, owner role, last
// done, and the state of the compliance check that watches the line. No write: duties change in the calendar file.
//
// API: GET /api/admin/platform/compliance/calendar (compliance.read; api/services/compliance_calendar.py). Pure helpers
// in lib/compliance.ts (calendarTone, filterCalendar, daysUntil). Kit only; test ids on wrapper spans.

import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { useMemo } from "react";
import {
  Badge, DataTable, EmptyState, ErrorState, Field, K, KpiTile, Section, Select, TableSkeleton, apiGet, mono,
  useFormat, useT, useUrlFilters, type ColumnDef,
} from "../../../_kit";
import {
  CALENDAR_PATH, calendarFilterOf, calendarTone, checkTone, daysUntil, filterCalendar, needsAttention, safeLink,
  type CalendarFilter, type CalendarLine, type CalendarResp,
} from "@/lib/compliance";
import { CALENDAR } from "./calendar.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const grid = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 } as const;
const tiles = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 150px), 1fr))", gap: 10 } as const;
const linkStyle = { color: K.accentDeep, display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12 } as const;

function useCalT() {
  return useT(CALENDAR);
}
type CalT = ReturnType<typeof useCalT>;
type Key = Parameters<CalT>[0];

function label(t: CalT, key: string): string {
  return t(key as Key) as string;
}

function DueCell({ line, today, t }: { line: CalendarLine; today: string | null; t: CalT }) {
  const { formatDate } = useFormat();
  if (!line.due) return <span style={small}>{line.extra ?? "—"}</span>;
  const n = daysUntil(line.due, today);
  return (
    <span style={{ display: "grid", gap: 2 }}>
      <span>{formatDate(line.due)}</span>
      {n !== null && line.state !== "done" && (
        <span style={{ ...small, color: n < 0 ? K.danger : small.color }}>{n < 0 ? t("daysAgo", -n) : t("inDays", n)}</span>
      )}
    </span>
  );
}

function CalendarTable({ data, filter, t }: { data: CalendarResp; filter: CalendarFilter; t: CalT }) {
  const { formatDate } = useFormat();
  const today = data.calendar?.today ?? null;
  const rows = useMemo(() => filterCalendar(data.lines, filter), [data.lines, filter]);
  const columns: ColumnDef<CalendarLine, unknown>[] = [
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: ({ row: { original: r } }) => (
        <span data-testid={`calendar-state-${r.id}`} data-state={r.state}>
          <Badge tone={calendarTone(r.state)}>{label(t, `state_${r.state}`)}</Badge>
        </span>
      ) },
    { id: "due", header: t("colDue"), accessorKey: "due",
      cell: ({ row: { original: r } }) => <DueCell line={r} today={today} t={t} /> },
    { id: "duty", header: t("colDuty"), accessorKey: "title",
      cell: ({ row: { original: r } }) => (
        <span style={{ display: "grid", gap: 2 }}>
          <span><span style={{ fontFamily: mono, fontSize: 11, color: K.muted }}>{r.id}</span> {r.title}</span>
          {needsAttention(r) && r.note && <span style={small}>{r.note}</span>}
          {r.due && r.extra && <span style={small}>{r.extra}</span>}
        </span>
      ) },
    { id: "kind", header: t("colKind"), accessorKey: "kind",
      cell: ({ row: { original: r } }) => <span style={small}>{label(t, `kind_${r.kind}`)}</span> },
    { id: "owner", header: t("colOwner"), accessorKey: "owner",
      cell: ({ row: { original: r } }) => <span style={small}>{label(t, `owner_${r.owner}`)}</span> },
    { id: "last", header: t("colLast"), accessorKey: "last",
      cell: ({ row: { original: r } }) => <span style={small}>{r.last ? formatDate(r.last) : "—"}</span> },
    { id: "check", header: t("colCheck"), accessorKey: "check",
      cell: ({ row: { original: r } }) => r.check ? (
        <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
          <span style={{ ...small, fontFamily: mono }}>{r.check}</span>
          {r.check_state && <Badge tone={checkTone(r.check_state)} dot={false}>
            {label(t, `check_${r.check_state}`)}</Badge>}
        </span>
      ) : <span style={small}>—</span> },
  ];
  return (
    <DataTable<CalendarLine> tableId="compliance-calendar" data={rows} columns={columns} getRowId={(r) => r.id}
      paginate={false} searchable={filter !== "attention"} tableMinWidth={980}
      emptyTitle={filter === "attention" ? t("empty_attention") : t("empty_all")} />
  );
}

function CheckSection({ data, t }: { data: CalendarResp; t: CalT }) {
  const { formatDateTime, formatDate } = useFormat();
  const check = data.check;
  const cal = data.calendar;
  if (!check || !cal) return null;
  const href = safeLink(check.link);
  return (
    <Section title={t("checkTitle")} testId="compliance-calendar-check"
      description={<span style={{ display: "inline-flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <span data-testid="compliance-calendar-check-state" data-state={check.state ?? ""}>
          <Badge tone={checkTone(check.state)}>{check.state ?? "—"}</Badge>
        </span>
        <span style={{ fontFamily: mono, fontSize: 12 }}>{check.check_id}</span>
        {check.sealed && <Badge tone="neutral" dot={false}>{t("sealed")}</Badge>}
        {href && <a href={href} target="_blank" rel="noopener noreferrer" style={linkStyle}>
          {t("openCalendarFile")}<ExternalLink size={11} /></a>}
      </span>}>
      <div style={{ ...small, display: "grid", gap: 4 }}>
        {check.reason && <span>{check.reason}</span>}
        {cal.today && <span>{t("asOf", formatDate(cal.today))}</span>}
        <span>
          {check.collected_at ? t("collected", formatDateTime(check.collected_at)) : null}
          {cal.updated ? ` · ${t("calendarUpdated", formatDate(cal.updated))}` : null}
        </span>
        {cal.drill && <span><Badge tone="info" dot={false}>{t("drill")}</Badge></span>}
        {cal.errors ? <span style={{ color: K.danger }}>{t("invalid", cal.errors)}</span> : null}
        {cal.truncated && <span>{t("truncated")}</span>}
        {cal.automated_omitted && !cal.truncated && <span>{t("automatedOmitted")}</span>}
      </div>
    </Section>
  );
}

export function CalendarTab() {
  const t = useCalT();
  const { get, setMany } = useUrlFilters();
  const filter = calendarFilterOf(get("cal"));
  const q = useQuery({ queryKey: ["platform-compliance", "calendar"], retry: false, staleTime: 60_000,
    queryFn: () => apiGet<CalendarResp>(CALENDAR_PATH) });
  const cal = q.data?.calendar;
  return (
    <div style={grid}>
      <div role="note" data-testid="compliance-calendar-note" style={small}>{t("intro")}</div>
      {q.error && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
        onRetry={() => void q.refetch()} />}
      {q.isLoading && <TableSkeleton rows={6} cols={6} />}
      {q.data && !q.data.available && <div data-testid="compliance-calendar-empty">
        <EmptyState title={t("notPublished")} description={t("notPublishedHint")} /></div>}
      {q.data?.available && cal && (
        <>
          <CheckSection data={q.data} t={t} />
          <div style={tiles} data-testid="compliance-calendar-kpis">
            <KpiTile size="sm" label={t("kpiOverdue")} value={cal.overdue}
              hintTone={cal.overdue ? "danger" : "neutral"} hint={cal.overdue ? label(t, "state_overdue") : undefined} />
            <KpiTile size="sm" label={t("kpiDueSoon")} value={cal.counts.due_soon ?? 0} />
            <KpiTile size="sm" label={t("kpiOpen")} value={cal.counts.open ?? 0} />
            <KpiTile size="sm" label={t("kpiLines")} value={cal.lines} />
          </div>
          <Section title={t("tabCalendar")} testId="compliance-calendar-lines">
            <div style={{ maxWidth: 320, marginBottom: 10 }}>
              <Field label={t("filter")}>
                <Select value={filter}
                  onChange={(e) => setMany({ cal: e.target.value === "attention" ? null : e.target.value })}
                  options={(["attention", "dated", "all"] as const).map((k) => ({ value: k, label: t(`filter_${k}`) }))} />
              </Field>
            </div>
            {/* Its own horizontal scroller: the table is wider than a phone. */}
            <div data-testid="compliance-calendar-scroll" style={{ maxWidth: "100%", overflowX: "auto", contain: "paint" }}>
              <CalendarTable data={q.data} filter={filter} t={t} />
            </div>
          </Section>
        </>
      )}
    </div>
  );
}
