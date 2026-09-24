"use client";
// app/admin/settings/page.tsx — AA-158 Admin Settings (4 tabs)

import { useState, useEffect, useCallback } from "react";
import { Settings, ChevronDown, ChevronUp, X, Plus, Save, AlertTriangle } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import {
  A, serif, sans, mono,
  Card, SLabel, TabBar, Badge, Btn, Spinner, LoadingScreen,
} from "../_components/adminUi";

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
          <span style={{ fontFamily: serif, fontSize: 36, fontWeight: 500, color: A.ink, letterSpacing: "-0.03em" }}>
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
                background: `${A.accent}15`, color: A.accentDeep,
                fontSize: 12, fontWeight: 600, fontFamily: mono,
                border: `1px solid ${A.accent}30`,
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

// ─── Brand Rules Tab ──────────────────────────────────────────────────────────

function BrandRulesTab({ brand }: { brand: SettingsData["brand_rules"] }) {
  const [expanded, setExpanded] = useState(false);

  if (!brand) {
    return (
      <Card>
        <div style={{ padding: 24, textAlign: "center", color: A.muted }}>
          No active brand rules found.
        </div>
      </Card>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Header */}
      <Card>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <SLabel style={{ marginBottom: 0 }}>Active Brand Identity</SLabel>
          <Badge color="blue">v{brand.version}</Badge>
          {brand.is_active && <Badge color="green">active</Badge>}
        </div>
        {brand.updated_at && (
          <div style={{ fontSize: 11, color: A.muted2, marginTop: 6 }}>
            Last updated: {new Date(brand.updated_at).toLocaleString()}
          </div>
        )}
        <div style={{
          marginTop: 14, padding: "10px 14px", borderRadius: 8,
          background: A.amberSoft, border: `1px solid ${A.amber}40`,
          fontSize: 12, color: "#78350F", fontFamily: sans,
        }}>
          To update brand rules, upload a new Brand Brief DOCX on the{" "}
          <a href="/admin/brand" style={{ color: A.accentDeep, fontWeight: 600 }}>Brand Identity</a> page.
        </div>
      </Card>

      {/* Style Guide */}
      <Card>
        <SLabel>Style Guide</SLabel>
        {brand.style_guide ? (
          <>
            <p style={{ fontSize: 13, color: A.body, lineHeight: 1.6, margin: 0, fontFamily: sans }}>
              {expanded ? (brand.style_guide_full ?? brand.style_guide) : brand.style_guide}
              {!expanded && brand.style_guide_full && brand.style_guide_full.length > 200 && "…"}
            </p>
            {brand.style_guide_full && brand.style_guide_full.length > 200 && (
              <button
                onClick={() => setExpanded(v => !v)}
                style={{
                  marginTop: 8, background: "none", border: "none",
                  cursor: "pointer", color: A.accentDeep, fontSize: 12,
                  fontWeight: 600, display: "flex", alignItems: "center", gap: 4,
                  padding: 0, fontFamily: sans,
                }}
              >
                {expanded ? <><ChevronUp size={13} /> Show less</> : <><ChevronDown size={13} /> View full</>}
              </button>
            )}
          </>
        ) : (
          <span style={{ fontSize: 13, color: A.muted2 }}>Not set</span>
        )}
      </Card>

      {/* Forbidden words */}
      <Card>
        <SLabel>Forbidden Words ({brand.forbidden_words.length})</SLabel>
        {brand.forbidden_words.length > 0 ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {brand.forbidden_words.map((w, i) => (
              <span key={i} style={{
                padding: "4px 10px", borderRadius: 999,
                background: A.redSoft, color: A.red,
                fontSize: 12, fontWeight: 500, fontFamily: mono,
              }}>
                {w}
              </span>
            ))}
          </div>
        ) : (
          <span style={{ fontSize: 13, color: A.muted2 }}>No forbidden words configured</span>
        )}
      </Card>

      {/* System prompt preview */}
      {brand.system_prompt && (
        <Card>
          <SLabel>System Prompt (preview)</SLabel>
          <p style={{
            fontSize: 12, color: A.body, fontFamily: mono,
            background: A.bg, padding: "10px 14px", borderRadius: 8,
            border: `1px solid ${A.line}`, margin: 0, lineHeight: 1.6,
            whiteSpace: "pre-wrap", wordBreak: "break-word",
          }}>
            {brand.system_prompt}
          </p>
        </Card>
      )}
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
              background: `${A.accent}12`, color: A.accentDeep,
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
            Last saved: {new Date(seo.updated_at).toLocaleString()}
          </span>
        )}
      </div>
    </div>
  );
}

