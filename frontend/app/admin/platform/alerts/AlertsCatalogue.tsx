"use client";
// app/admin/platform/alerts/AlertsCatalogue.tsx — AA-1171 (S287 decision D1): Platform › Alerts › Catalogue.
//
// The one full list of what the platform watches: one row per alert condition (CloudWatch alarm, EventBridge rule that
// notifies a topic, budget or cost anomaly alert, in-app check of CIS, Booking or the partner CRM) with its app,
// component, type, severity (page / ticket), condition and threshold, where the threshold is changed (Settings,
// Terraform or nowhere yet), channel, runbook, owner role, live state, freshness and coverage gap. The Ecosystem status
// card reads the same API's summary (one source, no second list).
//
// API (backend GET /admin/platform/alerts/catalogue, api/routers/admin_alerts_catalogue.py, ops.read; BFF
// /api/admin/platform/*, admin only — lib/rbac.ts). AWS reads are cached 5 minutes on the server. Filters live in the
// URL (FilterBar). Pure helpers: lib/alertsCatalogue.ts. Kit only; no issue ids in the UI.

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, ListChecks, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";
import {
  Badge, Button, DataTable, ErrorState, FilterBar, K, KpiTile, PageHeader, Section, TableSkeleton, apiGet,
  mono, useFormat, useT, useUrlFilters, type ColumnDef, type Tone,
} from "../../../_kit";
import {
  CATALOGUE_PATH, SOURCES, TYPES, appOptions, isFiring, matchesRow, staleSources, type CatalogueResp, type CatalogueRow,
} from "@/lib/alertsCatalogue";
import { CATALOGUE } from "./catalogue.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const linkStyle = { color: K.accentDeep, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 4 } as const;
const KEY = ["platform-alerts-catalogue"];

function useCatT() {
  return useT(CATALOGUE);
}
type CT = ReturnType<typeof useCatT>;

function severityTone(s: CatalogueRow["severity"]): Tone {
  return s === "page" ? "danger" : s === "ticket" ? "info" : "warning";
}

function stateTone(r: CatalogueRow): Tone {
  if (isFiring(r)) return "danger";
  if (r.state === "OK" || r.state === "ENABLED") return "success";
  if (r.state === "INSUFFICIENT_DATA" || r.state === "DISABLED") return "warning";
  return "neutral";
}

function ChangeCell({ r, t }: { r: CatalogueRow; t: CT }) {
  const label = t(`edit_${r.editable_via}` as "edit_none");
  const external = r.edit_href?.startsWith("https://");
  return (
    <div style={{ display: "grid", gap: 3, minWidth: 0 }}>
      <Badge tone={r.editable_via === "settings" ? "success" : r.editable_via === "terraform" ? "info" : "neutral"}
        dot={false}>{label}</Badge>
      {r.edit_href && (external
        ? <a href={r.edit_href} target="_blank" rel="noreferrer" style={linkStyle}>
          {t("openTerraform")} <ExternalLink size={11} /></a>
        : <Link href={r.edit_href} style={linkStyle}>{t("openSettings")}</Link>)}
      {r.threshold_source && <span style={{ ...small, wordBreak: "break-word" }}>{r.threshold_source}</span>}
    </div>
  );
}

function Kpis({ data, t }: { data: CatalogueResp | undefined; t: CT }) {
  const s = data?.summary;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
      <KpiTile label={t("kpiTotal")} value={s?.total} testId="alerts-kpi-total" />
      <KpiTile label={t("kpiFiring")} value={s?.firing} valueTone={s && s.firing > 0 ? "danger" : "success"}
        hint={s ? t("kpiPageFiring", s.page_firing) : undefined} testId="alerts-kpi-firing" />
      <KpiTile label={t("kpiPage")} value={s?.by_severity.page}
        hint={s ? `${t("sev_ticket")} ${s.by_severity.ticket} · ${t("sev_untagged")} ${s.by_severity.untagged}`
          : undefined} testId="alerts-kpi-page" />
      <KpiTile label={t("kpiGaps")} value={s?.gaps} valueTone={s && s.gaps > 0 ? "warning" : "success"}
        testId="alerts-kpi-gaps" />
    </div>
  );
}

