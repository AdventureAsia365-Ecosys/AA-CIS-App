"use client";
// app/admin/tenants/_components/TenantDetail.tsx
// AA-1039 R6 rest (root ADR 0019 d13, IA §6.2): Tenant 360 is a route, /admin/tenants/<id> (../[id]/page.tsx), not a
// drawer of the Directory. The panel and its tab bodies moved here unchanged from ../page.tsx (AA-666, AA-766,
// AA-1028, AA-1031, AA-1035); only the deep-link tab reads `?tab=` alone now (the tenant is the route's).

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";
import {
  Button, Tabs, useFormat, useLang, useT, Input, Textarea,
  K, serif, mono, sans, Badge,
  DataTable, type ColumnDef,
  type Tone,
  useAdminIdentity,
} from "../../../_kit";
import { can, isStaffRole } from "@/lib/rbac";
import { Tenant360Settings } from "./Tenant360Settings";
import { Tenant360Cost } from "./Tenant360Cost";
import { Tenant360Quality } from "./Tenant360Quality";
import { Tenant360Markets } from "./Tenant360Markets";
import { Tenant360VoiceMemory } from "./Tenant360VoiceMemory";
import { Tenant360Members } from "./Tenant360Members";
import { Tenant360Subscription } from "./Tenant360Subscription";
import { Tenant360BookingBilling } from "./Tenant360BookingBilling";
import { ViewAsStart } from "./ViewAsStart";
import { fmtDailyPublishLimit } from "./tenant360";
import { T360_WORDS_MESSAGES } from "./tenant360.messages";
import type { Tenant } from "./tenantRow";
import { SCORE_BANDS } from "@/lib/quality-registry.generated";
import { TEN } from "../messages";

// ─── Detail types (unchanged from previous) ───────────────────────────────────

interface RewrittenTour {
  version_id: string; tour_name: string; country: string | null;
  quality_score: number | null; version_number: number;
  status: string; created_at: string;
}
interface PipelineRun {
  run_id: string; started_at: string; tours_processed: number;
  tours_passed: number; llm_model: string | null;
  llm_cost_usd: number | null; status: string;   // AA-1035: null for support
}
interface TenantDetails {
  summary: {
    total_rewrites: number; total_llm_cost_usd: number | null;   // AA-1035: null for support
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
            cell: ({ row: { original: r } }) => <span style={{ color: K.accent }}>{r.llm_cost_usd == null ? "—" : `$${r.llm_cost_usd.toFixed(4)}`}</span> },
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
        <a href="/admin/tenant-qa/trace" style={{ color: K.accent }}>{t("contentTrace")}</a> {t("socialNoteB")}
      </div>
    </div>
  );
}

// ─── Tenant Detail Panel ──────────────────────────────────────────────────────

// AA-666: Tenant 360 — the detail panel is expanded IN PLACE (inline, under the list row), not a
// separate page. Added tabs: Usage & Billing, Quality (gate telemetry), Audit, Settings.
// AA-766: + Cost and Markets; Quality and Settings rebuilt (./_components).
export type DTab = "tours" | "pipeline" | "activity" | "api" | "brand" | "social_content"
          | "billing" | "subscription" | "cost" | "quality" | "markets" | "members" | "audit" | "settings";

export const DETAIL_TABS: readonly DTab[] = ["tours", "pipeline", "activity", "api", "brand", "social_content", "billing",
  "subscription", "cost", "quality", "markets", "members", "audit", "settings"];

/** The tab a link asks for (`/admin/tenants/<id>?tab=cost`); "tours" otherwise. The tenant is the route's own. */
export function initialDetailTab(linkTab: string | null): DTab {
  return (DETAIL_TABS as readonly string[]).includes(linkTab ?? "") ? (linkTab as DTab) : "tours";
}