// ─── LLM Models Tab (AA-518 Việc C) ────────────────────────────────────────────

interface ModelOption { model_id: string; label: string; available: boolean; reason?: string }
interface AccountOption { value: string; label: string }
interface StageConfigRow {
  stage: string; role: string; provider: string; model_id: string;
  account_route: string | null; updated_at: string | null; updated_by: string;
  options: ModelOption[]; account_route_options: AccountOption[];
}

// Human label + display grouping — a pure presentation layer over the 16 stage rows the API
// returns; the API itself is the source of truth for which stages/values actually exist.
const STAGE_GROUPS: { key: string; label: string; stages: string[] }[] = [
  // AA-620: s1_generate = A1 admin writer only; the tenant (T2) writer is its own t2_generate
  // stage (own group below) so admin can tune/price them independently. flag_fix/itinerary_nudge
  // are still shared by A1 and T2 (Haiku fine for both, not split).
  { key: "s1", label: "S1 rewrite — A1 admin writer + shared repair/judge",
    stages: ["s1_generate", "s1_judge", "s1_brand_audit", "s1_flag_fix", "s1_itinerary_nudge", "s1_atom_writer"] },
  { key: "t2", label: "T2 tenant rewrite writer (own model — AA-620)", stages: ["t2_generate"] },
  { key: "t5", label: "T5 — Atomize", stages: ["t5_atomize"] },
  { key: "t8", label: "T8 — Angle generation", stages: ["t8_angle_gen"] },
  { key: "t9", label: "T9 — Content write + T10 quality judge", stages: ["t9_write", "t10_judge"] },
  { key: "n7", label: "N7 — Production pipeline (blog/social)",
    stages: ["n7_draft", "n7_adapt", "n7_faq", "n7_repair", "n7_gap_research", "n7_judge"] },
];
const STAGE_LABELS: Record<string, string> = {
  s1_generate: "Content generate", s1_judge: "Brand-fit judge",
  s1_brand_audit: "Brand audit", s1_flag_fix: "Flag-fix repair",
  s1_itinerary_nudge: "Itinerary day nudge", s1_atom_writer: "Atom-based writer",
  t2_generate: "Tenant content generate",
  t5_atomize: "Atomize tour", t8_angle_gen: "Angle generation",
  t9_write: "Content write", t10_judge: "Quality judge (F8+F9)",
  n7_draft: "Draft (E2)", n7_adapt: "Channel adapt (E3)", n7_faq: "FAQ answer (E4)",
  n7_repair: "Repair (E5)", n7_gap_research: "Competitor gap research", n7_judge: "Framework/brand judge",
};
const ROLE_COLOR: Record<string, "gray" | "gold" | "green"> = { writer: "gold", judge: "green", validate: "gray" };

