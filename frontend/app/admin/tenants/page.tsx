"use client";
// app/admin/tenants/page.tsx — AA-159: lifecycle stats + country + count fix
// AA-473: Gate A removed entirely (ADR-2026-038 §0.2) — a new tenant is is_active=true
// immediately, no onboarding approval step left. The "Onboarding" tab and its pending-tenant
// UI (added AA-389, trimmed AA-472) are gone.
// AA-797: three states (active / suspended / offboarded). Suspend <-> active is the status
// toggle (PATCH body); Offboard (reason required) revokes the key and replaces the old unaudited
// Delete; Restore (tech lead decision, 14/10/2026) brings an offboarded tenant back with its data and settings
// and a new key.
// AA-766 (S236): Tenant 360 — Settings editors (plan, RPM, markets, channels, posts/week), and new
// Cost, Quality (per T10 gate + edit rate) and Markets tabs, in ./_components (admin-only data).
// AA-994: text in the UI language (messages.ts, _components/tenant360.messages.ts); values from the API stay as sent.

import { Suspense, useState, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import {
  Users, Plus, Key, RefreshCw, ChevronDown, ChevronUp,
  AlertCircle, Loader2, CheckCircle, Eye, EyeOff, Copy, X, Ban, Globe, RotateCcw,
} from "lucide-react";
import { A, serif, mono, sans, Card, SLabel, Badge, LoadingScreen, TH, TD } from "../_components/adminUi";
import { Button, ScrollX, PageFrame, Modal, PageHeader, useFormat, useIsPhone, useLang, useT } from "../../_kit";
import { Tenant360Settings } from "./_components/Tenant360Settings";
import { Tenant360Cost } from "./_components/Tenant360Cost";
import { Tenant360Quality } from "./_components/Tenant360Quality";
import { Tenant360Markets } from "./_components/Tenant360Markets";
import { Tenant360VoiceMemory } from "./_components/Tenant360VoiceMemory";
import { fmtDailyPublishLimit } from "./_components/tenant360";
import { T360_WORDS_MESSAGES } from "./_components/tenant360.messages";
import { SCORE_BANDS } from "@/lib/quality-registry.generated";
import { TEN } from "./messages";

// ─── Types ────────────────────────────────────────────────────────────────────

interface Lifecycle {
  source_active: number;
  source_superseded: number;
  source_trashed: number;
  master_active: number;
  master_inactive: number;
  master_trashed: number;
}

interface Tenant {
  tenant_id: string;
  name: string;
  slug: string;
  plan_tier: string;
  posts_per_week: number;
  country: string | null;
  rate_limit_rpm: number;
  daily_publish_limit?: number | null; // AA-819 — per-tenant override; null = code default, absent = backend not deployed
  is_active: boolean;
  status: TenantStatus;           // AA-797 — derived by GET /admin/tenants
  cancelled_at: string | null;
  created_at: string;
  plan_attached?: boolean;        // AA-798 — false: no plan, plan fields are null
  plan: { tours_quota_monthly: number | null; api_calls_quota_monthly: number | null; price_usd_monthly: number | null };
  this_month: {
    tours_rewritten: number; api_calls_used: number;
    quota_tours_pct: number; quota_calls_pct: number;
    tours_overage: number; overage_usd: number; llm_cost_usd: number;
  };
  lifecycle: Lifecycle;
}

type TenantStatus = "active" | "suspended" | "offboarded";
type StatusFilter = "all" | TenantStatus;
const TENANT_STATUS_STYLE: Record<
  TenantStatus, { label: "statusActive" | "statusSuspended" | "statusOffboarded"; bg: string; color: string }
> = {
  active:     { label: "statusActive",     bg: A.greenSoft, color: A.green },
  suspended:  { label: "statusSuspended",  bg: A.amberSoft, color: A.amber },
  offboarded: { label: "statusOffboarded", bg: A.redSoft,   color: A.red },
};

interface NewApiKey {
  tenant_id: string; tenant_name: string; api_key: string; rotated?: boolean;
  title?: string; note?: string;  // AA-797 — restore reuses this modal
}

// AA-629 Tier 2 — GET /admin/unmapped-market-requests
interface UnmappedMarketRequest {
  country_code: string; tenant_count: number;
  first_seen_at: string | null; last_requested_at: string | null; tenant_ids: string[];
}

const PLAN_OPTIONS = ["starter", "growth", "business"];
const PLAN_BADGE: Record<string, "blue" | "purple" | "green" | "red" | "gold"> = {
  starter: "blue", growth: "purple", business: "green", internal: "gold",
};

// ─── Lifecycle Bar ────────────────────────────────────────────────────────────

function LifecycleBar({ lc }: { lc: Lifecycle }) {
  const t = useT(TEN);
  const sourceTotal = lc.source_active + lc.source_superseded + lc.source_trashed;
  const masterTotal = lc.master_active + lc.master_inactive + lc.master_trashed;

  function StatPill({ n, label, color }: { n: number; label: string; color: string }) {
    return (
      <span title={label} style={{
        display: "inline-flex", alignItems: "center", gap: 3,
        fontSize: 11, fontWeight: n > 0 ? 600 : 400,
        color: n > 0 ? color : A.muted2,
      }}>
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: n > 0 ? color : A.line, flexShrink: 0 }} />
        {n}
      </span>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {/* Source row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: A.muted2, width: 42, flexShrink: 0 }}>{t("source")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.source_active}     label={t("active")}     color={A.green} />
          <StatPill n={lc.source_superseded} label={t("superseded")} color={A.gold} />
          <StatPill n={lc.source_trashed}    label={t("trashed")}    color={A.red} />
        </div>
        {sourceTotal > 0 && (
          <span style={{ fontSize: 10, color: A.muted2, marginLeft: "auto" }}>{t("total", sourceTotal)}</span>
        )}
      </div>
      {/* Master row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: A.muted2, width: 42, flexShrink: 0 }}>{t("master")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.master_active}   label={t("active")}   color={A.green} />
          <StatPill n={lc.master_inactive} label={t("inactive")} color={A.muted} />
          <StatPill n={lc.master_trashed}  label={t("trashed")}  color={A.red} />
        </div>
        {masterTotal > 0 && (
          <span style={{ fontSize: 10, color: A.muted2, marginLeft: "auto" }}>{t("total", masterTotal)}</span>
        )}
      </div>
    </div>
  );
}

// ─── Create Tenant Modal ──────────────────────────────────────────────────────

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (k: NewApiKey) => void }) {
  const t = useT(TEN);
  const [name, setName]   = useState("");
  const [slug, setSlug]   = useState("");
  const [plan, setPlan]   = useState("starter");
  // AA-384: posts_per_week is a free tenant choice now, not implied by plan — caller states its
  // own posting cadence at creation (1-14, no default that matches a tier).
  const [postsPerWeek, setPostsPerWeek] = useState("3");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const autoSlug = (n: string) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

  async function submit() {
    if (!name.trim() || !slug.trim()) { setError(t("nameSlugRequired")); return; }
    const ppw = Number(postsPerWeek);
    if (!Number.isInteger(ppw) || ppw < 1 || ppw > 14) {
      setError(t("ppwRange")); return;
    }
    setLoading(true); setError("");
    try {
      const res = await fetch("/api/admin/tenants", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), slug: slug.trim(), plan_tier: plan, posts_per_week: ppw }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail ?? t("createFailed")); return; }
      onCreated({ tenant_id: data.tenant_id, tenant_name: data.name, api_key: data.api_key });
    } catch { setError(t("connectionError")); } finally { setLoading(false); }
  }

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100 }}>
      <div style={{ background: A.card, border: `1px solid ${A.line}`, borderRadius: 16, padding: 32, width: 440 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: A.ink, letterSpacing: "-0.01em" }}>{t("newTenant")}</div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: A.muted }}><X size={16} /></button>
        </div>
        {[
          { key: "name", label: t("companyName"), value: name, onChange: (v: string) => { setName(v); setSlug(autoSlug(v)); }, placeholder: "WanderLux Travel" },
          { key: "slug", label: t("slug"),        value: slug, onChange: setSlug, placeholder: "wanderlux-travel" },
        ].map(f => (
          <div key={f.key} style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 6 }}>{f.label}</label>
            <input value={f.value} onChange={e => f.onChange(e.target.value)} placeholder={f.placeholder}
              style={{ width: "100%", padding: "10px 12px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, color: A.body, fontSize: 13, outline: "none", boxSizing: "border-box", fontFamily: sans }} />
          </div>
        ))}
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>{t("plan")}</label>
          <div style={{ display: "flex", gap: 8 }}>
            {PLAN_OPTIONS.map(p => (
              <button key={p} onClick={() => setPlan(p)} style={{
                flex: 1, padding: "9px 0", borderRadius: 8, cursor: "pointer", fontFamily: sans,
                border: `1px solid ${plan === p ? A.accent : A.line}`,
                background: plan === p ? A.accentTint : A.bg,
                color: plan === p ? A.accentDeep : A.muted,
                fontSize: 13, fontWeight: plan === p ? 700 : 400,
              }}>{p}</button>
            ))}
          </div>
        </div>
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>
            {t("ppwLabel")}
          </label>
          <input type="number" min={1} max={14} value={postsPerWeek} onChange={e => setPostsPerWeek(e.target.value)}
            style={{ width: 100, padding: "10px 12px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, color: A.body, fontSize: 13, outline: "none", fontFamily: sans }} />
          <p style={{ fontSize: 11, color: A.muted2, margin: "6px 0 0" }}>
            {t("ppwHelp")}
          </p>
        </div>
        {error && (
          <div style={{ marginBottom: 14, padding: "9px 12px", background: A.redSoft, border: `1px solid ${A.redBorder}`, borderRadius: 8, fontSize: 12, color: A.red }}>
            {error}
          </div>
        )}
        <div style={{ display: "flex", gap: 10 }}>
          <Button variant="secondary" onClick={onClose} style={{ flex: 1 }}>{t("cancel")}</Button>
          <Button variant="primary" disabled={loading} onClick={submit} style={{ flex: 2 }}>
            {loading ? <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> {t("creating")}</> : t("createTenant")}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ─── API Key Modal ────────────────────────────────────────────────────────────

function ApiKeyModal({ keyData, onClose }: { keyData: NewApiKey; onClose: () => void }) {
  const t = useT(TEN);
  const [show, setShow]     = useState(false);
  const [copied, setCopied] = useState(false);
  const copy = () => { navigator.clipboard.writeText(keyData.api_key); setCopied(true); setTimeout(() => setCopied(false), 2000); };
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 200 }}>
      <div style={{ background: A.card, border: "1px solid var(--aa-green-border)", borderRadius: 16, padding: 32, width: 460 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <CheckCircle size={20} color={A.green} />
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: A.green }}>{keyData.title ?? (keyData.rotated ? t("newApiKey") : t("tenantCreated"))}</div>
        </div>
        {keyData.note && <p style={{ fontSize: 13, color: A.muted, margin: "0 0 12px" }}>{keyData.note}</p>}
        <p style={{ fontSize: 13, color: A.muted, marginBottom: 24 }}>
          <strong style={{ color: A.ink }}>{keyData.tenant_name}</strong> {t("shareOnceA")} <strong style={{ color: A.red }}>{t("neverShownAgain")}</strong>.
        </p>
        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>{t("apiKey")}</label>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{
              flex: 1, fontFamily: mono, fontSize: 12, padding: "10px 14px",
              background: A.bg, border: "1px solid var(--aa-green-border)", borderRadius: 8,
              color: show ? A.green : A.muted, letterSpacing: show ? 0.3 : 3, wordBreak: "break-all",
            }}>
              {show ? keyData.api_key : "•".repeat(32)}
            </div>
            <button onClick={() => setShow(!show)} style={{ padding: "0 12px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, cursor: "pointer", color: A.muted }}>
              {show ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button onClick={copy} style={{ flex: 1, padding: "10px 0", background: copied ? A.greenSoft : "var(--aa-green-bg2)", border: "1px solid var(--aa-green-border)", borderRadius: 8, color: A.green, fontSize: 13, fontWeight: 600, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
            <Copy size={13} />{copied ? t("copied") : t("copyKey")}
          </button>
          <Button variant="secondary" onClick={onClose} style={{ flex: 1 }}>{t("done")}</Button>
        </div>
      </div>
    </div>
  );
}

// ─── Detail types (unchanged from previous) ───────────────────────────────────

interface RewrittenTour {
  version_id: string; tour_name: string; country: string | null;
  quality_score: number | null; version_number: number;
  status: string; created_at: string;
}
interface PipelineRun {
  run_id: string; started_at: string; tours_processed: number;
  tours_passed: number; llm_model: string | null;
  llm_cost_usd: number; status: string;
}
interface TenantDetails {
  summary: {
    total_rewrites: number; total_llm_cost_usd: number;
    api_calls_this_month: number; quota_pct: number;
    plan_name: string; member_since: string;
    tours_view?: string; pipeline_note?: string | null;
  };
  rewritten_tours: RewrittenTour[];
  pipeline_runs: PipelineRun[];
  api_usage: { total_calls: number; quota_used: number; quota_total: number; rate_limit_per_min: number };
  brand_rules: {
    system_prompt: string | null; style_guide: string | null; forbidden_words: string[];
    version_count: number; last_updated: string | null;
    // AA-557 J.24 — full field set, same table Tenant Portal's own Brand Identity page reads/writes.
    brand_name: string; brand_type: string; core_idea: string;
    customer_segment: string; customer_mindset: string;
    voice_examples: string[]; good_examples: string; target_markets: string[];
    default_cta?: string;
  };
}

const SCORE_COLOR = (s: number | null) =>
  s == null ? A.muted2 : s >= SCORE_BANDS.top ? A.green : s >= SCORE_BANDS.pass ? A.gold : A.red;

const STATUS_STYLE: Record<string, { bg: string; col: string }> = {
  approved:     { bg: A.greenSoft, col: "var(--aa-green-strong)" },
  ai_generated: { bg: "var(--aa-blue-bg)", col: "var(--aa-blue-deep)" },
  rejected:     { bg: A.redSoft, col: A.red },
  needs_review: { bg: "var(--aa-amber-bg)", col: "var(--aa-amber-orange2)" },
  pending:      { bg: "var(--aa-amber-bg2)", col: "var(--aa-amber-strong)" },
  completed:    { bg: A.greenSoft, col: "var(--aa-green-strong)" },
  failed:       { bg: A.redSoft, col: A.red },
  ingesting:    { bg: "var(--aa-blue-bg)", col: "var(--aa-blue-deep)" },
};
function StatusChip({ status }: { status: string }) {
  const s = STATUS_STYLE[status] ?? { bg: A.line2, col: A.muted2 };
  return <span style={{ fontSize: 10.5, padding: "2px 8px", borderRadius: 20, fontWeight: 600, background: s.bg, color: s.col, whiteSpace: "nowrap" }}>{status}</span>;
}
function EmptyRow({ cols, msg }: { cols: number; msg: string }) {
  return <tr><td colSpan={cols} style={{ padding: "20px 0", textAlign: "center", fontSize: 12, color: A.muted }}>{msg}</td></tr>;
}
function ToursTabContent({ tours, toursView }: { tours: RewrittenTour[]; toursView?: string }) {
  const tr = useT(TEN);
  const { formatDate: fmtD } = useFormat();
  const isPublished = toursView === "published";
  const headers = isPublished
    ? [tr("colName"), tr("colCountry"), tr("colScore"), tr("colStatus"), tr("colPublishedAt")]
    : [tr("colName"), tr("colCountry"), tr("colScore"), tr("colStatus"), tr("colVersion"), tr("colDate")];
  return (
    <>
      {isPublished && (
        <div style={{ fontSize: 11, color: A.muted, marginBottom: 10, fontStyle: "italic" }}>
          {tr("internalCatalog")}
        </div>
      )}
      <ScrollX>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>{headers.map((h, i) => (
          <th key={h} style={{ ...TH, fontSize: 10, textAlign: i >= 2 ? "right" : "left" }}>{h}</th>
        ))}</tr></thead>
        <tbody>
          {tours.length === 0
            ? <EmptyRow cols={headers.length} msg={isPublished ? tr("noPublished") : tr("noRewrites")} />
            : tours.map(t => (
              <tr key={t.version_id}>
                <td style={{ ...TD, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.tour_name}</td>
                <td style={{ ...TD, color: A.muted }}>{t.country ?? "—"}</td>
                <td style={{ ...TD, textAlign: "right", fontWeight: 700, color: SCORE_COLOR(t.quality_score) }}>
                  {t.quality_score != null ? t.quality_score.toFixed(1) : "—"}
                </td>
                <td style={{ ...TD, textAlign: "right" }}><StatusChip status={t.status} /></td>
                {!isPublished && (
                  <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted2 }}>
                    {t.version_number != null ? `v${t.version_number}` : "—"}
                  </td>
                )}
                <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted2 }}>{fmtD(t.created_at)}</td>
              </tr>
            ))
          }
        </tbody>
      </table>
      </ScrollX>
    </>
  );
}
function PipelineTabContent({ runs, pipelineNote }: { runs: PipelineRun[]; pipelineNote?: string | null }) {
  const t = useT(TEN);
  const { formatDateTime: fmtDT } = useFormat();
  return (
    <>
      {pipelineNote && (
        <div style={{ fontSize: 11, color: A.muted, marginBottom: 10, fontStyle: "italic" }}>{pipelineNote}</div>
      )}
      <ScrollX>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>{[t("colStarted"), t("colTours"), t("colPassed"), t("colModel"), t("colCost"), t("colStatus")].map((h, i) => (
          <th key={h} style={{ ...TH, fontSize: 10, textAlign: i >= 1 ? "right" : "left" }}>{h}</th>
        ))}</tr></thead>
        <tbody>
          {runs.length === 0 ? <EmptyRow cols={6} msg={t("noRuns")} /> : runs.map(r => (
            <tr key={r.run_id}>
              <td style={{ ...TD, fontFamily: mono, fontSize: 11, color: A.muted }}>{fmtDT(r.started_at)}</td>
              <td style={{ ...TD, textAlign: "right" }}>{r.tours_processed}</td>
              <td style={{ ...TD, textAlign: "right", color: A.green }}>{r.tours_passed}</td>
              <td style={{ ...TD, textAlign: "right", fontFamily: mono, fontSize: 10, color: A.muted2 }}>
                {r.llm_model ? r.llm_model.replace(/us\.anthropic\./, "").replace(/-v1:0$/, "") : "—"}
              </td>
              <td style={{ ...TD, textAlign: "right", color: A.gold }}>${r.llm_cost_usd.toFixed(4)}</td>
              <td style={{ ...TD, textAlign: "right" }}><StatusChip status={r.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      </ScrollX>
    </>
  );
}
function ApiTabContent({ usage }: { usage: TenantDetails["api_usage"] }) {
  const t = useT(TEN);
  const pct      = usage.quota_total > 0 ? Math.min(100, Math.round((usage.quota_used / usage.quota_total) * 100)) : 0;
  const barColor = pct > 90 ? A.red : pct > 70 ? A.amber : A.green;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
          <span style={{ color: A.muted }}>{t("apiQuota")}</span>
          <span style={{ fontWeight: 700, color: barColor }}>{usage.quota_used.toLocaleString()} / {usage.quota_total.toLocaleString()} ({pct}%)</span>
        </div>
        <div style={{ height: 8, background: A.line2, borderRadius: 4, overflow: "hidden" }}>
          <div style={{ height: "100%", width: `${pct}%`, background: barColor, borderRadius: 4, transition: "width .4s" }} />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))", gap: 10 }}>
        {([[t("totalCalls"), usage.total_calls.toLocaleString()], [t("rateLimit"), t("perMin", usage.rate_limit_per_min)], [t("quotaUsed"), `${pct}%`]] as [string, string][]).map(([l, v]) => (
          <div key={l} style={{ padding: "10px 14px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8 }}>
            <div style={{ fontSize: 10, color: A.muted2, marginBottom: 3 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>{v}</div>
          </div>
        ))}
      </div>
    </div>
  );
}


// AA-557 J.24 — was read-only, now editable: Admin can view AND overwrite the exact same
// shared.tenant_brand_rules row Tenant Portal's own BrandTab.tsx (J.23) reads/writes, via a new
// admin-scoped write path (PUT /admin/tenants/{id}/brand-identity — see api/routers/admin.py's
// own comment on why the tenant-facing endpoint couldn't just be reused for this).
const brandFieldStyle: React.CSSProperties = {
  width: "100%", padding: "9px 12px", background: A.card, border: `1px solid ${A.line}`,
  borderRadius: 8, color: A.body, fontSize: 12.5, fontFamily: sans, outline: "none", boxSizing: "border-box",
};
function BrandField({ label, value, onChange, rows }: {
  label: string; value: string; onChange: (v: string) => void; rows?: number;
}) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{label}</div>
      {rows ? (
        <textarea value={value} onChange={e => onChange(e.target.value)} rows={rows} style={{ ...brandFieldStyle, resize: "vertical", lineHeight: 1.5 }} />
      ) : (
        <input value={value} onChange={e => onChange(e.target.value)} style={brandFieldStyle} />
      )}
    </div>
  );
}

function BrandTabContent({ rules, tenantId }: { rules: TenantDetails["brand_rules"]; tenantId: string }) {
  const tr = useT(TEN);
  const { formatDate: fmtD } = useFormat();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [sp, setSp] = useState(rules.system_prompt ?? "");
  const [sg, setSg] = useState(rules.style_guide ?? "");
  const [fw, setFw] = useState(rules.forbidden_words.join(", "));
  const [brandName, setBrandName] = useState(rules.brand_name);
  const [brandType, setBrandType] = useState(rules.brand_type);
  const [coreIdea, setCoreIdea] = useState(rules.core_idea);
  const [targetMarkets, setTargetMarkets] = useState(rules.target_markets.join(", "));
  const [customerSegment, setCustomerSegment] = useState(rules.customer_segment);
  const [customerMindset, setCustomerMindset] = useState(rules.customer_mindset);
  const [toneOfVoice, setToneOfVoice] = useState(rules.voice_examples.join(", "));
  const [goodExamples, setGoodExamples] = useState(rules.good_examples);
  const [defaultCta, setDefaultCta] = useState(rules.default_cta ?? "");

  const hasAnyData = !!(rules.system_prompt || rules.style_guide || rules.forbidden_words.length > 0
    || rules.brand_name || rules.core_idea);

  async function save() {
    setSaving(true); setError(""); setSaved(false);
    try {
      const res = await fetch(`/api/admin/tenants/${tenantId}/brand-identity`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_prompt: sp, style_guide: sg,
          forbidden_words: fw.split(",").map(w => w.trim()).filter(Boolean),
          brand_name: brandName, brand_type: brandType, core_idea: coreIdea,
          target_markets: targetMarkets.split(",").map(w => w.trim()).filter(Boolean),
          customer_segment: customerSegment, customer_mindset: customerMindset,
          tone_of_voice: toneOfVoice.split(",").map(w => w.trim()).filter(Boolean),
          good_examples: goodExamples, default_cta: defaultCta,
        }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); setError(d.detail ?? tr("saveFailed")); return; }
      setSaved(true); setEditing(false);
      setTimeout(() => setSaved(false), 2500);
    } catch { setError(tr("connectionError")); } finally { setSaving(false); }
  }

  if (!editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", gap: 16, fontSize: 11, color: A.muted2 }}>
            <span>{tr("versions", rules.version_count)}</span>
            {rules.last_updated && <span>{tr("lastUpdated", fmtD(rules.last_updated))}</span>}
          </div>
          <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>{tr("edit")}</Button>
        </div>
        {saved && <span style={{ fontSize: 12, color: A.green }}>{tr("saved")}</span>}
        {!hasAnyData ? (
          <div style={{ padding: "20px 0", textAlign: "center", fontSize: 12, color: A.muted }}>{tr("noBrand")}</div>
        ) : (
          <>
            {rules.brand_name && <div><span style={{ fontSize: 10, fontWeight: 700, color: A.muted }}>{tr("brandNameInline")} </span><span style={{ fontSize: 13, color: A.ink, fontWeight: 600 }}>{rules.brand_name}</span></div>}
            {rules.brand_type && <div><span style={{ fontSize: 10, fontWeight: 700, color: A.muted }}>{tr("brandTypeInline")} </span><span style={{ fontSize: 12.5, color: A.body }}>{rules.brand_type}</span></div>}
            {rules.core_idea && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("coreIdea")}</div>
                <div style={{ fontSize: 12.5, color: A.body }}>{rules.core_idea}</div>
              </div>
            )}
            {rules.target_markets.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("targetMarkets")}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.target_markets.map((m, i) => <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: A.line2, color: A.body, borderRadius: 20 }}>{m}</span>)}
                </div>
              </div>
            )}
            {rules.customer_segment && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("customerSegment")}</div>
                <div style={{ fontSize: 12.5, color: A.body }}>{rules.customer_segment}</div>
              </div>
            )}
            {rules.customer_mindset && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("customerMindset")}</div>
                <div style={{ fontSize: 12.5, color: A.body }}>{rules.customer_mindset}</div>
              </div>
            )}
            {rules.voice_examples.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("toneOfVoice")}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.voice_examples.map((t, i) => <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: A.goldTint, color: A.gold, borderRadius: 20 }}>{t}</span>)}
                </div>
              </div>
            )}
            {rules.style_guide && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("writingStyle")}</div>
                <div style={{ padding: "10px 12px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, fontSize: 12, color: A.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{rules.style_guide}</div>
              </div>
            )}
            {rules.good_examples && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("goodExamples")}</div>
                <div style={{ padding: "10px 12px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, fontSize: 12, color: A.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{rules.good_examples}</div>
              </div>
            )}
            {rules.system_prompt && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("systemPrompt")}</div>
                <div style={{ padding: "10px 12px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, fontSize: 12, color: A.body, fontFamily: mono, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                  {rules.system_prompt.slice(0, 400)}{rules.system_prompt.length > 400 ? "…" : ""}
                </div>
              </div>
            )}
            {rules.forbidden_words.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>{tr("forbiddenWords", rules.forbidden_words.length)}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.forbidden_words.map((w, i) => (
                    <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: A.redSoft, color: A.red, borderRadius: 20, fontWeight: 600 }}>{w}</span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 560 }}>
      <BrandField label={tr("brandName")} value={brandName} onChange={setBrandName} />
      <BrandField label={tr("brandType")} value={brandType} onChange={setBrandType} />
      <BrandField label={tr("coreIdea")} value={coreIdea} onChange={setCoreIdea} rows={2} />
      <BrandField label={tr("targetMarketsCsv")} value={targetMarkets} onChange={setTargetMarkets} />
      <BrandField label={tr("customerSegment")} value={customerSegment} onChange={setCustomerSegment} rows={2} />
      <BrandField label={tr("customerMindset")} value={customerMindset} onChange={setCustomerMindset} rows={2} />
      <BrandField label={tr("toneCsv")} value={toneOfVoice} onChange={setToneOfVoice} />
      <BrandField label={tr("writingStyle")} value={sg} onChange={setSg} rows={4} />
      <BrandField label={tr("goodExamples")} value={goodExamples} onChange={setGoodExamples} rows={3} />
      <BrandField label={tr("defaultCta")} value={defaultCta} onChange={setDefaultCta} />
      <BrandField label={tr("systemPrompt")} value={sp} onChange={setSp} rows={5} />
      <BrandField label={tr("forbiddenCsv")} value={fw} onChange={setFw} />
      {error && (
        <div style={{ padding: "9px 12px", background: A.redSoft, border: `1px solid ${A.redBorder}`, borderRadius: 8, fontSize: 12, color: A.red }}>{error}</div>
      )}
      <div style={{ display: "flex", gap: 10 }}>
        <Button variant="primary" size="sm" disabled={saving} onClick={save}>
          {saving ? <><Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> {tr("saving")}</> : tr("save")}
        </Button>
        <Button variant="secondary" size="sm" onClick={() => setEditing(false)}>{tr("cancel")}</Button>
      </div>
    </div>
  );
}

// ─── Activity tab ─────────────────────────────────────────────────────────────

interface RewriteActivityItem {
  version_id: string; tour_name: string; country: string | null;
  version_number: number; status: string; quality_score: number | null;
  edit_source: string | null; created_at: string;
}
function ActivityTabContent({ items }: { items: RewriteActivityItem[] }) {
  const t = useT(TEN);
  const { formatDate: fmtD } = useFormat();
  return (
    <div>
      <div style={{ fontSize: 11, color: A.muted2, marginBottom: 10 }}>{t("activityIntro")}</div>
      <ScrollX>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>{[t("colTourName"), t("colCountry"), t("colVersion"), t("colStatus"), t("colScore"), t("colDate")].map((h, i) => (
          <th key={h} style={{ ...TH, fontSize: 10, textAlign: i >= 2 ? "right" : "left" }}>{h}</th>
        ))}</tr></thead>
        <tbody>
          {items.length === 0
            ? <EmptyRow cols={6} msg={t("noActivity")} />
            : items.map(r => (
              <tr key={r.version_id}>
                <td style={{ ...TD, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.tour_name}</td>
                <td style={{ ...TD, color: A.muted }}>{r.country ?? "—"}</td>
                <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted2 }}>v{r.version_number}</td>
                <td style={{ ...TD, textAlign: "right" }}><StatusChip status={r.status} /></td>
                <td style={{ ...TD, textAlign: "right", fontWeight: 700, color: SCORE_COLOR(r.quality_score) }}>
                  {r.quality_score != null ? r.quality_score.toFixed(1) : "—"}
                </td>
                <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted2 }}>{fmtD(r.created_at)}</td>
              </tr>
            ))
          }
        </tbody>
      </table>
      </ScrollX>
    </div>
  );
}

// ─── Social Content tab (AA-557 I.22) ──────────────────────────────────────────
// Skeleton only, per the issue's own scope: the full "detailed per-tenant Atom/Segment/Route/
// Slate usage" breakdown is blocked on AA-558 (INVESTIGATE — the 06/07/08 merge decision), so
// this tab intentionally shows only 2 real, already-cross-tenant-filterable counts (content-log/
// publish-log both already accept a real `tenant_id` query param, admin_a4.py — no new backend
// endpoint needed for the skeleton) rather than fabricating the detailed view ahead of that
// decision.
interface SocialContentSummary { pieces_written: number; pieces_published: number; }

function SocialContentTabContent({ tenantId }: { tenantId: string }) {
  const t = useT(TEN);
  const [data, setData] = useState<SocialContentSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true); setError("");
    Promise.all([
      fetch(`/api/admin/a4/content-log?tenant_id=${tenantId}&limit=500`).then(r => r.ok ? r.json() : Promise.reject()),
      fetch(`/api/admin/a4/publish-log?tenant_id=${tenantId}&limit=500`).then(r => r.ok ? r.json() : Promise.reject()),
    ])
      .then(([content, publish]) => setData({
        pieces_written: content.total ?? 0,
        pieces_published: publish.total ?? 0,
      }))
      .catch(() => setError(t("socialLoadError")))
      .finally(() => setLoading(false));
  }, [tenantId, t]);

  if (loading) return <div style={{ padding: "12px 0", fontSize: 12, color: A.muted }}>{t("loading")}</div>;
  if (error || !data) return <div style={{ padding: "12px 0", fontSize: 12, color: A.red }}>{error || t("loadFailed")}</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", gap: 14 }}>
        {([[t("piecesWritten"), data.pieces_written], [t("published"), data.pieces_published]] as [string, number][]).map(([l, v]) => (
          <div key={l} style={{ padding: "10px 14px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, minWidth: 140 }}>
            <div style={{ fontSize: 10, color: A.muted2, marginBottom: 3 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>{v}</div>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: A.muted2, fontStyle: "italic" }}>
        {t("socialNoteA")}{" "}
        <a href="/admin/tenant-activity" style={{ color: A.gold }}>{t("contentTrace")}</a> {t("socialNoteB")}
      </div>
    </div>
  );
}

// ─── Tenant Detail Panel ──────────────────────────────────────────────────────

// AA-666: Tenant 360 — the detail panel is expanded IN PLACE (inline, under the list row), not a
// separate page. Added tabs: Usage & Billing, Quality (gate telemetry), Audit, Settings.
// AA-766: + Cost and Markets; Quality and Settings rebuilt (./_components).
type DTab = "tours" | "pipeline" | "activity" | "api" | "brand" | "social_content"
          | "billing" | "cost" | "quality" | "markets" | "audit" | "settings";

const DETAIL_TABS: readonly DTab[] = ["tours", "pipeline", "activity", "api", "brand", "social_content", "billing",
  "cost", "quality", "markets", "audit", "settings"];

/** The tab a deep link asks for, only for the tenant it names; "tours" otherwise. */
function initialDetailTab(linkTenant: string | null, linkTab: string | null, tenantId: string): DTab {
  return linkTenant === tenantId && (DETAIL_TABS as readonly string[]).includes(linkTab ?? "")
    ? (linkTab as DTab) : "tours";
}

function TenantDetail({ tenant, onChanged }: {
  tenant: Tenant; onChanged: () => void;
}) {
  const tr = useT(TEN);
  const dplWords = T360_WORDS_MESSAGES[useLang()];
  const tenantId = tenant.tenant_id;
  const isInternal = tenant.plan_tier === "internal";
  const [data, setData]         = useState<TenantDetails | null>(null);
  const [loading, setLoading]   = useState(true);
  // C4 (S262): `?tenant=<id>&tab=cost` (AI providers › Budgets "Open the tenant") opens this tenant on that tab.
  const deepTab = useSearchParams();
  const [tab, setTab]           = useState<DTab>(() => initialDetailTab(deepTab.get("tenant"), deepTab.get("tab"), tenantId));
  const [activity, setActivity] = useState<RewriteActivityItem[]>([]);

  useEffect(() => {
    setLoading(true);
    const p1 = fetch(`/api/admin/tenants/${tenantId}/details`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then((d: TenantDetails) => {
        const fw = d.brand_rules?.forbidden_words;
        if (fw && typeof fw === "string") {
          try { d.brand_rules.forbidden_words = JSON.parse(fw as unknown as string); }
          catch { d.brand_rules.forbidden_words = []; }
        }
        setData(d);
      })
      .catch(() => setData(null));

    const p2 = !isInternal
      ? fetch(`/api/admin/tenants/${tenantId}/rewrite-activity`)
          .then(r => r.ok ? r.json() : Promise.reject())
          .then(d => setActivity(d.rewrite_activity ?? []))
          .catch(() => setActivity([]))
      : Promise.resolve();

    Promise.all([p1, p2]).finally(() => setLoading(false));
  }, [tenantId, isInternal]);

  if (loading) return <div style={{ padding: "12px 0", fontSize: 12, color: A.muted }}>{tr("loading")}</div>;
  if (!data)   return <div style={{ padding: "12px 0", fontSize: 12, color: A.red }}>{tr("detailsLoadError")}</div>;

  const s = data.summary;
  // AA-557 I.17 — "Planning" tab removed entirely (the tech lead confirmed: tenants no longer self-
  // serve Quarter-Plan, replaced by Slate since AA-511/519 — the tab had no meaning left).
  const TABS: { key: DTab; label: string }[] = [
    { key: "tours",   label: tr("tabTours", data.rewritten_tours.length) },
    ...(isInternal
      ? [{ key: "pipeline" as DTab, label: tr("tabPipeline", data.pipeline_runs.length) }]
      : [{ key: "activity" as DTab, label: tr("tabActivity", activity.length) }]
    ),
    { key: "api",   label: tr("tabApi") },
    { key: "brand", label: tr("tabBrand") },
    // AA-557 I.22 — skeleton tab only, see SocialContentTabContent's own comment for why.
    { key: "social_content", label: tr("tabSocial") },
    // AA-666 — Tenant 360: the remaining management facets, added to the inline panel.
    { key: "billing" as DTab,  label: tr("tabBilling") },
    { key: "cost" as DTab,     label: tr("tabCost") },
    { key: "quality" as DTab,  label: tr("tabQuality") },
    { key: "markets" as DTab,  label: tr("tabMarkets") },
    { key: "audit" as DTab,    label: tr("tabAudit") },
    { key: "settings" as DTab, label: tr("tabSettings") },
  ];

  return (
    <div style={{ background: A.bg, borderRadius: 10, padding: 16, border: `1px solid ${A.line}`, marginTop: 4 }}>
      <div style={{ display: "flex", gap: 20, marginBottom: 14, flexWrap: "wrap" }}>
        {([
          [tr("totalRewrites"), String(s.total_rewrites),              A.gold],
          [tr("llmCostAllTime"), `$${s.total_llm_cost_usd.toFixed(3)}`, A.body],
          [tr("apiCallsMo"), s.api_calls_this_month.toLocaleString(), A.body],
          // AA-702: this is the API-call quota; the list row shows the tour quota — label both.
          [tr("apiQuotaShort"), `${s.quota_pct}%`,                     s.quota_pct > 80 ? A.red : A.body],
          // AA-557 I.19 — real data, already fetched (`shared.tenants.rate_limit_rpm` via
          // `api_usage.rate_limit_per_min`, same value the API Usage tab below already showed) —
          // just also surfaced at header level so an admin doesn't need to open that tab for it.
          // AA-766: plan / RPM from the list row, which reloads after a Settings save.
          [tr("rateLimit"),  tr("perMin", tenant.rate_limit_rpm),   A.body],
          [tr("publishLimit"), fmtDailyPublishLimit(tenant.daily_publish_limit, dplWords), A.body], // AA-819
          [tr("plan"),       tenant.plan_tier,                      A.body],
          [tr("memberSince"), s.member_since,                       A.muted],
        ] as [string, string, string][]).map(([l, v, c]) => (
          <div key={l} style={{ minWidth: 80 }}>
            <div style={{ fontSize: 10, color: A.muted2, marginBottom: 2 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 500, color: c }}>{v}</div>
          </div>
        ))}
      </div>
      {/* S235: the tab row scrolls horizontally on phones — it overflowed a hidden container at 390px. */}
      <div style={{ display: "flex", borderBottom: `1px solid ${A.line}`, marginBottom: 14, overflowX: "auto", WebkitOverflowScrolling: "touch" }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)} style={{
            flexShrink: 0, whiteSpace: "nowrap",
            padding: "7px 14px", fontSize: 12, fontWeight: tab === t.key ? 700 : 400,
            color: tab === t.key ? A.accentDeep : A.muted,
            border: "none", borderBottom: `2px solid ${tab === t.key ? A.accent : "transparent"}`,
            background: "none", cursor: "pointer", fontFamily: sans,
          }}>{t.label}</button>
        ))}
      </div>
      {tab === "tours"    && <ToursTabContent    tours={data.rewritten_tours} toursView={data.summary.tours_view} />}
      {tab === "pipeline" && <PipelineTabContent runs={data.pipeline_runs}    pipelineNote={data.summary.pipeline_note} />}
      {tab === "activity" && <ActivityTabContent items={activity} />}
      {tab === "api"      && <ApiTabContent      usage={data.api_usage} />}
      {tab === "brand"    && <BrandTabContent    rules={data.brand_rules} tenantId={tenantId} />}
      {tab === "social_content" && <SocialContentTabContent tenantId={tenantId} />}
      {tab === "billing"  && <BillingTabContent  tenantId={tenantId} />}
      {tab === "cost"     && <Tenant360Cost      tenantId={tenantId} />}
      {tab === "quality"  && <Tenant360Quality   tenantId={tenantId} byChannel={<ChannelGateTelemetry tenantId={tenantId} />} />}
      {tab === "quality"  && <Tenant360VoiceMemory tenantId={tenantId} />}
      {tab === "markets"  && <Tenant360Markets   tenantId={tenantId} />}
      {tab === "audit"    && <AuditTabContent    tenantId={tenantId} />}
      {tab === "settings" && (
        <Tenant360Settings tenantId={tenantId} planTier={tenant.plan_tier} rateLimitRpm={tenant.rate_limit_rpm}
          dailyPublishLimit={tenant.daily_publish_limit}
          status={tenant.status ?? (tenant.is_active ? "active" : "suspended")} onChanged={onChanged} />
      )}
    </div>
  );
}

