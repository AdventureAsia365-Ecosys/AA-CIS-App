"use client";
// app/admin/settings/page.tsx — AA-158 Admin Settings (4 tabs)
// AA-994: text in the UI language (messages.ts); gate keys, pipeline steps, keywords and tenant values stay as sent.

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { X, Plus, Save } from "lucide-react";
import BrandIdentityEditor from "../_components/BrandIdentityEditor";
import SettingsKitTab from "../_components/SettingsKitTab";
import { ModelsTab } from "./ModelsTab";
import { JevCreditTab } from "./JevCreditTab";
import { A, alpha, serif, sans, mono, Card, SLabel, Badge, Spinner, LoadingScreen } from "../_components/adminUi";
import { Button, Tabs, PageFrame, PageHeader, apiGet, useAdminIdentity, useFormat, useT } from "../../_kit";
import { can, staffErrorMessage, type Capability } from "@/lib/rbac";
import { RBAC_MESSAGES } from "@/lib/rbac.messages";
import { SETTINGS } from "./messages";

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
  const t = useT(SETTINGS);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card>
        <SLabel>{t("brandThreshold")}</SLabel>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 36, fontWeight: 600, color: A.ink, letterSpacing: "-0.03em" }}>
            {gates.brand_audit_threshold.toFixed(1)}
          </span>
          <span style={{ fontSize: 14, color: A.muted }}>/&nbsp;10</span>
          <Badge color="amber">{t("hardcoded")}</Badge>
        </div>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 8, fontFamily: sans }}>
          {t("thresholdHelp")}
        </p>
      </Card>

      <Card>
        <SLabel>{t("dedupKey")}</SLabel>
        <code style={{
          display: "block", fontFamily: mono, fontSize: 12,
          background: A.bg, padding: "10px 14px", borderRadius: 8,
          color: A.body, border: `1px solid ${A.line}`, wordBreak: "break-all",
        }}>
          {gates.dedup_key}
        </code>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 8, fontFamily: sans }}>
          {t("dedupHelp")}
        </p>
      </Card>

      <Card>
        <SLabel>{t("pipelineFlow")}</SLabel>
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
          {t("flowHelp")}
        </p>
      </Card>
    </div>
  );
}

// ─── SEO Config Tab ───────────────────────────────────────────────────────────

