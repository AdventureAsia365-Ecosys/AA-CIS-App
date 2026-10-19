"use client";
// app/admin/overview/_components/AppHealthStrip.tsx
// AA-858 — "Apps health — last 24 hours": one tile per app (CIS, TripPlanner, Booking) with a plain status,
// requests, server errors, response time and a 24-bar hourly chart (requests vs errors). Numbers come from
// AWS CloudWatch through GET /admin/overview/ops (cached 5 min server-side).
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";
import { K } from "../../../_kit";
import { useChartColors } from "../../../_kit/useTheme";
import { CHART_TOOLTIP } from "../../_components/adminUi";
import { BOOKING_ADMIN_URL, TRIPPLANNER_URL } from "../../../../lib/apps";
import { SourceHint } from "./SourceHint";
import { fmtInt, fmtMs, Kpi, OpsCard, StatePill, UnavailableNote } from "./opsUi";
import type { AlarmBucket, AppHealth, HealthData, Section } from "./opsTypes";

const APP_LINK: Partial<Record<AppHealth["app"], { href: string; label: string }>> = {
  tripplanner: { href: TRIPPLANNER_URL, label: "Open TripPlanner" },
  booking: { href: BOOKING_ADMIN_URL, label: "Open Booking admin" },
};

const WHAT: Record<AppHealth["app"], string> = {
  cis: "Content system and this admin console",
  tripplanner: "Public trip planner for travellers",
  booking: "Trip cases, proposals and payments",
};

function src(app: AppHealth, metric: string): string {
  const where =
    app.kind === "apigw"
      ? `AWS CloudWatch, API Gateway ${app.resources.join(", ")}`
      : `AWS CloudWatch, Lambda ${app.resources.join(" + ")}`;
  return `${where} — ${metric}, last 24 hours`;
}

function hourLabel(iso: string): string {
  return `${iso.slice(11, 13)}:00 UTC`;
}

function Sparkline({ app }: { app: AppHealth }) {
  const c = useChartColors({ req: "--aa-gold", err: "--aa-red" });
  const data = app.hourly.map((h) => ({ hour: hourLabel(h.hour), ok: Math.max(0, h.requests - h.errors), err: h.errors }));
  return (
    <div style={{ height: 44, minWidth: 0 }} aria-label="Requests per hour, last 24 hours" role="img">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }} barCategoryGap={1}>
          <XAxis dataKey="hour" hide />
          <Tooltip
            {...CHART_TOOLTIP}
            cursor={{ fill: "transparent" }}
            formatter={(v, name) => [fmtInt(Number(v)), name === "err" ? "Server errors" : "Successful"]}
          />
          <Bar dataKey="ok" stackId="a" fill={c.req} fillOpacity={0.7} isAnimationActive={false} />
          <Bar dataKey="err" stackId="a" fill={c.err} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function AlarmList({ alarms }: { alarms: AlarmBucket | null }) {
  if (!alarms) return null;
  if (alarms.firing.length === 0) {
    return (
      <div style={{ fontSize: 11, color: K.muted }}>
        {alarms.total > 0 ? `${alarms.total} automatic checks watching, none firing` : "No automatic checks set up"}
      </div>
    );
  }
  return (
    <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12, color: K.danger }}>
      {alarms.firing.map((a) => (
        <li key={a.name} title={a.description || a.name} style={{ overflowWrap: "anywhere" }}>
          {a.description || a.name}
        </li>
      ))}
    </ul>
  );
}

function AppTile({ app }: { app: AppHealth }) {
  const link = APP_LINK[app.app];
  const errSub =
    app.error_rate_pct == null
      ? "no requests"
      : `${app.error_rate_pct}% of requests`;
  return (
    <div
      data-testid={`ops-health-${app.app}`}
      style={{
        border: `1px solid ${K.line}`,
        borderRadius: 12,
        padding: 14,
        display: "flex",
        flexDirection: "column",
        gap: 10,
        minWidth: 0,
        background: K.card,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: K.ink }}>{app.label}</div>
          <div style={{ fontSize: 11, color: K.muted }}>{WHAT[app.app]}</div>
        </div>
        <StatePill state={app.state} />
      </div>
      <div style={{ fontSize: 12, color: K.body }}>
        {app.state_reason}
        {app.partial && " (some numbers still arriving)"}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 6 }}>
        <Kpi
          compact
          label="Requests"
          value={fmtInt(app.requests)}
          source={src(app, app.kind === "apigw" ? "Count (sum)" : "Invocations (sum)")}
        />
        <Kpi
          compact
          label="Errors"
          value={fmtInt(app.errors)}
          color={app.errors ? K.danger : K.ink}
          sub={errSub}
          source={src(app, app.kind === "apigw" ? "5XXError (sum) — server errors" : "Errors (sum) — server errors")}
        />
        <Kpi
          compact
          label="Speed"
          value={fmtMs(app.p95_ms)}
          sub="95% answered within"
          source={src(app, app.kind === "apigw" ? "Latency p95" : "Duration p95 (slower of the two functions)")}
        />
      </div>
      <Sparkline app={app} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 12px", fontSize: 11, color: K.muted }}>
        {app.client_errors != null && (
          <span>
            Rejected requests (4xx): {fmtInt(app.client_errors)}
            <SourceHint source={src(app, "4XXError (sum) — bad input or not signed in, not a fault")} size={11} />
          </span>
        )}
        {app.throttles != null && (
          <span>
            Throttled: {fmtInt(app.throttles)}
            <SourceHint source={src(app, "Throttles (sum) — requests refused because of the concurrency limit")} size={11} />
          </span>
        )}
      </div>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0, flex: "1 1 160px" }}>
          <AlarmList alarms={app.alarms} />
        </div>
        {link && (
          <a
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            style={{ fontSize: 12, color: K.accentDeep, textDecoration: "none", fontWeight: 500, whiteSpace: "nowrap" }}
          >
            {link.label} ↗
          </a>
        )}
      </div>
    </div>
  );
}

export function AppHealthStrip({ section, source }: { section: Section<HealthData> | undefined; source: string }) {
  return (
    <OpsCard
      testId="ops-health"
      title="Apps health — last 24 hours"
      subtitle="Is each app up, and are customers getting errors?"
      source={source}
    >
      {!section ? null : section.status !== "ok" ? (
        <UnavailableNote section={section} />
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))",
              gap: 12,
            }}
          >
            {section.apps.map((a) => (
              <AppTile key={a.app} app={a} />
            ))}
          </div>
          <div style={{ marginTop: 10, fontSize: 11, color: K.muted, display: "flex", flexWrap: "wrap", gap: "4px 14px" }}>
            {section.shared_alarms && (
              <span data-testid="ops-health-shared">
                Shared services (network, staff sign-in, e-mail):{" "}
                {section.shared_alarms.firing.length > 0 ? (
                  <b style={{ color: K.danger }}>
                    {section.shared_alarms.firing.length} alarm{section.shared_alarms.firing.length > 1 ? "s" : ""} firing
                  </b>
                ) : (
                  <b style={{ color: K.success }}>all {section.shared_alarms.total} checks OK</b>
                )}
                <SourceHint source="AWS CloudWatch alarms named aa-ecosys-*, aa-identity-*, aa-email-*" size={11} />
              </span>
            )}
            {section.alarms_error && <span>Alarm states could not be read right now.</span>}
            <span>Updated {new Date(section.as_of).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</span>
          </div>
        </>
      )}
    </OpsCard>
  );
}