// AA-666 — Usage & Billing: quota meters + overage + LLM cost, from GET /admin/billing?tenant_id=
function BillingTabContent({ tenantId }: { tenantId: string }) {
  const t = useT(TEN);
  const [b, setB] = useState<Record<string, number | string | boolean | null> | null>(null);
  const [state, setState] = useState<"loading" | "error" | "ok">("loading");
  useEffect(() => {
    fetch(`/api/admin/billing?tenant_id=${tenantId}`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(d => { setB(d); setState("ok"); })
      .catch(() => setState("error"));
  }, [tenantId]);
  if (state === "loading") return <div style={{ fontSize: 12, color: A.muted }}>{t("loadingBilling")}</div>;
  if (state === "error" || !b) return <div style={{ fontSize: 12, color: A.red }}>{t("billingLoadError")}</div>;
  const num = (v: unknown) => (v == null ? 0 : Number(v));
  // AA-798 — no plan attached: show it, never an invented price/quota (and no "∞" for a null quota).
  const noPlan = b.plan_attached === false;
  const meter = (label: string, pct: number, used: number, quota: number | null) => (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
        <span style={{ color: A.ink, fontWeight: 600 }}>{label}</span>
        <span style={{ color: A.muted }}>{quota == null ? `${used} / —` : `${used} / ${quota >= 999999 ? t("unlimited") : quota} (${pct}%)`}</span>
      </div>
      <div style={{ height: 7, background: A.line2, borderRadius: 4, overflow: "hidden" }}>
        <div style={{ width: `${Math.min(100, pct)}%`, height: "100%", background: pct >= 100 ? A.red : A.accent }} />
      </div>
    </div>
  );
  return (
    <div>
      <div style={{ display: "flex", gap: 24, marginBottom: 16, flexWrap: "wrap" }}>
        {[
          [t("plan"), noPlan ? t("noPlan") : String(b.plan_tier ?? "—")],
          [t("pricePerMo"), b.price_usd_monthly == null ? "—" : `$${num(b.price_usd_monthly).toFixed(2)}`],
          [t("llmCostMtd"), `$${num(b.llm_cost_usd).toFixed(2)}`],
          [t("overage"), b.overage_usd == null ? "—" : t("overageTours", `$${num(b.overage_usd).toFixed(2)}`, num(b.tours_overage))],
        ].map(([l, v]) => (
          <div key={l}><div style={{ fontSize: 10, color: A.muted2 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 16, color: A.body }}>{v}</div></div>
        ))}
      </div>
      {meter(t("toursQuota"), num(b.quota_tours_pct), num(b.tours_rewritten), b.tours_quota_monthly == null ? null : num(b.tours_quota_monthly))}
      {meter(t("apiCallsQuota"), num(b.quota_calls_pct), num(b.api_calls_used), b.api_calls_quota_monthly == null ? null : num(b.api_calls_quota_monthly))}
      <div style={{ fontSize: 11, color: A.muted2, marginTop: 6 }}>
        {t("billingNote")}
      </div>
    </div>
  );
}

// AA-666 — per-channel gate telemetry for this tenant (client-filtered from the platform endpoint);
// AA-766 shows it under the per-gate Quality view.
function ChannelGateTelemetry({ tenantId }: { tenantId: string }) {
  const t = useT(TEN);
  const [rows, setRows] = useState<Record<string, number | string>[] | null>(null);
  const [state, setState] = useState<"loading" | "error" | "ok">("loading");
  useEffect(() => {
    fetch("/api/admin/a4/gate-telemetry")
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(d => {
        const all = (d?.data?.by_tenant_channel ?? []) as Record<string, number | string>[];
        setRows(all.filter(r => String(r.tenant_id) === tenantId));
        setState("ok");
      })
      .catch(() => setState("error"));
  }, [tenantId]);
  if (state === "loading") return <div style={{ fontSize: 12, color: A.muted }}>{t("loadingQuality")}</div>;
  if (state === "error") return <div style={{ fontSize: 12, color: A.red }}>{t("telemetryLoadError")}</div>;
  if (!rows || rows.length === 0) return <div style={{ fontSize: 12, color: A.muted }}>{t("noGateActivity")}</div>;
  return (
    <ScrollX>
    <table style={{ width: "100%", borderCollapse: "collapse" }}>
      <thead><tr>{[t("colChannel"), t("colTotal"), t("colWarn"), t("colHeld"), t("colFailed"), t("colPublished")].map((h, i) => (
        <th key={h} style={{ ...TH, textAlign: i === 0 ? "left" : "right" }}>{h}</th>))}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td style={{ ...TD, fontWeight: 600 }}>{String(r.channel)}</td>
            <td style={{ ...TD, textAlign: "right" }}>{r.total}</td>
            <td style={{ ...TD, textAlign: "right", color: A.amber }}>{r.warn_count}</td>
            <td style={{ ...TD, textAlign: "right", color: A.amber }}>{r.held_count}</td>
            <td style={{ ...TD, textAlign: "right", color: Number(r.failed_count) > 0 ? A.red : A.muted }}>{r.failed_count}</td>
            <td style={{ ...TD, textAlign: "right", color: A.green }}>{r.publish_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </ScrollX>
  );
}

// AA-666 — Audit: this tenant's action timeline, from GET /admin/tenants/{id}/audit (new endpoint).
function AuditTabContent({ tenantId }: { tenantId: string }) {
  const t = useT(TEN);
  const { formatDateTime } = useFormat();
  const [rows, setRows] = useState<{ action: string; actor: string; created_at: string; details?: unknown }[] | null>(null);
  const [state, setState] = useState<"loading" | "error" | "ok">("loading");
  useEffect(() => {
    fetch(`/api/admin/tenants/${tenantId}/audit?limit=100`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(d => { setRows(d.events ?? []); setState("ok"); })
      .catch(() => setState("error"));
  }, [tenantId]);
  if (state === "loading") return <div style={{ fontSize: 12, color: A.muted }}>{t("loadingAudit")}</div>;
  if (state === "error") return <div style={{ fontSize: 12, color: A.red }}>{t("auditLoadError")}</div>;
  if (!rows || rows.length === 0) return <div style={{ fontSize: 12, color: A.muted }}>{t("noAudit")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {rows.map((e, i) => (
        <div key={i} style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: `1px solid ${A.line2}`, fontSize: 12 }}>
          <span style={{ color: A.muted2, fontFamily: mono, whiteSpace: "nowrap" }}>{formatDateTime(e.created_at)}</span>
          <span style={{ fontWeight: 600, color: A.ink }}>{e.action}</span>
          <span style={{ color: A.muted, marginLeft: "auto", fontFamily: mono, fontSize: 11 }}>{e.actor}</span>
        </div>
      ))}
    </div>
  );
}

// ─── Tenant Row ───────────────────────────────────────────────────────────────

async function errorDetail(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return typeof body?.detail === "string" ? body.detail : fallback;
}

// AA-797 — Offboard needs a reason (offboarding audit trail): the key is revoked and queued jobs are
// cancelled. Data and settings are kept, so Restore can bring the tenant back.
function OffboardModal({ tenant, onClose, onDone }: {
  tenant: Tenant; onClose: () => void; onDone: () => void;
}) {
  const t = useT(TEN);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit() {
    if (!reason.trim()) { setError(t("reasonRequired")); return; }
    setBusy(true); setError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}/offboard`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: reason.trim() }),
      });
      if (!res.ok) { setError(await errorDetail(res, t("offboardFailed"))); return; }
      onDone();
    } catch { setError(t("connectionError")); } finally { setBusy(false); }
  }
  return (
    <Modal open onClose={busy ? () => {} : onClose} title={t("offboardTitle", tenant.name)}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <Button variant="danger" onClick={submit} disabled={busy}>
            {busy && <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} />} {t("offboard")}
          </Button>
        </>
      }>
      <p style={{ margin: "0 0 12px", fontSize: 13, color: A.muted }}>
        {t("offboardBody")}
      </p>
      <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: A.ink, marginBottom: 6 }}>{t("reasonLabel")}</label>
      <textarea value={reason} onChange={e => setReason(e.target.value)} rows={3}
        style={{ width: "100%", boxSizing: "border-box", padding: 10, borderRadius: 8, border: `1px solid ${A.line}`,
          background: A.bg, color: A.ink, fontSize: 13, fontFamily: "inherit", resize: "vertical" }} />
      {error && (
        <div style={{ marginTop: 10, fontSize: 12, color: A.red, display: "flex", alignItems: "center", gap: 6 }}>
          <AlertCircle size={13} /> {error}
        </div>
      )}
    </Modal>
  );
}

interface RestoreResult {
  tenant_id: string; tenant_name: string; api_key: string;
  cancelled_jobs: { id: string; kind: string }[];
}

// AA-797 — Restore (tech lead decision, 14/10/2026): offboarded -> active with the same plan, brand rules,
// config and content. A new API key is issued (the old one stays revoked); jobs the offboard
// cancelled are not re-run.
function RestoreModal({ tenant, onClose, onDone }: {
  tenant: Tenant; onClose: () => void; onDone: (r: RestoreResult) => void;
}) {
  const t = useT(TEN);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit() {
    setBusy(true); setError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}/restore`, { method: "POST" });
      if (!res.ok) { setError(await errorDetail(res, t("restoreFailed"))); return; }
      onDone(await res.json());
    } catch { setError(t("connectionError")); } finally { setBusy(false); }
  }
  return (
    <Modal open onClose={busy ? () => {} : onClose} title={t("restoreTitle", tenant.name)}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" onClick={submit} disabled={busy}>
            {busy && <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} />} {t("restore")}
          </Button>
        </>
      }>
      <p style={{ margin: "0 0 8px", fontSize: 13, color: A.muted }}>
        {t("restoreBody")}
      </p>
      <ul style={{ margin: "0 0 4px", paddingLeft: 18, fontSize: 13, color: A.muted }}>
        <li>{t("restoreKeyA")} <strong style={{ color: A.ink }}>{t("restoreKeyB")}</strong> {t("restoreKeyC")}</li>
        <li>{t("restoreJobsA")} <strong style={{ color: A.ink }}>{t("restoreJobsB")}</strong>{t("restoreJobsC")}</li>
      </ul>
      {error && (
        <div style={{ marginTop: 10, fontSize: 12, color: A.red, display: "flex", alignItems: "center", gap: 6 }}>
          <AlertCircle size={13} /> {error}
        </div>
      )}
    </Modal>
  );
}

