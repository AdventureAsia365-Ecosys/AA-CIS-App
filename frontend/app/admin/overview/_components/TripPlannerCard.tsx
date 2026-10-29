"use client";
// app/admin/overview/_components/TripPlannerCard.tsx
// AA-858 — "TripPlanner customers": how many travellers used the planner in the last 7 days (vs the 7 before)
// and a 14-day chart. Data: tripplanner.v_ops_traffic_daily (TripPlanner's own aggregate view) + AI searches
// from shared.llm_call_log; the on-screen source texts (plain words) are in ./sources.ts. Page visits are not in the console (they would need a Vercel token, which the
// admin console must not hold — S241 design §6); the link opens Vercel Web Analytics instead.
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { K, useLang, useT } from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import { CHART_TOOLTIP } from "../../_components/adminUi";
import { ChangeLine, fmtDay, fmtInt, Kpi, OpsCard, UnavailableNote } from "./opsUi";
import { SourceHint } from "./SourceHint";
import { OPS } from "./ops.messages";
import { useSources, type Sources } from "./sources";
import type { Section, TpMetric, TripPlannerData } from "./opsTypes";

/** Vercel Web Analytics of the TripPlanner project (needs a Vercel login on the team). Read-only link. */
export const TRIPPLANNER_ANALYTICS_URL = "https://vercel.com/pqnghiep1354-5974s-projects/aa-tripplanner/analytics";

const KPIS: { key: TpMetric & keyof Sources["TP_SRC"]; label: "plannersOpened" | "tripsStarted" | "sentToAdvisor" | "aiSearches" }[] = [
  { key: "planner_session_started", label: "plannersOpened" },
  { key: "trip_started", label: "tripsStarted" },
  { key: "inquiry_sent", label: "sentToAdvisor" },
  { key: "ai_search", label: "aiSearches" },
];

export function TripPlannerCard({ section }: { section: Section<TripPlannerData> | undefined }) {
  const t = useT(OPS);
  const lang = useLang();
  const { TP_SRC } = useSources();
  const c = useChartColors({ a: "--aa-gold", b: "--aa-info", g: "--aa-green", line: "--aa-line", muted: "--aa-muted" });
  return (
    <OpsCard
      testId="ops-tripplanner"
      title={t("tpTitle")}
      subtitle={t("tpSubtitle")}
      source={TP_SRC.card}
      link={{
        href: TRIPPLANNER_ANALYTICS_URL,
        label: t("pageVisits"),
        external: true,
        hint: t("pageVisitsHint"),
      }}
    >
      {!section ? null : section.status !== "ok" ? (
        <UnavailableNote section={section} />
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))",
              gap: 10,
              marginBottom: 14,
            }}
          >
            {KPIS.map((k) => {
              const pct = section.change_pct?.[k.key];
              return (
                <Kpi
                  key={k.key}
                  testId={`ops-tp-${k.key}`}
                  label={t(k.label)}
                  value={fmtInt(section.totals_7d[k.key])}
                  source={TP_SRC[k.key]}
                  sub={<ChangeLine pct={pct} prev={section.prev_7d?.[k.key]} />}
                  subTone={pct == null ? "muted" : pct >= 0 ? "good" : "bad"}
                />
              );
            })}
          </div>
          <div style={{ fontSize: 12, color: K.muted, marginBottom: 6, display: "flex", alignItems: "center" }}>
            {t("perDay14")}
            <SourceHint source={TP_SRC.chart} />
          </div>
          <div style={{ height: 200, minWidth: 0 }} role="img" aria-label={t("perDayAria")}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={section.days.map((d) => ({ ...d, label: fmtDay(d.day, lang) }))} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
                <CartesianGrid stroke={c.line} vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 10, fill: c.muted }} interval="preserveStartEnd" />
                <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: c.muted }} />
                <Tooltip {...CHART_TOOLTIP} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="planner_session_started" name={t("plannersOpened")} fill={c.a} isAnimationActive={false} />
                <Bar dataKey="trip_started" name={t("tripsStarted")} fill={c.b} isAnimationActive={false} />
                <Bar dataKey="inquiry_sent" name={t("sentToAdvisor")} fill={c.g} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          {section.notes?.ai_search && (
            <div style={{ fontSize: 11, color: K.muted, marginTop: 6 }}>{t("aiSearchesNote", section.notes.ai_search)}</div>
          )}
        </>
      )}
    </OpsCard>
  );
}
