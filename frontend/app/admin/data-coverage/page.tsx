"use client";
// app/admin/data-coverage/page.tsx
// AA-992 — Platform › Overview › Data coverage: live counts of every stage, from the raw upload to TripPlanner,
// Booking and the partner CRM, to audit where data is missing. Numbers only (no tour ids, names or samples).
//
// API: GET /api/admin/data-coverage (backend api/routers/admin_data_coverage.py → api/services/data_coverage/;
// ops.read = admin; BFF API_READ_RULES "data-coverage"). One call returns every row; the backend caches it (DB counts
// 60 s, the slow sources 600 s, the CRM 600 s). One load on open, no polling; Refresh sends ?refresh=1 (the backend
// recomputes the DB counts only when its copy is older than 15 s) and is disabled for 15 s after a click.
//
// The page computes nothing: status, missing and the expected gap come from the API; the badge colour is `status` +
// `kind` (lib/dataCoverage.ts statusBadge). Admin only (lib/rbac.ts PAGE_RULES). AA-994: text in the UI language
// (messages.ts + lib/dataCoverage.messages.ts); API labels, notes and reasons stay as sent.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowUpRight, Download, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  ErrorState,
  InfoTip,
  K,
  PageHeader,
  RADIUS,
  SkeletonBar,
  SkeletonStyle,
  apiGet,
  downloadCsv,
  sans,
  toCsv, PageFrame,
  useLang,
  useT,
  type Translator,
} from "../../_kit";
import {
  DATA_COVERAGE_PATH,
  REFRESH_THROTTLE_MS,
  csvColumns,
  csvRows,
  formatAge,
  formatCount,
  formatExpected,
  formatOf,
  formatPct,
  formatUtcTime,
  groupGaps,
  groupUnavailableReason,
  statusBadge,
  type CoverageGroup,
  type CoverageRow,
  type DataCoverageResponse,
  type DcWords,
} from "@/lib/dataCoverage";
import { DC_MESSAGES } from "@/lib/dataCoverage.messages";
import { DC } from "./messages";

type T = Translator<typeof DC.en>;

function useDcWords(): DcWords {
  return DC_MESSAGES[useLang()];
}

const QUERY_KEY = ["data-coverage"] as const;

export default function DataCoveragePage() {
  const t = useT(DC);
  const w = useDcWords();
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => apiGet<DataCoverageResponse>(DATA_COVERAGE_PATH),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const [throttledUntil, setThrottledUntil] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const refresh = useMutation({
    mutationFn: () => apiGet<DataCoverageResponse>(`${DATA_COVERAGE_PATH}?refresh=1`),
    onSuccess: (data) => qc.setQueryData(QUERY_KEY, data),
  });
  // One clock tick a second only while the Refresh button is throttled (re-enables it; no data polling).
  useEffect(() => {
    if (throttledUntil <= now) return;
    const t = setTimeout(() => setNow(Date.now()), 1000);
    return () => clearTimeout(t);
  }, [throttledUntil, now]);

  const data = query.data;
  const throttled = throttledUntil > now;
  const busy = query.isFetching || refresh.isPending;
  const error = (refresh.error as Error | null) ?? (query.error as Error | null);

  const onRefresh = () => {
    const at = Date.now();
    setThrottledUntil(at + REFRESH_THROTTLE_MS);
    setNow(at);
    refresh.mutate();
  };
  const onExport = () => {
    if (!data) return;
    downloadCsv(`data-coverage-${data.as_of.slice(0, 10)}`, toCsv(csvRows(data), csvColumns(w)));
  };

  return (
    <PageFrame>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {data && <AsOf data={data} />}
            <Button variant="secondary" size="sm" onClick={onRefresh} disabled={busy || throttled}
              title={throttled ? t("refreshedMoment") : t("refreshTitle")}>
              <RefreshCw size={13} /> {refresh.isPending ? t("counting") : t("refresh")}
            </Button>
            <Button variant="secondary" size="sm" onClick={onExport} disabled={!data}
              title={t("exportTitle")}>
              <Download size={13} /> {t("exportCsv")}
            </Button>
          </div>
        }
      />

      {error && !data ? (
        <ErrorState message={error.message || t("loadError")} onRetry={() => query.refetch()} />
      ) : !data ? (
        <PageSkeleton />
      ) : (
        <div data-testid="data-coverage-ready">
          {refresh.isError && (
            <div style={{ ...noteStyle, color: K.danger, marginBottom: 12 }}>
              {t("refreshFailed", (refresh.error as Error)?.message || t("unknownError"))}
            </div>
          )}
          <SummaryStrip data={data} />
          {data.groups.map((g) => <GroupSection key={g.id} group={g} />)}
          <div style={{ ...noteStyle, marginTop: 14 }}>
            {t("legend")} {t("builtIn", formatCount(data.generated_ms))}
          </div>
        </div>
      )}
    </PageFrame>
  );
}

