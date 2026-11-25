// lib/evalRuns.ts — AA-1169: Writing Lab › Eval runs (/admin/lab/eval-runs). The shapes of the three eval-run reads
// (GET /api/admin/writing-memory/eval-runs[/{run}[/items/{item}]], services/writing_memory/eval_review.py) and of the
// quality lineage reads (GET /api/admin/quality/lineage/*, services/quality/lineage.py and source_fanout.py), plus the
// pure helpers of the page: links, tones, the flagged-claim highlight and the Markdown export. Read-only, $0.
// Dependency-free so `npm run test:unit` loads it directly.

export const EVAL_RUNS_PATH = "/admin/lab/eval-runs";
export const EVAL_RUNS_API = "/api/admin/writing-memory/eval-runs";
export const LINEAGE_API = "/api/admin/quality/lineage";

export const EVAL_TABS = ["runs", "coverage", "lookup"] as const;
export type EvalTab = (typeof EVAL_TABS)[number];

export function parseEvalTab(v: string | null | undefined): EvalTab {
  return (EVAL_TABS as readonly string[]).includes(v ?? "") ? (v as EvalTab) : "runs";
}

export const EVAL_WRITERS = ["a1", "t2", "t9_blog", "t9_social"] as const;
export type EvalWriter = (typeof EVAL_WRITERS)[number];
export const RUN_KINDS = ["baseline_import", "r0_replay", "r1", "r2", "reverify", "probe", "shadow"] as const;
export const LINEAGE_WORKFLOWS = ["s1", "t2", "t9", "eval"] as const;
export type LineageWorkflow = (typeof LINEAGE_WORKFLOWS)[number];
/** The coverage windows offered (the API accepts 1-90 days). */
export const COVERAGE_DAYS = [7, 30, 90] as const;

// ── shapes ────────────────────────────────────────────────────────────────────────────────────────────────────────

export interface EvalRun {
  run_id: string; run_kind: string; writer: string | null; prompt_version: string; created_at: string;
  finished_at: string | null; verdict: string | null; stop_reason: string | null; cost_usd: number | null;
  cost_cap_usd: number | null; tour_count: number; judge_kappa: number | null; golden_set_id: string | null;
  release_id: string | null; triggered_by: string; prereg_id: string | null; prereg_arm: string | null;
  results: number; passes: number; fails: number; errors: number; outputs: number;
}

export interface RunsPage { runs: EvalRun[]; total: number; limit: number; offset: number }

export interface RunRow {
  result_id: string; arm: string; mode: string; phase: string; verdict: string; hard_stop: boolean;
  failure_codes: string[]; cost_usd: number | null; claims: number; claim_classes: Record<string, number>;
  has_output: boolean; scored_result_id: string | null;
}

export interface RunItem {
  item_id: string; sample_id: string; scope: string; split: string; kind: string; country: string | null;
  days: number | null; label_verdict: string; rows: RunRow[];
}

export interface ArmSummary { rows: number; judged: number; passes: number; claims: number; hard_stops: number;
  pass_share: number | null }

export interface RunView { run: EvalRun; items: RunItem[]; summary: Record<string, ArmSummary> }

export interface TypedClaim {
  claim: string; why: string | null; judge_class: string | null; policy_class: string; field: string;
  pos: number | null;
}

export interface GateReplay { passed?: boolean; first_failure?: string | null; blocking?: string[]; error?: string }

export interface ItemResult {
  result_id: string; arm: string; mode: string; phase: string; verdict: string; score: number | null;
  failure_codes: string[]; hard_stop: boolean; judge_model: string | null; judge_version: string | null;
  cost_usd: number | null; created_at: string | null; scored_result_id: string | null;
  output_source: "own" | "scored" | null; output_sha256: string | null;
  output_fields: { field: string; text: string }[]; claims: TypedClaim[]; checks: Record<string, unknown>;
  judge: Record<string, unknown> | null; gate_replay: GateReplay | null;
}

