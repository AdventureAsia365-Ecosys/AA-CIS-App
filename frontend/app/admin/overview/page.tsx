"use client";
// app/admin/overview/page.tsx
// AA-664 — Admin Overview dashboard: whole-system snapshot in one page.
// Data: GET /api/admin/overview (BFF -> /admin/overview, cached 60s). Built on the UI kit +
// react-query (AA-662 standard). Every widget links to its detail page.

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Boxes,
  CircleDollarSign,
  ListChecks,
  TriangleAlert,
  Users,
} from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, sans } from "../_components/adminUi";
import {
  apiGet,
  Badge,
  ErrorState,
  K,
  LoadingScreen,
  mono,
  PageHeader,
  SkeletonStyle,
  StatusBadge,
  type Tone,
} from "../../_kit";

// ── types ───────────────────────────────────────────────────────────────────
interface FunnelRow {
  country: string;
  raw_active: number;
  raw_7d: number;
  generated: number;
  generated_7d: number;
  in_review: number;
  published: number;
  published_7d: number;
  atomized: number;
}
interface Overview {
  pipeline_funnel: FunnelRow[];
  intelligence: {
    atom_count: number;
    segment_count: number;
    route_count: number;
    hub_count: number;
    score_count: number;
    research: {
      places_researched: number;
      places_stale: number;
      demand_rows: number;
      last_researched: string | null;
    };
  };
  tenants: {
    active_tenants: number;
    rewrites_7d: number;
    pieces_7d: number;
    published_pieces_7d: number;
    quota_outliers: {
      tenant_name: string;
      plan_tier: string;
      quota_tours_pct: number;
      quota_calls_pct: number;
    }[];
  };
  jobs: {
    counts: Record<string, number>;
    workers_alive: number | null;
    queue_depth: number | null;
    running_now: number | null;
  };
  cost: {
    spent_today_usd: Record<string, number>;
    month_to_date_usd: Record<string, number>;
    budgets: {
      provider: string;
      scope: string;
      per_day_usd: number | null;
      alert_pct: number;
    }[];
    dfs_balance: {
      balance_usd?: number;
      below_threshold?: boolean;
      threshold_usd?: number;
      has_data?: boolean;
    } | null;
  };
  alerts: {
    event_type: string;
    entity_type: string | null;
    entity_id: string | null;
    payload: Record<string, unknown>;
    created_at: string;
  }[];
  _cached?: boolean;
}

