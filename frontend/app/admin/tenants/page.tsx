"use client";
// app/admin/tenants/page.tsx — AA-159: lifecycle stats + country + count fix
// AA-473: Gate A removed entirely (ADR-2026-038 §0.2) — a new tenant is is_active=true
// immediately, no onboarding approval step left. The "Onboarding" tab and its pending-tenant
// UI (added AA-389, trimmed AA-472) are gone.

import { useState, useEffect, useCallback } from "react";
import {
  Users, Plus, Key, RefreshCw, ChevronDown, ChevronUp,
  AlertCircle, Loader2, CheckCircle, Eye, EyeOff, Copy, X, Trash2, Globe,
} from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import {
  A, serif, mono, sans,
  Card, SLabel, Btn, Badge, LoadingScreen, TH, TD,
} from "../_components/adminUi";

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
  is_active: boolean;
  created_at: string;
  plan: { tours_quota_monthly: number; api_calls_quota_monthly: number; price_usd_monthly: number };
  this_month: {
    tours_rewritten: number; api_calls_used: number;
    quota_tours_pct: number; quota_calls_pct: number;
    tours_overage: number; overage_usd: number; llm_cost_usd: number;
  };
  lifecycle: Lifecycle;
}

interface NewApiKey { tenant_id: string; tenant_name: string; api_key: string; }

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
        <span style={{ fontSize: 10, color: A.muted2, width: 42, flexShrink: 0 }}>Source</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.source_active}     label="active"     color={A.green} />
          <StatPill n={lc.source_superseded} label="superseded" color={A.gold} />
          <StatPill n={lc.source_trashed}    label="trashed"    color={A.red} />
        </div>
        {sourceTotal > 0 && (
          <span style={{ fontSize: 10, color: A.muted2, marginLeft: "auto" }}>{sourceTotal} total</span>
        )}
      </div>
      {/* Master row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: A.muted2, width: 42, flexShrink: 0 }}>Master</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.master_active}   label="active"   color={A.green} />
          <StatPill n={lc.master_inactive} label="inactive" color={A.muted} />
          <StatPill n={lc.master_trashed}  label="trashed"  color={A.red} />
        </div>
        {masterTotal > 0 && (
          <span style={{ fontSize: 10, color: A.muted2, marginLeft: "auto" }}>{masterTotal} total</span>
        )}
      </div>
    </div>
  );
}

// ─── Create Tenant Modal ──────────────────────────────────────────────────────

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (k: NewApiKey) => void }) {
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
    if (!name.trim() || !slug.trim()) { setError("Name and slug are required"); return; }
    const ppw = Number(postsPerWeek);
    if (!Number.isInteger(ppw) || ppw < 1 || ppw > 14) {
      setError("Posts/week must be an integer from 1 to 14"); return;
    }
    setLoading(true); setError("");
    try {
      const res = await fetch("/api/admin/tenants", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), slug: slug.trim(), plan_tier: plan, posts_per_week: ppw }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail ?? "Failed to create tenant"); return; }
      onCreated({ tenant_id: data.tenant_id, tenant_name: data.name, api_key: data.api_key });
    } catch { setError("Connection error"); } finally { setLoading(false); }
  }

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100 }}>
      <div style={{ background: A.card, border: `1px solid ${A.line}`, borderRadius: 16, padding: 32, width: 440 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: A.ink, letterSpacing: "-0.01em" }}>New Tenant</div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: A.muted }}><X size={16} /></button>
        </div>
        {[
          { label: "Company Name", value: name, onChange: (v: string) => { setName(v); setSlug(autoSlug(v)); }, placeholder: "WanderLux Travel" },
          { label: "Slug",         value: slug, onChange: setSlug, placeholder: "wanderlux-travel" },
        ].map(f => (
          <div key={f.label} style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 6 }}>{f.label}</label>
            <input value={f.value} onChange={e => f.onChange(e.target.value)} placeholder={f.placeholder}
              style={{ width: "100%", padding: "10px 12px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, color: A.body, fontSize: 13, outline: "none", boxSizing: "border-box", fontFamily: sans }} />
          </div>
        ))}
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>Plan</label>
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
            Posts/week (content posting cadence)
          </label>
          <input type="number" min={1} max={14} value={postsPerWeek} onChange={e => setPostsPerWeek(e.target.value)}
            style={{ width: 100, padding: "10px 12px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, color: A.body, fontSize: 13, outline: "none", fontFamily: sans }} />
          <p style={{ fontSize: 11, color: A.muted2, margin: "6px 0 0" }}>
            Tenant chooses freely, not limited by plan (1–14 posts/week).
          </p>
        </div>
        {error && (
          <div style={{ marginBottom: 14, padding: "9px 12px", background: A.redSoft, border: `1px solid ${A.redBorder}`, borderRadius: 8, fontSize: 12, color: A.red }}>
            {error}
          </div>
        )}
        <div style={{ display: "flex", gap: 10 }}>
          <Btn variant="secondary" onClick={onClose} style={{ flex: 1 }}>Cancel</Btn>
          <Btn variant="primary" disabled={loading} onClick={submit} style={{ flex: 2 }}>
            {loading ? <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> Creating…</> : "Create Tenant"}
          </Btn>
        </div>
      </div>
    </div>
  );
}

// ─── API Key Modal ────────────────────────────────────────────────────────────

function ApiKeyModal({ keyData, onClose }: { keyData: NewApiKey; onClose: () => void }) {
  const [show, setShow]     = useState(false);
  const [copied, setCopied] = useState(false);
  const copy = () => { navigator.clipboard.writeText(keyData.api_key); setCopied(true); setTimeout(() => setCopied(false), 2000); };
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 200 }}>
      <div style={{ background: A.card, border: "1px solid #86EFAC", borderRadius: 16, padding: 32, width: 460 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <CheckCircle size={20} color={A.green} />
          <div style={{ fontFamily: serif, fontSize: 20, fontWeight: 500, color: A.green }}>Tenant Created</div>
        </div>
        <p style={{ fontSize: 13, color: A.muted, marginBottom: 24 }}>
          <strong style={{ color: A.ink }}>{keyData.tenant_name}</strong> — share this API key once. It will <strong style={{ color: A.red }}>never be shown again</strong>.
        </p>
        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>API Key</label>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{
              flex: 1, fontFamily: mono, fontSize: 12, padding: "10px 14px",
              background: A.bg, border: "1px solid #86EFAC", borderRadius: 8,
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
          <button onClick={copy} style={{ flex: 1, padding: "10px 0", background: copied ? A.greenSoft : "#F0FDF4", border: "1px solid #86EFAC", borderRadius: 8, color: A.green, fontSize: 13, fontWeight: 600, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
            <Copy size={13} />{copied ? "Copied!" : "Copy Key"}
          </button>
          <Btn variant="secondary" onClick={onClose} style={{ flex: 1 }}>Done</Btn>
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
  };
}

const SCORE_COLOR = (s: number | null) =>
  s == null ? A.muted2 : s >= 9 ? A.green : s >= 7 ? A.gold : A.red;

const STATUS_STYLE: Record<string, { bg: string; col: string }> = {
  approved:     { bg: A.greenSoft, col: "#16A34A" },
  ai_generated: { bg: "#EFF6FF", col: "#2563EB" },
  rejected:     { bg: A.redSoft, col: A.red },
  needs_review: { bg: "#FEF3C7", col: "#D97706" },
  pending:      { bg: "#FEF9C3", col: "#B45309" },
  completed:    { bg: A.greenSoft, col: "#16A34A" },
  failed:       { bg: A.redSoft, col: A.red },
  ingesting:    { bg: "#EFF6FF", col: "#2563EB" },
};
function StatusChip({ status }: { status: string }) {
  const s = STATUS_STYLE[status] ?? { bg: A.line2, col: A.muted2 };
  return <span style={{ fontSize: 10.5, padding: "2px 8px", borderRadius: 20, fontWeight: 600, background: s.bg, color: s.col, whiteSpace: "nowrap" }}>{status}</span>;
}
function EmptyRow({ cols, msg }: { cols: number; msg: string }) {
  return <tr><td colSpan={cols} style={{ padding: "20px 0", textAlign: "center", fontSize: 12, color: A.muted }}>{msg}</td></tr>;
}
function fmtD(s: string) { return new Date(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }); }
function fmtDT(s: string) { return new Date(s).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }); }

function ToursTabContent({ tours, toursView }: { tours: RewrittenTour[]; toursView?: string }) {
  const isPublished = toursView === "published";
  const headers = isPublished
    ? ["Name", "Country", "Score", "Status", "Published At"]
    : ["Name", "Country", "Score", "Status", "Version", "Date"];
  return (
    <>
      {isPublished && (
        <div style={{ fontSize: 11, color: A.muted, marginBottom: 10, fontStyle: "italic" }}>
          Internal catalog — showing all published tours
        </div>
      )}
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>{headers.map((h, i) => (
          <th key={h} style={{ ...TH, fontSize: 10, textAlign: i >= 2 ? "right" : "left" }}>{h}</th>
        ))}</tr></thead>
        <tbody>
          {tours.length === 0
            ? <EmptyRow cols={headers.length} msg={isPublished ? "No published tours yet" : "No rewrites yet"} />
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
    </>
  );
}
function PipelineTabContent({ runs, pipelineNote }: { runs: PipelineRun[]; pipelineNote?: string | null }) {
  return (
    <>
      {pipelineNote && (
        <div style={{ fontSize: 11, color: A.muted, marginBottom: 10, fontStyle: "italic" }}>{pipelineNote}</div>
      )}
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>{["Started", "Tours", "Passed", "Model", "Cost", "Status"].map((h, i) => (
          <th key={h} style={{ ...TH, fontSize: 10, textAlign: i >= 1 ? "right" : "left" }}>{h}</th>
        ))}</tr></thead>
        <tbody>
          {runs.length === 0 ? <EmptyRow cols={6} msg="No pipeline runs" /> : runs.map(r => (
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
    </>
  );
}
function ApiTabContent({ usage }: { usage: TenantDetails["api_usage"] }) {
  const pct      = usage.quota_total > 0 ? Math.min(100, Math.round((usage.quota_used / usage.quota_total) * 100)) : 0;
  const barColor = pct > 90 ? A.red : pct > 70 ? A.amber : A.green;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
          <span style={{ color: A.muted }}>API Calls Quota (this month)</span>
          <span style={{ fontWeight: 700, color: barColor }}>{usage.quota_used.toLocaleString()} / {usage.quota_total.toLocaleString()} ({pct}%)</span>
        </div>
        <div style={{ height: 8, background: A.line2, borderRadius: 4, overflow: "hidden" }}>
          <div style={{ height: "100%", width: `${pct}%`, background: barColor, borderRadius: 4, transition: "width .4s" }} />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 10 }}>
        {([["Total Calls", usage.total_calls.toLocaleString()], ["Rate Limit", `${usage.rate_limit_per_min}/min`], ["Quota Used", `${pct}%`]] as [string, string][]).map(([l, v]) => (
          <div key={l} style={{ padding: "10px 14px", background: "#fff", border: `1px solid ${A.line}`, borderRadius: 8 }}>
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
  width: "100%", padding: "9px 12px", background: "#fff", border: `1px solid ${A.line}`,
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
          good_examples: goodExamples,
        }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); setError(d.detail ?? "Failed to save"); return; }
      setSaved(true); setEditing(false);
      setTimeout(() => setSaved(false), 2500);
    } catch { setError("Connection error"); } finally { setSaving(false); }
  }

  if (!editing) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", gap: 16, fontSize: 11, color: A.muted2 }}>
            <span>{rules.version_count} version{rules.version_count !== 1 ? "s" : ""}</span>
            {rules.last_updated && <span>Last updated: {fmtD(rules.last_updated)}</span>}
          </div>
          <Btn variant="secondary" size="sm" onClick={() => setEditing(true)}>Edit</Btn>
        </div>
        {saved && <span style={{ fontSize: 12, color: A.green }}>Saved</span>}
        {!hasAnyData ? (
          <div style={{ padding: "20px 0", textAlign: "center", fontSize: 12, color: A.muted }}>No brand rules configured — click Edit to set them up.</div>
        ) : (
          <>
            {rules.brand_name && <div><span style={{ fontSize: 10, fontWeight: 700, color: A.muted }}>BRAND NAME </span><span style={{ fontSize: 13, color: A.ink, fontWeight: 600 }}>{rules.brand_name}</span></div>}
            {rules.brand_type && <div><span style={{ fontSize: 10, fontWeight: 700, color: A.muted }}>BRAND TYPE </span><span style={{ fontSize: 12.5, color: A.body }}>{rules.brand_type}</span></div>}
            {rules.core_idea && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Core Idea</div>
                <div style={{ fontSize: 12.5, color: A.body }}>{rules.core_idea}</div>
              </div>
            )}
            {rules.target_markets.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Target Markets</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.target_markets.map((m, i) => <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: A.line2, color: A.body, borderRadius: 20 }}>{m}</span>)}
                </div>
              </div>
            )}
            {rules.customer_segment && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Customer Segment</div>
                <div style={{ fontSize: 12.5, color: A.body }}>{rules.customer_segment}</div>
              </div>
            )}
            {rules.customer_mindset && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Customer Mindset</div>
                <div style={{ fontSize: 12.5, color: A.body }}>{rules.customer_mindset}</div>
              </div>
            )}
            {rules.voice_examples.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Tone of Voice</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.voice_examples.map((t, i) => <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: A.goldTint, color: A.gold, borderRadius: 20 }}>{t}</span>)}
                </div>
              </div>
            )}
            {rules.style_guide && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Writing Style</div>
                <div style={{ padding: "10px 12px", background: "#fff", border: `1px solid ${A.line}`, borderRadius: 8, fontSize: 12, color: A.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{rules.style_guide}</div>
              </div>
            )}
            {rules.good_examples && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Good Examples</div>
                <div style={{ padding: "10px 12px", background: "#fff", border: `1px solid ${A.line}`, borderRadius: 8, fontSize: 12, color: A.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{rules.good_examples}</div>
              </div>
            )}
            {rules.system_prompt && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Should Write (System Prompt)</div>
                <div style={{ padding: "10px 12px", background: "#fff", border: `1px solid ${A.line}`, borderRadius: 8, fontSize: 12, color: A.body, fontFamily: mono, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                  {rules.system_prompt.slice(0, 400)}{rules.system_prompt.length > 400 ? "…" : ""}
                </div>
              </div>
            )}
            {rules.forbidden_words.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted, marginBottom: 6 }}>Forbidden Words ({rules.forbidden_words.length})</div>
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
      <BrandField label="Brand Name" value={brandName} onChange={setBrandName} />
      <BrandField label="Brand Type" value={brandType} onChange={setBrandType} />
      <BrandField label="Core Idea" value={coreIdea} onChange={setCoreIdea} rows={2} />
      <BrandField label="Target Markets (comma-separated)" value={targetMarkets} onChange={setTargetMarkets} />
      <BrandField label="Customer Segment" value={customerSegment} onChange={setCustomerSegment} rows={2} />
      <BrandField label="Customer Mindset" value={customerMindset} onChange={setCustomerMindset} rows={2} />
      <BrandField label="Tone of Voice (comma-separated)" value={toneOfVoice} onChange={setToneOfVoice} />
      <BrandField label="Writing Style" value={sg} onChange={setSg} rows={4} />
      <BrandField label="Good Examples" value={goodExamples} onChange={setGoodExamples} rows={3} />
      <BrandField label="Should Write (System Prompt)" value={sp} onChange={setSp} rows={5} />
      <BrandField label="Forbidden Words (comma-separated)" value={fw} onChange={setFw} />
      {error && (
        <div style={{ padding: "9px 12px", background: A.redSoft, border: `1px solid ${A.redBorder}`, borderRadius: 8, fontSize: 12, color: A.red }}>{error}</div>
      )}
      <div style={{ display: "flex", gap: 10 }}>
        <Btn variant="primary" size="sm" disabled={saving} onClick={save}>
          {saving ? <><Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> Saving…</> : "Save"}
        </Btn>
        <Btn variant="secondary" size="sm" onClick={() => setEditing(false)}>Cancel</Btn>
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
  return (
    <div>
      <div style={{ fontSize: 11, color: A.muted2, marginBottom: 10 }}>Rewrite activity for this tenant</div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr>{["Tour Name", "Country", "Version", "Status", "Score", "Date"].map((h, i) => (
          <th key={h} style={{ ...TH, fontSize: 10, textAlign: i >= 2 ? "right" : "left" }}>{h}</th>
        ))}</tr></thead>
        <tbody>
          {items.length === 0
            ? <EmptyRow cols={6} msg="No rewrite activity yet" />
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
      .catch(() => setError("Failed to load Social Content summary"))
      .finally(() => setLoading(false));
  }, [tenantId]);

  if (loading) return <div style={{ padding: "12px 0", fontSize: 12, color: A.muted }}>Loading…</div>;
  if (error || !data) return <div style={{ padding: "12px 0", fontSize: 12, color: A.red }}>{error || "Failed to load"}</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", gap: 14 }}>
        {([["Content pieces written", data.pieces_written], ["Published", data.pieces_published]] as [string, number][]).map(([l, v]) => (
          <div key={l} style={{ padding: "10px 14px", background: "#fff", border: `1px solid ${A.line}`, borderRadius: 8, minWidth: 140 }}>
            <div style={{ fontSize: 10, color: A.muted2, marginBottom: 3 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>{v}</div>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: A.muted2, fontStyle: "italic" }}>
        Detailed per-piece Atom/Segment/Route/Slate usage breakdown is pending the 06/07/08 merge
        decision (see AA-558) — this tab will grow once that&apos;s resolved. In the meantime, see{" "}
        <a href="/admin/tenant-activity" style={{ color: A.gold }}>Content Trace</a> for the
        full Write/Gate → Review → Publish detail (filterable by Tour, not yet by this Tenant
        directly from there).
      </div>
    </div>
  );
}

// ─── Tenant Detail Panel ──────────────────────────────────────────────────────

type DTab = "tours" | "pipeline" | "activity" | "api" | "brand" | "social_content";

function TenantDetail({ tenantId, planTier }: {
  tenantId: string; planTier: string;
}) {
  const isInternal = planTier === "internal";
  const [data, setData]         = useState<TenantDetails | null>(null);
  const [loading, setLoading]   = useState(true);
  const [tab, setTab]           = useState<DTab>("tours");
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

  if (loading) return <div style={{ padding: "12px 0", fontSize: 12, color: A.muted }}>Loading…</div>;
  if (!data)   return <div style={{ padding: "12px 0", fontSize: 12, color: A.red }}>Failed to load details</div>;

  const s = data.summary;
  // AA-557 I.17 — "Planning" tab removed entirely (Nghiệp confirmed: tenants no longer self-
  // serve Quarter-Plan, replaced by Slate since AA-511/519 — the tab had no meaning left).
  const TABS: { key: DTab; label: string }[] = [
    { key: "tours",   label: `Tours (${data.rewritten_tours.length})` },
    ...(isInternal
      ? [{ key: "pipeline" as DTab, label: `Pipeline (${data.pipeline_runs.length})` }]
      : [{ key: "activity" as DTab, label: `Activity (${activity.length})` }]
    ),
    { key: "api",   label: "API Usage" },
    { key: "brand", label: "Brand" },
    // AA-557 I.22 — skeleton tab only, see SocialContentTabContent's own comment for why.
    { key: "social_content", label: "Social Content" },
  ];

  return (
    <div style={{ background: A.bg, borderRadius: 10, padding: 16, border: `1px solid ${A.line}`, marginTop: 4 }}>
      <div style={{ display: "flex", gap: 20, marginBottom: 14, flexWrap: "wrap" }}>
        {([
          ["Total Rewrites", String(s.total_rewrites),              A.gold],
          ["LLM Cost",       `$${s.total_llm_cost_usd.toFixed(3)}`, A.body],
          ["API Calls (Mo)", s.api_calls_this_month.toLocaleString(), A.body],
          ["Quota",          `${s.quota_pct}%`,                     s.quota_pct > 80 ? A.red : A.body],
          // AA-557 I.19 — real data, already fetched (`shared.tenants.rate_limit_rpm` via
          // `api_usage.rate_limit_per_min`, same value the API Usage tab below already showed) —
          // just also surfaced at header level so an admin doesn't need to open that tab for it.
          ["Rate Limit",     `${data.api_usage.rate_limit_per_min}/min`, A.body],
          ["Plan",           s.plan_name,                           A.body],
          ["Member Since",   s.member_since,                        A.muted],
        ] as [string, string, string][]).map(([l, v, c]) => (
          <div key={l} style={{ minWidth: 80 }}>
            <div style={{ fontSize: 10, color: A.muted2, marginBottom: 2 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 500, color: c }}>{v}</div>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", borderBottom: `1px solid ${A.line}`, marginBottom: 14 }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)} style={{
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
    </div>
  );
}


// ─── Tenant Row ───────────────────────────────────────────────────────────────

function TenantRow({ tenant, onRotateKey, onDeleted }: {
  tenant: Tenant;
  onRotateKey: (t: Tenant) => void;
  onDeleted: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [isActive, setIsActive] = useState(tenant.is_active);
  const [deleting, setDeleting] = useState(false);

  async function toggle() {
    setToggling(true);
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !isActive }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body.detail ?? "Failed to update tenant status");
        return;
      }
      setIsActive(!isActive);
    } finally { setToggling(false); }
  }

  async function deleteTenant() {
    if (!confirm(`Delete tenant "${tenant.name}"? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      await fetch(`/api/admin/tenants/${tenant.tenant_id}`, { method: "DELETE" });
      onDeleted(tenant.tenant_id);
    } finally { setDeleting(false); }
  }

  const lc = tenant.lifecycle ?? {
    source_active: 0, source_superseded: 0, source_trashed: 0,
    master_active: 0, master_inactive: 0, master_trashed: 0,
  };

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
            {tenant.posts_per_week != null ? `${tenant.posts_per_week} posts/week` : "—"}
          </div>
        </td>

        {/* Lifecycle stats */}
        <td style={{ ...TD, minWidth: 200 }}>
          <LifecycleBar lc={lc} />
        </td>

        {/* API usage */}
        <td style={{ ...TD, textAlign: "right" }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: A.ink }}>
            {lc.source_active} active
          </div>
          <div style={{ fontSize: 11, color: A.green }}>
            {tenant.this_month.api_calls_used.toLocaleString()} calls
          </div>
        </td>

        {/* Quota */}
        <td style={{ ...TD, textAlign: "right", fontSize: 12, color: A.muted }}>
          {tenant.this_month.quota_tours_pct}% quota
        </td>

        {/* Status toggle */}
        <td style={TD}>
          <button onClick={toggle} disabled={toggling} style={{
            padding: "4px 12px", borderRadius: 20, border: "none", cursor: "pointer",
            fontSize: 11, fontWeight: 700,
            background: isActive ? A.greenSoft : A.redSoft,
            color: isActive ? A.green : A.red,
          }}>
            {toggling ? "…" : isActive ? "Active" : "Inactive"}
          </button>
        </td>

        {/* Actions */}
        <td style={TD}>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <button onClick={() => onRotateKey(tenant)} title="Rotate API key"
              style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: "pointer", color: A.muted, display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
              <Key size={12} /> Key
            </button>
            <button onClick={deleteTenant} disabled={deleting} title="Delete tenant"
              style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: "pointer", color: A.red, display: "flex", alignItems: "center" }}>
              {deleting ? <Loader2 size={12} style={{ animation: "spin 1s linear infinite" }} /> : <Trash2 size={12} />}
            </button>
            <button onClick={() => setExpanded(!expanded)}
              style={{ padding: "5px 10px", background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, cursor: "pointer", color: A.muted, display: "flex", alignItems: "center" }}>
              {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7} style={{ padding: "0 16px 14px", background: A.bg }}>
            <TenantDetail
              tenantId={tenant.tenant_id} planTier={tenant.plan_tier}
            />
          </td>
        </tr>
      )}
    </>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function TenantsPage() {
  const [tenants, setTenants]       = useState<Tenant[]>([]);
  const [loading, setLoading]       = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newKey, setNewKey]         = useState<NewApiKey | null>(null);
  const [error, setError]           = useState("");
  // AA-629 Tier 2 — best-effort, tolerate failure so the whole page still loads without it.
  const [unmappedMarkets, setUnmappedMarkets] = useState<UnmappedMarketRequest[]>([]);

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const res = await fetch("/api/admin/tenants");
      if (!res.ok) { setError("Failed to load tenants"); return; }
      const data = await res.json();
      setTenants(data.tenants ?? []);
    } catch { setError("Connection error"); } finally { setLoading(false); }
    fetch("/api/admin/unmapped-market-requests")
      .then(r => r.ok ? r.json() : null)
      .then(d => setUnmappedMarkets(d?.requests ?? []))
      .catch(() => setUnmappedMarkets([]));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function rotateKey(tenant: Tenant) {
    if (!confirm(`Rotate API key for ${tenant.name}? Old key stops working immediately.`)) return;
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}/generate-key`, { method: "POST" });
      const data = await res.json();
      if (res.ok) setNewKey({ tenant_id: tenant.tenant_id, tenant_name: tenant.name, api_key: data.api_key });
    } catch { /* silent */ }
  }

  function handleDeleted(tenantId: string) {
    setTenants(prev => prev.filter(t => t.tenant_id !== tenantId));
  }

  const totalActive     = tenants.filter(t => t.is_active).length;
  const totalSrcActive  = tenants.reduce((s, t) => s + (t.lifecycle?.source_active ?? 0), 0);
  const totalMstrActive = tenants.reduce((s, t) => s + (t.lifecycle?.master_active ?? 0), 0);

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: A.bg }}>
      <AdminSidebar />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh" }}>
        <header style={{ height: 56, background: "#fff", borderBottom: `1px solid ${A.line}`, display: "flex", alignItems: "center", padding: "0 32px", gap: 8, position: "sticky", top: 0, zIndex: 10 }}>
          <span style={{ fontSize: 12, color: A.muted2 }}>Admin /</span>
          <span style={{ fontSize: 12, fontWeight: 500, color: A.body }}>Tenants</span>
        </header>

        <main style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 24 }}>
            <div>
              <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: A.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
                Tenants
              </h1>
              <p style={{ fontSize: 13, color: A.muted, margin: 0 }}>
                {totalActive} active · {totalSrcActive.toLocaleString()} source tours · {totalMstrActive.toLocaleString()} published
              </p>
            </div>
            <div style={{ display: "flex", gap: 10 }}>
              <Btn variant="secondary" onClick={load}><RefreshCw size={13} /> Refresh</Btn>
              <Btn variant="primary" onClick={() => setShowCreate(true)}><Plus size={14} /> New Tenant</Btn>
            </div>
          </div>

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
                    <strong>{u.tenant_count}</strong> tenant{u.tenant_count === 1 ? "" : "s"} waiting on market <strong>{u.country_code}</strong> (not yet supported for SEO research)
                  </span>
                ))}
              </span>
            </div>
          )}

          {/* Summary cards */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14, marginBottom: 20 }}>
            {[
              { label: "Total Tenants",    value: String(tenants.length),            color: A.accent },
              { label: "Active",           value: String(totalActive),               color: A.green },
              { label: "Source (active)",  value: totalSrcActive.toLocaleString(),   color: A.gold },
              { label: "Master (active)",  value: totalMstrActive.toLocaleString(),  color: A.ink },
            ].map(c => (
              <Card key={c.label}>
                <SLabel>{c.label}</SLabel>
                <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 26, fontWeight: 600, color: c.color, letterSpacing: "-0.02em" }}>{c.value}</div>
              </Card>
            ))}
          </div>

          {/* Table */}
          <Card style={{ padding: 0, overflow: "hidden" }}>
            {loading ? <LoadingScreen msg="Loading tenants…" /> : (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {["Tenant", "Plan", "Lifecycle", "Source Active", "Quota", "Status", ""].map((h, i) => (
                      <th key={i} style={{ ...TH, textAlign: i >= 3 && i <= 4 ? "right" : "left" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tenants.length === 0 ? (
                    <tr><td colSpan={7} style={{ padding: 48, textAlign: "center", color: A.muted, fontSize: 13 }}>No tenants yet</td></tr>
                  ) : tenants.map(t => (
                    <TenantRow key={t.tenant_id} tenant={t} onRotateKey={rotateKey} onDeleted={handleDeleted} />
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </main>
      </div>

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={k => { setShowCreate(false); setNewKey(k); load(); }} />}
      {newKey && <ApiKeyModal keyData={newKey} onClose={() => setNewKey(null)} />}
    </div>
  );
}