export interface ItemView {
  run: EvalRun;
  item: {
    item_id: string; sample_id: string; writer: string; channel: string | null; scope: string;
    tenant_id: string | null; split: string; kind: string; tiers: string[]; country: string | null;
    days: number | null; label_verdict: string; label_source: string; label: unknown; input_sha256: string;
    pick_reason: string | null;
  };
  source_text: string | null;
  input: unknown;
  results: ItemResult[];
}

export interface CoverageField { count: number; share: number | null }
export interface CoverageRow {
  workflow: string; objects: number; fields: Record<string, CoverageField | null>; complete_share_floor: number | null;
}
export interface CoverageView { days: number; lineage_version: string; fields: string[]; workflows: CoverageRow[] }

export interface LineageCall {
  stage: string | null; prompt_version: string | null; route_config_version: number | null; model: string | null;
  calls: number; first_at: string | null; last_at: string | null;
}
export interface ObjectLineage {
  workflow: string; object_id: string; tenant_id: string | null; created_at: string | null; status: string | null;
  policy_sha: string | null; fs_id: string | null;
  prompt_version: { writer: string | null; stages: Record<string, string[]> };
  route_config_version: { stamped: number | null; ledger: number[] };
  calls: LineageCall[]; ledger_join: string | null; lineage_version: string | null;
  run?: { run_id: string; item_id: string; prompt_version: string | null; config_fp: unknown };
  tour_id?: string; missing: string[]; complete: boolean;
}

export interface FanoutMark { [key: string]: unknown }
export interface TourFanout {
  tour_id: string; open: number; cleared_recent: number; by_object_kind: Record<string, number>;
  by_reason: Record<string, number>; marks: FanoutMark[];
}

// ── links ─────────────────────────────────────────────────────────────────────────────────────────────────────────

export interface RunFilters { writer?: string; run_kind?: string; prereg?: string }

export function runsUrl(f: RunFilters, limit: number, offset: number): string {
  const q = new URLSearchParams();
  if (f.writer) q.set("writer", f.writer);
  if (f.run_kind) q.set("run_kind", f.run_kind);
  const prereg = (f.prereg ?? "").trim();
  if (prereg) q.set("prereg", prereg);
  q.set("limit", String(limit));
  q.set("offset", String(offset));
  return `${EVAL_RUNS_API}?${q.toString()}`;
}

export function runUrl(runId: string): string {
  return `${EVAL_RUNS_API}/${encodeURIComponent(runId)}`;
}

export function itemUrl(runId: string, itemId: string): string {
  return `${EVAL_RUNS_API}/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemId)}`;
}

export function lineageUrl(workflow: LineageWorkflow, objectId: string): string {
  return `${LINEAGE_API}/${workflow}/${encodeURIComponent(objectId.trim())}`;
}

export function coverageUrl(days: number): string {
  return `${LINEAGE_API}/coverage?days=${days}`;
}

export function fanoutUrl(tourId: string): string {
  return `${LINEAGE_API}/fanout/${encodeURIComponent(tourId.trim())}`;
}

