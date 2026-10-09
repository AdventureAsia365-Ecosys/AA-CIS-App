"use client";
// app/admin/settings/page.tsx — AA-158 Admin Settings (4 tabs)

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { X, Plus, Save } from "lucide-react";
import BrandIdentityEditor from "../_components/BrandIdentityEditor";
import SettingsKitTab from "../_components/SettingsKitTab";
import { ModelsTab } from "./ModelsTab";
import {
  A, alpha, serif, sans, mono,
  Card, SLabel, TabBar, Badge, Btn, Spinner, LoadingScreen,
} from "../_components/adminUi";
import { PageHeader, formatDateTime, apiGet } from "../../_kit";

// ─── Types ────────────────────────────────────────────────────────────────────

interface SettingsData {
  tenant: {
    tenant_id: string;
    name: string;
    slug: string;
    plan_tier: string;
    is_active: boolean;
  };
  plan: {
    tours_quota_monthly: number;
    price_usd_monthly: number;
    trash_retention_days: number;
  };
  brand_rules: {
    system_prompt: string | null;
    style_guide: string | null;
    style_guide_full: string | null;
    forbidden_words: string[];
    version: number;
    is_active: boolean;
    updated_at: string | null;
  } | null;
  seo_config: {
    seo_provider: string;
    custom_keywords: string[];
    target_market: Record<string, unknown>;
    overrides: Record<string, unknown>;
    updated_at: string | null;
  } | null;
  pipeline_gates: {
    brand_audit_threshold: number;
    dedup_key: string;
    pipeline_flow: string[];
  };
}

// ─── Pipeline Gates Tab ───────────────────────────────────────────────────────

function PipelineGatesTab({ gates }: { gates: SettingsData["pipeline_gates"] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card>
        <SLabel>Brand Audit Threshold</SLabel>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 36, fontWeight: 600, color: A.ink, letterSpacing: "-0.03em" }}>
            {gates.brand_audit_threshold.toFixed(1)}
          </span>
          <span style={{ fontSize: 14, color: A.muted }}>/&nbsp;10</span>
          <Badge color="amber">hardcoded</Badge>
        </div>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 8, fontFamily: sans }}>
          Tours scoring below this threshold are sent to flag_fix before re-evaluation.
        </p>
      </Card>

      <Card>
        <SLabel>Deduplication Key</SLabel>
        <code style={{
          display: "block", fontFamily: mono, fontSize: 12,
          background: A.bg, padding: "10px 14px", borderRadius: 8,
          color: A.body, border: `1px solid ${A.line}`, wordBreak: "break-all",
        }}>
          {gates.dedup_key}
        </code>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 8, fontFamily: sans }}>
          Composite key used to detect duplicate source records on upload.
        </p>
      </Card>

      <Card>
        <SLabel>Pipeline Flow</SLabel>
        <div style={{ display: "flex", alignItems: "center", gap: 0, flexWrap: "wrap" }}>
          {gates.pipeline_flow.map((step, i) => (
            <div key={step} style={{ display: "flex", alignItems: "center" }}>
              <div style={{
                padding: "6px 14px", borderRadius: 20,
                background: alpha(A.accent, 8), color: A.accentDeep,
                fontSize: 12, fontWeight: 600, fontFamily: mono,
                border: `1px solid ${alpha(A.accent, 19)}`,
              }}>
                {step}
              </div>
              {i < gates.pipeline_flow.length - 1 && (
                <div style={{ padding: "0 6px", color: A.muted2, fontSize: 16 }}>→</div>
              )}
            </div>
          ))}
        </div>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 10, fontFamily: sans }}>
          brand_audit only runs on tours that pass validate (score ≥ threshold).
        </p>
      </Card>
    </div>
  );
}

// ─── SEO Config Tab ───────────────────────────────────────────────────────────

