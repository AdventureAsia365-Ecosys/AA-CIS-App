"use client";
// app/admin/tenants/[id]/page.tsx
// AA-666 — Tenant 360: full tenant management page, 9 tabs, on the UI kit + react-query.
// Route param is a Promise in Next 16 (see frontend/AGENTS.md); unwrapped with React.use().
//
// Tabs backed by real endpoints: Overview (1), Tours (3), Usage & Billing (6), Settings (9).
// Partially real (real where data exists, placeholder for the gap): Profile & Brand (2, no
// per-version diff endpoint), Social Content (4, counts only), Channels (5, no admin WordPress
// read), Quality (7, platform telemetry client-filtered to this tenant). Placeholder/deferred:
// Audit (8, no per-tenant read endpoint yet). No new backend endpoint — this is a kit assembly
// over the endpoints that already exist (AA-666 scope: "khung kit + endpoint có sẵn, placeholder
// phần thiếu").

import { use, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import AdminSidebar from "../../_components/AdminSidebar";
import { sans } from "../../_components/adminUi";
import {
  apiGet,
  Badge,
  EmptyState,
  ErrorState,
  K,
  LoadingScreen,
  mono,
  PageHeader,
  SkeletonStyle,
  StatusBadge,
  Tabs,
  type Tone,
} from "../../../_kit";

// ── shapes (loose — endpoints return more; we read what each tab needs) ─────────────────────
interface TenantDetails {
  tenant_id: string;
  summary: {
    plan_name?: string;
    total_rewrites?: number;
    catalog_total?: number;
    avg_quality?: number;
    api_calls_this_month?: number;
    quota_pct?: number;
    total_llm_cost_usd?: number;
    llm_cost_window_usd?: number;
    member_since?: string;
    pipeline_note?: string;
  };
  brand_rules?: {
    brand_name?: string;
    brand_type?: string;
    core_idea?: string;
    customer_segment?: string;
    customer_mindset?: string;
    style_guide?: string;
    forbidden_words?: string[];
    target_markets?: string[];
    rewrite_language?: string;
    version_count?: number;
    last_updated?: string;
    good_examples?: string;
  };
  rewritten_tours?: {
    version_id: string;
    tour_name: string;
    country: string | null;
    quality_score: number | null;
    version_number: number;
    status: string;
    master_status?: string | null;
    created_at: string;
  }[];
  pagination?: { total?: number };
}
interface TenantRow {
  tenant_id: string;
  name: string;
  slug: string;
  plan_tier: string;
  is_active: boolean;
  rate_limit_rpm: number;
  posts_per_week: number;
  country: string | null;
}
interface Billing {
  tenant_name?: string;
  plan_tier?: string;
  tours_quota_monthly?: number;
  api_calls_quota_monthly?: number;
  price_usd_monthly?: number;
  tours_rewritten?: number;
  api_calls_used?: number;
  quota_tours_pct?: number;
  quota_calls_pct?: number;
  tours_overage?: number;
  overage_usd?: number;
  llm_cost_usd?: number;
}

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "brand", label: "Profile & Brand" },
  { key: "tours", label: "Tours" },
  { key: "social", label: "Social Content" },
  { key: "channels", label: "Channels & Integrations" },
  { key: "billing", label: "Usage & Billing" },
  { key: "quality", label: "Quality" },
  { key: "audit", label: "Audit" },
  { key: "settings", label: "Settings" },
];

const card: React.CSSProperties = {
  background: K.card, border: `1px solid ${K.line}`, borderRadius: 12, padding: "16px 18px",
};
const TH: React.CSSProperties = {
  textAlign: "left", fontSize: 10, fontWeight: 700, letterSpacing: "0.05em",
  textTransform: "uppercase", color: K.muted2, padding: "6px 10px", borderBottom: `1px solid ${K.line}`,
};
const TD: React.CSSProperties = { fontSize: 13, color: K.body, padding: "7px 10px", borderBottom: `1px solid ${K.line2}` };