function Breakdown({ data, t }: { data: CatalogueResp; t: CT }) {
  const labels = new Map(data.rows.map((r) => [r.app ?? "unknown", r.app_label]));
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
      <Section title={t("byTypeTitle")} testId="alerts-by-type">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {TYPES.filter((k) => (data.summary.by_type[k] ?? 0) > 0).map((k) => (
            <Badge key={k} tone="neutral" dot={false}>{t(`type_${k}` as "type_metric")}: {data.summary.by_type[k]}</Badge>
          ))}
        </div>
      </Section>
      <Section title={t("byAppTitle")} testId="alerts-by-app">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {Object.entries(data.summary.by_app).map(([app, n]) => (
            <Badge key={app} tone={(data.summary.firing_by_app[app] ?? 0) > 0 ? "danger" : "neutral"} dot={false}>
              {labels.get(app) ?? app}: {n}
            </Badge>
          ))}
        </div>
      </Section>
    </div>
  );
}

function Sources({ data, t }: { data: CatalogueResp; t: CT }) {
  const { formatDateTime } = useFormat();
  const stale = staleSources(data.freshness);
  return (
    <Section title={t("sourcesTitle")} testId="alerts-sources">
      {stale.length > 0 && (
        <p style={{ ...small, color: K.amber, margin: "0 0 8px" }} role="status">
          {t("staleNotice", stale.map((s) => t(`source_${s}` as "source_aws")).join(", "))}
        </p>
      )}
      <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }}>
        {SOURCES.map((s) => {
          const f = data.freshness[s];
          if (!f) return null;
          return (
            <li key={s} style={small}>
              <strong style={{ color: K.ink }}>{t(`source_${s}` as "source_aws")}</strong>{" — "}
              {f.status === "ok" || f.status === "partial"
                ? t("sourceOk", f.as_of ? formatDateTime(f.as_of) : "—")
                : t("sourceDown", String(f.reason ?? f.status ?? "unknown"))}
            </li>
          );
        })}
      </ul>
      <p style={{ ...small, margin: "8px 0 0" }}>{t("thresholdHow")}</p>
    </Section>
  );
}

