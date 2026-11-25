// lib/opsAssist.ts — AA-1154: the CIS admin assistant's answer as the Ecosystem status page reads it
// (backend api/services/ops_assist/, /api/admin/ops-assist/...). Pure helpers only: the paths, the tones of a risk
// level and a confidence, the order the panel shows. A0: rule-made cards, no model. A1: "Analyse" asks the AI gateway
// (stage ops_assist, its own month cap) to rank and explain those same cards; the model has no tools and nothing is
// executed from the panel: each card names the API a person would call.

export type RiskLevel = "R0" | "R1" | "R2" | "R3" | "R4";
export type Confidence = "code" | "measured" | "history" | "not_enough_data";

export interface Prediction { text: string; confidence: Confidence; basis: string }
export interface ApiCall { method: string; path: string; body?: unknown }

export interface ActionCard {
  id: string; rank: number; action: string; title: string; reason: string; evidence: string[];
  risk: { level: RiskLevel; axes: { direction: string; scope: string; money: string; reversible: boolean };
    why: string[] };
  predictions: Prediction[]; if_nothing: string;
  way_back: { text: string; call: ApiCall | null };
  cost: { usd: number | null; basis: string };
  approval: { who: string; capability: string[]; roles: string[]; needs_reason: boolean;
    reason_min_chars: number | null; second_person: boolean };
  call: ApiCall | null; dry_run: ApiCall | null; copy_only: boolean; runbook: string | null; copy_text: string | null;
  precondition: string | null; precondition_met: boolean; check_after: string[]; expires_at: string;
  /** A1: the rank the rules gave (the model may reorder inside the rule's groups) and the model's words. */
  rule_rank?: number; explanation?: { text: string; evidence: string[] } | null;
}

/** A1: what the model did for an answer (or why it did not). */
export interface LlmMeta {
  used: boolean; calls: number; depth?: "quick" | "deep"; error?: string | null; cached?: boolean;
  analysed_at?: string | null; model?: string | null; cost_usd?: number; connection?: string | null;
  dropped_ids?: string[];
  /** Deep analysis: the model the setting names, and whether it was unavailable (the stage route answered). */
  deep_model?: string | null; deep_fallback?: boolean;
}

export interface Evidence {
  id: string; label: string; value: string; source: string; as_of: string | null; href: string;
  untrusted_detail?: string | null; unavailable?: boolean;
}

export interface AssistAnswer {
  item_id: string; as_of: string; audience: "full" | "summary"; playbook: string;
  item: { id: string; title: string; severity: string } | null;
  evidence: Evidence[]; cards: ActionCard[]; not_proposed: { action: string; title: string; why: string }[];
  summary: { headline: string; lines: string[] };
  unavailable?: Record<string, string>;
  llm: LlmMeta;
  ai_summary?: { text: string | null; owner_text: string | null } | null;
  replay?: { title: string; what_happened: string; source: string };
}

export interface AssistSignal { id: string; severity: string; app: string; kind: string; title: string; detail: string }
export interface SignalsAnswer { as_of: string; signals: AssistSignal[]; window_min: number }

const RANGES = new Set(["1h", "6h", "24h", "7d"]);

/** The cards path of one Needs-attention item or signal; a custom range reads the page default (24 h). */
export function cardsPath(itemId: string, range: string): string {
  const r = RANGES.has(range) ? range : "24h";
  return `/api/admin/ops-assist/items/${encodeURIComponent(itemId)}/cards?range=${r}`;
}

export const SIGNALS_PATH = "/api/admin/ops-assist/signals";

export type Depth = "quick" | "deep";

/** A1: the "Analyse" path of one item (POST; one AI call, or the answer saved within the card lifetime). */
export function analysePath(itemId: string, range: string, depth: Depth, fresh: boolean): string {
  const r = RANGES.has(range) ? range : "24h";
  return `/api/admin/ops-assist/items/${encodeURIComponent(itemId)}/analyse?range=${r}&depth=${depth}` +
    (fresh ? "&fresh=true" : "");
}

/** A1: the automatic analysis of the open SEV1 / SEV2 incidents (POST; the server runs each incident once). */
export function autoPath(range: string): string {
  return `/api/admin/ops-assist/auto?range=${RANGES.has(range) ? range : "24h"}`;
}