function SeoConfigTab({ seo: initialSeo, canEdit }: { seo: SettingsData["seo_config"]; canEdit: boolean }) {
  const t = useT(SETTINGS);
  const tr = useT(RBAC_MESSAGES);
  const { formatDateTime } = useFormat();
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
      setError(t("invalidJson"));
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
        setError(staffErrorMessage(res.status, d.detail, t("saveFailed"), tr("adminOnly")));
        return;
      }
      const updated = await res.json();
      setSeo({ ...seo!, ...updated });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      setError(t("networkError"));
    } finally {
      setSaving(false);
    }
  }

  if (!seo) {
    return (
      <Card>
        <div style={{ padding: 24, textAlign: "center", color: A.muted }}>
          {t("noSeo")}
        </div>
      </Card>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card>
        <SLabel>{t("seoProvider")}</SLabel>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: A.ink }}>
            {seo.seo_provider === "dataforseo" ? "DataForSEO" : seo.seo_provider}
          </span>
          <Badge color="gray">{t("readOnly")}</Badge>
        </div>
        <p style={{ fontSize: 12, color: A.muted2, marginTop: 6, fontFamily: sans }}>
          {t("seedKeyword")} <code style={{ fontFamily: mono }}>{t("seedPattern")}</code> {t("seedHelp")}
        </p>
      </Card>

      <Card>
        <SLabel>{t("customKeywords")}</SLabel>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
          {keywords.map((kw, i) => (
            <span key={i} style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "4px 10px", borderRadius: 999,
              background: alpha(A.accent, 7), color: A.accentDeep,
              fontSize: 12, fontWeight: 500, fontFamily: mono,
            }}>
              {kw}
              {canEdit && (
                <button onClick={() => removeKeyword(kw)} style={{
                  background: "none", border: "none", cursor: "pointer",
                  color: A.accentDeep, display: "flex", padding: 0,
                }}>
                  <X size={11} />
                </button>
              )}
            </span>
          ))}
          {keywords.length === 0 && (
            <span style={{ fontSize: 13, color: A.muted2 }}>{t("noKeywords")}</span>
          )}
        </div>
        {canEdit && <div style={{ display: "flex", gap: 8 }}>
          <input
            value={kwInput}
            onChange={e => setKwInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && (e.preventDefault(), addKeyword())}
            placeholder={t("addKeyword")}
            style={{
              flex: 1, padding: "7px 12px", borderRadius: 8,
              border: `1px solid ${A.line}`, fontSize: 13,
              fontFamily: sans, outline: "none", color: A.body,
            }}
          />
          <Button variant="secondary" size="sm" onClick={addKeyword}>
            <Plus size={13} /> {t("add")}
          </Button>
        </div>}
      </Card>

      <Card>
        <SLabel>{t("targetMarket")}</SLabel>
        <textarea
          value={targetJson}
          onChange={e => setTargetJson(e.target.value)}
          readOnly={!canEdit}
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
        {canEdit ? (
          <Button variant="primary" onClick={save} disabled={saving}>
            {saving ? <Spinner size={13} /> : <Save size={13} />}
            {saving ? t("saving") : t("saveSeo")}
          </Button>
        ) : (
          <span data-testid="seo-read-only" style={{ fontSize: 12, color: A.muted }}>
            {t("seoReadOnly")}
          </span>
        )}
        {saved && (
          <span style={{ fontSize: 12, color: A.green, fontWeight: 600 }}>
            {t("saved")}
          </span>
        )}
        {error && (
          <span style={{ fontSize: 12, color: A.red }}>{error}</span>
        )}
        {seo.updated_at && (
          <span style={{ fontSize: 11, color: A.muted2, marginLeft: "auto" }}>
            {t("lastSaved", formatDateTime(seo.updated_at))}
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
  const t = useT(SETTINGS);
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
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))", gap: 20 }}>
          <div>
            <SLabel>{t("tenantName")}</SLabel>
            <div style={{ fontSize: 16, fontWeight: 600, color: A.ink, fontFamily: serif }}>
              {tenant.name}
            </div>
          </div>
          <div>
            <SLabel>{t("slug")}</SLabel>
            <code style={{ fontSize: 13, fontFamily: mono, color: A.body }}>{tenant.slug}</code>
          </div>
          <div>
            <SLabel>{t("planTier")}</SLabel>
            <Badge color={PLAN_COLOR[tenant.plan_tier] ?? "gray"}>
              {tenant.plan_tier}
            </Badge>
          </div>
          <div>
            <SLabel>{t("status")}</SLabel>
            <Badge color={tenant.is_active ? "green" : "red"}>
              {tenant.is_active ? t("active") : t("inactive")}
            </Badge>
          </div>
          <div>
            <SLabel>{t("tenantId")}</SLabel>
            <code style={{ fontSize: 11, fontFamily: mono, color: A.muted2, wordBreak: "break-all" }}>
              {tenant.tenant_id}
            </code>
          </div>
        </div>
      </Card>

      <Card>
        <SLabel>{t("planQuota")}</SLabel>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 16 }}>
          <div>
            <div style={{ fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.1em" }}>
              {t("monthlyTours")}
            </div>
            <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
              {plan.tours_quota_monthly > 0 ? plan.tours_quota_monthly.toLocaleString() : "∞"}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.1em" }}>
              {t("monthlyPrice")}
            </div>
            <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
              {plan.price_usd_monthly > 0 ? `$${plan.price_usd_monthly.toFixed(0)}` : "—"}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.1em" }}>
              {t("trashRetention")}
            </div>
            <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
              {t("days", plan.trash_retention_days)}
            </div>
            <div style={{ fontSize: 10, color: A.muted2, marginTop: 2 }}>{t("systemDefault")}</div>
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
// AA-882 — `needs`: a tab whose reads the backend keeps admin-only (LLM Models: model routes, catalog
// and the shadow cost report; Jev Credit: the top-up ledger) is not shown to a role without it.
// `label` is a key of messages.ts SETTINGS (AA-994).
type TabLabel = "tabPipeline" | "tabBrand" | "tabSeo" | "tabModels" | "tabJevCredit" | "tabTenant" | "tabUiKit";
const TABS: { key: string; label: TabLabel; needs?: Capability }[] = [
  { key: "pipeline",  label: "tabPipeline" },
  { key: "brand",     label: "tabBrand" },
  { key: "seo",       label: "tabSeo" },
  { key: "models",    label: "tabModels", needs: "ai.admin" },
  { key: "jev-credit", label: "tabJevCredit", needs: "spend.read" },
  { key: "tenant",    label: "tabTenant" },
  // AA-662 — UI-kit living reference (design system), moved here from a standalone /admin/kit-demo
  // page so it lives under Settings as a system/reference tab rather than a top-level nav item.
  { key: "ui-kit",    label: "tabUiKit" },
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
  const t = useT(SETTINGS);
  const wanted = useSearchParams().get("tab");
  const { role } = useAdminIdentity();
  const tabs = TABS.filter(x => !x.needs || can(role, x.needs)).map(x => ({ key: x.key, label: t(x.label) }));
  const [picked, setTab] = useState(() => (TABS.some(x => x.key === wanted) ? wanted! : "pipeline"));
  // A deep link to a tab the role does not see falls back to the first tab (derived, no effect).
  const tab = tabs.some(x => x.key === picked) ? picked : "pipeline";

  // AA-686 — react-query instead of a hand-rolled useState/useEffect fetch (the React Compiler
  // flags setState inside an effect; the kit standard is useQuery).
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: () => apiGet<SettingsData>("/api/admin/settings", { admin: true }),
  });

  return (
      <PageFrame>
        {/* Header */}
        <PageHeader
          title={t("title")}
          description={t("description")}
        />

        {isLoading && <LoadingScreen msg={t("loading")} />}

        {!isLoading && isError && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>{t("loadError")}</div>
            <Button variant="secondary" onClick={() => refetch()}>{t("retry")}</Button>
          </Card>
        )}

        {!isLoading && !isError && data && (
          <>
            {/* AA-601 — "content rendered" signal for the UI smoke: present only once the settings
                payload has loaded, never on the LoadingScreen. */}
            <div data-testid="admin-content-ready" style={{ marginBottom: 24 }}>
              <Tabs tabs={tabs} active={tab} onChange={setTab} />
            </div>

            {tab === "pipeline" && (
              <PipelineGatesTab gates={data.pipeline_gates} />
            )}
            {tab === "brand" && <BrandIdentityEditor />}
            {tab === "seo" && (
              <SeoConfigTab seo={data.seo_config} canEdit={can(role, "content.settings.write")} />
            )}
            {tab === "models" && <ModelsTab />}
            {tab === "jev-credit" && <JevCreditTab />}
            {tab === "tenant" && (
              <TenantInfoTab tenant={data.tenant} plan={data.plan} />
            )}
            {tab === "ui-kit" && <SettingsKitTab />}
          </>
        )}
      </PageFrame>
  );
}
