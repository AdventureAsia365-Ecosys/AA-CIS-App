"use client";
// app/admin/settings/page.tsx — AA-158 Admin Settings; AA-1039 R6 rest: Catalogue settings (Pipeline gates, AA house
// style, SEO config) of AA Catalogue
// AA-994: text in the UI language (messages.ts); gate keys, pipeline steps, keywords and tenant values stay as sent.
// AA-804 UI part 2: the LLM Models and Jev Credit tabs moved to AI providers (/admin/ai-gateway). Their old links
// (`?tab=models`, `?tab=jev-credit`: bookmarks, alert e-mails already sent) are replaced in the browser by the new
// page (lib/aiGateway RETIRED_SETTINGS_TABS), so they never fall back to Pipeline Gates without a word.

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { X, Plus, Save } from "lucide-react";
import BrandIdentityEditor from "../_components/BrandIdentityEditor";
import {
  Button, Tabs, PageFrame, PageHeader, apiGet, useAdminIdentity, useFormat, useT, Badge, Eyebrow, K, LoadingScreen,
  Section, Spinner, alpha, mono, sans,
  Input, Textarea,
  IconButton,
} from "../../_kit";
import { RETIRED_SETTINGS_TABS, retiredTabTarget } from "@/lib/aiGateway";
import { can, staffErrorMessage, type Capability } from "@/lib/rbac";
import { RBAC_MESSAGES } from "@/lib/rbac.messages";
import { SETTINGS } from "./messages";

// ─── Types ────────────────────────────────────────────────────────────────────

// GET /admin/settings also sends `tenant` and `plan` (the aa_internal row); this page no longer shows them.
interface SettingsData {
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
    /** S280: the Platform › Settings key that holds it (absent from an older API). */
    brand_audit_threshold_setting?: string | null;
    dedup_key: string;
    pipeline_flow: string[];
  };
}

// ─── Pipeline Gates Tab ───────────────────────────────────────────────────────

/** Platform › Settings opened on the CIS tab, searched for the key when the API names it. */
function thresholdSettingHref(key: string | null | undefined): string {
  const qs = new URLSearchParams({ app: "cis" });
  if (key) qs.set("key", key);
  return `/admin/platform/settings?${qs.toString()}`;
}

function PipelineGatesTab({ gates }: { gates: SettingsData["pipeline_gates"] }) {
  const t = useT(SETTINGS);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Section>
        <Eyebrow>{t("brandThreshold")}</Eyebrow>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 36, fontWeight: 600, color: K.ink, letterSpacing: "-0.03em" }}>
            {gates.brand_audit_threshold.toFixed(1)}
          </span>
          <span style={{ fontSize: 14, color: K.muted }}>/&nbsp;10</span>
          {/* S280: a registry setting since S279 L2 (it said "hardcoded"): changed on Platform › Settings. */}
          <Badge tone="info" dot={false}>{t("settingBadge")}</Badge>
        </div>
        <p style={{ fontSize: 12, color: K.muted2, marginTop: 8, fontFamily: sans }}>
          {t("thresholdHelp")}{" "}
          <a href={thresholdSettingHref(gates.brand_audit_threshold_setting)}
            style={{ color: K.accentDeep, fontWeight: 600 }} data-testid="settings-threshold-link">
            {t("changeOnPlatformSettings")}</a>
        </p>
      </Section>

      <Section>
        <Eyebrow>{t("dedupKey")}</Eyebrow>
        <code style={{
          display: "block", fontFamily: mono, fontSize: 12,
          background: K.bg, padding: "10px 14px", borderRadius: 8,
          color: K.body, border: `1px solid ${K.line}`, wordBreak: "break-all",
        }}>
          {gates.dedup_key}
        </code>
        <p style={{ fontSize: 12, color: K.muted2, marginTop: 8, fontFamily: sans }}>
          {t("dedupHelp")}
        </p>
      </Section>

      <Section>
        <Eyebrow>{t("pipelineFlow")}</Eyebrow>
        <div style={{ display: "flex", alignItems: "center", gap: 0, flexWrap: "wrap" }}>
          {gates.pipeline_flow.map((step, i) => (
            <div key={step} style={{ display: "flex", alignItems: "center" }}>
              <div style={{
                padding: "6px 14px", borderRadius: 20,
                background: alpha(K.accent, 8), color: K.accentDeep,
                fontSize: 12, fontWeight: 600, fontFamily: mono,
                border: `1px solid ${alpha(K.accent, 19)}`,
              }}>
                {step}
              </div>
              {i < gates.pipeline_flow.length - 1 && (
                <div style={{ padding: "0 6px", color: K.muted2, fontSize: 16 }}>→</div>
              )}
            </div>
          ))}
        </div>
        <p style={{ fontSize: 12, color: K.muted2, marginTop: 10, fontFamily: sans }}>
          {t("flowHelp")}
        </p>
      </Section>
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
      <Section>
        <div style={{ padding: 24, textAlign: "center", color: K.muted }}>
          {t("noSeo")}
        </div>
      </Section>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Section>
        <Eyebrow>{t("seoProvider")}</Eyebrow>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: K.ink }}>
            {seo.seo_provider === "dataforseo" ? "DataForSEO" : seo.seo_provider}
          </span>
          <Badge tone="neutral">{t("readOnly")}</Badge>
        </div>
        <p style={{ fontSize: 12, color: K.muted2, marginTop: 6, fontFamily: sans }}>
          {t("seedKeyword")} <code style={{ fontFamily: mono }}>{t("seedPattern")}</code> {t("seedHelp")}
        </p>
      </Section>

      <Section>
        <Eyebrow>{t("customKeywords")}</Eyebrow>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
          {keywords.map((kw, i) => (
            <span key={i} style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "4px 10px", borderRadius: 999,
              background: alpha(K.accent, 7), color: K.accentDeep,
              fontSize: 12, fontWeight: 500, fontFamily: mono,
            }}>
              {kw}
              {canEdit && (
                <IconButton variant="plain" size="xs" label={t("removeKeyword", kw)} onClick={() => removeKeyword(kw)}
                  style={{ color: K.accentDeep, width: 16, height: 16 }}>
                  <X size={11} />
                </IconButton>
              )}
            </span>
          ))}
          {keywords.length === 0 && (
            <span style={{ fontSize: 13, color: K.muted2 }}>{t("noKeywords")}</span>
          )}
        </div>
        {canEdit && <div style={{ display: "flex", gap: 8 }}>
          <Input
            value={kwInput}
            onChange={e => setKwInput(e.target.value)}
            onKeyDown={e => e.key === "Enter" && (e.preventDefault(), addKeyword())}
            placeholder={t("addKeyword")}
            style={{
              flex: 1, padding: "7px 12px", borderRadius: 8,
              border: `1px solid ${K.line}`, fontSize: 13,
              fontFamily: sans, outline: "none", color: K.body,
            }}
          />
          <Button variant="secondary" size="sm" onClick={addKeyword}>
            <Plus size={13} /> {t("add")}
          </Button>
        </div>}
      </Section>

      <Section>
        <Eyebrow>{t("targetMarket")}</Eyebrow>
        <Textarea
          value={targetJson}
          onChange={e => setTargetJson(e.target.value)}
          readOnly={!canEdit}
          rows={6}
          style={{
            width: "100%", padding: "10px 12px", borderRadius: 8,
            border: `1px solid ${K.line}`, fontSize: 12,
            fontFamily: mono, outline: "none", color: K.body,
            resize: "vertical", boxSizing: "border-box",
          }}
        />
      </Section>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {canEdit ? (
          <Button variant="primary" onClick={save} disabled={saving}>
            {saving ? <Spinner size={13} /> : <Save size={13} />}
            {saving ? t("saving") : t("saveSeo")}
          </Button>
        ) : (
          <span data-testid="seo-read-only" style={{ fontSize: 12, color: K.muted }}>
            {t("seoReadOnly")}
          </span>
        )}
        {saved && (
          <span style={{ fontSize: 12, color: K.success, fontWeight: 600 }}>
            {t("saved")}
          </span>
        )}
        {error && (
          <span style={{ fontSize: 12, color: K.danger }}>{error}</span>
        )}
        {seo.updated_at && (
          <span style={{ fontSize: 11, color: K.muted2, marginLeft: "auto" }}>
            {t("lastSaved", formatDateTime(seo.updated_at))}
          </span>
        )}
      </div>
    </div>
  );
}


