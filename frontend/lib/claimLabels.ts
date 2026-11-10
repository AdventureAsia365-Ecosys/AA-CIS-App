// lib/claimLabels.ts — AA-1063: types and pure helpers of Writing memory › Claim labels (the W2 claim ground truth).
// Backend: api/routers/admin_claim_labels.py through the BFF (/api/admin/writing-memory/claim-labels/*): the review
// view of golden_claim queue items, the agent proposals, the label set browser and the append-only corrections.
// Pure (no React), so `npm run test:unit` loads it directly (lib/claimLabels.test.mjs).

import { WM_API } from "./writingMemory.ts";

export const CLAIM_API = `${WM_API}/claim-labels`;
export const REVIEW_PAGE = 25;
export const CLAIMS_PAGE = 50;
export const BULK_MAX = 500;

export type ClaimLabel = "ok" | "violation" | "unsure";
export type Answer = "correct" | "wrong" | "unsure";
export type ReviewState = "open" | "resolved" | "all";
export type ProposalFilter = "" | ClaimLabel | "none";
export type FinalSource = "tech_lead" | "panel_agreed" | "none";
export type ClaimWriter = "a1" | "t2" | "t9_blog" | "t9_social";

export const ANSWERS: readonly Answer[] = ["correct", "wrong", "unsure"];
export const LABEL_OF_ANSWER: Record<Answer, ClaimLabel> = { correct: "ok", wrong: "violation", unsure: "unsure" };
export const ANSWER_OF_LABEL: Record<ClaimLabel, Answer> = { ok: "correct", violation: "wrong", unsure: "unsure" };
export const CLAIM_WRITERS: readonly ClaimWriter[] = ["t9_blog", "t9_social", "t2", "a1"];
export const LABELS: readonly ClaimLabel[] = ["ok", "violation", "unsure"];

export interface Lens {
  lens: string | null; label: ClaimLabel; claim_class: string; reason: string | null; labeller?: string;
  adjudication?: string;
}

export interface ProposalNotes {
  claim_vi?: string; claim_en?: string; source_says_en?: string; source_says_vi?: string; doubt_en?: string;
  doubt_vi?: string;
}

export interface Proposal {
  proposal_id: string; label: ClaimLabel; reason_en: string | null; reason_vi: string | null; bad_part: string | null;
  confidence: "high" | "medium" | "low" | null; run_id: string; notes: ProposalNotes; created_at: string | null;
}

export interface Decision { label: ClaimLabel; claim_class: string; by: string; at: string | null; reason: string | null }

export interface ReviewItem {
  item_id: string; status: "open" | "resolved"; resolution: string | null; note: string | null;
  resolved_by: string | null; resolved_at: string | null; created_at: string; machine_verdict: string;
  human_label: string; writer: ClaimWriter; scope: string; tenant_id: string | null; anchor_label_id: string;
  set_id: string; set_version: string; claim_hash: string; claim_text: string; field: string; span_start: number;
  span_end: number; split: "held_out" | "train"; stratum: string; sample_id: string | null; content_sha256: string;
  proposal: Proposal | null; panel: Lens[]; decision: Decision | null;
  context?: { found: boolean; before: string | null; after: string | null; evidence: string[] };
}

export interface Facet { set_id: string; set_version: string; status: string; writer: string; proposal: string; n: number }

export interface ReviewPage { items: ReviewItem[]; total: number; limit: number; offset: number; facets: Facet[] }

export interface ItemContext {
  item_id: string; found: boolean; before: string | null; after: string | null; evidence: string[];
  source_text: string | null;
}

export interface AnswerResult { item_id: string; answer: Answer; label: ClaimLabel; resolution: string; inserted: number }

export interface AcceptResult {
  accepted: number; no_proposal: number; not_open: number; unknown: number; refused: { item_id: string; code: string }[];
  labels: Partial<Record<ClaimLabel, number>>;
}

export interface SetSummary {
  set_id: string; set_version: string; rows: number; claims: number; relabels: number;
  evidence_sources: Record<FinalSource, number>; labels: Partial<Record<ClaimLabel, number>>;
  panel_states: Partial<Record<"agreed" | "disputed" | "audit", number>>;
  audit: { audited: number; flipped: number; panel_error_rate: number | null };
  splits: Partial<Record<"held_out" | "train", number>>; writers: Partial<Record<ClaimWriter, number>>;
  last_change: string | null; queue?: { open: number; resolved: number };
}

export interface HistoryEntry {
  kind: "tech_lead" | "relabel"; label: ClaimLabel; claim_class: string; by: string; at: string | null;
  reason: string | null; id: string; previous_label?: ClaimLabel; previous_source?: string;
}

export interface ClaimEntry {
  claim_hash: string; anchor_label_id: string; writer: ClaimWriter; field: string; claim_text: string;
  split: "held_out" | "train"; stratum: string; sample_id: string | null; claim_class: string; label: ClaimLabel;
  source: FinalSource; relabelled: boolean; head_relabel_id: string | null; panel_state: string | null;
  decided_by: string | null; decided_at: string | null; why: string | null; panel: Lens[]; history: HistoryEntry[];
}

export interface ClaimsPage {
  set: string; total: number; limit: number; offset: number; classes: string[]; summary: Omit<SetSummary, "set_id" |
    "set_version" | "queue">; claims: ClaimEntry[];
}

export interface ReviewFilters {
  set: string; writer: "" | ClaimWriter; proposal: ProposalFilter; state: ReviewState;
}

export interface ClaimFilters {
  source: "" | FinalSource; label: "" | ClaimLabel; writer: "" | ClaimWriter; split: "" | "held_out" | "train";
  relabelled: "" | "yes"; q: string;
}