function SeoConfigTab({ seo: initialSeo }: { seo: SettingsData["seo_config"] }) {
  const [seo, setSeo] = useState(initialSeo);
  const [keywords, setKeywords] = useState<string[]>(initialSeo?.custom_keywords ?? []);
  const [kwInput, setKwInput] = useState("");
  const [targetJson, setTargetJson] = useState(
    JSON.stringify(initialSeo?.target_market ?? {}, null, 2)
  );
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  function addKeyword() {
    const kw = kwInput.trim();
    if (kw && !keywords.includes(kw)) {
      setKeywords(prev => [...prev, kw]);
    }
    setKwInput("");
  }

  function removeKeyword(kw: string) {
    setKeywords(prev => prev.filter(k => k !== kw));
  }

  async function save() {
    setError("");
    let target: Record<string, unknown> = {};
    try {
      target = JSON.parse(targetJson);
    } catch {
      setError("Target Market is not valid JSON");
      return;
    }

    setSaving(true);
    try {
      const res = await fetch("/api/admin/settings/seo", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ custom_keywords: keywords, target_market: target }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.detail ?? "Save failed");
        return;
      }
      const updated = await res.json();
      setSeo({ ...seo!, ...updated });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      setError("Network error");
    } finally {
      setSaving(false);
    }
  }

  if (!seo) {
    return (
      <Card>
        <div style={{ padding: 24, textAlign: "center", color: A.muted }}>
          No SEO config found.
        </div>
      </Card>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card>
        <SLabel>SEO Provider</SLabel>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: A.ink }}>
            {seo.seo_provider === "dataforseo" ? "DataForSEO" : seo.seo_provider}
          </span>
          <Badge color="gray">read-only</Badge>
        </div>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 6, fontFamily: sans }}>
          Seed keyword: <code style={{ fontFamily: mono }}>&quot;{"{country}"} tours&quot;</code> — per-tour country from raw_tours.
        </p>
      </Card>

      <Card>
        <SLabel>Custom Keywords</SLabel>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
          {keywords.map((kw, i) => (
            <span key={i} style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "4px 10px", borderRadius: 999,
              background: alpha(A.accent, 7), color: A.accentDeep,
              fontSize: 12, fontWeight: 500, fontFamily: mono,
            }}>
              {kw}
              <button onClick={() => removeKeyword(kw)} style={{
                background: "none", border: "none", cursor: "pointer",
                color: A.accentDeep, display: "flex", padding: 0,
              }}>
                <X size={11} />
              </button>
            </span>
          ))}
          {keywords.length === 0 && (
            <span style={{ fontSize: 13, color: A.muted2 }}>No custom keywords</span>
          )}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            value={kwInput}
            onChange={e => setKwInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && (e.preventDefault(), addKeyword())}
            placeholder="Add keyword…"
            style={{
              flex: 1, padding: "7px 12px", borderRadius: 8,
              border: `1px solid ${A.line}`, fontSize: 13,
              fontFamily: sans, outline: "none", color: A.body,
            }}
          />
          <Btn variant="secondary" size="sm" onClick={addKeyword}>
            <Plus size={13} /> Add
          </Btn>
        </div>
      </Card>

      <Card>
        <SLabel>Target Market (JSON)</SLabel>
        <textarea
          value={targetJson}
          onChange={e => setTargetJson(e.target.value)}
          rows={6}
          style={{
            width: "100%", padding: "10px 12px", borderRadius: 8,
            border: `1px solid ${A.line}`, fontSize: 12,
            fontFamily: mono, outline: "none", color: A.body,
            resize: "vertical", boxSizing: "border-box",
          }}
        />
      </Card>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <Btn variant="primary" onClick={save} disabled={saving}>
          {saving ? <Spinner size={13} /> : <Save size={13} />}
          {saving ? "Saving…" : "Save SEO Config"}
        </Btn>
        {saved && (
          <span style={{ fontSize: 12, color: A.green, fontWeight: 600 }}>
            Saved ✓
          </span>
        )}
        {error && (
          <span style={{ fontSize: 12, color: A.red }}>{error}</span>
        )}
        {seo.updated_at && (
          <span style={{ fontSize: 11, color: A.muted2, marginLeft: "auto" }}>
            Last saved: {formatDateTime(seo.updated_at)}
          </span>
        )}
      </div>
    </div>
  );
}


// ─── Tenant Info Tab ──────────────────────────────────────────────────────────

