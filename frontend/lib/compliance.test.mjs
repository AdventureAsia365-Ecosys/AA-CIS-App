// frontend/lib/compliance.test.mjs
// AA-1170: unit tests for the Platform › Compliance helpers in compliance.ts (invented controls with the EX- prefix).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FRAMEWORKS, checkTone, controlTone, deltaToReference, fmtPct, frameworkList, frameworkName, inFramework, isLegal,
  lawsWithItems, obligationTone, pivotApplicability, refreshCounts, safeLink, safeSourceLink, sortControls,
  sortObligations, trendSeries, verdictTone,
} from "./compliance.ts";
import { readFileSync } from "node:fs";

const row = (id, state, frameworks = {}) => ({
  control_id: id, title: id, domain: "governance", owner_role: "tech lead", frequency: "daily", test_type: "auto",
  scope: "dev", active: true, state, frameworks, checks: [], last_evidence_at: null,
});

test("the six frameworks of CIS 466, EU AI Act marked legal", () => {
  assert.equal(FRAMEWORKS.length, 6);
  assert.equal(frameworkName("soc2:2017"), "SOC 2 (TSC common criteria)");
  assert.equal(frameworkName("unknown"), "unknown");
  assert.ok(isLegal("eu-ai-act:2024"));
  assert.ok(!isLegal("iso27001:2022"));
});

test("tones and the worst-first control order", () => {
  assert.equal(checkTone("pass"), "success");
  assert.equal(checkTone("stale"), "warning");
  assert.equal(checkTone(undefined), "neutral");
  assert.equal(controlTone("gap"), "danger");
  const sorted = sortControls([row("EX-B-01", "met"), row("EX-A-01", "gap"), row("EX-C-01", "stale"),
    row("EX-A-02", "gap")]);
  assert.deepEqual(sorted.map((r) => r.control_id), ["EX-A-01", "EX-A-02", "EX-C-01", "EX-B-01"]);
});

test("percent and delta to the reference", () => {
  assert.equal(fmtPct(45.81), "45.8 %");
  assert.equal(fmtPct(null), "—");
  const s = { framework: "iso27001:2022", n_requirements: 4, n_met: 1, n_partial: 0, n_gap: 3, n_stale: 0,
    n_unmapped: 0, strict_pct: 25, weighted_pct: 25, reference_pct: 45.8 };
  assert.equal(deltaToReference(s), "−20.8 pts");
  assert.equal(deltaToReference({ ...s, reference_pct: null }), null);
  assert.equal(deltaToReference({ ...s, reference_pct: 25 }), "±0.0 pts");
});

test("trend rows by date with one key per framework", () => {
  const rows = trendSeries([
    { date: "2026-11-21", framework: "soc2:2017", strict_pct: 10, weighted_pct: 20, reference_pct: 44 },
    { date: "2026-11-20", framework: "soc2:2017", strict_pct: 5, weighted_pct: 15, reference_pct: 44 },
    { date: "2026-11-21", framework: "iso27001:2022", strict_pct: 1, weighted_pct: 2, reference_pct: null },
  ]);
  assert.deepEqual(rows, [{ date: "2026-11-20", "soc2:2017": 15 },
    { date: "2026-11-21", "soc2:2017": 20, "iso27001:2022": 2 }]);
});

test("framework filter and safe links", () => {
  assert.ok(inFramework(row("EX-A-01", "met", { "soc2:2017": 2 }), "soc2:2017"));
  assert.ok(!inFramework(row("EX-A-01", "met", {}), "soc2:2017"));
  assert.ok(inFramework(row("EX-A-01", "met", {}), "all"));
  assert.equal(safeLink("https://github.com/x"), "https://github.com/x");
  assert.equal(safeLink("javascript:alert(1)"), null);
});

// ── AA-1174 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

const INFO = [
  { framework: "iso27001:2022", title: "ISO/IEC 27001:2022", kind: "standard", scored: true },
  { framework: "ex-law:2025", title: "Example personal data law", kind: "law", scored: false },
];

