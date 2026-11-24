"use client";
// app/admin/overview/_status/Capacity.tsx — AA-1141: the capacity forecast (P1 feature 4: days until RDS free storage
// and each AI connection's monthly cap reach their limit at today's pace; the GitHub Actions cache has no source and
// says "not measured yet") and the AI cost anomaly banner (P1 feature 5: an hour of one app on one provider route above
// N x its median hour). Every limit comes from the API (cis.ops.status_forecast_* / status_cost_anomaly_*); a level is
// said in words and a glyph, never by colour alone. Kit only.

import Link from "next/link";
import { Badge, DataTable, K, useFormat, useLang, type ColumnDef, type Tone } from "../../../_kit";
import {
  fmtDays, fmtNum, inApps, levelTone, type Anomaly, type Forecast, type ForecastRow, type Section, type View,
} from "@/lib/opsStatus";
import { fmtUsd } from "@/lib/numberFormat";
import { Card, LevelGlyph, NotMeasured, WhenUp, rowWrap, small, useST, type ST } from "./ui";
import { useWords } from "./words";

function levelWord(level: string, t: ST): string {
  if (level === "crit") return t("lvCrit");
  if (level === "warn") return t("lvWarn");
  return level === "ok" ? t("lvOk") : t("lvNone");
}

function label(r: ForecastRow, t: ST): string {
  return r.id === "rds_free" ? t("fcRds") : r.label;
}

function daysCell(r: ForecastRow, t: ST) {
  if (r.status !== "ok") return <NotMeasured reason={r.reason} />;
  const none = r.id === "rds_free" ? t("notShrinking") : t("notThisMonth");
  return (
    <span style={{ fontWeight: 600 }}>
      <LevelGlyph level={r.level} />{r.days_left == null ? none : t("daysLeft", fmtDays(r.days_left))}
    </span>
  );
}

function limitCell(r: ForecastRow, t: ST, lang: "en" | "vi") {
  if (r.status !== "ok") return "—";
  if (r.id === "rds_free") {
    const parts = [r.warn_gib != null ? t("fcRdsWarn", `${fmtNum(r.warn_gib)} GiB`) : null,
      r.max_allocated_gib ? t("fcRdsCeiling", `${fmtNum(r.max_allocated_gib)} GiB`) : null].filter(Boolean);
    return <span style={small}>{parts.join(" · ") || "—"}</span>;
  }
  return <span style={small}>{t("fcAiCap", fmtUsd(r.cap_usd ?? 0, lang), fmtUsd(r.forecast_usd ?? 0, lang))}</span>;
}

function columns(t: ST, lang: "en" | "vi"): ColumnDef<ForecastRow>[] {
  const val = (r: ForecastRow, v: number | null | undefined) =>
    v == null ? "—" : r.unit === "USD" ? fmtUsd(v, lang) : `${fmtNum(v)} ${r.unit ?? ""}`;
  return [
    { id: "resource", header: t("colResource"), accessorFn: (r) => label(r, t),
      cell: ({ row }) => <b style={{ color: K.ink, overflowWrap: "anywhere" }}>{label(row.original, t)}</b> },
    { id: "now", header: t("colNow"), accessorFn: (r) => r.latest ?? null, cell: ({ row }) => val(row.original,
      row.original.latest) },
    { id: "pace", header: t("colPace"), accessorFn: (r) => r.per_day ?? null,
      cell: ({ row }) => val(row.original, row.original.per_day) },
    { id: "days", header: t("colDaysLeft"), accessorFn: (r) => r.days_left ?? Number.POSITIVE_INFINITY,
      cell: ({ row }) => daysCell(row.original, t) },
    { id: "limit", header: t("colLimit"), enableSorting: false, cell: ({ row }) => limitCell(row.original, t, lang) },
    { id: "level", header: t("colLevel"), accessorFn: (r) => r.level,
      cell: ({ row }) => <Badge tone={levelTone(row.original.level) as Tone}>{levelWord(row.original.level, t)}</Badge> },
  ];
}

export function ForecastCard({ section }: { section: Section<Forecast> | undefined }) {
  const t = useST();
  const lang = useLang();
  return (
    <Card title={t("forecastTitle")} testId="status-forecast" asOf={section?.as_of} refreshS={section?.refresh_s}
      actions={<Link href="/admin/platform/settings?app=cis&area=ops" style={{ ...small, color: K.accentDeep,
        fontWeight: 600 }}>{t("lSettings")}</Link>}>
      <WhenUp section={section}>
        {(f) => (
          <>
            <span style={small}>{t("forecastNote", f.window_days, f.warn, f.crit)}</span>
            <DataTable<ForecastRow> tableId="status-forecast" data={f.rows} columns={columns(t, lang)}
              getRowId={(r) => r.id} searchable={false} paginate={false} compact tableMinWidth={620} />
          </>
        )}
      </WhenUp>
    </Card>
  );
}

/** The cost anomaly banner: shown only when an hour is abnormal, above the verdict. Empty otherwise. */
export function AnomalyBanner({ section, view }: { section: Section<Anomaly> | undefined; view: View }) {
  const t = useST();
  const w = useWords();
  const lang = useLang();
  const { formatDateTime } = useFormat();
  const a = section?.status === "ok" ? section.data : undefined;
  const rows = (a?.rows ?? []).filter((r) => inApps(r.app, view.apps));
  if (!a || rows.length === 0) return null;
  const usd = (v: number) => fmtUsd(v, lang);
  return (
    <div role="alert" data-testid="status-anomaly" style={{ border: `1px solid ${K.dangerBorder}`,
      background: K.dangerTint, borderRadius: 10, padding: "10px 14px", display: "grid", gap: 6 }}>
      <span style={{ ...rowWrap, fontWeight: 700, color: K.danger }}>
        <LevelGlyph level="crit" />{t("anomalyTitle")}
        <Link href="/admin/ai-gateway" style={{ ...small, color: K.accentDeep, fontWeight: 600 }}>{t("lAi")}</Link>
      </span>
      <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 2 }}>
        {rows.map((r) => (
          <li key={`${r.app}${r.route}${r.hour}`} style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>
            {t("anomalyLine", w.app(r.app), r.route, usd(r.usd), formatDateTime(r.hour),
              r.times_median != null ? `${fmtNum(r.times_median)}×` : "", usd(r.median_usd_h))}
            {r.partial ? ` · ${t("anomalyPartial")}` : ""}
          </li>
        ))}
      </ul>
      {a.factor != null && a.baseline_days != null && a.floor_usd_h != null && (
        <span style={small}>{t("anomalyRule", a.factor, a.baseline_days, usd(a.floor_usd_h))}</span>)}
    </div>
  );
}
