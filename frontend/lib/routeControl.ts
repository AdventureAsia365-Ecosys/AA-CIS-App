// lib/routeControl.ts — AA-1042 (S268, route control center): the Route control tab of AI providers
// (app/admin/ai-gateway/RouteControlTab.tsx). Types of the backend /admin/ai-gateway/route-control API
// (api/routers/admin_llm_route_control.py, api/services/llm_route_control.py) and the pure helpers of the editor:
// moving an item of a chain or a provider order, the draft against the config in force, the request body (only what
// changed). Node-loadable (no React): lib/routeControl.test.mjs runs it with --experimental-strip-types.

export const ROUTE_CONTROL_API = "/api/admin/ai-gateway/route-control";
export const ROUTE_CONTROL_KEY = ["ai-gateway", "route-control"] as const;
export const ROUTE_VERSIONS_KEY = ["ai-gateway", "route-control", "versions"] as const;

export const ORDER_MODES = ["model_first", "aws_first"] as const;
export type OrderMode = (typeof ORDER_MODES)[number];
export const PRESET_KEYS = ["prefer_anthropic_api", "prefer_aws", "serving_first"] as const;
export type PresetKey = (typeof PRESET_KEYS)[number];
/** The gateway's attempt cap per request (shared/llm_client/errors.MAX_CHAIN_ATTEMPTS); the API sends its own. */
export const DEFAULT_MAX_ATTEMPTS = 6;
/** The longest note a version keeps (shared/llm_client/route_versions.NOTE_MAX). */
export const NOTE_MAX = 500;

export interface StageOption {
  model_id: string;
  label: string;
  via?: string | null;
  available: boolean;
  reason?: string;
}

export interface ControlStage {
  stage: string;
  role: string;
  is_active: boolean;
  allowed_scopes: string[] | null;
  chain: string[];
  options: StageOption[];
  product: string | null;
}

export interface ProviderDeployment {
  connection_key: string;
  label: string;
  kind: string | null;
  data_scope: string | null;
  priority: number;
  enabled: boolean;
  connection_enabled: boolean;
  access_state?: string | null;
}

export interface ProviderOrderRow {
  model_key: string;
  label: string;
  family: string | null;
  deployments: ProviderDeployment[];
}

export interface RouteControlState {
  ready: boolean;
  version: number;
  order_mode: OrderMode;
  env_order_mode: OrderMode;
  updated_at: string | null;
  updated_by: string | null;
  max_attempts: number;
  stages: ControlStage[];
  models: Record<string, { label: string; family: string | null; enabled: boolean }>;
  provider_order: ProviderOrderRow[];
  presets: { key: PresetKey; label: string; description: string }[];
}

export type DiffChange =
  | { kind: "order_mode"; before: string | null; after: string | null }
  | { kind: "route"; stage: string; before: string[] | null; after: string[] | null }
  | { kind: "provider_order"; model: string; before: string[] | null; after: string[] | null };

export interface Problem {
  rule: string;
  message: string;
  stage: string | null;
  model: string | null;
  existing?: boolean;
}

export interface PreviewStage {
  stage: string;
  role: string;
  chain: string[] | null;
  changed: boolean;
  first: string | null;
  first_before: string | null;
  /** The first model@connection that would serve the stage differs from now (chain, provider order or order mode). */
  serving_changed?: boolean;
  /** The model@connection entries before `first` that will not serve the stage, in route order, each with why (switched
   * off, circuit open, refused on record: the probe or the breaker). */
  skipped?: SkippedDeployment[];
  attempts: {
    model: string; scope: string; status: string; connection?: string | null; reason?: string | null;
    refused?: { connection: string; reason: string }[] | null;
  }[];
}

export interface SkippedDeployment {
  model: string;
  connection: string | null;
  reason: string;
}

/** "model@connection" of a skipped entry (the model alone when no deployment was planned for it). */
export function skippedWhere(s: SkippedDeployment): string {
  return s.connection ? `${s.model}@${s.connection}` : s.model;
}

export interface RoutePreview {
  valid: boolean;
  errors: Problem[];
  warnings: Problem[];
  unchanged: boolean;
  diff: DiffChange[];
  needs_evidence: boolean;
  evidence_stages: string[];
  /** AA-1052 W6: the open rerun waves that would refuse this save (409); empty or absent = none. */
  frozen_by_waves?: { id: string; name: string | null }[];
  version: number;
  order_mode: OrderMode;
  stages: PreviewStage[];
  provider_order: ProviderOrderRow[];
}

