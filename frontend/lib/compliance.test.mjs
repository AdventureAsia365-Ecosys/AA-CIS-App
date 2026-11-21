// frontend/lib/compliance.test.mjs
// AA-1170: unit tests for the Platform › Compliance helpers in compliance.ts (invented controls with the EX- prefix).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FRAMEWORKS, checkTone, controlTone, deltaToReference, fmtPct, frameworkName, inFramework, isLegal, safeLink,
  sortControls, trendSeries,
} from "./compliance.ts";

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
