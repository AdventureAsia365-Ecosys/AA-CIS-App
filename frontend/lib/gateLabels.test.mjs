// frontend/lib/gateLabels.test.mjs
// AA-924 scope 4: the Content Quality gate filter and every gate chip label a retired gate "(retired)".
// Run: npm run test:unit (node --test with --experimental-strip-types).
//
// Reads the real generated registry (services/quality/taxonomy.yaml → quality-registry.generated.ts), so a gate the
// registry retires later is covered without editing this file.

import assert from "node:assert/strict";
import { test } from "node:test";

import { RETIRED_SUFFIX, gateFilterGroups, gateLabelOf } from "./gateLabels.ts";
import { GATE_LABELS, RETIRED_GATES } from "./quality-registry.generated.ts";

// The gates AA-924 (lexicon merge) and AA-787 (T10 stack cut) retired; older pieces still carry them in their ledger.
const RETIRED_BY_AA924 = ["F2_banned_patterns", "F2_brand_forbidden", "F15_voice_warn"];
// AA-800 word layers that AA-924 kept as their own gates (quality_gates.gate_lexicon docstring): never "(retired)".
const KEPT_ACTIVE = ["F2_superlatives", "F2_style_warn", "F2_platform_truth", "F2_lexicon", "F15_voice"];

test("the registry retires the AA-924 merged gates and keeps the AA-800 layers active", () => {
  for (const g of RETIRED_BY_AA924) assert.ok(RETIRED_GATES.has(g), `${g} should be retired`);
  for (const g of KEPT_ACTIVE) assert.ok(!RETIRED_GATES.has(g), `${g} should be active`);
});

test("gateLabelOf marks a retired gate and leaves an active or unknown one alone", () => {
  assert.equal(gateLabelOf(GATE_LABELS, RETIRED_GATES, "F2_banned_patterns"), "Cliché / banned phrase (retired)");
  assert.equal(gateLabelOf(GATE_LABELS, RETIRED_GATES, "F15_voice_warn"),
    "Trade register / audience leak / opener (retired)");
  assert.equal(gateLabelOf(GATE_LABELS, RETIRED_GATES, "F2_lexicon"), "Banned / forbidden / stock wording");
  assert.equal(gateLabelOf(GATE_LABELS, RETIRED_GATES, "F2_superlatives"), "More than one stock superlative");
  assert.equal(gateLabelOf(GATE_LABELS, RETIRED_GATES, "X_unknown"), "X_unknown");
  // An unlabelled name that is retired still says so.
  assert.equal(gateLabelOf({}, new Set(["old_gate"]), "old_gate"), "old_gate (retired)");
});

test("the gate filter lists every labelled gate once, active first, retired ones labelled (retired)", () => {
  const { active, retired } = gateFilterGroups(GATE_LABELS, RETIRED_GATES);
  const all = [...active, ...retired].map((o) => o.value);
  assert.deepEqual([...all].sort(), Object.keys(GATE_LABELS).sort());
  assert.equal(new Set(all).size, all.length);

  assert.deepEqual(retired.map((o) => o.value), Object.keys(GATE_LABELS).filter((g) => RETIRED_GATES.has(g)));
  for (const o of retired) {
    assert.ok(o.text.includes(RETIRED_SUFFIX), o.text);
    assert.ok(o.text.endsWith(`(${o.value})`), o.text);
  }
  for (const o of active) {
    assert.ok(!o.text.includes("(retired)"), o.text);
    assert.equal(o.text, `${GATE_LABELS[o.value]} (${o.value})`);
  }
  assert.deepEqual(retired.find((o) => o.value === "F2_brand_forbidden"),
    { value: "F2_brand_forbidden", text: "Brand-forbidden word (retired) (F2_brand_forbidden)" });
  for (const g of KEPT_ACTIVE) assert.ok(active.some((o) => o.value === g), g);
});