test("framework names and kinds come from the API, the six are only a fallback", () => {
  assert.equal(frameworkName("ex-law:2025", INFO), "Example personal data law");
  assert.equal(frameworkName("ex-law:2025"), "ex-law:2025");
  assert.ok(isLegal("ex-law:2025", INFO));
  assert.ok(!isLegal("iso27001:2022", INFO));
  assert.deepEqual(frameworkList(INFO).map((f) => f.key), ["iso27001:2022", "ex-law:2025"]);
  assert.equal(frameworkList(null).length, 6);
});

const mrow = (market, segment, framework, verdict = "applies") => ({
  market, segment, framework, test: "t", threshold: null, verdict, source_url: "https://eur-lex.europa.eu/x",
  source_title: "s", assessed_on: "2026-11-23", counsel_review: true, note: null, review_due: false,
});

test("the applicability matrix pivots to market x segment rows and one column per law", () => {
  const p = pivotApplicability([mrow("DE", "traveller", "gdpr:2016"), mrow("DE", "traveller", "eu-eprivacy:2002"),
    mrow("AU", "traveller", "au-privacy:1988", "not_yet"), mrow("DE", "agency_client", "gdpr:2016")]);
  assert.deepEqual(p.laws, ["gdpr:2016", "eu-eprivacy:2002", "au-privacy:1988"]);
  assert.deepEqual(p.rows.map((r) => r.key), ["AU|traveller", "DE|traveller", "DE|agency_client"]);
  assert.equal(p.rows[1].cells["eu-eprivacy:2002"].verdict, "applies");
  assert.equal(p.rows[0].cells["gdpr:2016"], undefined);
});

test("obligation and verdict tones, worst first, laws with a checklist", () => {
  assert.equal(obligationTone("met"), "success");
  assert.equal(obligationTone("not_met"), "danger");
  assert.equal(obligationTone("not_checked"), "neutral");
  assert.equal(verdictTone("applies"), "accent");
  assert.equal(verdictTone("not_yet"), "warning");
  const it = (ref, state, sort_order) => ({ framework: "ex-law:2025", ref, state, sort_order });
  assert.deepEqual(sortObligations([it("A", "met", 2), it("B", "not_met", 1)]).map((i) => i.ref), ["B", "A"]);
  const law = (framework, total) => ({ framework, counts: { total } });
  assert.deepEqual(lawsWithItems([law("a", 0), law("b", 3)]).map((l) => l.framework), ["b"]);
});

test("legal source links open only official hosts", () => {
  assert.equal(safeSourceLink("https://eur-lex.europa.eu/eli/reg/2016/679/oj"),
    "https://eur-lex.europa.eu/eli/reg/2016/679/oj");
  assert.equal(safeSourceLink("https://www.legislation.gov.uk/ukpga/2018/12"),
    "https://www.legislation.gov.uk/ukpga/2018/12");
  assert.equal(safeSourceLink("https://eur-lex.europa.eu.evil.example/x"), null);
  assert.equal(safeSourceLink("http://eur-lex.europa.eu/x"), null);
  assert.equal(safeSourceLink("javascript:alert(1)"), null);
  assert.equal(safeSourceLink(null), null);
  assert.equal(safeSourceLink("https://vbpl.moj.gov.vn/TW/x.doc"), "https://vbpl.moj.gov.vn/TW/x.doc");
  assert.equal(safeSourceLink("https://gov.vn.example.com/x"), null);
});

test("the refresh result counts the published file and each live source", () => {
  const ok = refreshCounts({ state: "ok", rows: 14, new: 3, live: { cis_db: { state: "ok", rows: 2, new: 2 },
    crm_ops: { state: "skipped", reason: "cognito_session_required", http: 401 } } });
  assert.deepEqual([ok.ok, ok.read, ok.fresh, ok.sources.length], [true, 14, 3, 2]);
  assert.equal(refreshCounts({ state: "ok", live: { cis_db: { state: "error" } } }).ok, false);
  assert.equal(refreshCounts({ state: "unavailable" }).ok, false);
  assert.equal(refreshCounts(null).ok, false);
});

test("no page text claims legal compliance and the page says it is not legal advice", () => {
  const text = readFileSync(new URL("../app/admin/platform/compliance/messages.ts", import.meta.url), "utf8");
  assert.ok(!/\b(compliant|complies|in compliance)\b/i.test(text));
  assert.match(text, /notLegalAdvice: "Readiness, not legal advice\./);
});