/** The page link of a run (and optionally one of its items). */
export function evalRunHref(runId?: string | null, itemId?: string | null): string {
  const q = new URLSearchParams();
  if (runId) q.set("run", runId);
  if (runId && itemId) q.set("item", itemId);
  const s = q.toString();
  return s ? `${EVAL_RUNS_PATH}?${s}` : EVAL_RUNS_PATH;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function isUuid(v: string | null | undefined): boolean {
  return UUID_RE.test((v ?? "").trim());
}

// ── tones and numbers ─────────────────────────────────────────────────────────────────────────────────────────────

export type VerdictTone = "success" | "danger" | "warning" | "neutral" | "info";

export function resultTone(verdict: string | null | undefined): VerdictTone {
  if (verdict === "pass") return "success";
  if (verdict === "fail") return "danger";
  if (verdict === "error") return "warning";
  return "neutral";
}

export function runTone(verdict: string | null | undefined): VerdictTone {
  if (verdict === "pass") return "success";
  if (verdict === "fail" || verdict === "invalid") return "danger";
  if (verdict === "informative" || verdict === "baseline") return "info";
  return "neutral";
}

export function usd(n: number | null | undefined): string {
  if (n == null) return "—";
  return `$${n.toFixed(n > 0 && n < 1 ? 4 : 2)}`;
}

export function pct(share: number | null | undefined): string {
  return share == null ? "—" : `${Math.round(share * 1000) / 10}%`;
}

/** "rarity 1 · route_timing 2" from a class count map. */
export function classLine(classes: Record<string, number>): string {
  return Object.entries(classes).map(([k, n]) => `${k} ${n}`).join(" · ");
}

/** The row of one arm in a run item: the candidate's regenerate row, the control's re-judge or regenerate row. */
export function armRow(item: RunItem, arm: "candidate" | "control"): RunRow | null {
  const rows = item.rows.filter((r) => r.arm === arm);
  return rows.find((r) => r.mode === "regenerate") ?? rows.find((r) => r.mode === "rejudge") ?? rows[0] ?? null;
}

// ── flagged claims in the text ────────────────────────────────────────────────────────────────────────────────────

export interface Segment { text: string; claim: number | null }

/** Split `text` into plain and flagged parts: each claim (index into `claims`) is found case-insensitively, first
 *  occurrence, trailing full stop optional. Claims not found (cut, rewritten) are simply not marked. Overlaps keep
 *  the earlier one. Pure. */
export function highlightSegments(text: string, claims: readonly { claim: string }[]): Segment[] {
  const lower = text.toLowerCase();
  const spans: { start: number; end: number; claim: number }[] = [];
  claims.forEach((c, i) => {
    const probe = (c.claim ?? "").trim().replace(/[.!?]+$/, "").toLowerCase();
    if (probe.length < 3) return;
    const at = lower.indexOf(probe);
    if (at >= 0) spans.push({ start: at, end: at + probe.length, claim: i });
  });
  spans.sort((a, b) => a.start - b.start);
  const out: Segment[] = [];
  let cursor = 0;
  for (const s of spans) {
    if (s.start < cursor) continue;
    if (s.start > cursor) out.push({ text: text.slice(cursor, s.start), claim: null });
    out.push({ text: text.slice(s.start, s.end), claim: s.claim });
    cursor = s.end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor), claim: null });
  return out;
}

// ── Markdown export ───────────────────────────────────────────────────────────────────────────────────────────────