/** A1: "what changed" of a window in minutes (null = the API's default: the hour when offered), from the records
 * (GET) or phrased by the model (POST). The offered windows are a setting (cis.ops.assist_lookup_windows_min): the
 * answer lists them (`windows`) and the page builds its choices from it. */
export function changesPath(minutes: number | null, phrase = false): string {
  const q = minutes !== null && Number.isInteger(minutes) && minutes > 0 ? `?window=${minutes}` : "";
  return `/api/admin/ops-assist/changes${phrase ? "/phrase" : ""}${q}`;
}

/** How a window reads after "in the last …": a whole number of hours, or minutes. */
export function windowWords(minutes: number): { unit: "hour" | "hours" | "minutes"; n: number } {
  if (minutes === 60) return { unit: "hour", n: 1 };
  if (minutes % 60 === 0) return { unit: "hours", n: minutes / 60 };
  return { unit: "minutes", n: minutes };
}

export interface ChangeRow { id: string; t: string | null; type: string; app: string; title: string; href?: string }
export interface ChangesAnswer {
  minutes: number; windows: number[]; since: string; until: string; changes: ChangeRow[]; events: ChangeRow[];
  answer: { headline: string; lines: string[] }; unavailable: Record<string, string>;
  ai_answer: { text: string; cited: string[] } | null; llm: LlmMeta;
}

/** The message key that says what the model did for an answer. */
export type LlmNoticeKey = "llmUsed" | "llmCached" | "llmOff" | "llmBudget" | "llmNotSet" | "llmFailed" | "llmNone";

export function llmNotice(llm: LlmMeta | undefined): LlmNoticeKey {
  if (!llm) return "llmNone";
  if (llm.error === "switched_off") return "llmOff";
  if (llm.error === "budget" || llm.error === "no_cap") return "llmBudget";
  if (llm.error === "not_configured") return "llmNotSet";
  if (llm.error) return "llmFailed";
  if (!llm.used) return "llmNone";
  return llm.cached ? "llmCached" : "llmUsed";
}

/** "0.0031 USD" for a model call (4 decimals: a call costs cents). */
export function costText(usd: number | undefined): string {
  return `${(usd ?? 0).toFixed(4)} USD`;
}

/** The evidence items the model's explanation of a card cites (unknown ids dropped). */
export function explanationEvidence(card: ActionCard, evidence: Evidence[]): Evidence[] {
  const ids = card.explanation?.evidence ?? [];
  const byId = new Map(evidence.map((e) => [e.id, e]));
  return ids.map((id) => byId.get(id)).filter((e): e is Evidence => !!e);
}


export function riskTone(level: RiskLevel): "neutral" | "info" | "warning" | "danger" {
  if (level === "R0") return "neutral";
  if (level === "R1") return "info";
  if (level === "R2") return "warning";
  return "danger";
}

export function confidenceTone(c: Confidence): "success" | "info" | "neutral" {
  if (c === "measured" || c === "history") return "success";
  return c === "code" ? "info" : "neutral";
}

/** Cards in rank order (the API ranks them; kept stable here). */
export function ranked(cards: ActionCard[]): ActionCard[] {
  return [...cards].sort((a, b) => a.rank - b.rank);
}

/** The evidence items one card cites, in the card's order, unknown ids dropped. */
export function citedEvidence(card: ActionCard, evidence: Evidence[]): Evidence[] {
  const byId = new Map(evidence.map((e) => [e.id, e]));
  return card.evidence.map((id) => byId.get(id)).filter((e): e is Evidence => !!e);
}

/** Has the card expired (the evidence must be read again)? */
export function expired(card: ActionCard, now: Date): boolean {
  const t = Date.parse(card.expires_at);
  return Number.isFinite(t) && t <= now.getTime();
}

/** "POST /admin/waves/x/pause" for a call, "" for none. */
export function callText(call: ApiCall | null): string {
  return call ? `${call.method} ${call.path}` : "";
}

/** The status page's own Needs-attention ids are shown on the list; the signals add only what it does not show. */
export function extraSignals(signals: AssistSignal[], attentionIds: string[]): AssistSignal[] {
  const seen = new Set(attentionIds);
  return signals.filter((s) => !seen.has(s.id));
}
