// lib/opsAssist.ts — AA-1154 (A0): the CIS admin assistant's answer as the Ecosystem status page reads it
// (backend api/services/ops_assist/, GET /api/admin/ops-assist/...). Pure helpers only: the paths, the tones of a
// risk level and a confidence, and the order the panel shows. No LLM and no action buttons in A0: the cards name the
// API a person would call; A1 adds the buttons.

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
  llm: { used: boolean; calls: number };
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