function TenantInfoTab({
  tenant, plan,
}: {
  tenant: SettingsData["tenant"];
  plan: SettingsData["plan"];
}) {
  const PLAN_COLOR: Record<string, "blue" | "green" | "gold" | "purple" | "gray"> = {
    internal:   "gold",
    starter:    "blue",
    growth:     "green",
    business:   "purple",
    enterprise: "purple",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
          <div>
            <SLabel>Tenant Name</SLabel>
            <div style={{ fontSize: 16, fontWeight: 600, color: A.ink, fontFamily: serif }}>
              {tenant.name}
            </div>
          </div>
          <div>
            <SLabel>Slug</SLabel>
            <code style={{ fontSize: 13, fontFamily: mono, color: A.body }}>{tenant.slug}</code>
          </div>
          <div>
            <SLabel>Plan Tier</SLabel>
            <Badge color={PLAN_COLOR[tenant.plan_tier] ?? "gray"}>
              {tenant.plan_tier}
            </Badge>
          </div>
          <div>
            <SLabel>Status</SLabel>
            <Badge color={tenant.is_active ? "green" : "red"}>
              {tenant.is_active ? "active" : "inactive"}
            </Badge>
          </div>
          <div>
            <SLabel>Tenant ID</SLabel>
            <code style={{ fontSize: 11, fontFamily: mono, color: A.muted2, wordBreak: "break-all" }}>
              {tenant.tenant_id}
            </code>
          </div>
        </div>
      </Card>

      <Card>
        <SLabel>Plan Quota</SLabel>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 16 }}>
          <div>
            <div style={{ fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.1em" }}>
              Monthly Tours
            </div>
            <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
              {plan.tours_quota_monthly > 0 ? plan.tours_quota_monthly.toLocaleString() : "∞"}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.1em" }}>
              Monthly Price
            </div>
            <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
              {plan.price_usd_monthly > 0 ? `$${plan.price_usd_monthly.toFixed(0)}` : "—"}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.1em" }}>
              Trash Retention
            </div>
            <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
              {plan.trash_retention_days}d
            </div>
            <div style={{ fontSize: 10, color: A.muted2, marginTop: 2 }}>system default</div>
          </div>
        </div>
      </Card>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

// AA-663 — "Brand Rules" (a read-only summary of the active brand) is replaced by the full Brand
// Identity editor, which used to be its own top-level page at /admin/brand (now a redirect to
// ?tab=brand). `?tab=` selects the initial tab so old links land in the right place.
const TABS = [
  { key: "pipeline",  label: "Pipeline Gates" },
  { key: "brand",     label: "Brand Identity" },
  { key: "seo",       label: "SEO Config" },
  { key: "models",    label: "LLM Models" },
  { key: "tenant",    label: "Tenant Info" },
  // AA-662 — UI-kit living reference (design system), moved here from a standalone /admin/kit-demo
  // page so it lives under Settings as a system/reference tab rather than a top-level nav item.
  { key: "ui-kit",    label: "UI Kit" },
];

export default function SettingsPage() {
  // useSearchParams() needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <SettingsPageInner />
    </Suspense>
  );
}

function SettingsPageInner() {
  const wanted = useSearchParams().get("tab");
  const [tab, setTab] = useState(() => (TABS.some(t => t.key === wanted) ? wanted! : "pipeline"));

  // AA-686 — react-query instead of a hand-rolled useState/useEffect fetch (the React Compiler
  // flags setState inside an effect; the kit standard is useQuery).
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: () => apiGet<SettingsData>("/api/admin/settings", { admin: true }),
  });

  return (
      <main className="aa-admin-main" style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        {/* Header */}
        <PageHeader
          title="Settings"
          description="Pipeline gates, brand identity, SEO config, models per stage, and tenant info for aa_internal"
        />

        {isLoading && <LoadingScreen msg="Loading settings…" />}

        {!isLoading && isError && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>Failed to load settings</div>
            <Btn variant="secondary" onClick={() => refetch()}>Retry</Btn>
          </Card>
        )}

        {!isLoading && !isError && data && (
          <>
            {/* AA-601 — "content rendered" signal for the UI smoke: present only once the settings
                payload has loaded, never on the LoadingScreen. */}
            <div data-testid="admin-content-ready" style={{ marginBottom: 24 }}>
              <TabBar tabs={TABS} active={tab} onChange={setTab} />
            </div>

            {tab === "pipeline" && (
              <PipelineGatesTab gates={data.pipeline_gates} />
            )}
            {tab === "brand" && <BrandIdentityEditor />}
            {tab === "seo" && (
              <SeoConfigTab seo={data.seo_config} />
            )}
            {tab === "models" && <ModelsTab />}
            {tab === "tenant" && (
              <TenantInfoTab tenant={data.tenant} plan={data.plan} />
            )}
            {tab === "ui-kit" && <SettingsKitTab />}
          </>
        )}
      </main>
  );
}
