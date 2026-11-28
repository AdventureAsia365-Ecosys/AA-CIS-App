// frontend/lib/compliance.test.mjs
// AA-1170: unit tests for the Platform › Compliance helpers in compliance.ts (invented controls with the EX- prefix).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FRAMEWORKS, checkTone, controlTone, deltaToReference, fmtPct, frameworkList, frameworkName, inFramework, isLegal,
  lawsWithItems, obligationTone, pivotApplicability, refreshCounts, safeLink, safeSourceLink, sortControls,
  skipReason, sortObligations, trendSeries, verdictTone,
  exceptionActions, exceptionTone, maxExpiry, riskTone, sealedTone, sortExceptions,
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
  // worst first inside a law, then the law's own order (sort_order alone would put the met Art.1 first)
  assert.deepEqual(sortObligations([it("Art.1", "met", 1), it("Art.2", "not_checked", 2), it("Art.3", "not_met", 3),
    it("Art.4", "not_met", 4)]).map((i) => i.ref), ["Art.3", "Art.4", "Art.2", "Art.1"]);
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
    crm_ops: { state: "recent" } } });
  assert.deepEqual([ok.ok, ok.tone, ok.read, ok.fresh, ok.sources.length], [true, "ok", 14, 3, 2]);
  assert.equal(refreshCounts({ state: "ok", live: { cis_db: { state: "error" } } }).tone, "error");
  assert.equal(refreshCounts({ state: "unavailable" }).tone, "error");
  assert.equal(refreshCounts(null).ok, false);
});

test("a skipped CRM collection or nothing published is a warning, never a success", () => {
  const skipped = refreshCounts({ state: "ok", rows: 14, new: 3, live: { cis_db: { state: "ok", rows: 2, new: 2 },
    crm_ops: { state: "skipped", reason: "cognito_session_required", http: 401 } } });
  assert.deepEqual([skipped.ok, skipped.tone], [false, "warning"]);
  assert.deepEqual([refreshCounts({ state: "missing" }).ok, refreshCounts({ state: "missing" }).tone],
    [false, "warning"]);
  assert.equal(skipReason("cognito_session_required", 401), "cognito_session");
  assert.equal(skipReason("Unauthorized", 401), "unauthorized");
  assert.equal(skipReason("forbidden", 403), "forbidden");
  assert.equal(skipReason("view_as_read_only"), "view_as");
  assert.equal(skipReason(undefined), "unknown");
});