/** A fence longer than any backtick run inside `body`, so the export stays valid Markdown. */
function fence(body: string, lang = "text"): string {
  const longest = Math.max(2, ...Array.from(body.matchAll(/`+/g), (m) => m[0].length));
  const f = "`".repeat(longest + 1);
  return `${f}${lang}\n${body}\n${f}`;
}

function oneLine(v: unknown): string {
  return String(v ?? "—").replace(/\s+/g, " ").trim() || "—";
}

export function runHeaderMarkdown(run: EvalRun): string {
  return [
    `- Run: \`${run.run_id}\` · ${run.run_kind} · writer ${run.writer ?? "—"}`,
    `- Pre-registration: ${run.prereg_id ? `${run.prereg_id} (arm ${run.prereg_arm ?? "—"})` : "—"}`,
    `- Verdict: ${run.verdict ?? "—"} · stop: ${run.stop_reason ?? "—"} · cost ${usd(run.cost_usd)}`,
    `- Created: ${run.created_at}`,
  ].join("\n");
}

/** One item of a run as Markdown: the source the judge read, then each row (verdict, gates, claims, output) and an
 *  empty review checklist for the reader's own notes. Pure. */
export function itemMarkdown(v: ItemView, opts: { withRun?: boolean; level?: number } = {}): string {
  const h = (n: number) => "#".repeat(Math.min(6, (opts.level ?? 1) + n - 1));
  const it = v.item;
  const out: string[] = [];
  out.push(`${h(1)} ${it.sample_id} (${it.writer}${it.channel ? ` · ${it.channel}` : ""})`);
  out.push("");
  if (opts.withRun !== false) out.push(runHeaderMarkdown(v.run));
  out.push(`- Item: \`${it.item_id}\` · ${it.scope} · split ${it.split} · ${it.kind}` +
    `${it.country ? ` · ${it.country}` : ""}${it.days ? ` · ${it.days} days` : ""}`);
  out.push(`- Golden label: ${it.label_verdict} (${it.label_source})`);
  out.push("");
  out.push(`${h(2)} Source the judge read`);
  out.push("");
  out.push(v.source_text ? fence(v.source_text) : "_Not rebuilt from the stored input (see the raw input)._");
  for (const r of v.results) {
    out.push("");
    out.push(`${h(2)} ${r.arm} · ${r.mode} — ${r.verdict}${r.hard_stop ? " (hard stop)" : ""}`);
    out.push("");
    out.push(`- Result: \`${r.result_id}\` · phase ${r.phase} · judge ${r.judge_model ?? "—"} ${r.judge_version ?? ""}`.trimEnd());
    out.push(`- Failure codes: ${r.failure_codes.length ? r.failure_codes.join(", ") : "none"}`);
    if (r.gate_replay) {
      out.push(r.gate_replay.error
        ? `- Gate replay: could not be replayed (${r.gate_replay.error})`
        : `- Gate replay: ${r.gate_replay.passed ? "passed" : "failed"}` +
          `${r.gate_replay.first_failure ? ` · first failure ${r.gate_replay.first_failure}` : ""}` +
          `${r.gate_replay.blocking?.length ? ` · blocking ${r.gate_replay.blocking.join(", ")}` : ""}`);
    }
    if (r.output_source === "scored") out.push(`- Output: the scored row \`${r.scored_result_id}\``);
    out.push("");
    out.push(`${h(3)} Flagged claims (${r.claims.length})`);
    out.push("");
    if (r.claims.length) {
      r.claims.forEach((c, i) => out.push(`${i + 1}. [${c.policy_class} · ${c.field}] ${oneLine(c.claim)}` +
        `${c.why ? ` — ${oneLine(c.why)}` : ""}`));
    } else {
      out.push("None.");
    }
    out.push("");
    out.push(`${h(3)} Output`);
    out.push("");
    if (r.output_fields.length) {
      for (const f of r.output_fields) {
        out.push(`**${f.field}**`);
        out.push("");
        out.push(fence(f.text));
        out.push("");
      }
    } else {
      out.push("No stored output.");
      out.push("");
    }
  }
  out.push(`${h(2)} Review`);
  out.push("");
  out.push("- [ ] Contract violations: ");
  out.push("- [ ] Unsupported trip promises: ");
  out.push("- Notes: ");
  return out.join("\n") + "\n";
}

/** A whole run as one Markdown file: the run header, then every item read (in sample order). Pure. */
export function runMarkdown(run: EvalRun, items: ItemView[]): string {
  const head = [`# Eval run ${run.prereg_id ?? run.run_id}`, "", runHeaderMarkdown(run), `- Items: ${items.length}`, ""];
  return head.join("\n") + "\n" + items.map((v) => itemMarkdown(v, { withRun: false, level: 2 })).join("\n");
}

export function markdownFilename(run: EvalRun, sampleId?: string): string {
  const base = (run.prereg_id || run.run_id.slice(0, 8)).replace(/[^a-zA-Z0-9-]+/g, "-");
  return sampleId ? `${base}-${sampleId.replace(/[^a-zA-Z0-9-]+/g, "-")}.md` : `${base}.md`;
}