export interface RouteVersion {
  version: number;
  created_at: string | null;
  created_by: string;
  action: "baseline" | "save" | "preset" | "revert" | "external";
  preset: string | null;
  reverted_from: number | null;
  note: string | null;
  diff: DiffChange[];
}

export interface RouteVersionsResp {
  current: number;
  versions: RouteVersion[];
}

/** What the admin is editing: the order mode, every stage's chain, every model's provider order. */
export interface RouteDraft {
  order_mode: OrderMode;
  routes: Record<string, string[]>;
  provider_order: Record<string, string[]>;
}

export interface ProposalBody {
  order_mode?: OrderMode;
  routes?: Record<string, string[]>;
  provider_order?: Record<string, string[]>;
}

/** `list` with the item at `from` moved to `to` (a new array; out-of-range indexes leave it as it is). */
export function moveItem<T>(list: readonly T[], from: number, to: number): T[] {
  const out = [...list];
  if (from < 0 || from >= out.length || to < 0 || to >= out.length || from === to) return out;
  const [item] = out.splice(from, 1);
  out.splice(to, 0, item);
  return out;
}

function sameList(a: readonly string[] | undefined, b: readonly string[] | undefined): boolean {
  return !!a && !!b && a.length === b.length && a.every((x, i) => x === b[i]);
}

/** The draft of the config in force. */
export function draftOf(state: RouteControlState): RouteDraft {
  return {
    order_mode: state.order_mode,
    routes: Object.fromEntries(state.stages.map((s) => [s.stage, [...s.chain]])),
    provider_order: Object.fromEntries(
      state.provider_order.map((p) => [p.model_key, p.deployments.map((d) => d.connection_key)])),
  };
}

/** The request body: only the parts of `draft` that differ from the config in force (empty = nothing to save). */
export function proposalBody(state: RouteControlState, draft: RouteDraft): ProposalBody {
  const base = draftOf(state);
  const out: ProposalBody = {};
  if (draft.order_mode !== base.order_mode) out.order_mode = draft.order_mode;
  const routes = Object.fromEntries(Object.entries(draft.routes).filter(([s, c]) => !sameList(c, base.routes[s])));
  if (Object.keys(routes).length) out.routes = routes;
  const order = Object.fromEntries(
    Object.entries(draft.provider_order).filter(([m, c]) => !sameList(c, base.provider_order[m])));
  if (Object.keys(order).length) out.provider_order = order;
  return out;
}

export function isEmptyProposal(body: ProposalBody): boolean {
  return body.order_mode === undefined && !body.routes && !body.provider_order;
}

/** Problems of one stage (errors first). */
export function problemsOf(preview: RoutePreview | undefined, stage: string): { errors: Problem[]; warnings: Problem[] } {
  return {
    errors: (preview?.errors ?? []).filter((p) => p.stage === stage),
    warnings: (preview?.warnings ?? []).filter((p) => p.stage === stage),
  };
}

/** The model keys a stage may add: available options not already in its chain. */
export function addableOptions(stage: ControlStage, chain: readonly string[]): StageOption[] {
  return stage.options.filter((o) => o.available && !chain.includes(o.model_id));
}

/** "a → b → c" for a chain, or an em dash for none. */
export function chainText(chain: readonly string[] | null | undefined): string {
  return chain && chain.length ? chain.join(" → ") : "—";
}

/** Stages grouped by the given group order (unknown stages last, in API order). */
export function groupStages<S extends { stage: string }>(
  stages: readonly S[], groups: readonly { key: string; stages: readonly string[] }[],
): { key: string; stages: S[] }[] {
  const byStage = new Map(stages.map((s) => [s.stage, s]));
  const used = new Set<string>();
  const out: { key: string; stages: S[] }[] = [];
  for (const g of groups) {
    const list = g.stages.map((s) => byStage.get(s)).filter((s): s is S => !!s);
    list.forEach((s) => used.add(s.stage));
    if (list.length) out.push({ key: g.key, stages: list });
  }
  const rest = stages.filter((s) => !used.has(s.stage));
  if (rest.length) out.push({ key: "other", stages: rest });
  return out;
}

export function versionPath(version: number): string {
  return `${ROUTE_CONTROL_API}/versions/${encodeURIComponent(String(version))}`;
}

export function presetPath(preset: string): string {
  return `${ROUTE_CONTROL_API}/presets/${encodeURIComponent(preset)}`;
}
