// frontend/lib/gateSeverity.test.mjs
// AA-924 PR2: unit tests for the per-violation severity helpers in gateSeverity.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types; gateSeverity.ts holds only type-level TS).
//
// The merged entry below is the shape AA-924 PR1 stores (quality_gates.merge_ledger): a blocking F2_lexicon entry
// with a blocking banned pattern and a warn stock phrase found by F15_voice.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SEVERITY_LABEL,
  SEVERITY_TONE,
  countsLabel,
  entrySeverity,
  findingsOf,
  markSeverity,
  severityCounts,
  worstOf,
} from "./gateSeverity.ts";

const merged = {
  gate: "F2_lexicon", passed: false, blocking: true, severity: "block", repairable: true,
  violations: ["banned pattern /x/ -> 'hidden gem'", "stock phrase 'worth the trip'"],
  findings: [
    { text: "banned pattern /x/ -> 'hidden gem'", severity: "block", source: "F2_banned_patterns" },
    { text: "stock phrase 'worth the trip'", severity: "warn", source: "F15_voice" },
  ],
  parts: [],
};

test("a merged entry gives each finding with its own severity and source", () => {
  assert.deepEqual(findingsOf(merged), [
    { text: "banned pattern /x/ -> 'hidden gem'", severity: "block", source: "F2_banned_patterns" },
    { text: "stock phrase 'worth the trip'", severity: "warn", source: "F15_voice" },
  ]);
  assert.equal(entrySeverity(merged), "block");
  assert.deepEqual(severityCounts(merged), { block: 1, warn: 1 });
  assert.equal(countsLabel(severityCounts(merged)), "1 blocking · 1 warning");
});

test("a source equal to the stored gate is not repeated", () => {
  const g = { gate: "F15_voice", passed: false, findings: [{ text: "contrast frame", severity: "warn", source: "F15_voice" }] };
  assert.equal(findingsOf(g)[0].source, null);
  assert.equal(entrySeverity(g), "warn"); // no stored severity: the worst finding
});

test("a legacy entry reads every violation with the entry's blocking", () => {
  const legacy = { gate: "F12_scope", passed: false, blocking: false, violations: ["a", "b"] };
  assert.deepEqual(findingsOf(legacy).map((f) => f.severity), ["warn", "warn"]);
  assert.equal(entrySeverity(legacy), "warn");
  const blocking = { gate: "F1_grounding", passed: false, violations: ["n"] }; // blocking missing = blocking
  assert.equal(entrySeverity(blocking), "block");
  assert.deepEqual(findingsOf(blocking), [{ text: "n", severity: "block", source: null }]);
  assert.equal(countsLabel(severityCounts({ ...legacy, violations: ["a", "b", "c"] })), "3 warnings");
});

test("a passed entry has no severity; junk findings are skipped", () => {
  assert.equal(entrySeverity({ ...merged, passed: true }), null);
  const g = { gate: "F2_lexicon", passed: false, findings: [null, { text: 3 }, { text: "ok", severity: "oops" }] };
  assert.deepEqual(findingsOf(g), [{ text: "ok", severity: "warn", source: null }]);
  assert.equal(entrySeverity({ gate: "x", passed: false, findings: [], blocking: false }), "warn");
  assert.equal(countsLabel({ block: 0, warn: 0 }), "");
});

test("a highlight or problem reads its own severity first, then blocking", () => {
  assert.equal(markSeverity({ severity: "warn", blocking: true }), "warn");
  assert.equal(markSeverity({ blocking: false }), "warn");
  assert.equal(markSeverity({}), "block");
  assert.equal(worstOf([{ severity: "warn" }, { blocking: true }]), "block");
  assert.equal(worstOf([{ severity: "warn" }]), "warn");
  assert.equal(worstOf([]), null);
});

test("tones and labels match the kit badge tones", () => {
  assert.deepEqual(SEVERITY_TONE, { block: "danger", warn: "warning" });
  assert.deepEqual(SEVERITY_LABEL, { block: "blocking", warn: "warning" });
});
