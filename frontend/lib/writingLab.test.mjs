// frontend/lib/writingLab.test.mjs — AA-1039 R6 slice 3: the Writing Lab stages and helpers (lib/writingLab.ts), and
// that every stage label and tab label has an English and a Vietnamese message (app/admin/lab/messages.ts).
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  DEFAULT_STAGE, LAB_STAGES, LAB_TABS, countRows, evalTone, evidenceSummary, gateTone, parseLabTab, pct, releaseTone,
  releasesUrl, share, stageBySlug, stageHref, verdictTone, writerWeeks, writingMemoryHref,
} from "./writingLab.ts";

test("four stages, one per writing-memory writer, unique slugs that never shadow a Lab page", () => {
  assert.deepEqual(LAB_STAGES.map((s) => s.writer), ["a1", "t2", "t9_blog", "t9_social"]);
  const slugs = LAB_STAGES.map((s) => s.slug);
  assert.equal(new Set(slugs).size, slugs.length);
  for (const s of slugs) assert.match(s, /^[a-z0-9-]+$/);
  assert.ok(!slugs.includes("writing-memory"), "a stage slug must not collide with /admin/lab/writing-memory");
  assert.equal(DEFAULT_STAGE, "a1-s1");
});

test("stage lookup, links and tabs", () => {
  assert.equal(stageBySlug("t9-social")?.writer, "t9_social");
  assert.equal(stageBySlug("t9_social"), null);
  assert.equal(stageBySlug(undefined), null);
  assert.equal(stageHref("t2-rewrite"), "/admin/lab/t2-rewrite");
  assert.equal(stageHref("t2-rewrite", "gate"), "/admin/lab/t2-rewrite");
  assert.equal(stageHref("t2-rewrite", "samples"), "/admin/lab/t2-rewrite?tab=samples");
  assert.deepEqual([...LAB_TABS], ["gate", "samples", "labels", "experiments", "prompts", "releases", "metrics"]);
  assert.equal(parseLabTab("releases"), "releases");
  assert.equal(parseLabTab("metrics"), "metrics");
  assert.equal(parseLabTab("prompts"), "prompts");
  assert.equal(parseLabTab("nope"), "gate");
  assert.equal(parseLabTab(null), "gate");
});

test("tones, shares and count rows", () => {
  assert.equal(gateTone("passing"), "success");
  assert.equal(gateTone("regression"), "danger");
  assert.equal(gateTone("reverify"), "warning");
  assert.equal(gateTone("memory_off"), "warning");
  assert.equal(gateTone("no_release"), "neutral");
  assert.equal(verdictTone("pass"), "success");
  assert.equal(verdictTone("fail"), "danger");
  assert.equal(verdictTone("baseline"), "info");
  assert.equal(verdictTone(null), "warning");
  // the live-release evaluation tile: release 0's baseline import is green, a fail or a regression red
  assert.equal(evalTone(null), "neutral");
  assert.equal(evalTone({ verdict: "baseline", regression_detected: false }), "success");
  assert.equal(evalTone({ verdict: "pass", regression_detected: false }), "success");
  assert.equal(evalTone({ verdict: "pass", regression_detected: true }), "danger");
  assert.equal(evalTone({ verdict: "fail", regression_detected: false }), "danger");
  assert.equal(evalTone({ verdict: "informative", regression_detected: false }), "neutral");
  assert.equal(share(1, 4), "25%");
  assert.equal(share(1, 0), "—");
  assert.deepEqual(countRows({ b: 2, a: 2, c: 5 }), [{ key: "c", n: 5 }, { key: "a", n: 2 }, { key: "b", n: 2 }]);
  assert.deepEqual(countRows(null), []);
});

test("every Lab message has a Vietnamese twin; stage and gate words exist; no issue ids", () => {
  // messages.ts imports the kit with an extensionless path, so it is read as text (like lib/adminIa.test.mjs does).
  const src = readFileSync(new URL("../app/admin/lab/messages.ts", import.meta.url), "utf8");
  const [en, vi] = src.split(/\n  \},\n  \{\n/);
  assert.ok(en && vi, "two catalogues");
  const keys = (block) => [...block.matchAll(/^    ([A-Za-z0-9_]+):/gm)].map((m) => m[1]);
  assert.deepEqual(keys(vi), keys(en), "same keys, same order");
  for (const code of ["no_release", "memory_off", "reverify", "regression", "passing"]) {
    assert.ok(keys(en).includes(`gate_${code}`), code);
  }
  for (const k of ["stageA1", "stageT2", "stageT9Blog", "stageT9Social"]) assert.ok(keys(en).includes(k), k);
  assert.doesNotMatch(src.replace(/^\/\/.*$/gm, ""), /AA-\d+/);
});