function AsOf({ data }: { data: DataCoverageResponse }) {
  const t = useT(DC);
  const age = formatAge(data.as_of, undefined, useDcWords());
  return (
    <span data-testid="data-coverage-as-of" style={{ fontSize: 12, color: K.muted, fontFamily: sans, display: "inline-flex", gap: 6, alignItems: "center" }}>
      {t("asOf", formatUtcTime(data.as_of))}{data.cached && age ? t("cached", age) : ""}
      {data.stale && <Badge tone="warning">{t("stale")}</Badge>}
    </span>
  );
}

function SummaryStrip({ data }: { data: DataCoverageResponse }) {
  const t = useT(DC);
  const tiles: { label: string; value: string; color: string; testId: string }[] = [
    { label: t("tileLive"), value: formatCount(data.live_tours), color: K.ink, testId: "dc-live" },
    { label: t("tileGaps"), value: formatCount(data.summary.gaps), color: data.summary.gaps ? K.danger : K.ink, testId: "dc-gaps" },
    { label: t("tileBacklog"), value: formatCount(data.summary.backlog), color: data.summary.backlog ? K.amber : K.ink, testId: "dc-backlog" },
    { label: t("tileNa"), value: formatCount(data.summary.na), color: K.ink3, testId: "dc-na" },
  ];
  return (
    <div className="aa-kpi-grid" style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12, marginBottom: 22 }}>
      {tiles.map((tile) => (
        <div key={tile.testId} data-testid={tile.testId} style={tileStyle}>
          <div style={{ fontSize: 11, color: K.muted, textTransform: "uppercase", letterSpacing: "0.04em", fontWeight: 600 }}>{tile.label}</div>
          <div style={{ fontSize: 24, fontWeight: 700, color: tile.color, fontVariantNumeric: "tabular-nums", marginTop: 4 }}>{tile.value}</div>
        </div>
      ))}
    </div>
  );
}