// ─── Main Page ────────────────────────────────────────────────────────────────

// AA-663 — "Brand Rules" (a read-only summary of the active brand) is replaced by the full Brand
// Identity editor, which used to be its own top-level page at /admin/brand (now a redirect to
// ?tab=brand). `?tab=` selects the initial tab so old links land in the right place.
// AA-882 — `needs`: a tab whose reads the backend keeps admin-only is not shown to a role without it (none today:
// the two such tabs, LLM Models and Jev Credit, moved to AI providers, an admin-only page).
// `label` is a key of messages.ts SETTINGS (AA-994).
// AA-1039 R6 rest (IA §6.2: AA Catalogue › Catalogue settings — the gate and the "AA house style"): the page holds the
// catalogue's own settings only. Tenant Info (the aa_internal tenant row and plan) is that tenant's Tenant 360
// (Settings tab) and the UI kit reference is /admin/platform/ui-kit; `?tab=tenant` / `?tab=ui-kit` answer 307 there
// (lib/adminIa.ts ADMIN_ROUTE_MOVES).
type TabLabel = "tabPipeline" | "tabBrand" | "tabSeo";
const TABS: { key: string; label: TabLabel; needs?: Capability }[] = [
  { key: "pipeline",  label: "tabPipeline" },
  { key: "brand",     label: "tabBrand" },
  { key: "seo",       label: "tabSeo" },
];

export default function SettingsPage() {
  // useSearchParams() needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <SettingsRouter />
    </Suspense>
  );
}

/** A retired tab's link goes to its new page (replace: the old URL leaves no history entry); any other renders. */
function SettingsRouter() {
  const router = useRouter();
  const target = retiredTabTarget(RETIRED_SETTINGS_TABS, useSearchParams().get("tab"));
  useEffect(() => {
    if (target) router.replace(target);
  }, [target, router]);
  return target ? null : <SettingsPageInner />;
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

        {isLoading && <LoadingScreen message={t("loading")} />}

        {!isLoading && isError && (
          <Section bodyStyle={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: K.danger, marginBottom: 12 }}>{t("loadError")}</div>
            <Button variant="secondary" onClick={() => refetch()}>{t("retry")}</Button>
          </Section>
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
          </>
        )}
      </PageFrame>
  );
}