test("no page text claims legal compliance and the page says it is not legal advice", () => {
  const text = readFileSync(new URL("../app/admin/platform/compliance/messages.ts", import.meta.url), "utf8");
  assert.ok(!/\b(compliant|complies|in compliance)\b/i.test(text));
  assert.match(text, /notLegalAdvice: "Readiness, not legal advice\./);
});

// ── AA-1173: exceptions, reviews, sealed evidence ───────────────────────────────────────────────────────────────────

const exc = (id, state, extra = {}) => ({
  exception_id: id, subject_kind: "control", subject_ref: `EX-IAM-0${id}`, title: "Invented", reason: "x".repeat(20),
  compensating: null, risk_level: "low", status: state === "expired" ? "approved" : state, state, days_left: 10,
  proposed_by: "admin:maker", proposed_at: `2026-11-0${id}T00:00:00Z`, expires_on: `2026-12-0${id}`,
  decided_by: null, decided_at: null, decision_note: null, self_approved: false, revoked_by: null, revoked_at: null,
  revoke_note: null, ...extra,
});

test("exceptions: open first, then approved by nearest expiry, then the rest newest first", () => {
  const rows = [exc(1, "revoked"), exc(2, "approved", { expires_on: "2026-12-20" }), exc(3, "proposed"),
    exc(4, "approved", { expires_on: "2026-12-05" }), exc(5, "expired")];
  assert.deepEqual(sortExceptions(rows).map((r) => r.exception_id), [3, 4, 2, 5, 1]);
});

test("exceptions: maker-checker actions follow two-person approval", () => {
  const open = exc(1, "proposed");
  assert.deepEqual(exceptionActions(open, "admin:checker", true), { approve: true, reject: true, revoke: true });
  assert.deepEqual(exceptionActions(open, "admin:maker", true), { approve: false, reject: false, revoke: true });
  assert.deepEqual(exceptionActions(open, "admin:maker", false), { approve: true, reject: false, revoke: true });
  assert.deepEqual(exceptionActions(exc(2, "approved"), "admin:maker", true), { approve: false, reject: false, revoke: true });
  assert.deepEqual(exceptionActions(exc(3, "revoked"), "admin:checker", false), { approve: false, reject: false, revoke: false });
});

test("exceptions: the latest expiry, tones", () => {
  assert.equal(maxExpiry("2026-12-01", 90), "2027-03-01");
  assert.equal(maxExpiry("2026-12-01", 0), "2026-12-02");
  assert.equal(exceptionTone("approved"), "success");
  assert.equal(exceptionTone("expired"), "warning");
  assert.equal(riskTone("high"), "danger");
  assert.equal(sealedTone("ok"), "success");
  assert.equal(sealedTone("broken"), "danger");
  assert.equal(sealedTone("missing"), "neutral");
});

test("the governance messages have every Vietnamese string", async () => {
  const src = readFileSync(new URL("../app/admin/platform/compliance/governance.messages.ts", import.meta.url), "utf8");
  assert.ok(src.includes("tabExceptions: \"Ngoại lệ\""));
});

// ── AA-1184: the Calendar tab ──────────────────────────────────────────────────────────────────────────────────────

const line = (id, state, due = null, extra = {}) => ({
  id, kind: "deadline", state, due, last: null, owner: "tech_lead", title: `Invented duty ${id}`, check: null,
  check_state: null, extra: null, note: null, ...extra,
});

test("calendar tones: overdue red, due soon and open amber, calm states neutral or green", async () => {
  const { calendarTone, needsAttention } = await import("./compliance.ts");
  assert.equal(calendarTone("overdue"), "danger");
  assert.equal(calendarTone("due_soon"), "warning");
  assert.equal(calendarTone("open"), "warning");
  assert.equal(calendarTone("done"), "success");
  assert.equal(calendarTone("automated"), "neutral");
  assert.equal(calendarTone("bogus"), "neutral");
  assert.ok(needsAttention(line("OC-1", "open")));
  assert.ok(!needsAttention(line("OC-1", "ok", "2026-12-20")));
});

test("calendar filters keep worst first, then by due date", async () => {
  const { filterCalendar, calendarFilterOf } = await import("./compliance.ts");
  const lines = [line("OC-3", "ok", "2026-12-20"), line("OC-2", "due_soon", "2026-12-04"),
    line("OC-1", "overdue", "2026-12-01"), line("OC-4", "automated"), line("OC-5", "open")];
  assert.deepEqual(filterCalendar(lines, "attention").map((l) => l.id), ["OC-1", "OC-2", "OC-5"]);
  assert.deepEqual(filterCalendar(lines, "dated").map((l) => l.id), ["OC-1", "OC-2", "OC-3"]);
  assert.deepEqual(filterCalendar(lines, "all").map((l) => l.id), ["OC-1", "OC-2", "OC-5", "OC-3", "OC-4"]);
  assert.equal(calendarFilterOf("all"), "all");
  assert.equal(calendarFilterOf("nonsense"), "attention");
  assert.equal(calendarFilterOf(null), "attention");
});

test("days until a due date", async () => {
  const { daysUntil } = await import("./compliance.ts");
  assert.equal(daysUntil("2026-12-04", "2026-12-02"), 2);
  assert.equal(daysUntil("2026-11-27", "2026-12-02"), -5);
  assert.equal(daysUntil(null, "2026-12-02"), null);
  assert.equal(daysUntil("not-a-date", "2026-12-02"), null);
});
