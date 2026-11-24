"use client";
// app/admin/overview/_status/Apps.tsx — AA-1140: one tile per app (CIS, TripPlanner, Booking, Partner CRM): overall
// health (the App health definition), the health check of the sampler, requests / 5xx / 4xx / p95 over the range
// coloured by the page's limits (cis.ops.status_*), the change against the previous period when compare is on, the
// deployed commit, readiness and the business activity of the range. A signal with no source says "not measured yet".

import { Badge, K, useFormat, type Tone } from "../../../_kit";
import {
  deltaPct, fmtMs, fmtPct, inApps, levelTone, previousRequests, shortSha, type AppTile, type Section, type Signals,
  type View,
} from "@/lib/opsStatus";
import { fmtCount } from "@/lib/numberFormat";
import { Card, NotMeasured, WhenUp, levelColor, num, rowWrap, small, useST } from "./ui";

function Metric({ label, value, level, sub }: { label: string; value: string; level?: string; sub?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase",
        color: K.muted2 }}>{label}</div>
      <div style={{ ...num, fontSize: 18, fontWeight: 600, color: levelColor(level) }}>{value}</div>
      {sub && <div style={{ ...small, fontSize: 11 }}>{sub}</div>}
    </div>
  );
}

function Delta({ value }: { value: number | null }) {
  const t = useST();
  if (value == null) return null;
  const sign = value > 0 ? "+" : "";
  return <span style={{ ...small, fontSize: 11 }} title={t("vsPrevious")}>{`${sign}${value} %`}</span>;
}

function Traffic({ tile, sig, compare }: { tile: AppTile; sig: Signals; compare: boolean }) {
  const t = useST();
  const tr = tile.traffic;
  if (tr.status === "not_measured") return <NotMeasured reason={tr.reason} />;
  if (tr.status !== "ok") return <NotMeasured reason={tr.reason} />;
  const delta = compare ? deltaPct(tr.requests, previousRequests(sig, tile.app)) : null;
  const few = tr.errors_level === "few" ? t("fewRequests") : undefined;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 8 }}>
      <div><Metric label={t("requests")} value={fmtCount(tr.requests)} /><Delta value={delta} /></div>
      <Metric label={t("errors5xx")} value={fmtPct(tr.error_rate_pct, 2)} level={tr.errors_level} sub={few} />
      {tr.client_error_rate_pct == null
        ? <Metric label={t("errors4xx")} value="—" />
        : <Metric label={t("errors4xx")} value={fmtPct(tr.client_error_rate_pct, 2)} level={tr.client_errors_level} />}
      <Metric label={t("p95")} value={fmtMs(tr.p95_ms)} level={tr.p95_level} />
    </div>
  );
}

function Activity({ tile }: { tile: AppTile }) {
  const t = useST();
  const a = tile.activity;
  if (a.status !== "ok") return <NotMeasured reason={a.reason} />;
  return (
    <span style={{ ...small, ...num }}>
      {t("activity")}: {(a.items ?? []).map((i) => `${i.metric.replace(/_/g, " ")} ${fmtCount(i.n)}`).join(" · ") || "0"}
    </span>
  );
}

function Health({ tile }: { tile: AppTile }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  const h = tile.health;
  if (!h) return <NotMeasured reason={null} />;
  return (
    <span style={rowWrap} title={h.reason ?? undefined}>
      <Badge tone={levelTone(h.state) as Tone}>{t("health")}: {h.state.replace("_", " ")}</Badge>
      <span style={small}>{formatDateTime(h.sampled_at)}</span>
    </span>
  );
}

function Tile({ tile, sig, compare }: { tile: AppTile; sig: Signals; compare: boolean }) {
  const t = useST();
  return (
    <div data-testid={`status-app-${tile.app}`} style={{ border: `1px solid ${K.line}`, borderRadius: 10,
      padding: "12px 14px", background: K.card, display: "grid", gap: 10, minWidth: 0 }}>
      <div style={{ ...rowWrap, justifyContent: "space-between" }}>
        <span style={{ fontWeight: 700, color: K.ink }}>{tile.label}</span>
        <span title={tile.overall.reason}>
          <Badge tone={levelTone(tile.overall.status) as Tone}>{tile.overall.status.replace("_", " ")}</Badge></span>
      </div>
      <Health tile={tile} />
      <Traffic tile={tile} sig={sig} compare={compare} />
      <Activity tile={tile} />
      <div style={{ ...rowWrap, ...small }}>
        <span>{t("deployed")}: <code>{shortSha(tile.deployed_sha)}</code></span>
        <span>{t("ready")}: {tile.ready ?? "—"}</span>
        {tile.workers != null && <span>{t("workers")}: {tile.workers}</span>}
      </div>
    </div>
  );
}

export function AppsCard({ section, sig, view }: { section: Section<AppTile[]>; sig: Signals; view: View }) {
  const t = useST();
  return (
    <Card title={t("appsTitle")} testId="status-apps-card" asOf={section.as_of}>
      <WhenUp section={section}>
        {(tiles) => (
          <div style={{ display: "grid", gap: 12,
            gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))" }}>
            {tiles.filter((x) => inApps(x.app, view.apps)).map((x) => (
              <Tile key={x.app} tile={x} sig={sig} compare={view.compare} />))}
          </div>
        )}
      </WhenUp>
    </Card>
  );
}
