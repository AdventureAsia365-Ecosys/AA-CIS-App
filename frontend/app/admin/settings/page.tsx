"use client";
// app/admin/settings/page.tsx — AA-158 Admin Settings (4 tabs)

import { Suspense, useState, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import { Settings, X, Plus, Save, AlertTriangle } from "lucide-react";
import BrandIdentityEditor from "../_components/BrandIdentityEditor";
import SettingsKitTab from "../_components/SettingsKitTab";
import {
  A, alpha, serif, sans, mono,
  Card, SLabel, TabBar, Badge, Btn, Spinner, LoadingScreen,
} from "../_components/adminUi";
import { PageHeader, formatDateTime } from "../../_kit";

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

// ─── LLM Models Tab (AA-518 Việc C) ────────────────────────────────────────────

interface ModelOption { model_id: string; label: string; via?: string; available: boolean; reason?: string }
interface AccountOption { value: string; label: string }
// AA-714 — the route the gateway really runs: primary, fallbacks in order, optional shadow.
interface RouteItem { model_id: string; label: string; via?: string | null }
interface StageRoute { chain: RouteItem[]; shadow: (RouteItem & { sample_pct: number }) | null }
interface StageConfigRow {
  stage: string; role: string; provider: string; model_id: string;
  account_route: string | null; updated_at: string | null; updated_by: string;
  options: ModelOption[]; account_route_options: AccountOption[];
  route?: StageRoute;
}

// "GPT-6 Luna · Bedrock acc3" vs "GPT-6 Luna (OpenAI) · OpenAI API" — the same model can run on
// two providers, so the provider is always shown next to the name.
const withVia = (label: string, via?: string | null) => (via ? `${label} · ${via}` : label);

function RouteLine({ route }: { route?: StageRoute }) {
  if (!route || route.chain.length === 0) return null;
  const chip = (i: RouteItem, key: string, tone: "main" | "fb" | "shadow") => (
    <span key={key} style={{
      display: "inline-flex", gap: 4, alignItems: "center", padding: "2px 8px", borderRadius: 999,
      fontSize: 11, border: `1px solid ${A.line}`,
      background: tone === "main" ? A.line2 : A.card, color: tone === "shadow" ? A.muted2 : A.ink2,
    }}>
      <span style={{ fontWeight: tone === "main" ? 600 : 400 }}>{i.label}</span>
      {i.via && <span style={{ color: A.muted2 }}>· {i.via}</span>}
    </span>
  );
  return (
    <div data-testid="llm-route-line" style={{
      width: "100%", display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6,
      fontSize: 11, color: A.muted2, paddingLeft: 2,
    }}>
      <span>Route:</span>
      {route.chain.map((i, n) => (
        <span key={`${i.model_id}-${n}`} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {n > 0 && <span aria-hidden>→</span>}
          {chip(i, i.model_id, n === 0 ? "main" : "fb")}
        </span>
      ))}
      {route.shadow && (
        <>
          <span style={{ marginLeft: 8 }}>Shadow ({route.shadow.sample_pct}%, compare only):</span>
          {chip(route.shadow, "shadow", "shadow")}
        </>
      )}
    </div>
  );
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
  // AA-685: call sites that bypassed the gateway before.
  { key: "a0", label: "A0 — Excel column auto-detect", stages: ["a0_column_map"] },
  { key: "embed", label: "Embeddings (question/atom matching)", stages: ["f10_embed"] },
  { key: "tp", label: "TripPlanner",
    stages: ["tp_compose", "tp_search_embed", "tp_extract", "tp_component_embed"] },
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
  a0_column_map: "Column mapping", f10_embed: "Content embedding",
  tp_compose: "Trip plan compose", tp_search_embed: "Search query embedding",
  tp_extract: "Component extraction (offline)", tp_component_embed: "Component embedding (offline)",
};
const ROLE_COLOR: Record<string, "gray" | "gold" | "green"> = {
  writer: "gold", judge: "green", validate: "gray", embed: "gray",
};

function ModelRow({ row, onSaved }: { row: StageConfigRow; onSaved: (r: StageConfigRow) => void }) {
  const [modelId, setModelId] = useState(row.model_id);
  const [accountRoute, setAccountRoute] = useState(row.account_route ?? "");
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [error, setError] = useState("");

  const dirty = modelId !== row.model_id || accountRoute !== (row.account_route ?? "");
  const modelLabel = (id: string) => {
    const o = row.options.find(x => x.model_id === id);
    return o ? withVia(o.label, o.via) : id;
  };
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
          fontSize: 12.5, fontFamily: sans, color: A.body, background: A.card, minWidth: 260,
        }}
      >
        {row.options.map(o => (
          <option key={o.model_id} value={o.model_id} disabled={!o.available}>
            {withVia(o.label, o.via)}{!o.available ? " — not available" : ""}
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
          {row.updated_at ? formatDateTime(row.updated_at) : "—"} · {row.updated_by}
        </span>
        <Btn variant="secondary" size="sm" disabled={!dirty || saving} onClick={() => setConfirming(true)}>
          {saving ? <Spinner size={12} /> : <Save size={12} />}
          {saving ? "Saving…" : "Save"}
        </Btn>
      </div>

      <RouteLine route={row.route} />

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
        still appear in the list with a reason rather than being hidden entirely. Each model shows
        where it runs: <b>Bedrock acc3</b> (GPT models on Bedrock exist only on acc3) or{" "}
        <b>OpenAI API</b> (the OpenAI platform account, billed separately). Fallbacks and the shadow
        model are read-only here.
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
      <JevStagesCard />
    </div>
  );
}

// AA-660 — the Jev (TypeSafe) side of "which model does each stage use": read-only here, edited on
// /admin/decisions where the verdicts and calibration live.
interface JevQuestionRow {
  question_key: string; stage: string; kind: string; mode: "off" | "shadow" | "enforce";
  accept_floor: number | null; reject_ceiling: number | null; calibration_ref: string | null;
  verdicts: number; acted: number; cost_usd: number;
}

function JevStagesCard() {
  const [rows, setRows] = useState<JevQuestionRow[] | null>(null);
  useEffect(() => {
    fetch("/api/admin/decisions/summary?days=7")
      .then(r => (r.ok ? r.json() : null))
      .then(d => setRows(d?.questions ?? []))
      .catch(() => setRows([]));
  }, []);
  const modeColor = { off: "gray", shadow: "blue", enforce: "gold" } as const;
  return (
    <Card>
      <SLabel>Jev decisions per stage (TypeSafe · jev-latest)</SLabel>
      <p style={{ fontSize: 12, color: A.muted2, margin: "0 0 10px" }}>
        Stages that ask Jev typed questions. Only <b>enforce</b> questions with a confident verdict change
        what a stage does. Edit modes and floors on <a href="/admin/decisions" style={{ color: A.gold }}>Jev Decisions</a>.
      </p>
      {rows === null ? <Spinner /> : rows.length === 0 ? (
        <div style={{ fontSize: 12.5, color: A.muted }}>No Jev questions configured yet.</div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
          <tbody>
            {rows.map(q => (
              <tr key={q.question_key} style={{ borderTop: `1px solid ${A.line}` }}>
                <td style={{ padding: "6px 0", fontFamily: mono }}>{q.stage}</td>
                <td style={{ padding: "6px 8px", fontFamily: mono }}>{q.question_key}</td>
                <td style={{ padding: "6px 8px" }}><Badge color={modeColor[q.mode]}>{q.mode}</Badge></td>
                <td style={{ padding: "6px 8px", fontFamily: mono }}>
                  {q.accept_floor ?? "—"} / {q.reject_ceiling ?? "—"}
                </td>
                <td style={{ padding: "6px 8px", color: A.muted }}>{q.calibration_ref ? "calibrated" : "not calibrated"}</td>
                <td style={{ padding: "6px 8px", fontFamily: mono }}>{q.verdicts} verdicts · {q.acted} acted · 7d</td>
                <td style={{ padding: "6px 0", fontFamily: mono, textAlign: "right" }}>${q.cost_usd.toFixed(5)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
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
  const [tab, setTab]     = useState(() => (TABS.some(t => t.key === wanted) ? wanted! : "pipeline"));
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
      <main className="aa-admin-main" style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        {/* Header */}
        <PageHeader
          title="Settings"
          description="Pipeline gates, brand identity, SEO config, models per stage, and tenant info for aa_internal"
        />

        {loading && <LoadingScreen msg="Loading settings…" />}

        {!loading && error && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>{error}</div>
            <Btn variant="secondary" onClick={load}>Retry</Btn>
          </Card>
        )}

        {!loading && data && (
          <>
            {/* AA-601 — "content rendered" signal for the UI smoke: present only once the settings
                payload has loaded (!loading && data), never on the LoadingScreen. */}
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