test("releases per stage: the writer's list, evidence slots, tones and the Writing memory link", () => {
  assert.equal(releasesUrl("t9_blog"), "/api/admin/writing-memory/releases?writer=t9_blog&limit=50");
  assert.equal(writingMemoryHref("releases"), "/admin/lab/writing-memory?tab=releases");
  assert.equal(writingMemoryHref(), "/admin/lab/writing-memory");
  const ev = evidenceSummary({
    r0_run_id: "r0", eval_run_id: null, shadow_run_id: "sh",
    evidence: { r0: { run_kind: "r0_replay", verdict: "pass", finished: true }, eval: null,
      shadow: { run_kind: "shadow", verdict: null, finished: false } },
  });
  assert.deepEqual(ev, [{ slot: "r0", verdict: "pass", finished: true }, { slot: "shadow", verdict: null, finished: false }]);
  assert.deepEqual(evidenceSummary({ r0_run_id: null, eval_run_id: null, shadow_run_id: null }), []);
  assert.equal(releaseTone("active"), "success");
  assert.equal(releaseTone("rejected"), "danger");
  assert.equal(releaseTone("approved"), "info");
  assert.equal(releaseTone("draft"), "warning");
  assert.equal(releaseTone("retired"), "neutral");
});

test("metrics per stage: one row per stored week, the writer's block only, never 0 for 'not measured'", () => {
  const kpi = { writes: 12, flagged: 1, labels: 3, tenant_edits: 0, cost_usd: 0.5, reflag_rate: 0.083,
    reflag_ci95: [0.01, 0.3], repeat_error_rate: null, light_edit_share: 0.5, minutes_to_fix: 2.5,
    tenant_edit_rate: null, capture_complete: 0.9, capture_ok: true, cost_per_write: 0.0417 };
  const reports = [
    { week_start: "2026-11-02", trigger: "schedule",
      kpis: { writers: { a1: { overall: kpi, tenants: {}, clean_pairs: 4, funnel: { total: {}, this_week: {} },
        replay: { explained: 2, usable: 4, rate: 0.5 } } }, learning_spend: { month_spent_usd: 1, month_cap_usd: 10, share: 0.1 } },
      soft_stops: [
        { release_id: "r1", writer: "a1", scope: "platform", tenant_id: null, release_no: 3,
          stops: [{ code: "cost_up", value: 2, baseline: 1, n: 12 }], kept: [], after_writes: 12, before_writes: 10 },
        { release_id: "r2", writer: "t2", scope: "platform", tenant_id: null, release_no: 1,
          stops: [{ code: "cost_up", value: 2, baseline: 1, n: 5 }], kept: [], after_writes: 5, before_writes: 5 },
        { release_id: "r3", writer: "a1", scope: "platform", tenant_id: null, release_no: 2, stops: [], kept: [],
          after_writes: 1, before_writes: 1 },
      ],
      actions: [{ release_id: "r1", writer: "a1", scope: "platform", tenant_id: null, release_no: 3, codes: ["cost_up"],
        kept_codes: [], action: "kept" }],
      rules: { expired: [], reconfirmed: [] }, project_stop: { flagged: [], rule: "x" }, disagreements: 0 },
    { week_start: "2026-10-26", trigger: "manual", kpis: { writers: {}, learning_spend: { month_spent_usd: null,
      month_cap_usd: 10, share: null } }, soft_stops: [], actions: [], rules: { expired: [], reconfirmed: [] },
      project_stop: { flagged: [], rule: "x" }, disagreements: 0 },
  ];
  const a1 = writerWeeks(reports, "a1");
  assert.deepEqual(a1.map((w) => w.week_start), ["2026-11-02", "2026-10-26"]);
  assert.equal(a1[0].kpi.writes, 12);
  assert.equal(a1[0].replay_rate, 0.5);
  assert.equal(a1[0].clean_pairs, 4);
  assert.deepEqual(a1[0].stops.map((s) => s.release_no), [3]);       // the other writer and an empty stop are left out
  assert.deepEqual(a1[0].actions, [{ release_no: 3, scope: "platform", action: "kept", codes: ["cost_up"] }]);
  assert.equal(a1[1].kpi, null);                                    // not measured that week
  assert.equal(a1[1].clean_pairs, null);
  assert.deepEqual(writerWeeks(null, "t2"), []);
  assert.equal(pct(null), "—");
  assert.equal(pct(0), "0.0%");
  assert.equal(pct(0.0833), "8.3%");
});
