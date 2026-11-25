"use client";
// app/admin/overview/_status/External.tsx — AA-1142 (P2): the external strip, next to the internal Services grid. Two
// vantage points check the public hosts (DNS, the TLS certificate, the health path) and write
// acp_shared.ops_external_check: the CIS worker's job every cis.ops.external_check_interval_min minutes (S293) and the
// root repo workflow on GitHub Actions (copied by the 5-minute sampler), each optional (cis.ops.external_check_sources).
// One row per target and source: where it ran, state now, HTTP status and time, days until the certificate ends (amber
// / red at the API's cis.ops.status_cert_warn_days / _crit_days), the strip of the range. A source that stopped
// reporting is grey and says so; the card greys only when every source stopped. Kit only; a level is said in words and
// a glyph.

import Link from "next/link";
import { Badge, DataTable, K, useFormat, type ColumnDef, type Tone } from "../../../_kit";
import { fmtNum, inApps, levelTone, type Section, type View } from "@/lib/opsStatus";
import { externalRowId, externalRowLevel, type External, type ExternalRow, type ExternalSourceInfo }
  from "@/lib/opsStatusP2";
import { Card, LevelGlyph, WhenUp, levelColor, small } from "./ui";
import { useWords } from "./words";
import { useP2 } from "./Slo";

type PT = ReturnType<typeof useP2>;
const CELL: Readonly<Record<string, string>> = { ok: K.success, warn: K.amber, degraded: K.amber, down: K.danger };

function ExtStrip({ cells }: { cells: string[] }) {
  const t = useP2();
  if (!cells.length) return <span style={small}>—</span>;
  const up = cells.filter((s) => s === "ok").length;
  const down = cells.filter((s) => s === "down" || s === "degraded" || s === "warn").length;
  const label = t("stripLabel", up, down, cells.length - up - down);
  return (
    <span role="img" aria-label={label} title={label}
      style={{ display: "inline-flex", gap: 1, alignItems: "stretch", height: 14, width: 200, maxWidth: "100%" }}>
      {cells.map((s, i) => <span key={i} style={{ flex: "1 1 0", minWidth: 1, background: CELL[s] ?? K.line,
        borderRadius: 1 }} />)}
    </span>
  );
}

function stateWord(state: string, t: PT): string {
  if (state === "ok") return t("stOk");
  if (state === "warn") return t("stWarn");
  if (state === "degraded") return t("stDegraded");
  return state === "down" ? t("stDown") : t("stUnknown");
}

function whyCell(r: ExternalRow, t: PT): string {
  if (r.dns_ok === false) return t("dnsFail");
  if (r.tls_ok === false) return t("tlsFail");
  return r.http_status == null ? "—" : String(r.http_status);
}

function sourceWord(source: string | undefined, t: PT): string {
  return source === "worker" ? t("srcWorker") : t("srcGithub");
}

function SourcesLine({ sources, t, when }: { sources: ExternalSourceInfo[]; t: PT; when: (iso: string) => string }) {
  if (!sources.length) return null;
  return (
    <span style={small} data-testid="status-external-sources">
      {sources.map((s, i) => (
        <span key={s.source} style={s.stale ? { color: K.amber, fontWeight: 600 } : undefined}>
          {i > 0 && " · "}
          {s.stale && <LevelGlyph level="warn" />}
          {t("srcLine", sourceWord(s.source, t), s.interval_minutes, s.newest ? when(s.newest) : t("srcNever"),
            s.stale)}
        </span>))}
    </span>
  );
}

function columns(t: PT, app: (id: string) => string): ColumnDef<ExternalRow>[] {
  return [
    { id: "target", header: t("colTarget"), accessorFn: (r) => r.host,
      cell: ({ row }) => (
        <span style={{ display: "grid", gap: 1 }}>
          <b style={{ color: K.ink, overflowWrap: "anywhere" }}>{row.original.host}</b>
          <span style={small}>{app(row.original.app)}</span>
        </span>) },
    { id: "source", header: t("colSource"), accessorFn: (r) => r.source ?? "github",
      cell: ({ row }) => (
        <span style={{ display: "grid", gap: 1 }}>
          <span>{sourceWord(row.original.source, t)}</span>
          {row.original.stale && <span style={{ ...small, color: K.amber }}>{t("srcStopped")}</span>}
        </span>) },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state,
      cell: ({ row }) => {
        const lv = externalRowLevel(row.original);
        return <Badge tone={levelTone(lv) as Tone}><LevelGlyph level={lv} />{stateWord(row.original.state, t)}</Badge>;
      } },
    { id: "http", header: t("colHttp"), accessorFn: (r) => r.http_status ?? null,
      cell: ({ row }) => (
        <span style={{ display: "grid", gap: 1 }}>
          <span>{whyCell(row.original, t)}</span>
          {row.original.error && <span style={{ ...small, overflowWrap: "anywhere" }}>{row.original.error}</span>}
        </span>) },
    { id: "latency", header: t("colLatency"), accessorFn: (r) => r.latency_ms ?? null,
      cell: ({ row }) => (row.original.latency_ms == null ? "—" : `${row.original.latency_ms} ms`) },
    { id: "cert", header: t("colCert"), accessorFn: (r) => r.cert_days_left ?? Number.POSITIVE_INFINITY,
      cell: ({ row }) => {
        const r = row.original;
        if (r.cert_days_left == null) return "—";
        return (
          <span style={{ fontWeight: 600, color: levelColor(r.cert_level) }}>
            <LevelGlyph level={r.cert_level} />{t("certDays", fmtNum(r.cert_days_left))}
          </span>
        );
      } },
    { id: "strip", header: t("colStrip"), enableSorting: false, cell: ({ row }) => <ExtStrip cells={row.original.cells} /> },
  ];
}

export function ExternalCard({ section, view }: { section: Section<External> | undefined; view: View }) {
  const t = useP2();
  const w = useWords();
  const { formatDateTime } = useFormat();
  if (!section) return null;
  return (
    <Card title={t("extTitle")} testId="status-external" asOf={section.as_of} refreshS={section.refresh_s}
      stale={section.status === "ok" && !!section.data?.stale}
      actions={<Link href="/admin/platform/settings?app=cis&area=ops" style={{ ...small, color: K.accentDeep,
        fontWeight: 600 }}>{t("lSettings")}</Link>}>
      <WhenUp section={section} testId="status-external-down">
        {(e) => (
          <>
            <span style={small}>{t("extNote")} {t("certNote", e.cert_days.warn, e.cert_days.crit)}</span>
            <SourcesLine sources={e.sources ?? []} t={t} when={(iso) => formatDateTime(iso)} />
            {e.stale && (
              <span role="status" style={{ ...small, color: K.amber, fontWeight: 600 }} data-testid="status-external-stale">
                <LevelGlyph level="warn" />{t("extStale", formatDateTime(e.newest))}
              </span>)}
            <DataTable<ExternalRow> tableId="status-external" data={e.rows.filter((r) => inApps(r.app, view.apps))}
              columns={columns(t, w.app)} getRowId={externalRowId} searchable={false} paginate={false} compact
              tableMinWidth={820} />
          </>
        )}
      </WhenUp>
    </Card>
  );
}
