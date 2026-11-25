// lib/opsAssist.ts — AA-1154: the CIS admin assistant's answer as the Ecosystem status page reads it
// (backend api/services/ops_assist/, /api/admin/ops-assist/...). Pure helpers only: the paths, the tones of a risk
// level and a confidence, the order the panel shows. A0: rule-made cards, no model. A1: "Analyse" asks the AI gateway
// (stage ops_assist, its own month cap) to rank and explain those same cards; the model has no tools and nothing is
// executed from the panel: each card names the API a person would call. A2: a person marks a card as done once they ran
// it (POST .../runs); the status sampler checks its measures again at the minutes of cis.ops.assist_check_after_min
// and the page shows each check (hit, miss, unclear) and the hit rate of the last days (GET .../track).

export type RiskLevel = "R0" | "R1" | "R2" | "R3" | "R4";

const RANGES = new Set(["1h", "6h", "24h", "7d"]);
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
  /** A2: what is read again after the card is marked as done, and the direction the card predicts. */
  measures?: Measure[];
}

/** A2: one measure of a card: zero (it is gone), nonzero (it happened), down / up against the value when marked done. */
export type Expect = "zero" | "nonzero" | "down" | "up";
export interface Measure {
  metric: string; params: Record<string, unknown>; expect: Expect; label: string; baseline?: number | null;
}
export type Verdict = "hit" | "miss" | "unclear";
export interface Checkpoint {
  after_min: number; at: string; verdict: Verdict; late: boolean;
  measures: { value: number | null; verdict: Verdict }[];
}
export interface AssistRun {
  run_id: number; item_id: string; card_id: string; action: string; measures: Measure[]; check_after_min: number[];
  checks: Checkpoint[]; verdict: Verdict | null; recorded_at: string; recorded_by: string;
  checked_at?: string | null; incident_id?: number | null;
}
export interface RunsAnswer { runs: AssistRun[]; check_after_min: number[] }
export interface TrackAnswer {
  days: number; recorded: number; checked: number; hits: number; unclear: number; open: number; text: string;
  by_action: { action: string; title: string; recorded: number; checked: number; hits: number }[];
}

export const RUNS_PATH = "/api/admin/ops-assist/runs";
export const TRACK_PATH = "/api/admin/ops-assist/track";

/** The runs of one item (GET), newest first. */
export function runsPath(itemId: string): string {
  return `${RUNS_PATH}?item_id=${encodeURIComponent(itemId)}`;
}

/** The newest run of a card, if it was marked as done. */
export function runOfCard(runs: AssistRun[], cardId: string): AssistRun | undefined {
  return runs.filter((r) => r.card_id === cardId).sort((a, b) => b.recorded_at.localeCompare(a.recorded_at))[0];
}

/** The checkpoints (minutes) of a run that are still to come. */
export function pendingChecks(run: AssistRun): number[] {
  const taken = new Set(run.checks.map((c) => c.after_min));
  return run.check_after_min.filter((m) => !taken.has(m));
}

export function verdictTone(v: Verdict | null): "success" | "danger" | "neutral" | "info" {
  if (v === "hit") return "success";
  if (v === "miss") return "danger";
  return v === "unclear" ? "neutral" : "info";
}

/** The page range a card was built for (a custom range reads the page default, as cardsPath does). */
export function assistRange(range: string): string {
  return RANGES.has(range) ? range : "24h";
}

/** technical-constant: API Gateway ends every request at 30 s; a BFF call still open after this is stuck (S292: a
 * GET hung for more than 5 minutes during a deploy). The API itself answers 503 "try again" before that
 * (cis.ops.assist_build_timeout_s). */
export const FETCH_TIMEOUT_MS = 35_000;

/** ``p``, or a rejection with ``message`` once ``ms`` have passed (the page then offers Retry instead of waiting). */
export function withTimeout<T>(p: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    p.then((v) => { clearTimeout(timer); resolve(v); }, (e) => { clearTimeout(timer); reject(e); });
  });
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