function TenantRow({ tenant, onRotateKey, onChanged, onRestored, initialExpanded = false, isPhone = false }: {
  tenant: Tenant;
  onRotateKey: (t: Tenant) => void;
  onChanged: () => void;
  onRestored: (r: RestoreResult) => void;
  initialExpanded?: boolean;
  isPhone?: boolean;
}) {
  const t = useT(TEN);
  const [expanded, setExpanded] = useState(initialExpanded);
  const [toggling, setToggling] = useState(false);
  const [rowError, setRowError] = useState("");
  const [offboarding, setOffboarding] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const status: TenantStatus = tenant.status ?? (tenant.is_active ? "active" : "suspended");
  const offboarded = status === "offboarded";
  const chip = TENANT_STATUS_STYLE[status];

  async function toggle() {
    if (offboarded) return;
    setToggling(true); setRowError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: status !== "active" }),
      });
      if (!res.ok) { setRowError(await errorDetail(res, t("statusUpdateFailed"))); return; }
      onChanged();
    } catch { setRowError(t("connectionError")); } finally { setToggling(false); }
  }

  const toggleTitle = offboarded ? t("offboardedHint")
    : status === "active" ? t("suspendHint") : t("resumeHint");
  const statusButton = (extra: React.CSSProperties = {}) => (
    <button onClick={toggle} disabled={toggling || offboarded} title={toggleTitle} style={{
      padding: "4px 12px", borderRadius: 20, border: "none", cursor: offboarded ? "default" : "pointer",
      fontSize: 11, fontWeight: 700, background: chip.bg, color: chip.color, ...extra,
    }}>
      {toggling ? "…" : t(chip.label)}
    </button>
  );
  // Offboarded rows swap Offboard for Restore (same slot).
  const offboardButton = offboarded ? (
    <button onClick={() => setRestoring(true)} title={t("restoreHint")}
      style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6,
        cursor: "pointer", color: A.green, display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
      <RotateCcw size={12} /> {t("restore")}
    </button>
  ) : (
    <button onClick={() => setOffboarding(true)} title={t("offboardHint")}
      style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6,
        cursor: "pointer", color: A.red, display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
      <Ban size={12} />{isPhone && ` ${t("offboard")}`}
    </button>
  );
  const offboardModal = (
    <>
      {offboarding && (
        <OffboardModal tenant={tenant} onClose={() => setOffboarding(false)}
          onDone={() => { setOffboarding(false); onChanged(); }} />
      )}
      {restoring && (
        <RestoreModal tenant={tenant} onClose={() => setRestoring(false)}
          onDone={r => { setRestoring(false); onRestored(r); }} />
      )}
    </>
  );
  const errorLine = rowError && (
    <div style={{ marginTop: 6, fontSize: 11.5, color: A.red, display: "flex", alignItems: "center", gap: 4 }}>
      <AlertCircle size={12} /> {rowError}
    </div>
  );

  const lc = tenant.lifecycle ?? {
    source_active: 0, source_superseded: 0, source_trashed: 0,
    master_active: 0, master_inactive: 0, master_trashed: 0,
  };

  if (isPhone) {
    // AA-752 — phone card (< 768px): the same tenant data as the table row, laid out as a card so
    // the wide table does not force page-level horizontal scroll. Rotate key / delete / expand
    // detail are all kept.
    return (
      <div style={{ borderBottom: `1px solid ${A.line2}`, padding: "14px 16px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, color: A.ink, fontSize: 14 }}>{tenant.name}</div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3, flexWrap: "wrap" }}>
              <code style={{ fontSize: 10.5, color: A.muted, fontFamily: mono }}>{tenant.slug}</code>
              {tenant.country && (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 10.5, color: A.muted2 }}>
                  <Globe size={10} />{tenant.country}
                </span>
              )}
            </div>
          </div>
          {statusButton({ flexShrink: 0 })}
        </div>
        {errorLine}

        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <Badge color={PLAN_BADGE[tenant.plan_tier] ?? "gray"}>{tenant.plan_tier}</Badge>
          <span style={{ fontSize: 11, color: A.muted2 }}>
            {tenant.posts_per_week != null ? t("postsPerWeek", tenant.posts_per_week) : "—"}
          </span>
        </div>

        <div style={{ marginTop: 10 }}><LifecycleBar lc={lc} /></div>

        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginTop: 10, fontSize: 12 }}>
          <span style={{ color: A.muted }}>
            <span style={{ fontWeight: 600, color: A.ink }}>{lc.source_active}</span> {t("sourceActive")} ·{" "}
            <span style={{ color: A.green }}>{t("calls", tenant.this_month.api_calls_used.toLocaleString())}</span>
          </span>
          <span style={{ color: A.muted }}>{t("tourQuota", tenant.this_month.quota_tours_pct)}</span>
        </div>

        <div style={{ display: "flex", gap: 6, marginTop: 12 }}>
          <button onClick={() => onRotateKey(tenant)} disabled={offboarded} title={offboarded ? t("keyRevoked") : t("rotateKey")}
            style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: offboarded ? "not-allowed" : "pointer", color: offboarded ? A.muted2 : A.muted, display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
            <Key size={12} /> {t("key")}
          </button>
          {offboardButton}
          <button onClick={() => setExpanded(!expanded)}
            style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: "pointer", color: A.muted, display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />} {t("detail")}
          </button>
        </div>
        {offboardModal}

        {expanded && (
          <div style={{ marginTop: 12 }}>
            <TenantDetail tenant={tenant} onChanged={onChanged} />
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      <tr style={{ borderBottom: `1px solid ${A.line2}` }}>
        {/* Tenant name + slug + country */}
        <td style={TD}>
          <div style={{ fontWeight: 600, color: A.ink, fontSize: 13 }}>{tenant.name}</div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3 }}>
            <code style={{ fontSize: 10.5, color: A.muted, fontFamily: mono }}>{tenant.slug}</code>
            {tenant.country && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 10.5, color: A.muted2 }}>
                <Globe size={10} />
                {tenant.country}
              </span>
            )}
          </div>
        </td>

        {/* Plan */}
        <td style={TD}>
          <Badge color={PLAN_BADGE[tenant.plan_tier] ?? "gray"}>{tenant.plan_tier}</Badge>
          <div style={{ fontSize: 10.5, color: A.muted2, marginTop: 4 }}>
            {tenant.posts_per_week != null ? t("postsPerWeek", tenant.posts_per_week) : "—"}
          </div>
        </td>

        {/* Lifecycle stats */}
        <td style={{ ...TD, minWidth: 200 }}>
          <LifecycleBar lc={lc} />
        </td>

        {/* API usage */}
        <td style={{ ...TD, textAlign: "right" }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: A.ink }}>
            {t("activeCount", lc.source_active)}
          </div>
          <div style={{ fontSize: 11, color: A.green }}>
            {t("calls", tenant.this_month.api_calls_used.toLocaleString())}
          </div>
        </td>

        {/* Quota */}
        <td style={{ ...TD, textAlign: "right", fontSize: 12, color: A.muted }}>
          {t("tourQuota", tenant.this_month.quota_tours_pct)}
        </td>

        {/* Status toggle (AA-797: active <-> suspended; offboarded is read-only) */}
        <td style={TD}>
          {statusButton()}
          {errorLine}
        </td>

        {/* Actions */}
        <td style={TD}>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <button onClick={() => onRotateKey(tenant)} disabled={offboarded} title={offboarded ? t("keyRevoked") : t("rotateKey")}
              style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: offboarded ? "not-allowed" : "pointer", color: offboarded ? A.muted2 : A.muted, display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
              <Key size={12} /> {t("key")}
            </button>
            {offboardButton}
            <button onClick={() => setExpanded(!expanded)}
              style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: "pointer", color: A.muted, display: "flex", alignItems: "center" }}>
              {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            </button>
          </div>
          {offboardModal}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7} style={{ padding: "0 16px 14px", background: A.bg }}>
            <TenantDetail tenant={tenant} onChanged={onChanged} />
          </td>
        </tr>
      )}
    </>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function TenantsPage() {
  // useSearchParams() needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <TenantsPageInner />
    </Suspense>
  );
}