function ModelRow({ row, onSaved }: { row: StageConfigRow; onSaved: (r: StageConfigRow) => void }) {
  const [modelId, setModelId] = useState(row.model_id);
  const [accountRoute, setAccountRoute] = useState(row.account_route ?? "");
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [error, setError] = useState("");

  const dirty = modelId !== row.model_id || accountRoute !== (row.account_route ?? "");
  const modelLabel = (id: string) => row.options.find(o => o.model_id === id)?.label ?? id;
  const acctLabel = (v: string) => row.account_route_options.find(o => o.value === v)?.label ?? v;

  async function confirmSave() {
    setConfirming(false);
    setSaving(true);
    setError("");
    try {
      const res = await fetch(`/api/admin/llm-config/${row.stage}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId, account_route: accountRoute || null }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.detail ?? "Save failed");
        setModelId(row.model_id);
        setAccountRoute(row.account_route ?? "");
        return;
      }
      const updated = await res.json();
      onSaved(updated);
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 3000);
    } catch {
      setError("Network error — model not changed");
      setModelId(row.model_id);
      setAccountRoute(row.account_route ?? "");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div data-testid={`llm-model-row-${row.stage}`} style={{
      display: "flex", alignItems: "center", gap: 10, padding: "10px 4px",
      borderBottom: `1px solid ${A.line2}`, flexWrap: "wrap",
    }}>
      <div style={{ minWidth: 200 }}>
        <div style={{ fontFamily: mono, fontSize: 12, color: A.ink, fontWeight: 600 }}>{row.stage}</div>
        <div style={{ fontSize: 11.5, color: A.muted2 }}>{STAGE_LABELS[row.stage] ?? row.stage}</div>
      </div>
      <Badge color={ROLE_COLOR[row.role] ?? "gray"}>{row.role}</Badge>

      <select
        data-testid={`llm-model-select-${row.stage}`}
        value={modelId}
        onChange={e => setModelId(e.target.value)}
        disabled={saving}
        style={{
          padding: "6px 10px", borderRadius: 7, border: `1px solid ${A.line}`,
          fontSize: 12.5, fontFamily: sans, color: A.body, background: A.card, minWidth: 190,
        }}
      >
        {row.options.map(o => (
          <option key={o.model_id} value={o.model_id} disabled={!o.available}>
            {o.label}{!o.available ? " — not available" : ""}
          </option>
        ))}
      </select>
      {(() => {
        const opt = row.options.find(o => o.model_id === modelId);
        return !opt?.available && opt?.reason ? (
          <span title={opt.reason} style={{ display: "inline-flex", color: A.amber }}>
            <AlertTriangle size={13} />
          </span>
        ) : null;
      })()}

      {row.account_route_options.length > 0 && (
        <select
          value={accountRoute}
          onChange={e => setAccountRoute(e.target.value)}
          disabled={saving}
          style={{
            padding: "6px 10px", borderRadius: 7, border: `1px solid ${A.line}`,
            fontSize: 12.5, fontFamily: sans, color: A.body, background: A.card, minWidth: 150,
          }}
        >
          {row.account_route_options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      )}

      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
        {savedFlash && <span style={{ fontSize: 11.5, color: A.green, fontWeight: 600 }}>Saved ✓</span>}
        {error && <span style={{ fontSize: 11.5, color: A.red }}>{error}</span>}
        <span style={{ fontSize: 10.5, color: A.muted2 }}>
          {row.updated_at ? new Date(row.updated_at).toLocaleString() : "—"} · {row.updated_by}
        </span>
        <Btn variant="secondary" size="sm" disabled={!dirty || saving} onClick={() => setConfirming(true)}>
          {saving ? <Spinner size={12} /> : <Save size={12} />}
          {saving ? "Saving…" : "Save"}
        </Btn>
      </div>

      {confirming && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(31,41,51,0.45)",
          display: "grid", placeItems: "center", zIndex: 50,
        }}>
          <Card style={{ maxWidth: 440, width: "90%" }}>
            <div style={{ fontFamily: serif, fontSize: 16, color: A.ink, marginBottom: 8 }}>
              Change model for <span style={{ fontFamily: mono }}>{row.stage}</span>?
            </div>
            <p style={{ fontSize: 12.5, color: A.muted, marginBottom: 12 }}>
              Changing the model affects the ENTIRE SYSTEM (every LLM call at this stage, not just
              yours) — it takes effect on the next call, no redeploy needed.
            </p>
            <div style={{
              background: A.line2, borderRadius: 8, padding: "10px 12px",
              fontSize: 12.5, fontFamily: mono, color: A.ink2, marginBottom: 16,
            }}>
              {modelLabel(row.model_id)}{row.account_route ? ` (${acctLabel(row.account_route)})` : ""}
              {" → "}
              {modelLabel(modelId)}{accountRoute ? ` (${acctLabel(accountRoute)})` : ""}
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <Btn variant="secondary" size="sm" onClick={() => setConfirming(false)}>Cancel</Btn>
              <Btn variant="primary" size="sm" onClick={confirmSave}>Confirm change</Btn>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

function ModelsTab() {
  const [rows, setRows] = useState<StageConfigRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    fetch("/api/admin/llm-config")
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => { setRows(d.stages); setError(""); })
      .catch(() => setError("Failed to load model configuration"))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  function onRowSaved(updated: StageConfigRow) {
    setRows(prev => prev ? prev.map(r => r.stage === updated.stage ? { ...r, ...updated } : r) : prev);
  }

  if (loading) return <LoadingScreen msg="Loading model configuration…" />;
  if (error || !rows) {
    return (
      <Card style={{ textAlign: "center", padding: 40 }}>
        <div style={{ color: A.red, marginBottom: 12 }}>{error || "No data"}</div>
        <Btn variant="secondary" onClick={load}>Retry</Btn>
      </Card>
    );
  }

  const byStage = Object.fromEntries(rows.map(r => [r.stage, r]));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <p style={{ fontSize: 12, color: A.muted2, margin: 0 }}>
        Only admins (aa_internal) can change models here — tenants have no access. Blocked models
        (e.g. GPT-5.6) still appear in the list with a reason rather than being hidden entirely, so
        you can see the roadmap.
      </p>
      {STAGE_GROUPS.map(group => (
        <Card key={group.key}>
          <SLabel>{group.label}</SLabel>
          <div>
            {group.stages.map(stage => byStage[stage] && (
              <ModelRow key={stage} row={byStage[stage]} onSaved={onRowSaved} />
            ))}
          </div>
        </Card>
      ))}
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

const TABS = [
  { key: "pipeline",  label: "Pipeline Gates" },
  { key: "brand",     label: "Brand Rules" },
  { key: "seo",       label: "SEO Config" },
  { key: "models",    label: "LLM Models" },
  { key: "tenant",    label: "Tenant Info" },
];

export default function SettingsPage() {
  const [tab, setTab]     = useState("pipeline");
  const [data, setData]   = useState<SettingsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    fetch("/api/admin/settings")
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => { setData(d); setError(""); })
      .catch(() => setError("Failed to load settings"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />

      <main style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 28 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 9,
            background: `${A.accent}15`, color: A.accent,
            display: "grid", placeItems: "center",
          }}>
            <Settings size={18} />
          </div>
          <div>
            <h1 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em", margin: 0 }}>
              Settings
            </h1>
            <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 2 }}>
              Pipeline configuration, brand rules, SEO config, and tenant info for aa_internal
            </div>
          </div>
        </div>

        {loading && <LoadingScreen msg="Loading settings…" />}

        {!loading && error && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>{error}</div>
            <Btn variant="secondary" onClick={load}>Retry</Btn>
          </Card>
        )}

        {!loading && data && (
          <>
            <div style={{ marginBottom: 24 }}>
              <TabBar tabs={TABS} active={tab} onChange={setTab} />
            </div>

            {tab === "pipeline" && (
              <PipelineGatesTab gates={data.pipeline_gates} />
            )}
            {tab === "brand" && (
              <BrandRulesTab brand={data.brand_rules} />
            )}
            {tab === "seo" && (
              <SeoConfigTab seo={data.seo_config} />
            )}
            {tab === "models" && <ModelsTab />}
            {tab === "tenant" && (
              <TenantInfoTab tenant={data.tenant} plan={data.plan} />
            )}
          </>
        )}
      </main>
    </div>
  );
}