/** "CL-1@1.1.0" <-> {set_id, set_version}; "" = every set. */
export function setKey(setId: string, version: string): string {
  return `${setId}@${version}`;
}

export function parseSetKey(key: string): { set_id: string; set_version: string } | null {
  const i = key.lastIndexOf("@");
  if (i <= 0 || i === key.length - 1) return null;
  return { set_id: key.slice(0, i), set_version: key.slice(i + 1) };
}

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const out = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== "" && v !== null && v !== undefined) out.set(k, String(v));
  const s = out.toString();
  return s ? `?${s}` : "";
}

/** The review list URL for the filters and page (context=false: ids and labels only, for the bulk accept). */
export function reviewUrl(f: ReviewFilters, offset = 0, limit = REVIEW_PAGE, context = true): string {
  const set = parseSetKey(f.set);
  return `${CLAIM_API}/review${qs({ set_id: set?.set_id, set_version: set?.set_version, writer: f.writer,
    proposal: f.proposal, state: f.state, limit, offset, context: context ? undefined : false })}`;
}

export function claimsUrl(setId: string, version: string, f: ClaimFilters, offset = 0, limit = CLAIMS_PAGE): string {
  return `${CLAIM_API}/${encodeURIComponent(setId)}/${encodeURIComponent(version)}/claims${qs({
    source: f.source, label: f.label, writer: f.writer, split: f.split, relabelled: f.relabelled ? true : undefined,
    q: f.q.trim(), limit, offset })}`;
}

/** The sentence cut around the unsupported part (first case-insensitive match), for a <mark>. */
export function splitMark(text: string, bad: string | null | undefined): { text: string; mark: boolean }[] {
  const b = (bad ?? "").trim();
  if (!b) return [{ text, mark: false }];
  const i = text.toLowerCase().indexOf(b.toLowerCase());
  if (i < 0) return [{ text, mark: false }];
  return [{ text: text.slice(0, i), mark: false }, { text: text.slice(i, i + b.length), mark: true },
    { text: text.slice(i + b.length), mark: false }].filter((p) => p.text);
}

/** Facet counts for the filters: the set versions in the queue, and per state / proposal / writer within `set`. */
export function facetCounts(facets: Facet[], set: string) {
  const want = parseSetKey(set);
  const sets = new Map<string, number>();
  const state: Record<string, number> = {};
  const proposal: Record<string, number> = {};
  const writer: Record<string, number> = {};
  for (const f of facets) {
    const key = setKey(f.set_id, f.set_version);
    sets.set(key, (sets.get(key) ?? 0) + f.n);
    if (want && (f.set_id !== want.set_id || f.set_version !== want.set_version)) continue;
    state[f.status] = (state[f.status] ?? 0) + f.n;
    if (f.status === "open") {
      proposal[f.proposal] = (proposal[f.proposal] ?? 0) + f.n;
      writer[f.writer] = (writer[f.writer] ?? 0) + f.n;
    }
  }
  return { sets: [...sets.entries()].map(([key, n]) => ({ key, n })).sort((a, b) => a.key.localeCompare(b.key)),
    state, proposal, writer };
}

/** The open items of a list that carry a proposal (what "accept every remaining proposal" answers). */
export function acceptable(items: Pick<ReviewItem, "item_id" | "status" | "proposal">[]): string[] {
  return items.filter((i) => i.status === "open" && i.proposal).map((i) => i.item_id);
}

/** Item ids in chunks the accept endpoint takes (at most BULK_MAX per call). */
export function chunks<T>(xs: T[], size = BULK_MAX): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += size) out.push(xs.slice(i, i + size));
  return out;
}

/** The proposal's reason in the UI language, else the other one. */
export function proposalReason(p: Pick<Proposal, "reason_en" | "reason_vi">, lang: "en" | "vi"): string {
  return (lang === "vi" ? p.reason_vi || p.reason_en : p.reason_en || p.reason_vi) ?? "";
}

/** A note in the UI language, else the other one ("" when neither). */
export function note(n: ProposalNotes | undefined, key: "claim" | "source_says" | "doubt", lang: "en" | "vi"): string {
  if (!n) return "";
  const en = n[`${key}_en` as keyof ProposalNotes];
  const vi = n[`${key}_vi` as keyof ProposalNotes];
  return (lang === "vi" ? vi || en : en || vi) ?? "";
}

export function labelTone(label: string | null | undefined): "success" | "danger" | "warning" | "neutral" {
  return label === "ok" ? "success" : label === "violation" ? "danger" : label === "unsure" ? "warning" : "neutral";
}

/** Who decided, without the admin id prefix noise ("admin:<uuid>" -> "admin <first 8>"). */
export function deciderLabel(by: string | null | undefined): string {
  if (!by) return "—";
  const m = /^admin:([0-9a-f]{8})/i.exec(by);
  return m ? `admin ${m[1]}` : by;
}

/** How many open items "accept every remaining proposal" would answer under the filters (from the facets). */
export function acceptableCount(facets: Facet[], f: Pick<ReviewFilters, "set" | "writer" | "proposal">): number {
  const want = parseSetKey(f.set);
  let n = 0;
  for (const x of facets) {
    if (x.status !== "open" || x.proposal === "none") continue;
    if (want && (x.set_id !== want.set_id || x.set_version !== want.set_version)) continue;
    if (f.writer && x.writer !== f.writer) continue;
    if (f.proposal && x.proposal !== f.proposal) continue;
    n += x.n;
  }
  return n;
}