function Stat({ label, value, sub, color = K.ink }: {
  label: string; value: React.ReactNode; sub?: string; color?: string;
}) {
  return (
    <div style={{ background: K.bg, border: `1px solid ${K.line}`, borderRadius: 10, padding: "12px 14px" }}>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase", color: K.muted2 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color, fontVariantNumeric: "tabular-nums", marginTop: 3 }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: K.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const money = (n: number | undefined | null) =>
  n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

function Deferred({ what }: { what: string }) {
  return (
    <EmptyState
      title="Not wired yet"
      description={`${what} — this tab's backend endpoint is not available yet; the layout is in place and will fill in once the endpoint lands.`}
    />
  );
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
function OverviewTab({ d, billing }: { d: TenantDetails; billing?: Billing }) {
  const s = d.summary || {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Stat label="Tours rewritten" value={s.total_rewrites ?? 0} color={K.accentDeep} />
        <Stat label="Catalog tours" value={s.catalog_total ?? 0} />
        <Stat label="Avg quality" value={s.avg_quality != null ? Number(s.avg_quality).toFixed(1) : "—"} color={K.success} />
        <Stat label="API calls (month)" value={s.api_calls_this_month ?? 0} sub={s.quota_pct != null ? `${s.quota_pct}% quota` : undefined} />
        <Stat label="LLM cost (all time)" value={money(s.total_llm_cost_usd)} />
        <Stat label="Overage (month)" value={money(billing?.overage_usd)} color={billing?.overage_usd ? K.amber : K.ink} />
      </div>
      {s.pipeline_note && <div style={{ fontSize: 12, color: K.muted }}>{s.pipeline_note}</div>}
    </div>
  );
}

function BrandTab({ b }: { b: TenantDetails["brand_rules"] }) {
  if (!b || !b.brand_name) return <EmptyState title="No brand identity" description="This tenant has no brand rules yet." />;
  const row = (label: string, val?: React.ReactNode) =>
    val ? (
      <div style={{ display: "flex", gap: 12, padding: "7px 0", borderBottom: `1px solid ${K.line2}` }}>
        <div style={{ width: 160, flexShrink: 0, fontSize: 12, fontWeight: 600, color: K.muted }}>{label}</div>
        <div style={{ fontSize: 13, color: K.body, minWidth: 0 }}>{val}</div>
      </div>
    ) : null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={card}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
          <span style={{ fontSize: 15, fontWeight: 600, color: K.ink }}>{b.brand_name}</span>
          {b.brand_type && <Badge tone="accent" dot={false}>{b.brand_type}</Badge>}
          {b.version_count != null && <Badge tone="neutral" dot={false}>v{b.version_count}</Badge>}
        </div>
        {row("Core idea", b.core_idea)}
        {row("Customer segment", b.customer_segment)}
        {row("Customer mindset", b.customer_mindset)}
        {row("Markets", (b.target_markets || []).join(", ") || undefined)}
        {row("Language", b.rewrite_language)}
        {row("Forbidden words", (b.forbidden_words || []).join(", ") || undefined)}
        {row("Style guide", b.style_guide ? `${b.style_guide.slice(0, 300)}${b.style_guide.length > 300 ? "…" : ""}` : undefined)}
        {row("Last updated", b.last_updated ? new Date(b.last_updated).toLocaleString() : undefined)}
      </div>
      <div style={{ fontSize: 12, color: K.muted2 }}>
        Version history &amp; diff: {b.version_count ?? 1} version(s) on record. Per-version content/diff
        needs a dedicated endpoint (not yet available) — tracked for a follow-up.
      </div>
    </div>
  );
}

function ToursTab({ tours }: { tours: TenantDetails["rewritten_tours"] }) {
  if (!tours || tours.length === 0) return <EmptyState title="No tours" description="This tenant has no rewritten tours yet." />;
  return (
    <div style={{ ...card, padding: 0 }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th style={TH}>Tour</th>
            <th style={TH}>Country</th>
            <th style={{ ...TH, textAlign: "right" }}>Score</th>
            <th style={{ ...TH, textAlign: "right" }}>Version</th>
            <th style={TH}>Status</th>
            <th style={TH}>Rewritten</th>
          </tr>
        </thead>
        <tbody>
          {tours.map((t) => (
            <tr key={t.version_id}>
              <td style={{ ...TD, fontWeight: 600, color: K.ink }}>{t.tour_name}</td>
              <td style={TD}>{t.country || "—"}</td>
              <td style={{ ...TD, textAlign: "right", fontWeight: 600, color: (t.quality_score ?? 0) >= 7 ? K.success : K.amber }}>
                {t.quality_score != null ? Number(t.quality_score).toFixed(1) : "—"}
              </td>
              <td style={{ ...TD, textAlign: "right" }}>v{t.version_number}</td>
              <td style={TD}><StatusBadge status={t.status} /></td>
              <td style={{ ...TD, color: K.muted }}>{new Date(t.created_at).toLocaleDateString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BillingTab({ b }: { b?: Billing }) {
  if (!b) return <LoadingScreen message="Loading billing…" />;
  const meter = (label: string, pct?: number, used?: number, quota?: number) => (
    <div style={card}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
        <span style={{ fontWeight: 600, color: K.ink }}>{label}</span>
        <span style={{ color: K.muted }}>{used ?? 0} / {quota ?? "∞"} ({pct ?? 0}%)</span>
      </div>
      <div style={{ height: 8, background: K.line2, borderRadius: 4, overflow: "hidden" }}>
        <div style={{ width: `${Math.min(100, pct ?? 0)}%`, height: "100%", background: (pct ?? 0) >= 100 ? K.danger : K.accent }} />
      </div>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Stat label="Plan" value={b.plan_tier ?? "—"} sub={money(b.price_usd_monthly) + "/mo"} />
        <Stat label="LLM cost (month)" value={money(b.llm_cost_usd)} color={K.accentDeep} />
        <Stat label="Overage" value={money(b.overage_usd)} sub={`${b.tours_overage ?? 0} tours`} color={b.overage_usd ? K.amber : K.ink} />
      </div>
      {meter("Tours quota", b.quota_tours_pct, b.tours_rewritten, b.tours_quota_monthly)}
      {meter("API calls quota", b.quota_calls_pct, b.api_calls_used, b.api_calls_quota_monthly)}
      <div style={{ fontSize: 12, color: K.muted2 }}>
        Invoices and per-stage cost breakdown are deferred (no endpoint yet). Totals above are from
        the monthly usage view.
      </div>
    </div>
  );
}

function QualityTab({ tenantId }: { tenantId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["gate-telemetry"],
    queryFn: () => apiGet<{ data?: { by_tenant_channel?: Record<string, unknown>[] } }>(
      "/api/admin/a4/gate-telemetry", { admin: true }),
  });
  if (isLoading) return <LoadingScreen message="Loading quality telemetry…" />;
  if (isError) return <ErrorState message="Failed to load gate telemetry." />;
  const rows = (data?.data?.by_tenant_channel || []).filter(
    (r) => String((r as { tenant_id?: string }).tenant_id) === tenantId);
  if (rows.length === 0) return <EmptyState title="No gate telemetry" description="No gate activity recorded for this tenant yet." />;
  return (
    <div style={{ ...card, padding: 0 }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {["Channel", "Total", "Warn", "Held", "Failed", "Published"].map((h, i) => (
              <th key={h} style={{ ...TH, textAlign: i === 0 ? "left" : "right" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const o = r as Record<string, number | string>;
            return (
              <tr key={i}>
                <td style={{ ...TD, fontWeight: 600, color: K.ink }}>{String(o.channel)}</td>
                <td style={{ ...TD, textAlign: "right" }}>{o.total}</td>
                <td style={{ ...TD, textAlign: "right", color: K.amber }}>{o.warn_count}</td>
                <td style={{ ...TD, textAlign: "right", color: K.amber }}>{o.held_count}</td>
                <td style={{ ...TD, textAlign: "right", color: Number(o.failed_count) > 0 ? K.danger : K.muted }}>{o.failed_count}</td>
                <td style={{ ...TD, textAlign: "right", color: K.success }}>{o.publish_count}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SettingsTab({ row }: { row?: TenantRow }) {
  if (!row) return <LoadingScreen message="Loading settings…" />;
  const line = (label: string, val: React.ReactNode) => (
    <div style={{ display: "flex", gap: 12, padding: "8px 0", borderBottom: `1px solid ${K.line2}` }}>
      <div style={{ width: 180, flexShrink: 0, fontSize: 12, fontWeight: 600, color: K.muted }}>{label}</div>
      <div style={{ fontSize: 13, color: K.body }}>{val}</div>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={card}>
        {line("Plan tier", row.plan_tier)}
        {line("Rate limit (RPM)", row.rate_limit_rpm)}
        {line("Posts / week", row.posts_per_week)}
        {line("Country", row.country || "—")}
        {line("Status", row.is_active ? <Badge tone="success" dot={false}>active</Badge> : <Badge tone="danger" dot={false}>inactive</Badge>)}
      </div>
      <div style={{ fontSize: 12, color: K.muted2 }}>
        Editing (plan, RPM, markets, posts/week, activate/deactivate) uses the existing
        PATCH/PUT tenant + config endpoints; wiring the inline editors is a follow-up — this view is
        read-only for now.
      </div>
    </div>
  );
}

export default function Tenant360Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tab, setTab] = useState("overview");

  const details = useQuery({
    queryKey: ["tenant-details", id],
    queryFn: () => apiGet<TenantDetails>(`/api/admin/tenants/${id}/details`, { admin: true }),
  });
  const billing = useQuery({
    queryKey: ["tenant-billing", id],
    queryFn: () => apiGet<Billing>(`/api/admin/billing?tenant_id=${id}`, { admin: true }),
  });
  const tenantRow = useQuery({
    queryKey: ["tenant-row", id],
    queryFn: async () => {
      const all = await apiGet<{ tenants?: TenantRow[] }>("/api/admin/tenants", { admin: true });
      return (all.tenants || []).find((t) => t.tenant_id === id) ?? null;
    },
  });

  const name = tenantRow.data?.name || details.data?.brand_rules?.brand_name || "Tenant";
  const plan = details.data?.summary?.plan_name || tenantRow.data?.plan_tier;
  const active = tenantRow.data?.is_active;

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: K.bg }}>
      <SkeletonStyle />
      <AdminSidebar />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh" }}>
        <header style={{
          height: 56, background: "#fff", borderBottom: `1px solid ${K.line}`, display: "flex",
          alignItems: "center", padding: "0 32px", gap: 8, position: "sticky", top: 0, zIndex: 10,
        }}>
          <a href="/admin/tenants" style={{ fontSize: 12, color: K.muted2, textDecoration: "none" }}>Admin / Tenants</a>
          <span style={{ fontSize: 12, color: K.muted2 }}>/</span>
          <span style={{ fontSize: 12, fontWeight: 500, color: K.body }}>{name}</span>
        </header>

        <main style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
          <PageHeader
            title={name}
            description={
              <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                {plan && <Badge tone="accent" dot={false}>{plan}</Badge>}
                {active != null && <Badge tone={active ? ("success" as Tone) : ("danger" as Tone)} dot={false}>{active ? "active" : "inactive"}</Badge>}
                {details.data?.summary?.member_since && (
                  <span style={{ fontSize: 12, color: K.muted }}>since {new Date(details.data.summary.member_since).toLocaleDateString()}</span>
                )}
              </span>
            }
          />
          <div style={{ marginBottom: 20 }}>
            <Tabs tabs={TABS} active={tab} onChange={setTab} />
          </div>

          {details.isLoading ? (
            <LoadingScreen message="Loading tenant…" />
          ) : details.isError ? (
            <ErrorState message={(details.error as Error)?.message || "Failed to load tenant."} onRetry={() => details.refetch()} />
          ) : !details.data ? null : (
            <>
              {tab === "overview" && <OverviewTab d={details.data} billing={billing.data} />}
              {tab === "brand" && <BrandTab b={details.data.brand_rules} />}
              {tab === "tours" && <ToursTab tours={details.data.rewritten_tours} />}
              {tab === "social" && <Deferred what="Per-tenant slate and pieces detail (Content Trace)" />}
              {tab === "channels" && <Deferred what="Admin view of WordPress connection + API keys" />}
              {tab === "billing" && <BillingTab b={billing.data} />}
              {tab === "quality" && <QualityTab tenantId={id} />}
              {tab === "audit" && <Deferred what="Per-tenant audit timeline" />}
              {tab === "settings" && <SettingsTab row={tenantRow.data ?? undefined} />}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
