"use client";
// app/admin/overview/_status/External.tsx — AA-1142 (P2): the external strip, next to the internal Services grid. The
// root repo workflow External checks runs every 15 minutes on GitHub Actions against the public hosts (DNS, the TLS
// certificate, the health path); the 5-minute sampler copies each run into acp_shared.ops_external_check. One row per
// target: state now, HTTP status and time, days until the certificate ends (amber / red at the API's
// cis.ops.status_cert_warn_days / _crit_days), the strip of the range. A feed that stopped reporting greys the card and
// says since when. Kit only; a level is said in words and a glyph.

import Link from "next/link";
import { Badge, DataTable, K, useFormat, type ColumnDef, type Tone } from "../../../_kit";
import { fmtNum, inApps, levelTone, type Section, type View } from "@/lib/opsStatus";
import { externalLevel, type External, type ExternalRow } from "@/lib/opsStatusP2";
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

function columns(t: PT, app: (id: string) => string): ColumnDef<ExternalRow>[] {
  return [
    { id: "target", header: t("colTarget"), accessorFn: (r) => r.host,
      cell: ({ row }) => (
        <span style={{ display: "grid", gap: 1 }}>
          <b style={{ color: K.ink, overflowWrap: "anywhere" }}>{row.original.host}</b>
          <span style={small}>{app(row.original.app)}</span>
        </span>) },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state,
      cell: ({ row }) => {
        const lv = externalLevel(row.original);
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
            <span style={small}>{t("extNote", e.schedule_minutes)} {t("certNote", e.cert_days.warn, e.cert_days.crit)}</span>
            {e.stale && (
              <span role="status" style={{ ...small, color: K.amber, fontWeight: 600 }} data-testid="status-external-stale">
                <LevelGlyph level="warn" />{t("extStale", formatDateTime(e.newest))}
              </span>)}
            <DataTable<ExternalRow> tableId="status-external" data={e.rows.filter((r) => inApps(r.app, view.apps))}
              columns={columns(t, w.app)} getRowId={(r) => r.target} searchable={false} paginate={false} compact
              tableMinWidth={720} />
          </>
        )}
      </WhenUp>
    </Card>
  );
}
