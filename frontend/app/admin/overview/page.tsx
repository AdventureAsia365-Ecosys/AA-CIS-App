"use client";
// app/admin/overview/page.tsx
// AA-664 — Admin Overview dashboard: whole-system snapshot in one page.
// Data: GET /api/admin/overview (BFF -> /admin/overview, cached 60s). Built on the UI kit +
// react-query (AA-662 standard). Every widget links to its detail page.
// AA-858 — a business view on top (apps health, TripPlanner customers, Booking funnel, cost per app) from
// GET /api/admin/overview/ops (cached 5–15 min server-side, so no auto-refresh here); the AA-664 sections
// follow under "CIS content & operations". Cards live in ./_components/.
// S243 — Jobs: "Workers alive now" counts only live workers (the Apps page / deploy-gate rule); the provider cost
// table reads month-to-date AI from the cost module (all bedrock* providers, rolled-up days included), so its AI
// total equals the AI estimate on Cost per app; the per-country table keeps every column and scrolls on a phone.
// AA-858 (S243) — every number on the page names its source in a SourceHint (kit InfoTip); the wording, in plain
// business words, is in ./_components/sources.ts.

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Boxes,
  CircleDollarSign,
  ListChecks,
  TriangleAlert,
  Users,
} from "lucide-react";
import Link from "next/link";
import { sans } from "../_components/adminUi";
import { AppHealthStrip } from "./_components/AppHealthStrip";
import { BookingFunnelCard } from "./_components/BookingFunnelCard";
import { CostByAppCard } from "./_components/CostByAppCard";
import { TripPlannerCard } from "./_components/TripPlannerCard";
import type { OpsOverview } from "./_components/opsTypes";
import { SourceHint } from "./_components/SourceHint";
import { useSources, type Sources } from "./_components/sources";
import { OVERVIEW } from "./messages";
import {
  apiGet,
  Badge,
  ErrorState,
  K,
  LoadingScreen,
  PageHeader,
  ScrollX,
  SkeletonStyle,
  Spinner,
  StatusBadge,
  type Tone, AdminMain,
  useFormat,
  useT,
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
    /** AI month to date, every provider (= the AI estimate of /admin/platform/cost) */
    ai_month_to_date_usd?: number;
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
  source,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  href?: string;
  /** Where the section's numbers come from, shown next to the title. */
  source?: string;
  children: React.ReactNode;
}) {
  const t = useT(OVERVIEW);
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
            {source && <SourceHint source={source} />}
          </h2>
        </div>
        {href && (
          <a
            href={href}
            style={{ fontSize: 12, color: K.accentDeep, textDecoration: "none", fontWeight: 500 }}
          >
            {t("viewDetail")}
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
  source,
  sub,
  color = K.ink,
}: {
  label: string;
  value: React.ReactNode;
  /** Where the number comes from (SourceHint next to the label). */
  source: string;
  sub?: string;
  color?: string;
}) {
  return (
    <div
      data-kpi=""
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
        <SourceHint source={source} size={11} />
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

// Number columns of the per-country pipeline table, with where each comes from (message key, source key).
type PipelineWord = "raw" | "generated" | "inReview" | "published" | "atomized";
const PIPELINE_COLS: { label: PipelineWord; source: keyof Sources["PIPELINE_SRC"] }[] = [
  { label: "raw", source: "raw" },
  { label: "generated", source: "generated" },
  { label: "inReview", source: "review" },
  { label: "published", source: "published" },
  { label: "atomized", source: "atomized" },
];

// Rows of the provider cost table: the spend_budget providers, in plain words (a message key, or a product name
// that stays as it is). "other" shows only when it has spend.
const COST_PROVIDERS: { key: string; label: "bedrock" | "otherAi" | null; name?: string; ai: boolean }[] = [
  { key: "bedrock", label: "bedrock", ai: true },
  { key: "openai", label: null, name: "OpenAI", ai: true },
  { key: "jev", label: null, name: "Jev", ai: true },
  { key: "other", label: "otherAi", ai: true },
  { key: "dfs", label: null, name: "DataForSEO", ai: false },
];

export default function OverviewPage() {
  const t = useT(OVERVIEW);
  const { formatDateTime } = useFormat();
  const { INTEL_SRC, JOBS_SRC, PIPELINE_SRC, PROVIDER_COST_SRC, TENANT_SRC } = useSources();
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["admin-overview"],
    queryFn: () => apiGet<Overview>("/api/admin/overview", { admin: true }),
    refetchInterval: 60_000,
  });
  // AA-858 — the ops view: no polling (AWS-backed, cached server-side), Refresh refetches it too.
  const ops = useQuery({
    queryKey: ["admin-overview-ops"],
    queryFn: () => apiGet<OpsOverview>("/api/admin/overview/ops", { admin: true }),
    staleTime: 300_000,
    refetchOnWindowFocus: false,
  });
  const opsDown = ops.isError
    ? { status: "unavailable" as const, reason: "error", message: t("sectionError") }
    : undefined;

  const money = (n: number | undefined) =>
    n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

  return (
    <>
      <SkeletonStyle />
      <AdminMain>
          <PageHeader
            title={t("title")}
            description={t("description")}
            actions={
              <button
                onClick={() => {
                  refetch();
                  ops.refetch();
                }}
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
                {isFetching || ops.isFetching ? t("refreshing") : t("refresh")}
              </button>
            }
          />

          {/* ── AA-858: business view ── */}
          <div data-testid="ops-overview" style={{ display: "flex", flexDirection: "column", gap: 18, marginBottom: 28 }}>
            {ops.isLoading ? (
              <div style={{ display: "flex", alignItems: "center", gap: 10, minHeight: 120, color: K.muted, fontSize: 13 }}>
                <Spinner size={18} /> {t("loadingOps")}
              </div>
            ) : (
              <>
                <AppHealthStrip section={ops.data?.health ?? opsDown} />
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))",
                    gap: 18,
                    alignItems: "start",
                  }}
                >
                  <TripPlannerCard section={ops.data?.tripplanner ?? opsDown} />
                  <BookingFunnelCard section={ops.data?.booking ?? opsDown} />
                </div>
                <CostByAppCard section={ops.data?.cost ?? opsDown} />
              </>
            )}
          </div>

          <h2
            data-testid="overview-cis-heading"
            style={{ margin: "0 0 12px", fontSize: 17, fontWeight: 600, color: K.ink, fontFamily: sans }}
          >
            {t("cisHeading")}
          </h2>

          {isLoading ? (
            <LoadingScreen message={t("loading")} />
          ) : isError ? (
            <ErrorState
              message={(error as Error)?.message || t("loadFailed")}
              onRetry={() => refetch()}
            />
          ) : !data ? null : (
            <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
              {/* ── Section 1: Pipeline funnel ── */}
              <Section
                icon={<Activity size={16} />}
                title={t("pipelineTitle")}
                href="/admin/master-content"
                source={PIPELINE_SRC.card}
              >
                <ScrollX testId="overview-pipeline-table">
                  <table style={{ width: "100%", minWidth: 560, borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={TH}>{t("country")}</th>
                        {PIPELINE_COLS.map((c) => (
                          <th key={c.label} data-num-col="" style={{ ...TH, textAlign: "right", whiteSpace: "nowrap" }}>
                            {t(c.label)}
                            <SourceHint source={PIPELINE_SRC[c.source]} size={11} />
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {data.pipeline_funnel.map((r) => (
                        <tr key={r.country}>
                          <td style={{ ...TD, fontWeight: 600, color: K.ink }}>
                            {r.country || t("unknown")}
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
                </ScrollX>
              </Section>

              {/* ── Section 2: Intelligence ── */}
              <Section
                icon={<Boxes size={16} />}
                title={t("intelligence")}
                href="/admin/atom-curation"
              >
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
                    gap: 12,
                  }}
                >
                  <Stat
                    label={t("atoms")}
                    value={data.intelligence.atom_count.toLocaleString()}
                    source={INTEL_SRC.atoms}
                    color={K.accentDeep}
                  />
                  <Stat label={t("segments")} value={data.intelligence.segment_count.toLocaleString()} source={INTEL_SRC.segments} />
                  <Stat label={t("routes")} value={data.intelligence.route_count.toLocaleString()} source={INTEL_SRC.routes} />
                  <Stat label={t("hubs")} value={data.intelligence.hub_count.toLocaleString()} source={INTEL_SRC.hubs} />
                  <Stat label={t("scoreRows")} value={data.intelligence.score_count.toLocaleString()} source={INTEL_SRC.scores} />
                  <Stat
                    label={t("researchPlaces")}
                    source={INTEL_SRC.research}
                    value={data.intelligence.research.places_researched.toLocaleString()}
                    sub={
                      data.intelligence.research.places_stale > 0
                        ? t("stale", data.intelligence.research.places_stale)
                        : t("allFresh")
                    }
                    color={data.intelligence.research.places_stale > 0 ? K.amber : K.ink}
                  />
                </div>
              </Section>

              {/* ── Section 3: Tenants ── */}
              <Section icon={<Users size={16} />} title={t("tenants")} href="/admin/tenants">
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
                    gap: 12,
                    marginBottom: data.tenants.quota_outliers.length > 0 ? 14 : 0,
                  }}
                >
                  <Stat label={t("activeTenants")} value={data.tenants.active_tenants} source={TENANT_SRC.active} />
                  <Stat label={t("rewrites7d")} value={data.tenants.rewrites_7d} source={TENANT_SRC.rewrites} />
                  <Stat label={t("pieces7d")} value={data.tenants.pieces_7d} source={TENANT_SRC.pieces} />
                  <Stat label={t("published7d")} value={data.tenants.published_pieces_7d} source={TENANT_SRC.published} />
                </div>
                {data.tenants.quota_outliers.length > 0 && (
                  <ScrollX>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={TH}>{t("quotaOutlier")}</th>
                        <th style={TH}>{t("plan")}</th>
                        <th data-num-col="" style={{ ...TH, textAlign: "right" }}>
                          {t("toursPct")}
                          <SourceHint source={TENANT_SRC.quotaTours} size={11} />
                        </th>
                        <th data-num-col="" style={{ ...TH, textAlign: "right" }}>
                          {t("apiPct")}
                          <SourceHint source={TENANT_SRC.quotaCalls} size={11} />
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.tenants.quota_outliers.map((q) => (
                        <tr key={q.tenant_name}>
                          <td style={{ ...TD, fontWeight: 600, color: K.ink }}>{q.tenant_name}</td>
                          <td style={TD}>{q.plan_tier}</td>
                          <td style={{ ...TD, textAlign: "right", color: q.quota_tours_pct >= 100 ? K.danger : K.amber }}>
                            {q.quota_tours_pct}%
                          </td>
                          <td style={{ ...TD, textAlign: "right", color: q.quota_calls_pct >= 100 ? K.danger : K.amber }}>
                            {q.quota_calls_pct}%
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  </ScrollX>
                )}
              </Section>

              {/* ── Section 4: Jobs ── */}
              <Section icon={<ListChecks size={16} />} title={t("jobs30d")} href="/admin/jobs" source={JOBS_SRC.counts}>
                <div data-testid="overview-job-counts" style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
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
                    <span style={{ fontSize: 13, color: K.muted }}>{t("noJobs")}</span>
                  )}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 18px", marginTop: 12, fontSize: 12, color: K.muted }}>
                  <span data-testid="overview-workers-alive" data-kpi="">
                    {t("workersAlive")}{" "}
                    <b style={{ color: data.jobs.workers_alive ? K.success : K.danger }}>
                      {data.jobs.workers_alive ?? "—"}
                    </b>
                    <SourceHint source={JOBS_SRC.workers} size={11} />
                  </span>
                  <span data-kpi="">
                    {t("jobsWaiting")} <b style={{ color: K.ink }}>{data.jobs.queue_depth ?? "—"}</b>
                    <SourceHint source={JOBS_SRC.waiting} size={11} />
                  </span>
                  <span data-kpi="">
                    {t("jobsRunning")} <b style={{ color: K.ink }}>{data.jobs.running_now ?? "—"}</b>
                    <SourceHint source={JOBS_SRC.running} size={11} />
                  </span>
                </div>
              </Section>

              {/* ── Section 5: Cost ── */}
              <Section
                icon={<CircleDollarSign size={16} />}
                title={t("providersTitle")}
                href="/admin/cost-ops"
              >
                <ScrollX testId="overview-provider-cost-table">
                  <table style={{ width: "100%", minWidth: 480, borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={TH}>{t("provider")}</th>
                        <th data-num-col="" style={{ ...TH, textAlign: "right" }}>
                          {t("today")}
                          <SourceHint source={PROVIDER_COST_SRC.today} size={11} />
                        </th>
                        <th data-num-col="" style={{ ...TH, textAlign: "right" }}>
                          {t("monthToDate")}
                          <SourceHint source={PROVIDER_COST_SRC.month} size={11} />
                        </th>
                        <th data-num-col="" style={{ ...TH, textAlign: "right" }}>
                          {t("dailyBudget")}
                          <SourceHint source={PROVIDER_COST_SRC.budget} size={11} />
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {COST_PROVIDERS.filter(
                        (c) => c.key !== "other" || (data.cost.month_to_date_usd.other ?? 0) > 0,
                      ).map(({ key: p, label, name }) => {
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
                            <td style={TD}>{label ? t(label) : name}</td>
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
                              {money(data.cost.month_to_date_usd[p] ?? 0)}
                            </td>
                            <td style={{ ...TD, textAlign: "right", color: K.muted }}>
                              {budget?.per_day_usd != null ? money(budget.per_day_usd) : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </ScrollX>
                {data.cost.ai_month_to_date_usd != null && (
                  <div data-testid="overview-ai-mtd-note" style={{ marginTop: 10, fontSize: 12, color: K.muted, lineHeight: 1.5 }}>
                    {t("aiThisMonth")} <b style={{ color: K.ink }}>{money(data.cost.ai_month_to_date_usd)}</b>
                    <SourceHint source={PROVIDER_COST_SRC.aiMonth} size={11} />{" "}
                    {t("sameAsEstimate")}{" "}
                    <Link href="/admin/platform/cost" style={{ color: K.accentDeep, fontWeight: 600 }}>{t("costPerApp")}</Link>.{" "}
                    {t("todayFromMidnight")}
                  </div>
                )}
                {data.cost.dfs_balance?.has_data && (
                  <div
                    data-testid="overview-dfs-balance"
                    style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
                  >
                    <span style={{ fontSize: 12, color: K.muted, display: "inline-flex", alignItems: "center" }}>
                      {t("dfsBalance")}
                      <SourceHint source={PROVIDER_COST_SRC.dfsBalance} size={11} />
                    </span>
                    <Badge tone={data.cost.dfs_balance.below_threshold ? "danger" : "success"}>
                      {money(data.cost.dfs_balance.balance_usd)}
                    </Badge>
                    {data.cost.dfs_balance.below_threshold && (
                      <span style={{ fontSize: 11, color: K.danger }}>
                        {t("belowThreshold", String(data.cost.dfs_balance.threshold_usd))}
                      </span>
                    )}
                  </div>
                )}
              </Section>

              {/* ── Section 6: Alerts ── */}
              <Section icon={<TriangleAlert size={16} />} title={t("alerts")} href="/admin/jobs">
                {data.alerts.length === 0 ? (
                  <div style={{ fontSize: 13, color: K.muted }}>{t("noAlerts")}</div>
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
                            {formatDateTime(a.created_at)}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Section>

              <div style={{ fontSize: 11, color: K.muted2, textAlign: "right" }}>
                {data._cached ? t("fromCache") : t("fresh")} · {t("autoRefresh")}
              </div>
            </div>
          )}
        </AdminMain>
    </>
  );
}
