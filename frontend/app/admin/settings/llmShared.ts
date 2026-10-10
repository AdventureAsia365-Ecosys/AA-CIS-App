// app/admin/settings/llmShared.ts
// AA-686 — shared types + presentation constants for the LLM Models tab (route/shadow editor,
// model catalog, shadow A/B report). The API (api/routers/admin_llm_ops.py) is the source of
// truth for which stages/values exist; everything here is a pure presentation layer over its rows.

// ── /admin/llm-config ───────────────────────────────────────────────────────
export interface ModelOption {
  model_id: string;
  label: string;
  via?: string;
  available: boolean;
  reason?: string;
}
export interface AccountOption {
  value: string;
  label: string;
}
// AA-714 — the route the gateway really runs: primary, fallbacks in order, optional shadow.
export interface RouteItem {
  model_id: string;
  label: string;
  via?: string | null;
}
export interface StageRoute {
  chain: RouteItem[];
  shadow: (RouteItem & { sample_pct: number }) | null;
}
export interface StageConfigRow {
  stage: string;
  role: string;
  provider: string;
  model_id: string;
  account_route: string | null;
  fallback_model_ids: string[];
  shadow_model_id: string | null;
  shadow_sample_pct: number | null;
  updated_at: string | null;
  updated_by: string;
  options: ModelOption[];
  account_route_options: AccountOption[];
  route?: StageRoute;
}

// ── /admin/llm-catalog ──────────────────────────────────────────────────────
export interface CatalogRow {
  model_key: string;
  label: string;
  vendor: string;
  provider: string;
  api_style: string;
  bedrock_profile_ids: Record<string, string>;
  wire_model: string | null;
  callable_via: string[];
  price_in_per_mtok: number | null;
  price_out_per_mtok: number | null;
  price_cache_read_per_mtok: number | null;
  price_cache_write_per_mtok: number | null;
  price_source: string | null;
  enabled: boolean;
  blocked_reason: string | null;
  notes: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

// ── /admin/llm-shadow/report ────────────────────────────────────────────────
export interface ShadowGroup {
  stage: string;
  primary_model: string;
  shadow_model: string;
  n: number;
  shadow_error: number;
  unparsed: number;
  agreement_rate: number | null;
  agreement_sample: number;
  mean_abs_score_delta: number | null;
  score_delta_sample: number;
  primary_cost_usd: number;
  shadow_cost_usd: number;
  primary_cost_per_call: number | null;
  shadow_cost_per_call: number | null;
  shadow_latency_p50_ms: number | null;
  shadow_latency_p95_ms: number | null;
  primary_repeat_score_stddev: number | null;
  shadow_repeat_score_stddev: number | null;
}

// ── Dead stages (contract) ────────────────────────────────────────────────────
// A tiny allow-list of stages the route editor and A/B report must never render, even if the API
// still returns a row for them. Empty since AA-753 (10/10/2026) removed the N7 produce pipeline:
// its last hidden stage, n7_judge, was dropped from shared.llm_role_config (migration 207) and
// from the code SAFE_DEFAULTS, so there is nothing left to hide. The mechanism stays — add a
// stage here only when it is genuinely dead, not merely unused this week.
export const HIDDEN_STAGES: ReadonlySet<string> = new Set<string>([]);

// "GPT-6 Luna · Bedrock acc3" vs "GPT-6 Luna (OpenAI) · OpenAI API" — the same model can run on
// two providers, so the provider is always shown next to the name.
export const withVia = (label: string, via?: string | null) => (via ? `${label} · ${via}` : label);

// Human label + display grouping — a pure presentation layer over the stage rows the API returns.
export const STAGE_GROUPS: { key: string; label: string; stages: string[] }[] = [
  // AA-620: s1_generate = A1 admin writer only; the tenant (T2) writer is its own t2_generate
  // stage (own group below). flag_fix/itinerary_nudge are shared by A1 and T2.
  {
    key: "s1",
    label: "S1 rewrite — A1 admin writer + shared repair/judge",
    stages: ["s1_generate", "s1_judge", "s1_brand_audit", "s1_flag_fix", "s1_itinerary_nudge", "s1_atom_writer"],
  },
  { key: "t2", label: "T2 tenant rewrite writer (own model — AA-620)", stages: ["t2_generate"] },
  // AA-757 (S224): stage key renamed t5_atomize -> a3_atomize (atomize is A3 platform only).
  // Historical llm_call_log rows keep t5_atomize, so both keys map to this one "A3 atomize"
  // group/label.
  { key: "a3", label: "A3 — Atomize", stages: ["a3_atomize", "t5_atomize"] },
  { key: "t8", label: "T8 — Angle generation", stages: ["t8_angle_gen"] },
  { key: "t9", label: "T9 — Content write + T10 quality judge", stages: ["t9_write", "t10_judge"] },
  // AA-685: call sites that bypassed the gateway before.
  { key: "a0", label: "A0 — Excel column auto-detect", stages: ["a0_column_map"] },
  { key: "embed", label: "Embeddings (question/atom matching)", stages: ["f10_embed"] },
  {
    key: "tp",
    label: "TripPlanner",
    stages: ["tp_compose", "tp_search_embed", "tp_extract", "tp_component_embed"],
  },
];

export const STAGE_LABELS: Record<string, string> = {
  s1_generate: "Content generate",
  s1_judge: "Brand-fit judge",
  s1_brand_audit: "Brand audit",
  s1_flag_fix: "Flag-fix repair",
  s1_itinerary_nudge: "Itinerary day nudge",
  s1_atom_writer: "Atom-based writer",
  t2_generate: "Tenant content generate",
  a3_atomize: "A3 atomize",
  t5_atomize: "A3 atomize",  // AA-757: historical rows under the old stage key — same stage.
  t8_angle_gen: "Angle generation",
  t9_write: "Content write",
  t10_judge: "Quality judge (F8+F9)",
  a0_column_map: "Column mapping",
  f10_embed: "Content embedding",
  tp_compose: "Trip plan compose",
  tp_search_embed: "Search query embedding",
  tp_extract: "Component extraction (offline)",
  tp_component_embed: "Component embedding (offline)",
};

export type RoleTone = "neutral" | "accent" | "success";
export const ROLE_TONE: Record<string, RoleTone> = {
  writer: "accent",
  judge: "success",
  validate: "neutral",
  embed: "neutral",
};

// label for a model_id within a stage row's own options (falls back to the id).
export const modelLabelIn = (options: ModelOption[], id: string): string => {
  const o = options.find((x) => x.model_id === id);
  return o ? withVia(o.label, o.via) : id;
};