function TenantsPageInner() {
  const tr = useT(TEN);
  // AA-752 — deep link from the ⌘K palette: /admin/tenants?tenant=<id> expands that tenant's
  // inline 360 panel. Read once (no setState-in-effect).
  const deepTenant = useSearchParams().get("tenant");
  const [tenants, setTenants]       = useState<Tenant[]>([]);
  const isPhone = useIsPhone();
  const [loading, setLoading]       = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newKey, setNewKey]         = useState<NewApiKey | null>(null);
  const [error, setError]           = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  // AA-629 Tier 2 — best-effort, tolerate failure so the whole page still loads without it.
  const [unmappedMarkets, setUnmappedMarkets] = useState<UnmappedMarketRequest[]>([]);

  // silent = refresh in place after a row action (keeps expanded rows; no loading screen).
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError("");
    try {
      // AA-797 — this page manages suspended/offboarded tenants too; other callers stay active-only.
      const res = await fetch("/api/admin/tenants?include_inactive=true");
      if (!res.ok) { setError(tr("loadError")); return; }
      const data = await res.json();
      setTenants(data.tenants ?? []);
    } catch { setError(tr("connectionError")); } finally { setLoading(false); }
    fetch("/api/admin/unmapped-market-requests")
      .then(r => r.ok ? r.json() : null)
      .then(d => setUnmappedMarkets(d?.requests ?? []))
      .catch(() => setUnmappedMarkets([]));
  }, [tr]);

  useEffect(() => { load(); }, [load]);

  function restored(r: RestoreResult) {
    const n = r.cancelled_jobs?.length ?? 0;
    setNewKey({
      tenant_id: r.tenant_id, tenant_name: r.tenant_name, api_key: r.api_key, rotated: true,
      title: tr("tenantRestored"),
      note: n ? tr("restoredJobs", n, r.cancelled_jobs.map(j => j.kind).join(", "))
        : tr("restoredNoJobs"),
    });
    load(true);
  }

  async function rotateKey(tenant: Tenant) {
    if (!confirm(tr("confirmRotate", tenant.name))) return;
    setError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}/generate-key`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) setNewKey({ tenant_id: tenant.tenant_id, tenant_name: tenant.name, api_key: data.api_key, rotated: true });
      else setError(typeof data?.detail === "string" ? data.detail : tr("rotateFailed"));
    } catch { setError(tr("connectionError")); }
  }

  const statusOf = (t: Tenant): TenantStatus => t.status ?? (t.is_active ? "active" : "suspended");
  const countBy = (st: TenantStatus) => tenants.filter(t => statusOf(t) === st).length;
  const totalActive     = countBy("active");
  const shown = statusFilter === "all" ? tenants : tenants.filter(t => statusOf(t) === statusFilter);
  const totalSrcActive  = tenants.reduce((s, t) => s + (t.lifecycle?.source_active ?? 0), 0);
  const totalMstrActive = tenants.reduce((s, t) => s + (t.lifecycle?.master_active ?? 0), 0);

  return (
    <>
        <PageFrame>
          <PageHeader
            title={tr("title")}
            description={tr("description", totalActive, totalSrcActive.toLocaleString(), totalMstrActive.toLocaleString())}
            actions={
              <>
                <Button variant="secondary" onClick={() => load()}><RefreshCw size={13} /> {tr("refresh")}</Button>
                <Button variant="primary" onClick={() => setShowCreate(true)}><Plus size={14} /> {tr("newTenant")}</Button>
              </>
            }
          />

          {error && (
            <div style={{ marginBottom: 16, padding: "10px 14px", background: A.redSoft, border: `1px solid ${A.redBorder}`, borderRadius: 8, fontSize: 13, color: A.red, display: "flex", alignItems: "center", gap: 8 }}>
              <AlertCircle size={14} /> {error}
            </div>
          )}

          {/* AA-629 Tier 2 — tenants waiting on a market outside DFS_LOCATION_MAP. Data-driven
              visibility for the Tier 3 decision (add the market? worth the DFS spend?) — no
              action here, just the queue. */}
          {unmappedMarkets.length > 0 && (
            <div style={{ marginBottom: 16, padding: "10px 14px", background: A.amberSoft, border: `1px solid ${A.amber}`, borderRadius: 8, fontSize: 13, color: A.ink, display: "flex", alignItems: "flex-start", gap: 8 }}>
              <Globe size={14} style={{ marginTop: 1, flexShrink: 0, color: A.amber }} />
              <span>
                {unmappedMarkets.map((u, i) => (
                  <span key={u.country_code}>
                    {i > 0 && " · "}
                    <strong>{u.tenant_count}</strong> {tr("waitingOnMarket", u.tenant_count)} <strong>{u.country_code}</strong> {tr("notSupported")}
                  </span>
                ))}
              </span>
            </div>
          )}

          {/* Summary cards */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 14, marginBottom: 20 }}>
            {[
              { label: tr("statusActive"),     value: String(totalActive),               color: A.green },
              { label: tr("statusSuspended"),  value: String(countBy("suspended")),      color: A.amber },
              { label: tr("statusOffboarded"), value: String(countBy("offboarded")),     color: A.red },
              { label: tr("masterActive"),     value: totalMstrActive.toLocaleString(),  color: A.ink },
            ].map(c => (
              <Card key={c.label}>
                <SLabel>{c.label}</SLabel>
                <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 26, fontWeight: 600, color: c.color, letterSpacing: "-0.02em" }}>{c.value}</div>
              </Card>
            ))}
          </div>

          {/* AA-797 — status filter */}
          <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
            {(["all", "active", "suspended", "offboarded"] as StatusFilter[]).map(f => {
              const on = statusFilter === f;
              const n = f === "all" ? tenants.length : countBy(f);
              return (
                <button key={f} onClick={() => setStatusFilter(f)} aria-pressed={on}
                  style={{ padding: "5px 12px", borderRadius: 20, fontSize: 12, fontWeight: 600, cursor: "pointer",
                    border: `1px solid ${on ? A.accent : A.line}`, background: on ? A.accent : A.bg, color: on ? "#fff" : A.muted }}>
                  {f === "all" ? tr("all") : tr(TENANT_STATUS_STYLE[f].label)} ({n})
                </button>
              );
            })}
          </div>

          {/* Table */}
          <Card style={{ padding: 0, overflow: "hidden" }}>
            {loading ? <LoadingScreen msg={tr("loadingTenants")} /> : isPhone ? (
              // AA-752 — phone card list (< 768px) instead of the wide table.
              shown.length === 0 ? (
                <div style={{ padding: 48, textAlign: "center", color: A.muted, fontSize: 13 }}>{tr("noTenants")}</div>
              ) : (
                <div>
                  {shown.map(t => (
                    <TenantRow key={t.tenant_id} tenant={t} onRotateKey={rotateKey} onChanged={() => load(true)} onRestored={restored} initialExpanded={t.tenant_id === deepTenant} isPhone />
                  ))}
                </div>
              )
            ) : (
              <ScrollX>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {[tr("colTenant"), tr("plan"), tr("colLifecycle"), tr("colSourceActive"), tr("colQuota"), tr("colStatus"), ""].map((h, i) => (
                      <th key={i} style={{ ...TH, textAlign: i >= 3 && i <= 4 ? "right" : "left" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.length === 0 ? (
                    <tr><td colSpan={7} style={{ padding: 48, textAlign: "center", color: A.muted, fontSize: 13 }}>{tr("noTenants")}</td></tr>
                  ) : shown.map(t => (
                    <TenantRow key={t.tenant_id} tenant={t} onRotateKey={rotateKey} onChanged={() => load(true)} onRestored={restored} initialExpanded={t.tenant_id === deepTenant} />
                  ))}
                </tbody>
              </table>
              </ScrollX>
            )}
          </Card>
        </PageFrame>

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={k => { setShowCreate(false); setNewKey(k); load(); }} />}
      {newKey && <ApiKeyModal keyData={newKey} onClose={() => setNewKey(null)} />}
    </>
  );
}
