"use client";
// app/admin/ai-gateway/RouteTraceDrawer.tsx — AA-1042 (S268): one gateway request on AI gateway › Trace: every
// attempt it needed in order (the refused or failed ones at $0 with their error class and text, the one that
// answered), with latency and cost, and the stage's route for reference. AA-1042 (S270): a rung deferred behind an
// open refusal circuit has no row of its own; the backend lists it as an attempt with status "skipped" ($0, no time). GET
// /api/admin/ai-gateway/route-trace/requests/{trace id}?at= (backend api/services/llm_route_trace.py read_trace).
// Opened by a Requests row of the Trace tab or a Calls row's "Route" link (?tab=trace&rt=<id>&rt_at=<time>).
// Built on the kit only (Drawer, TraceTimeline, Badge, LinkButton).

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import {
  ApiError, Badge, Drawer, K, TraceTimeline, apiGet, formatDuration, mono, useT, type TimelineEvent,
} from "../../_kit";
import { usd } from "@/lib/aiGateway";
import {
  ROUTE_KEY, SKIPPED_STATUS, attemptTone, outcomeTone, rungLabel, tracePath, type RouteTarget, type RouteTrace,
} from "@/lib/routeTrace";
import { RT, RT_OUTCOME } from "./routeTrace.messages";
import { useErrorClassText } from "./useErrorClassText";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, fontSize: 12.5, padding: "3px 0", flexWrap: "wrap" }}>
      <span style={{ color: K.muted, minWidth: 110 }}>{label}</span>
      <span style={{ color: K.body, minWidth: 0, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

export default function RouteTraceDrawer({ target, onClose }: { target: RouteTarget; onClose: () => void }) {
  const t = useT(RT);
  const to = useT(RT_OUTCOME);
  const errorText = useErrorClassText();
  const q = useQuery({
    queryKey: [...ROUTE_KEY, "request", target.id, target.at],
    queryFn: () => apiGet<RouteTrace>(tracePath(target.id, target.at), { admin: true }),
    retry: false,
  });
  const data = q.data;
  const events = useMemo<TimelineEvent[]>(() => (data?.attempts ?? []).map((a, i) => ({
    id: String(i),
    at: a.at ?? data?.first_at ?? new Date().toISOString(),
    label: a.attempt_no != null ? t("attempt", a.attempt_no)
      : a.status === SKIPPED_STATUS ? t("skippedAttempt") : undefined,
    tone: attemptTone(a.status),
    title: (
      <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <span style={{ fontFamily: mono, fontSize: 12 }}>{rungLabel(a.model, a.connection)}</span>
        <Badge tone={attemptTone(a.status)} dot={false}>{a.status}</Badge>
      </span>
    ),
    detail: [
      a.position != null ? t("position", a.position + 1) : null,
      a.error_class ? errorText(a.error_class) : null,
      a.billed_unusable ? t("billedUnusable") : null,
      a.error ? a.error.slice(0, 160) : null,
    ].filter(Boolean).join(" · ") || null,
    amount: usd(a.cost_usd),
    durationMs: a.latency_ms,
  })), [data, t, errorText]);

  const notFound = q.error instanceof ApiError && q.error.status === 404;
  return (
    <Drawer open onClose={onClose} width={720}
      title={<span style={{ display: "inline-flex", gap: 10, alignItems: "center", minWidth: 0 }}>
        {t("drawerTitle")}{data?.stage ? <span style={{ fontFamily: mono, fontSize: 13 }}>{data.stage}</span> : null}
      </span>}>
      <div data-testid="route-trace-drawer" style={{ display: "grid", gap: 14 }}>
        {q.isLoading && <div style={small}>…</div>}
        {q.error && (
          <div role="alert" style={{ fontSize: 12.5, color: notFound ? K.muted : K.danger }}>
            {notFound ? t("drawerNotFound") : t("loadError")}</div>
        )}
        {data && (
          <>
            <div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
                <Badge tone={outcomeTone(data.outcome)}>{to(data.outcome)}</Badge>
                {data.route_test && <Badge tone="info" dot={false}>{t("testBadge")}</Badge>}
              </div>
              <Line label={t("drawerServed")}>
                {data.served ? <span style={{ fontFamily: mono }}>{rungLabel(data.served.model, data.served.connection)}
                </span> : t("drawerNone")}
              </Line>
              {data.route && data.route.length > 0 && (
                <Line label={t("drawerRoute")}>
                  <span style={{ fontFamily: mono }}>{data.route.join(" → ")}</span></Line>
              )}
              <Line label={t("drawerCost")}><span style={{ fontFamily: mono }}>{usd(data.cost_usd)}</span></Line>
              <Line label={t("drawerTime")}>
                <span style={{ fontFamily: mono }}>{formatDuration(data.latency_ms)}</span></Line>
              {data.job_id && (
                <Line label={t("drawerJob")}>
                  <a href={`/admin/jobs?job=${encodeURIComponent(data.job_id)}`}
                    style={{ color: K.accent, fontFamily: mono, overflowWrap: "anywhere" }}>{data.job_id}</a>
                </Line>
              )}
            </div>
            <TraceTimeline events={events} testId="route-trace-attempts" ariaLabel={t("drawerTitle")} />
          </>
        )}
      </div>
    </Drawer>
  );
}