function GroupSection({ group }: { group: CoverageGroup }) {
  const t = useT(DC);
  const w = useDcWords();
  const columns = useMemo(() => buildColumns(t, w), [t, w]);
  const gaps = groupGaps(group);
  const allNa = groupUnavailableReason(group, w);
  return (
    <section data-testid={`dc-group-${group.id}`} style={{ marginBottom: 26 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap", marginBottom: 8, fontFamily: sans }}>
        <h2 style={{ fontSize: 15, fontWeight: 700, color: K.ink, margin: 0 }}>{group.label}</h2>
        <span style={{ fontSize: 12, color: gaps ? K.danger : K.muted }}>
          {t("groupCount", group.rows.length, gaps)}
        </span>
      </div>
      {group.note && <div style={{ ...noteStyle, marginBottom: 8 }}>{group.note}</div>}
      {allNa && <div style={{ ...noteStyle, marginBottom: 8 }}>{t("notAvailable", allNa)}</div>}
      <DataTable<CoverageRow>
        tableId={`data-coverage-${group.id}`}
        data={group.rows}
        columns={columns}
        getRowId={(r) => r.id}
        compact
        paginate={false}
        searchable={false}
        enableCsv={false}
        stickyHeader
      />
    </section>
  );
}

const buildColumns = (t: T, w: DcWords): ColumnDef<CoverageRow, unknown>[] => [
  {
    id: "metric",
    header: t("colMetric"),
    accessorFn: (r) => r.label,
    cell: ({ row }) => {
      const r = row.original;
      return (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <span>{r.label}</span>
          {r.issue && <Badge tone="neutral" dot={false} style={{ textTransform: "none", letterSpacing: 0 }}>{r.issue}</Badge>}
          <InfoTip text={t("sourceTip", r.source, r.timing_ms)} label={t("sourceOf", r.label)} testId={`dc-src-${r.id}`} />
        </span>
      );
    },
  },
  {
    id: "count",
    header: t("colCount"),
    accessorFn: (r) => r.value,
    meta: { align: "right" },
    cell: ({ row }) => <span style={numStyle}>{formatCount(row.original.value)}</span>,
  },
  {
    id: "of",
    header: t("colOf"),
    accessorFn: (r) => r.denominator,
    cell: ({ row }) => <span style={{ ...numStyle, color: K.ink3 }}>{formatOf(row.original)}</span>,
  },
  {
    id: "pct",
    header: "%",
    accessorFn: (r) => r.pct,
    meta: { align: "right" },
    cell: ({ row }) => <span style={numStyle}>{formatPct(row.original.pct)}</span>,
  },
  {
    id: "missing",
    header: t("colMissing"),
    accessorFn: (r) => r.missing,
    meta: { align: "right" },
    cell: ({ row }) => <span style={numStyle}>{formatCount(row.original.missing)}</span>,
  },
  {
    id: "expected",
    header: t("colExpected"),
    accessorFn: (r) => r.expected_gap,
    cell: ({ row }) => <span style={{ fontSize: 12, color: K.muted }}>{formatExpected(row.original)}</span>,
  },
  {
    id: "status",
    header: t("colStatus"),
    accessorFn: (r) => r.status,
    cell: ({ row }) => {
      const r = row.original;
      const b = statusBadge(r, w);
      return (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }} data-testid={`dc-status-${r.id}`}>
          <Badge tone={b.tone}>{b.label}</Badge>
          {r.status === "na" && r.reason && <InfoTip text={r.reason} label={t("whyNa", r.label)} testId={`dc-na-${r.id}`} />}
        </span>
      );
    },
  },
  {
    id: "open",
    header: t("colOpen"),
    enableSorting: false,
    meta: { hideLabelOnCard: true },
    cell: ({ row }) => {
      const d = row.original.drill;
      return d ? (
        <Link href={d} aria-label={t("open", row.original.label)} style={{ color: K.accent, display: "inline-flex" }}>
          <ArrowUpRight size={15} />
        </Link>
      ) : null;
    },
  },
];

function PageSkeleton() {
  return (
    <div aria-busy="true">
      <div className="aa-kpi-grid" style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12, marginBottom: 22 }}>
        {[0, 1, 2, 3].map((i) => <div key={i} style={tileStyle}><SkeletonBar w="60%" /><SkeletonBar w="40%" h={22} style={{ marginTop: 8 }} /></div>)}
      </div>
      {[0, 1, 2].map((i) => (
        <div key={i} style={{ marginBottom: 22 }}>
          <SkeletonBar w={220} h={16} />
          {[0, 1, 2, 3].map((j) => <SkeletonBar key={j} w="100%" h={28} style={{ marginTop: 8 }} />)}
        </div>
      ))}
    </div>
  );
}

const noteStyle: React.CSSProperties = { fontSize: 12, color: K.muted, fontFamily: sans, lineHeight: 1.5 };
const numStyle: React.CSSProperties = { fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" };
const tileStyle: React.CSSProperties = {
  background: K.card,
  border: `1px solid ${K.line}`,
  borderRadius: RADIUS.md,
  padding: "12px 14px",
  minWidth: 0,
  fontFamily: sans,
};