export function TenantDetail({ tenant, onChanged }: {
  tenant: Tenant; onChanged: () => void;
}) {
  const tr = useT(TEN);
  const dplWords = T360_WORDS_MESSAGES[useLang()];
  const tenantId = tenant.tenant_id;
  const isInternal = tenant.plan_tier === "internal";
  // AA-1035: support reaches the tenant without its plans, usage, cost or Tenant QA (the backend refuses them); hide
  // those tabs for a known role that lacks the read. UI hint only.
  const role = useAdminIdentity().role;
  const known = isStaffRole(role);
  const seesAccount = !known || can(role, "tenant.read") || can(role, "tenant.book.account.read");
  const seesBilling = seesAccount || can(role, "billing.read");
  // AA-1035 (S267 fix pass): finance reaches Tenant 360 for its Billing / Subscription tabs only (matrix: "R (Billing
  // tab)"); /details answers it the header without tours, runs or brand text, and every other tab would be refused.
  const billingOnly = known && !can(role, "tenant.read") && !can(role, "tenant.book.read") && can(role, "billing.read");
  // AA-1035 R2 slice 2: read-only "view as partner" (tenant.view_as: admin, customer_success, support; the backend
  // re-checks the scope). Not for the AA internal tenant or an offboarded one (the API answers 409).
  const viewAs = can(role, "tenant.view_as") && !isInternal && (tenant.status ?? "active") !== "offboarded";

  // C4 (S262): `?tab=cost` (AI gateway › Budgets "Open the tenant") opens this tenant on that tab. AA-1039 R6 rest: the
  // tenant is the route's (/admin/tenants/<id>), so the tab no longer checks a `tenant` query value.
  const deepTab = useSearchParams();
  const [picked, setTab]        = useState<DTab>(() => initialDetailTab(deepTab.get("tab")));
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
  const ALL_TABS: { key: DTab; label: string }[] = [
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
    ...(seesBilling ? [{ key: "billing" as DTab,  label: tr("tabBilling") },
      // AA-1028 R1 slice 3: plans, add-ons, entitlements with their source, overrides (root ADR 0019 d1 / d2)
      { key: "subscription" as DTab, label: tr("tabSubscription") }] : []),
    ...(seesAccount ? [{ key: "cost" as DTab,     label: tr("tabCost") },
      { key: "quality" as DTab,  label: tr("tabQuality") }] : []),
    { key: "markets" as DTab,  label: tr("tabMarkets") },
    // AA-1031 (S264): Content studio members (invite, send again, role, remove) — root ADR 0010a decision 4.
    { key: "members" as DTab,  label: tr("tabMembers") },
    { key: "audit" as DTab,    label: tr("tabAudit") },
    { key: "settings" as DTab, label: tr("tabSettings") },
  ];
  const TABS = billingOnly ? ALL_TABS.filter((x) => x.key === "billing" || x.key === "subscription") : ALL_TABS;
  // A deep link or the default tab may name a tab this role does not get: open its first tab instead.
  const tab: DTab = TABS.some((x) => x.key === picked) ? picked : TABS[0].key;

  return (
    <div style={{ background: K.bg, borderRadius: 10, padding: 16, border: `1px solid ${K.line}`, marginTop: 4 }}>
      <div style={{ display: "flex", gap: 20, marginBottom: 14, flexWrap: "wrap" }}>
        {([
          [tr("totalRewrites"), String(s.total_rewrites),              K.accent],
          [tr("llmCostAllTime"), s.total_llm_cost_usd == null ? "—" : `$${s.total_llm_cost_usd.toFixed(3)}`, K.body],
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
      {viewAs && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
          <ViewAsStart tenantId={tenantId} tenantName={tenant.name} />
        </div>
      )}
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
      {/* AA-1037 (root ADR 0019 d9): Booking's agency billing status above the AA-666 meters (not for AA itself) */}
      {tab === "billing" && !isInternal && <Tenant360BookingBilling tenantId={tenantId} />}
      {tab === "billing"  && <BillingTabContent  tenantId={tenantId} />}
      {tab === "subscription" && <Tenant360Subscription tenantId={tenantId} />}
      {tab === "cost"     && <Tenant360Cost      tenantId={tenantId} />}
      {tab === "quality"  && <Tenant360Quality   tenantId={tenantId} byChannel={<ChannelGateTelemetry tenantId={tenantId} />} />}
      {tab === "quality"  && <Tenant360VoiceMemory tenantId={tenantId} />}
      {tab === "markets"  && <Tenant360Markets   tenantId={tenantId} />}
      {tab === "members"  && <Tenant360Members   tenantId={tenantId} />}
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