function CatalogueTable({ data, t }: { data: CatalogueResp; t: CT }) {
  const { get } = useUrlFilters();
  const { formatDateTime } = useFormat();
  const filter = { q: get("q"), app: get("app"), type: get("type"), severity: get("severity"), state: get("state"),
    edit: get("edit"), gaps: get("gaps") };
  const rows = useMemo(() => data.rows.filter((r) => matchesRow(r, filter)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, filter.q, filter.app, filter.type, filter.severity, filter.state, filter.edit, filter.gaps]);
  const columns = useMemo<ColumnDef<CatalogueRow, unknown>[]>(() => [
    { id: "alert", header: t("colAlert"), accessorFn: (r) => r.name,
      cell: ({ row: { original: r } }) => (
        <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
          <span style={{ fontFamily: mono, fontSize: 12.5, color: K.ink, wordBreak: "break-all" }}>{r.name}</span>
          {r.description && <span style={{ ...small, maxWidth: 380 }}>{r.description}</span>}
          <span style={small}>
            {r.component ? `${r.component} · ` : ""}{t("owner", r.owner_role)}
            {r.source === "budgets" && <> · {r.account_id ? t("account", r.account_id) : t("accountMissing")}</>}
          </span>
          {r.runbook && <a href={r.runbook} target="_blank" rel="noreferrer" style={linkStyle}>
            {t("runbook")} <ExternalLink size={11} /></a>}
        </div>
      ) },
    { id: "app", header: t("colApp"), accessorFn: (r) => r.app_label,
      cell: ({ row: { original: r } }) => <span style={small}>{r.app_label}</span> },
    { id: "type", header: t("colType"), accessorKey: "type",
      cell: ({ row: { original: r } }) => <span style={small}>{t(`type_${r.type}` as "type_metric")}</span> },
    { id: "severity", header: t("colSeverity"), accessorFn: (r) => r.severity ?? "untagged",
      cell: ({ row: { original: r } }) => (
        <Badge tone={severityTone(r.severity)} dot={false}>{t(`sev_${r.severity ?? "untagged"}` as "sev_page")}</Badge>
      ) },
    { id: "condition", header: t("colCondition"), accessorFn: (r) => r.condition ?? "",
      cell: ({ row: { original: r } }) => (
        <div style={{ display: "grid", gap: 2, maxWidth: 360 }}>
          <span style={{ fontSize: 12.5, color: K.ink, wordBreak: "break-word" }}>{r.condition ?? "—"}</span>
          {r.threshold && <strong style={{ fontFamily: mono, fontSize: 12 }}>{r.threshold}</strong>}
        </div>
      ) },
    { id: "change", header: t("colChange"), accessorKey: "editable_via",
      cell: ({ row: { original: r } }) => <ChangeCell r={r} t={t} /> },
    { id: "channel", header: t("colChannel"), accessorFn: (r) => r.channel.join(", "), enableSorting: false,
      cell: ({ row: { original: r } }) => <span style={small}>{r.channel.join(", ") || "—"}</span> },
    { id: "state", header: t("colState"), accessorFn: (r) => r.state ?? "",
      cell: ({ row: { original: r } }) => (
        <div style={{ display: "grid", gap: 2 }}>
          <Badge tone={stateTone(r)} dot={false}>{r.state === "not_live" ? t("notLive") : r.state ?? "—"}</Badge>
          {r.state_since && <span style={small}>{t("since")} {formatDateTime(r.state_since)}</span>}
          {r.last_alert_at && <span style={small}>{t("lastAlert")}: {formatDateTime(r.last_alert_at)}</span>}
        </div>
      ) },
    { id: "gap", header: t("colGap"), accessorFn: (r) => r.coverage_gap ?? "", enableSorting: false,
      cell: ({ row: { original: r } }) => <span style={{ ...small, maxWidth: 260, display: "block" }}>
        {r.coverage_gap ?? "—"}</span> },
  ], [t, formatDateTime]);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      <FilterBar testId="alerts-catalogue-filters" search={{ placeholder: t("search") }} selects={[
        { key: "app", label: t("app"), allLabel: t("allApps"), options: appOptions(data.rows), width: 170 },
        { key: "type", label: t("type"), allLabel: t("allTypes"),
          options: TYPES.map((k) => ({ value: k, label: t(`type_${k}` as "type_metric") })), width: 170 },
        { key: "severity", label: t("severity"), allLabel: t("allSeverities"), options: [
          { value: "page", label: t("sev_page") }, { value: "ticket", label: t("sev_ticket") },
          { value: "untagged", label: t("sev_untagged") }], width: 150 },
        { key: "state", label: t("state"), allLabel: t("anyState"), options: [
          { value: "firing", label: t("stateFiring") }, { value: "quiet", label: t("stateQuiet") }], width: 140 },
        { key: "edit", label: t("edit"), allLabel: t("anyEdit"), options: [
          { value: "settings", label: t("edit_settings") }, { value: "terraform", label: t("edit_terraform") },
          { value: "none", label: t("edit_none") }], width: 150 },
        { key: "gaps", label: t("gapFilter"), allLabel: t("gapsAll"), options: [{ value: "1", label: t("gapsOnly") }],
          width: 170 },
      ]} />
      <DataTable<CatalogueRow> tableId="alerts-catalogue" data={rows} columns={columns} getRowId={(r) => r.id}
        rowTestId={(r) => `alerts-row-${r.id}`} paginate={false} searchable={false} tableMinWidth={1300}
        emptyTitle={t("noRows")} enableCsv csvFilename="alerts-catalogue.csv" />
    </div>
  );
}

export default function AlertsCatalogue() {
  const t = useCatT();
  const q = useQuery({ queryKey: KEY, queryFn: () => apiGet<CatalogueResp>(CATALOGUE_PATH), staleTime: 60_000 });
  const data = q.data;
  return (
    // The page (page.tsx) renders the one PageFrame and the tab strip above this header.
    <>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><ListChecks size={18} />
          {t("title")}</span>}
        description={t("intro")}
        actions={<Button size="sm" variant="secondary" onClick={() => void q.refetch()} disabled={q.isFetching}>
          <RefreshCw size={13} /> {t("refresh")}</Button>}
      />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        <Kpis data={data} t={t} />
        {q.error && !data && <ErrorState message={`${t("loadError")} ${(q.error as Error).message}`}
          onRetry={() => void q.refetch()} />}
        {!data && !q.error && <TableSkeleton rows={8} cols={6} />}
        {data && (
          <>
            <span data-testid="alerts-catalogue-ready" hidden />
            <Breakdown data={data} t={t} />
            <Sources data={data} t={t} />
            <CatalogueTable data={data} t={t} />
            <Section title={t("gapsTitle")} description={t("gapsNote")} testId="alerts-gaps">
              <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 6 }}>
                {data.gaps.map((g) => <li key={g.key} style={{ fontSize: 13, color: K.ink, lineHeight: 1.5 }}>
                  {g.text}</li>)}
              </ul>
            </Section>
          </>
        )}
      </div>
    </>
  );
}
