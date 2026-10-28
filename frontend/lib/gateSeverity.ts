// frontend/lib/gateSeverity.ts
// AA-924 PR2 — per-violation severity in the Content Quality view (admin only).
//
// Since AA-924 PR1 a T10 gate-ledger entry may carry `findings: [{text, severity, source}]` and `severity` (the
// worst finding). A merged entry (F2_lexicon, F15_voice) holds findings of several legacy source gates, each with
// its own severity: a warn stock phrase can sit inside an entry that blocks on a banned pattern. A ledger stored
// before AA-924 has no `findings`: every violation then reads the entry's own `blocking` (missing = blocking, the
// legacy default). Highlights and flagged-sentence problems carry their finding's `severity` / `blocking` and the
// `source` gate when it differs from the stored entry (api/routers/admin_content_quality.py highlights()).
//
// Pure helpers with type-level TS only, so lib/gateSeverity.test.mjs runs them with node --test
// --experimental-strip-types.

export type Severity = "block" | "warn";

export interface Finding {
  text: string;
  severity: Severity;
  /** The legacy gate that found it, only when it differs from the stored entry's gate. */
  source: string | null;
}

/** The ledger / flag entry fields these helpers read (old and new shapes). */
export interface LedgerEntryLike {
  gate: string;
  passed: boolean;
  blocking?: boolean;
  severity?: string | null;
  violations?: string[] | null;
  findings?: { text?: unknown; severity?: unknown; source?: unknown }[] | null;
}

/** A highlight or a flagged-sentence problem: its own severity when the API sends one, else `blocking`. */
export interface MarkLike {
  blocking?: boolean | null;
  severity?: string | null;
}

export const SEVERITY_TONE: Record<Severity, "danger" | "warning"> = { block: "danger", warn: "warning" };
export const SEVERITY_LABEL: Record<Severity, string> = { block: "blocking", warn: "warning" };

function asSeverity(v: unknown): Severity | null {
  return v === "block" || v === "warn" ? v : null;
}

/** The severity of a legacy entry (no findings): blocking unless `blocking === false`. */
function legacySeverity(g: { blocking?: boolean }): Severity {
  return g.blocking === false ? "warn" : "block";
}

/** Every finding of an entry, each with its own severity. A legacy entry gives its violations with the entry's
 *  `blocking`. A finding with an unknown severity reads warn (the API's own default). */
export function findingsOf(g: LedgerEntryLike): Finding[] {
  if (Array.isArray(g.findings)) {
    return g.findings
      .filter((f) => f && typeof f.text === "string")
      .map((f) => ({
        text: f.text as string,
        severity: asSeverity(f.severity) ?? "warn",
        source: typeof f.source === "string" && f.source && f.source !== g.gate ? f.source : null,
      }));
  }
  const sev = legacySeverity(g);
  return (g.violations ?? []).filter((v) => typeof v === "string").map((v) => ({ text: v, severity: sev, source: null }));
}

/** The entry's overall severity: null when it passed; else its stored `severity`, else the worst finding, else the
 *  legacy `blocking`. */
export function entrySeverity(g: LedgerEntryLike): Severity | null {
  if (g.passed) return null;
  const stored = asSeverity(g.severity);
  if (stored) return stored;
  if (Array.isArray(g.findings) && g.findings.length > 0) {
    return findingsOf(g).some((f) => f.severity === "block") ? "block" : "warn";
  }
  return legacySeverity(g);
}

/** Counts per severity of an entry's findings (for the "2 blocking · 1 warning" line). */
export function severityCounts(g: LedgerEntryLike): Record<Severity, number> {
  const out: Record<Severity, number> = { block: 0, warn: 0 };
  for (const f of findingsOf(g)) out[f.severity] += 1;
  return out;
}

/** How countsLabel words one count. English by default; AA-994: a page passes the UI language's
 *  (lib/gateSeverity.messages.ts SEVERITY_MESSAGES, keys countBlock / countWarn). */
export interface CountWords {
  countBlock: (n: number) => string;
  countWarn: (n: number) => string;
}

export const COUNT_WORDS: CountWords = {
  countBlock: (n) => `${n} blocking`,
  countWarn: (n) => `${n} warning${n === 1 ? "" : "s"}`,
};

/** "2 blocking · 1 warning" (only the non-zero parts; "" when nothing). */
export function countsLabel(c: Record<Severity, number>, words: CountWords = COUNT_WORDS): string {
  const parts: string[] = [];
  if (c.block) parts.push(words.countBlock(c.block));
  if (c.warn) parts.push(words.countWarn(c.warn));
  return parts.join(" · ");
}

/** The severity of a highlight or a problem: its own `severity`, else `blocking` (missing = blocking). */
export function markSeverity(m: MarkLike): Severity {
  return asSeverity(m.severity) ?? (m.blocking === false ? "warn" : "block");
}

/** The worst severity of the marks a gate has on one sentence (block when any blocks); null when none. */
export function worstOf(marks: MarkLike[]): Severity | null {
  if (marks.length === 0) return null;
  return marks.some((m) => markSeverity(m) === "block") ? "block" : "warn";
}