// ── small building blocks ─────────────────────────────────────────────────────
function Section({
  icon,
  title,
  href,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  href?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      style={{
        background: K.card,
        border: `1px solid ${K.line}`,
        borderRadius: 12,
        padding: "18px 20px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 14,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: K.accentDeep, display: "inline-flex" }}>{icon}</span>
          <h2
            style={{
              margin: 0,
              fontSize: 14,
              fontWeight: 600,
              color: K.ink,
              fontFamily: sans,
              letterSpacing: "0.01em",
            }}
          >
            {title}
          </h2>
        </div>
        {href && (
          <a
            href={href}
            style={{ fontSize: 12, color: K.accentDeep, textDecoration: "none", fontWeight: 500 }}
          >
            View detail →
          </a>
        )}
      </div>
      {children}
    </section>
  );
}

function Stat({
  label,
  value,
  sub,
  color = K.ink,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  color?: string;
}) {
  return (
    <div
      style={{
        background: K.bg,
        border: `1px solid ${K.line}`,
        borderRadius: 10,
        padding: "12px 14px",
        minWidth: 0,
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: K.muted2,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 24,
          fontWeight: 600,
          color,
          fontVariantNumeric: "tabular-nums",
          letterSpacing: "-0.02em",
          marginTop: 3,
        }}
      >
        {value}
      </div>
      {sub && <div style={{ fontSize: 11, color: K.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const TH: React.CSSProperties = {
  textAlign: "left",
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.05em",
  textTransform: "uppercase",
  color: K.muted2,
  padding: "6px 10px",
  borderBottom: `1px solid ${K.line}`,
};
const TD: React.CSSProperties = {
  fontSize: 13,
  color: K.body,
  padding: "7px 10px",
  borderBottom: `1px solid ${K.line2}`,
};

function delta(n: number) {
  if (!n) return null;
  return (
    <span style={{ fontSize: 11, color: K.success, marginLeft: 6, fontWeight: 600 }}>
      +{n}/7d
    </span>
  );
}

const JOB_FAIL = new Set(["failed", "stopped_budget"]);

export default function OverviewPage() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["admin-overview"],
    queryFn: () => apiGet<Overview>("/api/admin/overview", { admin: true }),
    refetchInterval: 60_000,
  });

  const money = (n: number | undefined) =>
    n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: K.bg }}>
      <SkeletonStyle />
      <AdminSidebar />
      <div className="aa-admin-main" style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh" }}>
        <header
          style={{
            height: 56,
            background: "#fff",
            borderBottom: `1px solid ${K.line}`,
            display: "flex",
            alignItems: "center",
            padding: "0 32px",
            gap: 8,
            position: "sticky",
            top: 0,
            zIndex: 10,
          }}
        >
          <span style={{ fontSize: 12, color: K.muted2 }}>Admin /</span>
          <span style={{ fontSize: 12, fontWeight: 500, color: K.body }}>Overview</span>
        </header>

        <main style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
          <PageHeader
            title="Overview"
            description="Whole-system snapshot across the content pipeline, intelligence, tenants, jobs and cost."
            actions={
              <button
                onClick={() => refetch()}
                style={{
                  padding: "7px 14px",
                  borderRadius: 999,
                  border: `1px solid ${K.line}`,
                  background: K.card,
                  color: K.ink3,
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: "pointer",
                  fontFamily: sans,
                }}
              >
                {isFetching ? "Refreshing…" : "Refresh"}
              </button>
            }
          />

          {isLoading ? (
            <LoadingScreen message="Loading overview…" />
          ) : isError ? (
            <ErrorState
              message={(error as Error)?.message || "Failed to load overview."}
              onRetry={() => refetch()}
            />
          ) : !data ? null : (
            <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
              {/* ── Section 1: Pipeline funnel ── */}
              <Section
                icon={<Activity size={16} />}
                title="Pipeline funnel — per country"
                href="/admin/master-content"
              >
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={TH}>Country</th>
                        {["Raw (active)", "S1 generated", "In review", "Published", "Atomized"].map(
                          (h) => (
                            <th key={h} style={{ ...TH, textAlign: "right" }}>
                              {h}
                            </th>
                          ),
                        )}
                      </tr>
                    </thead>
                    <tbody>
                      {data.pipeline_funnel.map((r) => (
                        <tr key={r.country}>
                          <td style={{ ...TD, fontWeight: 600, color: K.ink }}>
                            {r.country || "Unknown"}
                          </td>
                          <td style={{ ...TD, textAlign: "right" }}>
                            {r.raw_active}
                            {delta(r.raw_7d)}
                          </td>
                          <td style={{ ...TD, textAlign: "right" }}>
                            {r.generated}
                            {delta(r.generated_7d)}
                          </td>
                          <td style={{ ...TD, textAlign: "right" }}>
                            {r.in_review > 0 ? (
                              <span style={{ color: K.amber, fontWeight: 600 }}>{r.in_review}</span>
                            ) : (
                              <span style={{ color: K.muted2 }}>0</span>
                            )}
                          </td>
                          <td style={{ ...TD, textAlign: "right", fontWeight: 600 }}>
                            {r.published}
                            {delta(r.published_7d)}
                          </td>
                          <td style={{ ...TD, textAlign: "right" }}>{r.atomized}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Section>

              {/* ── Section 2: Intelligence ── */}
              <Section
                icon={<Boxes size={16} />}
                title="Intelligence"
                href="/admin/atom-curation"
              >
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
                    gap: 12,
                  }}
                >
                  <Stat label="Atoms" value={data.intelligence.atom_count.toLocaleString()} color={K.accentDeep} />
                  <Stat label="Segments" value={data.intelligence.segment_count.toLocaleString()} />
                  <Stat label="Routes" value={data.intelligence.route_count.toLocaleString()} />
                  <Stat label="Hubs" value={data.intelligence.hub_count.toLocaleString()} />
                  <Stat label="Score rows" value={data.intelligence.score_count.toLocaleString()} />
                  <Stat
                    label="Research places"
                    value={data.intelligence.research.places_researched.toLocaleString()}
                    sub={
                      data.intelligence.research.places_stale > 0
                        ? `${data.intelligence.research.places_stale} stale (>30d)`
                        : "all fresh"
                    }
                    color={data.intelligence.research.places_stale > 0 ? K.amber : K.ink}
                  />
                </div>
              </Section>

              {/* ── Section 3: Tenants ── */}
              <Section icon={<Users size={16} />} title="Tenants" href="/admin/tenants">
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
                    gap: 12,
                    marginBottom: data.tenants.quota_outliers.length > 0 ? 14 : 0,
                  }}
                >
                  <Stat label="Active tenants" value={data.tenants.active_tenants} />
                  <Stat label="Rewrites (7d)" value={data.tenants.rewrites_7d} />
                  <Stat label="Pieces (7d)" value={data.tenants.pieces_7d} />
                  <Stat label="Published (7d)" value={data.tenants.published_pieces_7d} />
                </div>
                {data.tenants.quota_outliers.length > 0 && (
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={TH}>Quota outlier</th>
                        <th style={TH}>Plan</th>
                        <th style={{ ...TH, textAlign: "right" }}>Tours %</th>
                        <th style={{ ...TH, textAlign: "right" }}>API %</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.tenants.quota_outliers.map((t) => (
                        <tr key={t.tenant_name}>
                          <td style={{ ...TD, fontWeight: 600, color: K.ink }}>{t.tenant_name}</td>
                          <td style={TD}>{t.plan_tier}</td>
                          <td style={{ ...TD, textAlign: "right", color: t.quota_tours_pct >= 100 ? K.danger : K.amber }}>
                            {t.quota_tours_pct}%
                          </td>
                          <td style={{ ...TD, textAlign: "right", color: t.quota_calls_pct >= 100 ? K.danger : K.amber }}>
                            {t.quota_calls_pct}%
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Section>

              {/* ── Section 4: Jobs ── */}
              <Section icon={<ListChecks size={16} />} title="Jobs (30d)" href="/admin/jobs">
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
                  {Object.entries(data.jobs.counts).map(([status, n]) => (
                    <div
                      key={status}
                      style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                    >
                      <StatusBadge status={status} />
                      <span style={{ fontSize: 13, fontWeight: 600, color: JOB_FAIL.has(status) ? K.danger : K.ink }}>
                        {n}
                      </span>
                    </div>
                  ))}
                  {Object.keys(data.jobs.counts).length === 0 && (
                    <span style={{ fontSize: 13, color: K.muted }}>No jobs in the last 30 days.</span>
                  )}
                </div>
                <div style={{ display: "flex", gap: 18, marginTop: 12, fontSize: 12, color: K.muted }}>
                  <span>
                    Workers alive:{" "}
                    <b style={{ color: data.jobs.workers_alive ? K.success : K.danger }}>
                      {data.jobs.workers_alive ?? "—"}
                    </b>
                  </span>
                  <span>
                    Queue depth: <b style={{ color: K.ink }}>{data.jobs.queue_depth ?? "—"}</b>
                  </span>
                  <span>
                    Running now: <b style={{ color: K.ink }}>{data.jobs.running_now ?? "—"}</b>
                  </span>
                </div>
              </Section>

              {/* ── Section 5: Cost ── */}
              <Section
                icon={<CircleDollarSign size={16} />}
                title="Cost — today & month-to-date vs budget"
                href="/admin/llm-usage"
              >
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={TH}>Provider</th>
                        <th style={{ ...TH, textAlign: "right" }}>Today</th>
                        <th style={{ ...TH, textAlign: "right" }}>Month-to-date</th>
                        <th style={{ ...TH, textAlign: "right" }}>Daily budget</th>
                      </tr>
                    </thead>
                    <tbody>
                      {["bedrock", "openai", "dfs", "jev"].map((p) => {
                        const budget = data.cost.budgets.find(
                          (b) => b.provider === p && b.scope === "global",
                        );
                        const today = data.cost.spent_today_usd[p];
                        const over =
                          budget?.per_day_usd != null &&
                          today != null &&
                          today >= budget.per_day_usd * (budget.alert_pct / 100);
                        return (
                          <tr key={p}>
                            <td style={{ ...TD, fontFamily: mono, fontSize: 12 }}>{p}</td>
                            <td
                              style={{
                                ...TD,
                                textAlign: "right",
                                color: over ? K.danger : K.body,
                                fontWeight: over ? 700 : 400,
                              }}
                            >
                              {money(today)}
                            </td>
                            <td style={{ ...TD, textAlign: "right" }}>
                              {money(data.cost.month_to_date_usd[p])}
                            </td>
                            <td style={{ ...TD, textAlign: "right", color: K.muted }}>
                              {budget?.per_day_usd != null ? money(budget.per_day_usd) : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {data.cost.dfs_balance?.has_data && (
                  <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 12, color: K.muted }}>DataForSEO balance:</span>
                    <Badge tone={data.cost.dfs_balance.below_threshold ? "danger" : "success"}>
                      {money(data.cost.dfs_balance.balance_usd)}
                    </Badge>
                    {data.cost.dfs_balance.below_threshold && (
                      <span style={{ fontSize: 11, color: K.danger }}>
                        below ${data.cost.dfs_balance.threshold_usd} threshold
                      </span>
                    )}
                  </div>
                )}
              </Section>

              {/* ── Section 6: Alerts ── */}
              <Section icon={<TriangleAlert size={16} />} title="Alerts" href="/admin/jobs">
                {data.alerts.length === 0 ? (
                  <div style={{ fontSize: 13, color: K.muted }}>No open admin alerts.</div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {data.alerts.map((a, i) => {
                      const tone: Tone = a.event_type.includes("exhausted")
                        ? "danger"
                        : a.event_type.includes("low") || a.event_type.includes("budget")
                          ? "warning"
                          : "neutral";
                      return (
                        <div
                          key={i}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            padding: "8px 12px",
                            background: K.bg,
                            border: `1px solid ${K.line}`,
                            borderRadius: 8,
                          }}
                        >
                          <Badge tone={tone} dot>
                            {a.event_type}
                          </Badge>
                          <span style={{ fontSize: 12, color: K.body }}>
                            {a.entity_type}
                            {a.entity_id ? ` · ${a.entity_id}` : ""}
                          </span>
                          <span style={{ marginLeft: "auto", fontSize: 11, color: K.muted2 }}>
                            {new Date(a.created_at).toLocaleString()}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Section>

              <div style={{ fontSize: 11, color: K.muted2, textAlign: "right" }}>
                {data._cached ? "Served from 60s cache" : "Freshly computed"} · auto-refresh 60s
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
