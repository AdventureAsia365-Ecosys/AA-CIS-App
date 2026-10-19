"use client";
// app/admin/overview/_components/TripPlannerCard.tsx
// AA-858 — "TripPlanner customers": how many travellers used the planner in the last 7 days (vs the 7 before)
// and a 14-day chart. Data: tripplanner.v_ops_traffic_daily (TripPlanner's own aggregate view) + AI searches
// from shared.llm_call_log; the on-screen source texts (plain words) are in ./sources.ts. Page visits are not in the console (they would need a Vercel token, which the
// admin console must not hold — S241 design §6); the link opens Vercel Web Analytics instead.
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { K } from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import { CHART_TOOLTIP } from "../../_components/adminUi";
import { ChangeLine, fmtDay, fmtInt, Kpi, OpsCard, UnavailableNote } from "./opsUi";
import { SourceHint } from "./SourceHint";
import { TP_SRC } from "./sources";
import type { Section, TpMetric, TripPlannerData } from "./opsTypes";

/** Vercel Web Analytics of the TripPlanner project (needs a Vercel login on the team). Read-only link. */
export const TRIPPLANNER_ANALYTICS_URL = "https://vercel.com/pqnghiep1354-5974s-projects/aa-tripplanner/analytics";

const KPIS: { key: TpMetric; label: string; source: string }[] = [
  { key: "planner_session_started", label: "Planners opened", source: TP_SRC.planner_session_started },
  { key: "trip_started", label: "Trips started", source: TP_SRC.trip_started },
  { key: "inquiry_sent", label: "Sent to an advisor", source: TP_SRC.inquiry_sent },
  { key: "ai_search", label: "AI searches", source: TP_SRC.ai_search },
];

export function TripPlannerCard({ section }: { section: Section<TripPlannerData> | undefined }) {
  const c = useChartColors({ a: "--aa-gold", b: "--aa-info", g: "--aa-green", line: "--aa-line", muted: "--aa-muted" });
  return (
    <OpsCard
      testId="ops-tripplanner"
      title="TripPlanner customers"
      subtitle="Last 7 days, compared with the 7 days before (UTC days)"
      source={TP_SRC.card}
      link={{
        href: TRIPPLANNER_ANALYTICS_URL,
        label: "Page visits (Vercel Analytics)",
        external: true,
        hint: "Opens Vercel Web Analytics for TripPlanner in a new tab (needs a Vercel login)",
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
                  label={k.label}
                  value={fmtInt(section.totals_7d[k.key])}
                  source={k.source}
                  sub={<ChangeLine pct={pct} prev={section.prev_7d?.[k.key]} />}
                  subTone={pct == null ? "muted" : pct >= 0 ? "good" : "bad"}
                />
              );
            })}
          </div>
          <div style={{ fontSize: 12, color: K.muted, marginBottom: 6, display: "flex", alignItems: "center" }}>
            Per day, last 14 days
            <SourceHint source={TP_SRC.chart} />
          </div>
          <div style={{ height: 200, minWidth: 0 }} role="img" aria-label="Planners opened and trips started per day">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={section.days.map((d) => ({ ...d, label: fmtDay(d.day) }))} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
                <CartesianGrid stroke={c.line} vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 10, fill: c.muted }} interval="preserveStartEnd" />
                <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: c.muted }} />
                <Tooltip {...CHART_TOOLTIP} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="planner_session_started" name="Planners opened" fill={c.a} isAnimationActive={false} />
                <Bar dataKey="trip_started" name="Trips started" fill={c.b} isAnimationActive={false} />
                <Bar dataKey="inquiry_sent" name="Sent to an advisor" fill={c.g} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          {section.notes?.ai_search && (
            <div style={{ fontSize: 11, color: K.muted, marginTop: 6 }}>AI searches: {section.notes.ai_search}</div>
          )}
        </>
      )}
    </OpsCard>
  );
}
