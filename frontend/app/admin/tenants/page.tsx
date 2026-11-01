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
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import {
  Plus, Key, RefreshCw, ChevronRight,
  AlertCircle, Loader2, CheckCircle, Eye, EyeOff, Copy, Ban, Globe, RotateCcw,
} from "lucide-react";
import {
  Button, PageFrame, Modal, PageHeader, Tabs, useFormat, useLang, useT, Input, Textarea,
  K, serif, mono, sans, Section, Eyebrow, Badge, LoadingScreen,
  DataTable, IconButton, KX, SegmentedControl, type ColumnDef,
  type Tone,
  ButtonGroup, ChipButton, Drawer, Pressable,
} from "../../_kit";
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
  TenantStatus, { label: "statusActive" | "statusSuspended" | "statusOffboarded"; tone: Tone }
> = {
  active:     { label: "statusActive",     tone: "success" },
  suspended:  { label: "statusSuspended",  tone: "warning" },
  offboarded: { label: "statusOffboarded", tone: "danger" },
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
const PLAN_BADGE: Record<string, Tone> = {
  starter: "info", growth: "purple", business: "success", internal: "accent",
};

// ─── Lifecycle Bar ────────────────────────────────────────────────────────────

function StatPill({ n, label, color }: { n: number; label: string; color: string }) {
  return (
    <span title={label} style={{
      display: "inline-flex", alignItems: "center", gap: 3,
      fontSize: 11, fontWeight: n > 0 ? 600 : 400,
      color: n > 0 ? color : K.muted2,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: n > 0 ? color : K.line, flexShrink: 0 }} />
      {n}
    </span>
  );
}


function LifecycleBar({ lc }: { lc: Lifecycle }) {
  const t = useT(TEN);
  const sourceTotal = lc.source_active + lc.source_superseded + lc.source_trashed;
  const masterTotal = lc.master_active + lc.master_inactive + lc.master_trashed;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {/* Source row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: K.muted2, width: 42, flexShrink: 0 }}>{t("source")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.source_active}     label={t("active")}     color={K.success} />
          <StatPill n={lc.source_superseded} label={t("superseded")} color={K.accent} />
          <StatPill n={lc.source_trashed}    label={t("trashed")}    color={K.danger} />
        </div>
        {sourceTotal > 0 && (
          <span style={{ fontSize: 10, color: K.muted2, marginLeft: "auto" }}>{t("total", sourceTotal)}</span>
        )}
      </div>
      {/* Master row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: K.muted2, width: 42, flexShrink: 0 }}>{t("master")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.master_active}   label={t("active")}   color={K.success} />
          <StatPill n={lc.master_inactive} label={t("inactive")} color={K.muted} />
          <StatPill n={lc.master_trashed}  label={t("trashed")}  color={K.danger} />
        </div>
        {masterTotal > 0 && (
          <span style={{ fontSize: 10, color: K.muted2, marginLeft: "auto" }}>{t("total", masterTotal)}</span>
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
    <Modal
      open
      onClose={onClose}
      title={t("newTenant")}
      width={440}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>{t("cancel")}</Button>
          <Button variant="primary" disabled={loading} onClick={submit}>
            {loading ? <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> {t("creating")}</> : t("createTenant")}
          </Button>
        </>
      }
    >
        {[
          { key: "name", label: t("companyName"), value: name, onChange: (v: string) => { setName(v); setSlug(autoSlug(v)); }, placeholder: "WanderLux Travel" },
          { key: "slug", label: t("slug"),        value: slug, onChange: setSlug, placeholder: "wanderlux-travel" },
        ].map(f => (
          <div key={f.key} style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 6 }}>{f.label}</label>
            <Input value={f.value} onChange={e => f.onChange(e.target.value)} placeholder={f.placeholder}
              aria-label={f.label} style={{ width: "100%" }} />
          </div>
        ))}
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>{t("plan")}</label>
          <SegmentedControl
            label={t("plan")}
            value={plan}
            onChange={setPlan}
            options={PLAN_OPTIONS.map(p => ({ value: p, label: p }))}
          />
        </div>
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>
            {t("ppwLabel")}
          </label>
          <Input type="number" min={1} max={14} value={postsPerWeek} onChange={e => setPostsPerWeek(e.target.value)}
            aria-label={t("ppwLabel")} style={{ width: 100 }} />
          <p style={{ fontSize: 11, color: K.muted2, margin: "6px 0 0" }}>
            {t("ppwHelp")}
          </p>
        </div>
        {error && (
          <div style={{ marginBottom: 14, padding: "9px 12px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, fontSize: 12, color: K.danger }}>
            {error}
          </div>
        )}
    </Modal>
  );
}

// ─── API Key Modal ────────────────────────────────────────────────────────────

function ApiKeyModal({ keyData, onClose }: { keyData: NewApiKey; onClose: () => void }) {
  const t = useT(TEN);
  const [show, setShow]     = useState(false);
  const [copied, setCopied] = useState(false);
  const copy = () => { navigator.clipboard.writeText(keyData.api_key); setCopied(true); setTimeout(() => setCopied(false), 2000); };
  return (
    <Modal
      open
      onClose={onClose}
      width={460}
      ariaLabel={keyData.title ?? (keyData.rotated ? t("newApiKey") : t("tenantCreated"))}
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10, color: K.success }}>
          <CheckCircle size={20} color={K.success} />
          {keyData.title ?? (keyData.rotated ? t("newApiKey") : t("tenantCreated"))}
        </span>
      }
      footer={
        <>
          <Button variant="secondary" onClick={copy}>
            <Copy size={13} />{copied ? t("copied") : t("copyKey")}
          </Button>
          <Button variant="primary" onClick={onClose}>{t("done")}</Button>
        </>
      }
    >
        {keyData.note && <p style={{ fontSize: 13, color: K.muted, margin: "0 0 12px" }}>{keyData.note}</p>}
        <p style={{ fontSize: 13, color: K.muted, marginBottom: 24 }}>
          <strong style={{ color: K.ink }}>{keyData.tenant_name}</strong> {t("shareOnceA")} <strong style={{ color: K.danger }}>{t("neverShownAgain")}</strong>.
        </p>
        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>{t("apiKey")}</label>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{
              flex: 1, fontFamily: mono, fontSize: 12, padding: "10px 14px",
              background: K.bg, border: `1px solid ${KX.successBorder}`, borderRadius: 8,
              color: show ? K.success : K.muted, letterSpacing: show ? 0.3 : 3, wordBreak: "break-all",
            }}>
              {show ? keyData.api_key : "•".repeat(32)}
            </div>
            <IconButton size="lg" onClick={() => setShow(!show)} label={show ? t("hideKey") : t("showKey")} pressed={show}>
              {show ? <EyeOff size={14} /> : <Eye size={14} />}
            </IconButton>
          </div>
        </div>
    </Modal>
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
  s == null ? K.muted2 : s >= SCORE_BANDS.top ? K.success : s >= SCORE_BANDS.pass ? K.accent : K.danger;

const STATUS_TONE: Record<string, Tone> = {
  approved: "success", ai_generated: "info", rejected: "danger", needs_review: "warning",
  pending: "warning", completed: "success", failed: "danger", ingesting: "info",
};
function StatusChip({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"} dot={false} style={{ textTransform: "none", letterSpacing: 0 }}>{status}</Badge>;
}
function ToursTabContent({ tours, toursView }: { tours: RewrittenTour[]; toursView?: string }) {
  const tr = useT(TEN);
  const { formatDate: fmtD } = useFormat();
  const isPublished = toursView === "published";
  return (
    <>
      {isPublished && (
        <div style={{ fontSize: 11, color: K.muted, marginBottom: 10, fontStyle: "italic" }}>
          {tr("internalCatalog")}
        </div>
      )}
      <DataTable<RewrittenTour>
        tableId={isPublished ? "tenant-tours-published" : "tenant-tours-rewrites"}
        data={tours}
        getRowId={(x) => x.version_id}
        searchable={false}
        paginate={false}
        compact
        emptyTitle={isPublished ? tr("noPublished") : tr("noRewrites")}
        columns={[
          { id: "name", header: tr("colName"), accessorKey: "tour_name", cell: ({ row: { original: x } }) => (
            <span style={{ display: "inline-block", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", verticalAlign: "bottom" }}>{x.tour_name}</span>
          ) },
          { id: "country", header: tr("colCountry"), accessorFn: (x) => x.country ?? "", cell: ({ row: { original: x } }) => <span style={{ color: K.muted }}>{x.country ?? "—"}</span> },
          { id: "score", header: tr("colScore"), accessorFn: (x) => x.quality_score ?? -1, meta: { align: "right" }, cell: ({ row: { original: x } }) => (
            <span style={{ fontWeight: 700, color: SCORE_COLOR(x.quality_score) }}>{x.quality_score != null ? x.quality_score.toFixed(1) : "—"}</span>
          ) },
          { id: "status", header: tr("colStatus"), accessorKey: "status", meta: { align: "right" }, cell: ({ row: { original: x } }) => <StatusChip status={x.status} /> },
          ...(isPublished ? [] : [{ id: "version", header: tr("colVersion"), accessorFn: (x: RewrittenTour) => x.version_number ?? -1, meta: { align: "right" },
            cell: ({ row: { original: x } }: { row: { original: RewrittenTour } }) => (
              <span style={{ fontFamily: mono, color: K.muted2 }}>{x.version_number != null ? `v${x.version_number}` : "—"}</span>
            ) } as ColumnDef<RewrittenTour, unknown>]),
          { id: "date", header: isPublished ? tr("colPublishedAt") : tr("colDate"), accessorKey: "created_at", meta: { align: "right" },
            cell: ({ row: { original: x } }) => <span style={{ fontFamily: mono, color: K.muted2 }}>{fmtD(x.created_at)}</span> },
        ]}
      />
    </>
  );
}
function PipelineTabContent({ runs, pipelineNote }: { runs: PipelineRun[]; pipelineNote?: string | null }) {
  const t = useT(TEN);
  const { formatDateTime: fmtDT } = useFormat();
  return (
    <>
      {pipelineNote && (
        <div style={{ fontSize: 11, color: K.muted, marginBottom: 10, fontStyle: "italic" }}>{pipelineNote}</div>
      )}
      <DataTable<PipelineRun>
        tableId="tenant-pipeline-runs"
        data={runs}
        getRowId={(r) => r.run_id}
        searchable={false}
        paginate={false}
        compact
        emptyTitle={t("noRuns")}
        columns={[
          { id: "started", header: t("colStarted"), accessorKey: "started_at",
            cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 11, color: K.muted }}>{fmtDT(r.started_at)}</span> },
          { id: "tours", header: t("colTours"), accessorKey: "tours_processed", meta: { align: "right" } },
          { id: "passed", header: t("colPassed"), accessorKey: "tours_passed", meta: { align: "right" },
            cell: ({ row: { original: r } }) => <span style={{ color: K.success }}>{r.tours_passed}</span> },
          { id: "model", header: t("colModel"), accessorFn: (r) => r.llm_model ?? "", meta: { align: "right" },
            cell: ({ row: { original: r } }) => (
              <span style={{ fontFamily: mono, fontSize: 10, color: K.muted2 }}>
                {r.llm_model ? r.llm_model.replace(/us\.anthropic\./, "").replace(/-v1:0$/, "") : "—"}
              </span>
            ) },
          { id: "cost", header: t("colCost"), accessorKey: "llm_cost_usd", meta: { align: "right" },
            cell: ({ row: { original: r } }) => <span style={{ color: K.accent }}>${r.llm_cost_usd.toFixed(4)}</span> },
          { id: "status", header: t("colStatus"), accessorKey: "status", meta: { align: "right" },
            cell: ({ row: { original: r } }) => <StatusChip status={r.status} /> },
        ]}
      />
    </>
  );
}
function ApiTabContent({ usage }: { usage: TenantDetails["api_usage"] }) {
  const t = useT(TEN);
  const pct      = usage.quota_total > 0 ? Math.min(100, Math.round((usage.quota_used / usage.quota_total) * 100)) : 0;
  const barColor = pct > 90 ? K.danger : pct > 70 ? K.amber : K.success;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
          <span style={{ color: K.muted }}>{t("apiQuota")}</span>
          <span style={{ fontWeight: 700, color: barColor }}>{usage.quota_used.toLocaleString()} / {usage.quota_total.toLocaleString()} ({pct}%)</span>
        </div>
        <div style={{ height: 8, background: K.line2, borderRadius: 4, overflow: "hidden" }}>
          <div style={{ height: "100%", width: `${pct}%`, background: barColor, borderRadius: 4, transition: "width .4s" }} />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))", gap: 10 }}>
        {([[t("totalCalls"), usage.total_calls.toLocaleString()], [t("rateLimit"), t("perMin", usage.rate_limit_per_min)], [t("quotaUsed"), `${pct}%`]] as [string, string][]).map(([l, v]) => (
          <div key={l} style={{ padding: "10px 14px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8 }}>
            <div style={{ fontSize: 10, color: K.muted2, marginBottom: 3 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink }}>{v}</div>
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
  width: "100%", padding: "9px 12px", background: K.card, border: `1px solid ${K.line}`,
  borderRadius: 8, color: K.body, fontSize: 12.5, fontFamily: sans, outline: "none", boxSizing: "border-box",
};
function BrandField({ label, value, onChange, rows }: {
  label: string; value: string; onChange: (v: string) => void; rows?: number;
}) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{label}</div>
      {rows ? (
        <Textarea value={value} onChange={e => onChange(e.target.value)} rows={rows} style={{ ...brandFieldStyle, resize: "vertical", lineHeight: 1.5 }} />
      ) : (
        <Input value={value} onChange={e => onChange(e.target.value)} style={brandFieldStyle} />
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
          <div style={{ display: "flex", gap: 16, fontSize: 11, color: K.muted2 }}>
            <span>{tr("versions", rules.version_count)}</span>
            {rules.last_updated && <span>{tr("lastUpdated", fmtD(rules.last_updated))}</span>}
          </div>
          <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>{tr("edit")}</Button>
        </div>
        {saved && <span style={{ fontSize: 12, color: K.success }}>{tr("saved")}</span>}
        {!hasAnyData ? (
          <div style={{ padding: "20px 0", textAlign: "center", fontSize: 12, color: K.muted }}>{tr("noBrand")}</div>
        ) : (
          <>
            {rules.brand_name && <div><span style={{ fontSize: 10, fontWeight: 700, color: K.muted }}>{tr("brandNameInline")} </span><span style={{ fontSize: 13, color: K.ink, fontWeight: 600 }}>{rules.brand_name}</span></div>}
            {rules.brand_type && <div><span style={{ fontSize: 10, fontWeight: 700, color: K.muted }}>{tr("brandTypeInline")} </span><span style={{ fontSize: 12.5, color: K.body }}>{rules.brand_type}</span></div>}
            {rules.core_idea && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("coreIdea")}</div>
                <div style={{ fontSize: 12.5, color: K.body }}>{rules.core_idea}</div>
              </div>
            )}
            {rules.target_markets.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("targetMarkets")}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.target_markets.map((m, i) => <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: K.line2, color: K.body, borderRadius: 20 }}>{m}</span>)}
                </div>
              </div>
            )}
            {rules.customer_segment && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("customerSegment")}</div>
                <div style={{ fontSize: 12.5, color: K.body }}>{rules.customer_segment}</div>
              </div>
            )}
            {rules.customer_mindset && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("customerMindset")}</div>
                <div style={{ fontSize: 12.5, color: K.body }}>{rules.customer_mindset}</div>
              </div>
            )}
            {rules.voice_examples.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("toneOfVoice")}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.voice_examples.map((t, i) => <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: K.accentTint, color: K.accent, borderRadius: 20 }}>{t}</span>)}
                </div>
              </div>
            )}
            {rules.style_guide && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("writingStyle")}</div>
                <div style={{ padding: "10px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, fontSize: 12, color: K.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{rules.style_guide}</div>
              </div>
            )}
            {rules.good_examples && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("goodExamples")}</div>
                <div style={{ padding: "10px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, fontSize: 12, color: K.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{rules.good_examples}</div>
              </div>
            )}
            {rules.system_prompt && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("systemPrompt")}</div>
                <div style={{ padding: "10px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, fontSize: 12, color: K.body, fontFamily: mono, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                  {rules.system_prompt.slice(0, 400)}{rules.system_prompt.length > 400 ? "…" : ""}
                </div>
              </div>
            )}
            {rules.forbidden_words.length > 0 && (
              <div>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted, marginBottom: 6 }}>{tr("forbiddenWords", rules.forbidden_words.length)}</div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {rules.forbidden_words.map((w, i) => (
                    <span key={i} style={{ fontSize: 11, padding: "2px 8px", background: K.dangerSoft, color: K.danger, borderRadius: 20, fontWeight: 600 }}>{w}</span>
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
        <div style={{ padding: "9px 12px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, fontSize: 12, color: K.danger }}>{error}</div>
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
      <div style={{ fontSize: 11, color: K.muted2, marginBottom: 10 }}>{t("activityIntro")}</div>
      <DataTable<RewriteActivityItem>
        tableId="tenant-rewrite-activity"
        data={items}
        getRowId={(r) => r.version_id}
        searchable={false}
        paginate={false}
        compact
        emptyTitle={t("noActivity")}
        columns={[
          { id: "name", header: t("colTourName"), accessorKey: "tour_name", cell: ({ row: { original: r } }) => (
            <span style={{ display: "inline-block", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", verticalAlign: "bottom" }}>{r.tour_name}</span>
          ) },
          { id: "country", header: t("colCountry"), accessorFn: (r) => r.country ?? "", cell: ({ row: { original: r } }) => <span style={{ color: K.muted }}>{r.country ?? "—"}</span> },
          { id: "version", header: t("colVersion"), accessorKey: "version_number", meta: { align: "right" },
            cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, color: K.muted2 }}>v{r.version_number}</span> },
          { id: "status", header: t("colStatus"), accessorKey: "status", meta: { align: "right" }, cell: ({ row: { original: r } }) => <StatusChip status={r.status} /> },
          { id: "score", header: t("colScore"), accessorFn: (r) => r.quality_score ?? -1, meta: { align: "right" }, cell: ({ row: { original: r } }) => (
            <span style={{ fontWeight: 700, color: SCORE_COLOR(r.quality_score) }}>{r.quality_score != null ? r.quality_score.toFixed(1) : "—"}</span>
          ) },
          { id: "date", header: t("colDate"), accessorKey: "created_at", meta: { align: "right" },
            cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, color: K.muted2 }}>{fmtD(r.created_at)}</span> },
        ]}
      />
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
  const q = useQuery({
    queryKey: ["tenant-social-summary", tenantId],
    queryFn: () => Promise.all([
      fetch(`/api/admin/a4/content-log?tenant_id=${tenantId}&limit=500`).then(r => r.ok ? r.json() : Promise.reject()),
      fetch(`/api/admin/a4/publish-log?tenant_id=${tenantId}&limit=500`).then(r => r.ok ? r.json() : Promise.reject()),
    ]).then(([content, publish]): SocialContentSummary => ({
      pieces_written: content.total ?? 0,
      pieces_published: publish.total ?? 0,
    })),
  });
  const data = q.data ?? null;
  const loading = q.isPending;
  const error = q.isError ? t("socialLoadError") : "";

  if (loading) return <div style={{ padding: "12px 0", fontSize: 12, color: K.muted }}>{t("loading")}</div>;
  if (error || !data) return <div style={{ padding: "12px 0", fontSize: 12, color: K.danger }}>{error || t("loadFailed")}</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", gap: 14 }}>
        {([[t("piecesWritten"), data.pieces_written], [t("published"), data.pieces_published]] as [string, number][]).map(([l, v]) => (
          <div key={l} style={{ padding: "10px 14px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, minWidth: 140 }}>
            <div style={{ fontSize: 10, color: K.muted2, marginBottom: 3 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: K.ink }}>{v}</div>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: K.muted2, fontStyle: "italic" }}>
        {t("socialNoteA")}{" "}
        <a href="/admin/tenant-activity" style={{ color: K.accent }}>{t("contentTrace")}</a> {t("socialNoteB")}
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

  // C4 (S262): `?tenant=<id>&tab=cost` (AI providers › Budgets "Open the tenant") opens this tenant on that tab.
  const deepTab = useSearchParams();
  const [tab, setTab]           = useState<DTab>(() => initialDetailTab(deepTab.get("tenant"), deepTab.get("tab"), tenantId));
  // AA-1006: one react-query load for the details and the rewrite activity (was an effect that set state first).
  const detailQ = useQuery({
    queryKey: ["tenant-details", tenantId, isInternal],
    queryFn: () => {
      const p1 = fetch(`/api/admin/tenants/${tenantId}/details`)
        .then(r => r.ok ? r.json() : Promise.reject())
        .then((d: TenantDetails) => {
          const fw = d.brand_rules?.forbidden_words;
          if (fw && typeof fw === "string") {
            try { d.brand_rules.forbidden_words = JSON.parse(fw as unknown as string); }
            catch { d.brand_rules.forbidden_words = []; }
          }
          return d as TenantDetails | null;
        })
        .catch(() => null);
      const p2: Promise<RewriteActivityItem[]> = !isInternal
        ? fetch(`/api/admin/tenants/${tenantId}/rewrite-activity`)
            .then(r => r.ok ? r.json() : Promise.reject())
            .then(d => d.rewrite_activity ?? [])
            .catch(() => [])
        : Promise.resolve([]);
      return Promise.all([p1, p2]).then(([details, activity]) => ({ details, activity }));
    },
  });
  const data = detailQ.data?.details ?? null;
  const activity = detailQ.data?.activity ?? [];
  const loading = detailQ.isPending;

  if (loading) return <div style={{ padding: "12px 0", fontSize: 12, color: K.muted }}>{tr("loading")}</div>;
  if (!data)   return <div style={{ padding: "12px 0", fontSize: 12, color: K.danger }}>{tr("detailsLoadError")}</div>;

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
    <div style={{ background: K.bg, borderRadius: 10, padding: 16, border: `1px solid ${K.line}`, marginTop: 4 }}>
      <div style={{ display: "flex", gap: 20, marginBottom: 14, flexWrap: "wrap" }}>
        {([
          [tr("totalRewrites"), String(s.total_rewrites),              K.accent],
          [tr("llmCostAllTime"), `$${s.total_llm_cost_usd.toFixed(3)}`, K.body],
          [tr("apiCallsMo"), s.api_calls_this_month.toLocaleString(), K.body],
          // AA-702: this is the API-call quota; the list row shows the tour quota — label both.
          [tr("apiQuotaShort"), `${s.quota_pct}%`,                     s.quota_pct > 80 ? K.danger : K.body],
          // AA-557 I.19 — real data, already fetched (`shared.tenants.rate_limit_rpm` via
          // `api_usage.rate_limit_per_min`, same value the API Usage tab below already showed) —
          // just also surfaced at header level so an admin doesn't need to open that tab for it.
          // AA-766: plan / RPM from the list row, which reloads after a Settings save.
          [tr("rateLimit"),  tr("perMin", tenant.rate_limit_rpm),   K.body],
          [tr("publishLimit"), fmtDailyPublishLimit(tenant.daily_publish_limit, dplWords), K.body], // AA-819
          [tr("plan"),       tenant.plan_tier,                      K.body],
          [tr("memberSince"), s.member_since,                       K.muted],
        ] as [string, string, string][]).map(([l, v, c]) => (
          <div key={l} style={{ minWidth: 80 }}>
            <div style={{ fontSize: 10, color: K.muted2, marginBottom: 2 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 500, color: c }}>{v}</div>
          </div>
        ))}
      </div>
      {/* S235: the tab row scrolls sideways on phones. C4 polish (N3, S262): kit Tabs (underline), which fades the
          edge with more tabs and scrolls the active tab into view, so a deep link (`?tab=cost`) shows its tab at 390. */}
      <div style={{ borderBottom: `1px solid ${K.line}`, marginBottom: 14 }}>
        <Tabs variant="underline" gap={18} ariaLabel={tr("detailTabsLabel")} testId={`tenant-detail-tabs-${tenantId}`}
          tabs={TABS.map((x) => ({ key: x.key, label: x.label }))} active={tab} onChange={(k) => setTab(k as DTab)}
          style={{ marginBottom: -1 }} />
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
  if (state === "loading") return <div style={{ fontSize: 12, color: K.muted }}>{t("loadingBilling")}</div>;
  if (state === "error" || !b) return <div style={{ fontSize: 12, color: K.danger }}>{t("billingLoadError")}</div>;
  const num = (v: unknown) => (v == null ? 0 : Number(v));
  // AA-798 — no plan attached: show it, never an invented price/quota (and no "∞" for a null quota).
  const noPlan = b.plan_attached === false;
  const meter = (label: string, pct: number, used: number, quota: number | null) => (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
        <span style={{ color: K.ink, fontWeight: 600 }}>{label}</span>
        <span style={{ color: K.muted }}>{quota == null ? `${used} / —` : `${used} / ${quota >= 999999 ? t("unlimited") : quota} (${pct}%)`}</span>
      </div>
      <div style={{ height: 7, background: K.line2, borderRadius: 4, overflow: "hidden" }}>
        <div style={{ width: `${Math.min(100, pct)}%`, height: "100%", background: pct >= 100 ? K.danger : K.accent }} />
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
          <div key={l}><div style={{ fontSize: 10, color: K.muted2 }}>{l}</div>
            <div style={{ fontFamily: serif, fontSize: 16, color: K.body }}>{v}</div></div>
        ))}
      </div>
      {meter(t("toursQuota"), num(b.quota_tours_pct), num(b.tours_rewritten), b.tours_quota_monthly == null ? null : num(b.tours_quota_monthly))}
      {meter(t("apiCallsQuota"), num(b.quota_calls_pct), num(b.api_calls_used), b.api_calls_quota_monthly == null ? null : num(b.api_calls_quota_monthly))}
      <div style={{ fontSize: 11, color: K.muted2, marginTop: 6 }}>
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
  if (state === "loading") return <div style={{ fontSize: 12, color: K.muted }}>{t("loadingQuality")}</div>;
  if (state === "error") return <div style={{ fontSize: 12, color: K.danger }}>{t("telemetryLoadError")}</div>;
  if (!rows || rows.length === 0) return <div style={{ fontSize: 12, color: K.muted }}>{t("noGateActivity")}</div>;
  return (
    <DataTable<Record<string, number | string> & { rowId: string }>
      tableId="tenant-gate-quality"
      data={rows.map((r, i) => ({ ...r, rowId: String(i) }))}
      getRowId={(r) => r.rowId}
      searchable={false}
      paginate={false}
      compact
      columns={[
        { id: "channel", header: t("colChannel"), accessorFn: (r) => String(r.channel), cell: ({ row: { original: r } }) => <span style={{ fontWeight: 600 }}>{String(r.channel)}</span> },
        { id: "total", header: t("colTotal"), accessorFn: (r) => Number(r.total), meta: { align: "right" }, cell: ({ row: { original: r } }) => r.total },
        { id: "warn", header: t("colWarn"), accessorFn: (r) => Number(r.warn_count), meta: { align: "right" }, cell: ({ row: { original: r } }) => <span style={{ color: K.amber }}>{r.warn_count}</span> },
        { id: "held", header: t("colHeld"), accessorFn: (r) => Number(r.held_count), meta: { align: "right" }, cell: ({ row: { original: r } }) => <span style={{ color: K.amber }}>{r.held_count}</span> },
        { id: "failed", header: t("colFailed"), accessorFn: (r) => Number(r.failed_count), meta: { align: "right" },
          cell: ({ row: { original: r } }) => <span style={{ color: Number(r.failed_count) > 0 ? K.danger : K.muted }}>{r.failed_count}</span> },
        { id: "published", header: t("colPublished"), accessorFn: (r) => Number(r.publish_count), meta: { align: "right" }, cell: ({ row: { original: r } }) => <span style={{ color: K.success }}>{r.publish_count}</span> },
      ]}
    />
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
  if (state === "loading") return <div style={{ fontSize: 12, color: K.muted }}>{t("loadingAudit")}</div>;
  if (state === "error") return <div style={{ fontSize: 12, color: K.danger }}>{t("auditLoadError")}</div>;
  if (!rows || rows.length === 0) return <div style={{ fontSize: 12, color: K.muted }}>{t("noAudit")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {rows.map((e, i) => (
        <div key={i} style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: `1px solid ${K.line2}`, fontSize: 12 }}>
          <span style={{ color: K.muted2, fontFamily: mono, whiteSpace: "nowrap" }}>{formatDateTime(e.created_at)}</span>
          <span style={{ fontWeight: 600, color: K.ink }}>{e.action}</span>
          <span style={{ color: K.muted, marginLeft: "auto", fontFamily: mono, fontSize: 11 }}>{e.actor}</span>
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
      <p style={{ margin: "0 0 12px", fontSize: 13, color: K.muted }}>
        {t("offboardBody")}
      </p>
      <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{t("reasonLabel")}</label>
      <Textarea value={reason} onChange={e => setReason(e.target.value)} rows={3}
        style={{ width: "100%", boxSizing: "border-box", padding: 10, borderRadius: 8, border: `1px solid ${K.line}`,
          background: K.bg, color: K.ink, fontSize: 13, fontFamily: "inherit", resize: "vertical" }} />
      {error && (
        <div style={{ marginTop: 10, fontSize: 12, color: K.danger, display: "flex", alignItems: "center", gap: 6 }}>
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
      <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>
        {t("restoreBody")}
      </p>
      <ul style={{ margin: "0 0 4px", paddingLeft: 18, fontSize: 13, color: K.muted }}>
        <li>{t("restoreKeyA")} <strong style={{ color: K.ink }}>{t("restoreKeyB")}</strong> {t("restoreKeyC")}</li>
        <li>{t("restoreJobsA")} <strong style={{ color: K.ink }}>{t("restoreJobsB")}</strong>{t("restoreJobsC")}</li>
      </ul>
      {error && (
        <div style={{ marginTop: 10, fontSize: 12, color: K.danger, display: "flex", alignItems: "center", gap: 6 }}>
          <AlertCircle size={13} /> {error}
        </div>
      )}
    </Modal>
  );
}

// AA-1006: the tenant list is a kit DataTable (a card per tenant on a phone); the status toggle and the row actions are
// small components that keep their own state (busy, error, the offboard / restore dialogs), and Detail opens the
// tenant in a kit Drawer (was a table row expanded under the tenant).
function tenantStatus(tenant: Tenant): TenantStatus {
  return tenant.status ?? (tenant.is_active ? "active" : "suspended");
}

function tenantLifecycle(tenant: Tenant): Lifecycle {
  return tenant.lifecycle ?? {
    source_active: 0, source_superseded: 0, source_trashed: 0,
    master_active: 0, master_inactive: 0, master_trashed: 0,
  };
}

/** Status pill that toggles active <-> suspended (AA-797; offboarded is read-only). */
function TenantStatusToggle({ tenant, onChanged }: { tenant: Tenant; onChanged: () => void }) {
  const t = useT(TEN);
  const [toggling, setToggling] = useState(false);
  const [rowError, setRowError] = useState("");
  const status = tenantStatus(tenant);
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
  return (
    <div onClick={e => e.stopPropagation()}>
      <Pressable display="inline" onClick={toggle} disabled={toggling || offboarded} title={toggleTitle}
        style={{ cursor: offboarded ? "default" : "pointer" }}>
        <Badge tone={chip.tone} dot={false}>{toggling ? "…" : t(chip.label)}</Badge>
      </Pressable>
      {rowError && (
        <div style={{ marginTop: 6, fontSize: 11.5, color: K.danger, display: "flex", alignItems: "center", gap: 4 }}>
          <AlertCircle size={12} /> {rowError}
        </div>
      )}
    </div>
  );
}

/** Rotate key, offboard / restore (same slot) and Detail. */
function TenantActions({ tenant, onRotateKey, onChanged, onRestored, onOpen }: {
  tenant: Tenant;
  onRotateKey: (t: Tenant) => void;
  onChanged: () => void;
  onRestored: (r: RestoreResult) => void;
  onOpen: (tenantId: string) => void;
}) {
  const t = useT(TEN);
  const [offboarding, setOffboarding] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const offboarded = tenantStatus(tenant) === "offboarded";
  return (
    <div onClick={e => e.stopPropagation()}>
      <ButtonGroup label={t("actionsFor", tenant.name)} gap="sm">
        <Button size="sm" variant="ghost" onClick={() => onRotateKey(tenant)} disabled={offboarded}
          title={offboarded ? t("keyRevoked") : t("rotateKey")}>
          <Key size={12} /> {t("key")}
        </Button>
        {offboarded ? (
          <Button size="sm" variant="ghost" onClick={() => setRestoring(true)} title={t("restoreHint")}>
            <RotateCcw size={12} /> {t("restore")}
          </Button>
        ) : (
          <Button size="sm" variant="danger" onClick={() => setOffboarding(true)} title={t("offboardHint")}>
            <Ban size={12} /> {t("offboard")}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => onOpen(tenant.tenant_id)}>
          <ChevronRight size={12} /> {t("detail")}
        </Button>
      </ButtonGroup>
      {offboarding && (
        <OffboardModal tenant={tenant} onClose={() => setOffboarding(false)}
          onDone={() => { setOffboarding(false); onChanged(); }} />
      )}
      {restoring && (
        <RestoreModal tenant={tenant} onClose={() => setRestoring(false)}
          onDone={r => { setRestoring(false); onRestored(r); }} />
      )}
    </div>
  );
}

function TenantsTable({ tenants, onRotateKey, onChanged, onRestored, onOpen }: {
  tenants: Tenant[];
  onRotateKey: (t: Tenant) => void;
  onChanged: () => void;
  onRestored: (r: RestoreResult) => void;
  onOpen: (tenantId: string) => void;
}) {
  const t = useT(TEN);
  const columns: ColumnDef<Tenant, unknown>[] = [
    { id: "tenant", header: t("colTenant"), accessorKey: "name", cell: ({ row: { original: x } }) => (
      <div>
        <div style={{ fontWeight: 600, color: K.ink, fontSize: 13 }}>{x.name}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3, flexWrap: "wrap" }}>
          <code style={{ fontSize: 10.5, color: K.muted, fontFamily: mono }}>{x.slug}</code>
          {x.country && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 10.5, color: K.muted2 }}>
              <Globe size={10} />
              {x.country}
            </span>
          )}
        </div>
      </div>
    ) },
    { id: "plan", header: t("plan"), accessorKey: "plan_tier", cell: ({ row: { original: x } }) => (
      <div>
        <Badge tone={PLAN_BADGE[x.plan_tier] ?? "neutral"}>{x.plan_tier}</Badge>
        <div style={{ fontSize: 10.5, color: K.muted2, marginTop: 4 }}>
          {x.posts_per_week != null ? t("postsPerWeek", x.posts_per_week) : "—"}
        </div>
      </div>
    ) },
    { id: "lifecycle", header: t("colLifecycle"), enableSorting: false,
      cell: ({ row: { original: x } }) => <div style={{ minWidth: 200 }}><LifecycleBar lc={tenantLifecycle(x)} /></div> },
    { id: "sourceActive", header: t("colSourceActive"), accessorFn: (x) => tenantLifecycle(x).source_active, meta: { align: "right" },
      cell: ({ row: { original: x } }) => (
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("activeCount", tenantLifecycle(x).source_active)}</div>
          <div style={{ fontSize: 11, color: K.success }}>{t("calls", x.this_month.api_calls_used.toLocaleString())}</div>
        </div>
      ) },
    { id: "quota", header: t("colQuota"), accessorFn: (x) => x.this_month.quota_tours_pct, meta: { align: "right" },
      cell: ({ row: { original: x } }) => <span style={{ fontSize: 12, color: K.muted }}>{t("tourQuota", x.this_month.quota_tours_pct)}</span> },
    { id: "status", header: t("colStatus"), accessorFn: (x) => tenantStatus(x),
      cell: ({ row: { original: x } }) => <TenantStatusToggle tenant={x} onChanged={onChanged} /> },
    { id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
      cell: ({ row: { original: x } }) => (
        <TenantActions tenant={x} onRotateKey={onRotateKey} onChanged={onChanged} onRestored={onRestored} onOpen={onOpen} />
      ) },
  ];
  return (
    <DataTable<Tenant>
      tableId="admin-tenants"
      data={tenants}
      columns={columns}
      getRowId={(x) => x.tenant_id}
      onRowClick={(x) => onOpen(x.tenant_id)}
      searchable={false}
      paginate={false}
      emptyTitle={t("noTenants")}
    />
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
  // AA-752 — deep link from the ⌘K palette: /admin/tenants?tenant=<id> opens that tenant's 360 panel (a kit Drawer
  // since AA-1006). Read once (no setState-in-effect).
  const deepTenant = useSearchParams().get("tenant");
  const [tenants, setTenants]       = useState<Tenant[]>([]);
  const [openTenantId, setOpenTenantId] = useState<string | null>(deepTenant);
  const openTenant = tenants.find(x => x.tenant_id === openTenantId) ?? null;
  const [loading, setLoading]       = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newKey, setNewKey]         = useState<NewApiKey | null>(null);
  const [error, setError]           = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  // AA-629 Tier 2 — best-effort, tolerate failure so the whole page still loads without it.
  const [unmappedMarkets, setUnmappedMarkets] = useState<UnmappedMarketRequest[]>([]);

  // The fetches alone (state set only once an answer arrives), so the mount effect can call it; `loading` starts true.
  const fetchAll = useCallback(() => {
    // AA-797 — this page manages suspended/offboarded tenants too; other callers stay active-only.
    const tenantsP = fetch("/api/admin/tenants?include_inactive=true")
      .then(async (res) => {
        if (!res.ok) { setError(tr("loadError")); return; }
        const data = await res.json();
        setError("");
        setTenants(data.tenants ?? []);
      })
      .catch(() => setError(tr("connectionError")))
      .finally(() => setLoading(false));
    fetch("/api/admin/unmapped-market-requests")
      .then(r => r.ok ? r.json() : null)
      .then(d => setUnmappedMarkets(d?.requests ?? []))
      .catch(() => setUnmappedMarkets([]));
    return tenantsP;
  }, [tr]);
  // silent = refresh in place after a row action (keeps expanded rows; no loading screen).
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    await fetchAll();
  }, [fetchAll]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

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
            <div style={{ marginBottom: 16, padding: "10px 14px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, fontSize: 13, color: K.danger, display: "flex", alignItems: "center", gap: 8 }}>
              <AlertCircle size={14} /> {error}
            </div>
          )}

          {/* AA-629 Tier 2 — tenants waiting on a market outside DFS_LOCATION_MAP. Data-driven
              visibility for the Tier 3 decision (add the market? worth the DFS spend?) — no
              action here, just the queue. */}
          {unmappedMarkets.length > 0 && (
            <div style={{ marginBottom: 16, padding: "10px 14px", background: K.amberSoft, border: `1px solid ${K.amber}`, borderRadius: 8, fontSize: 13, color: K.ink, display: "flex", alignItems: "flex-start", gap: 8 }}>
              <Globe size={14} style={{ marginTop: 1, flexShrink: 0, color: K.amber }} />
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
              { label: tr("statusActive"),     value: String(totalActive),               color: K.success },
              { label: tr("statusSuspended"),  value: String(countBy("suspended")),      color: K.amber },
              { label: tr("statusOffboarded"), value: String(countBy("offboarded")),     color: K.danger },
              { label: tr("masterActive"),     value: totalMstrActive.toLocaleString(),  color: K.ink },
            ].map(c => (
              <Section key={c.label}>
                <Eyebrow>{c.label}</Eyebrow>
                <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 26, fontWeight: 600, color: c.color, letterSpacing: "-0.02em" }}>{c.value}</div>
              </Section>
            ))}
          </div>

          {/* AA-797 — status filter */}
          <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
            {(["all", "active", "suspended", "offboarded"] as StatusFilter[]).map(f => {
              const on = statusFilter === f;
              const n = f === "all" ? tenants.length : countBy(f);
              return (
                <ChipButton key={f} size="sm" selected={on} onClick={() => setStatusFilter(f)}>
                  {f === "all" ? tr("all") : tr(TENANT_STATUS_STYLE[f].label)} ({n})
                </ChipButton>
              );
            })}
          </div>

          {/* Table */}
          <Section padded={false} style={{ overflow: "hidden" }} bodyStyle={{ padding: loading ? 0 : 12 }}>
            {loading ? <LoadingScreen message={tr("loadingTenants")} /> : (
              <TenantsTable tenants={shown} onRotateKey={rotateKey} onChanged={() => load(true)} onRestored={restored}
                onOpen={setOpenTenantId} />
            )}
          </Section>
        </PageFrame>

      <Drawer open={openTenant != null} onClose={() => setOpenTenantId(null)} width={960}
        title={openTenant ? openTenant.name : undefined}>
        {openTenant && <TenantDetail key={openTenant.tenant_id} tenant={openTenant} onChanged={() => load(true)} />}
      </Drawer>

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={k => { setShowCreate(false); setNewKey(k); load(); }} />}
      {newKey && <ApiKeyModal keyData={newKey} onClose={() => setNewKey(null)} />}
    </>
  );
}
