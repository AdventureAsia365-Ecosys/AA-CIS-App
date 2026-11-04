"use client";
// app/admin/cost-ops/IncidentCard.tsx — AA-665: a recorded cost incident (backend api/services/cost_incidents.py) shown
// on Cost Operations › DataForSEO when the window overlaps it. The text is the reviewed finding (trigger, scope,
// failure point, cause, what changed since); every number comes from the logs over the incident window, read live by
// GET /api/admin/dfs-usage/runs (field `incidents`). AA-994: labels in the UI language; the incident text stays as sent.

import type React from "react";
import { Badge, K, RADIUS, mono, useFormat, useT } from "../../_kit";
import { Stat, int, noteStyle, numStyle, statGrid, usd } from "./costOpsUi";
import { PANELS } from "./panels.messages";

export interface IncidentView {
  id: string;
  provider: string;
  title: string;
  started_at: string;
  ended_at: string;
  trigger: string;
  scope: string;
  failure_point: string;
  cause: string;
  since_fixed: string[];
  investigated: string;
  dfs: {
    calls: number;
    live_calls: number;
    cost_usd: number;
    linked_calls: number;
    first_paid_at: string | null;
    last_paid_at: string | null;
    endpoints: { endpoint: string; calls: number; live_calls: number; cost_usd: number; keywords: number }[];
  };
  llm: {
    source: "raw" | "daily" | null;
    calls: number;
    cost_usd: number;
    stages: { stage: string; calls: number; cost_usd: number }[];
  };
}

const cardStyle: React.CSSProperties = {
  border: `1px solid ${K.dangerBorder}`, background: K.dangerTint, borderRadius: RADIUS.md, padding: "12px 14px",
  marginBottom: 14, minWidth: 0,
};
const termStyle: React.CSSProperties = { fontSize: 11.5, fontWeight: 600, color: K.ink3, margin: "10px 0 2px" };
const textStyle: React.CSSProperties = { fontSize: 13, color: K.ink, lineHeight: 1.5, margin: 0 };

function Field({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={termStyle}>{term}</div>
      <div style={textStyle}>{children}</div>
    </div>
  );
}

export default function IncidentCard({ incident }: { incident: IncidentView }) {
  const t = useT(PANELS);
  const { formatDateTime } = useFormat();
  const { dfs, llm } = incident;
  const llmSub = llm.source === "daily"
    ? t("wholeDays")
    : llm.stages.map((s) => s.stage).join(", ");
  return (
    <section style={cardStyle} data-testid="cost-ops-incident" aria-label={t("incidentAria", incident.title)}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        <Badge tone="danger">{t("recordedIncident")}</Badge>
        <strong style={{ fontSize: 14, color: K.ink }}>{incident.title}</strong>
      </div>
      <div style={{ ...noteStyle, marginTop: 4 }}>
        {formatDateTime(incident.started_at)} → {formatDateTime(incident.ended_at)}
      </div>
      <div style={{ ...statGrid, marginTop: 10 }}>
        <Stat label={t("dfsSpend")} value={usd(dfs.cost_usd)} tone="danger"
              sub={t("callsLive", int(dfs.calls), int(dfs.live_calls))} />
        <Stat label={t("llmSameRun")} value={usd(llm.cost_usd)} sub={t("callsWith", int(llm.calls), llmSub)} />
        <Stat label={t("firstPaid")} value={dfs.first_paid_at ? formatDateTime(dfs.first_paid_at) : "—"} />
        <Stat label={t("lastPaid")} value={dfs.last_paid_at ? formatDateTime(dfs.last_paid_at) : "—"}
              sub={t("whereStopped")} />
      </div>
      {dfs.endpoints.length > 0 && (
        <div style={{ ...noteStyle, marginTop: 6 }}>
          {dfs.endpoints.filter((e) => e.calls > 0).map((e) => (
            <span key={e.endpoint} style={{ marginRight: 14, whiteSpace: "nowrap" }}>
              <span style={{ fontFamily: mono }}>{e.endpoint}</span>{" "}
              <span style={numStyle}>{int(e.calls)} · {usd(e.cost_usd, 2)}</span>
            </span>
          ))}
        </div>
      )}
      <Field term={t("trigger")}>{incident.trigger}</Field>
      <Field term={t("scope")}>{incident.scope}</Field>
      <Field term={t("failurePoint")}>{incident.failure_point}</Field>
      <Field term={t("cause")}>{incident.cause}</Field>
      {incident.since_fixed.length > 0 && (
        <Field term={t("changedSince")}>
          <ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>
            {incident.since_fixed.map((f) => <li key={f}>{f}</li>)}
          </ul>
        </Field>
      )}
      {incident.investigated && <div style={{ ...noteStyle, marginTop: 10 }}>{incident.investigated}</div>}
    </section>
  );
}
