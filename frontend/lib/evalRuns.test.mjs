// frontend/lib/evalRuns.test.mjs — AA-1169: Writing Lab › Eval runs helpers (lib/evalRuns.ts): links, tones, the
// flagged-claim highlight and the Markdown export; the page is in the nav and every message has a Vietnamese twin.
// Invented names and places only. Run: npm run test:unit.

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  EVAL_RUNS_PATH, EVAL_TABS, armRow, classLine, coverageUrl, evalRunHref, fanoutUrl, highlightSegments, isUuid,
  itemMarkdown, itemUrl, lineageUrl, markdownFilename, parseEvalTab, pct, resultTone, runMarkdown, runTone, runUrl,
  runsUrl, usd,
} from "./evalRuns.ts";
import { ADMIN_IA } from "./adminIa.ts";
import { canSeePage } from "./rbac.ts";

const RUN_ID = "00000000-0000-0000-0000-0000000a1169";
const ITEM_ID = "00000000-0000-0000-0000-0000000b1169";

const RUN = {
  run_id: RUN_ID, run_kind: "r2", writer: "t9_blog", prompt_version: "v1", created_at: "2026-11-25T09:00:00+00:00",
  finished_at: null, verdict: "informative", stop_reason: "completed", cost_usd: 1.42, cost_cap_usd: 5,
  tour_count: 12, judge_kappa: null, golden_set_id: null, release_id: null, triggered_by: "test",
  prereg_id: "proof-x", prereg_arm: "B", results: 24, passes: 10, fails: 14, errors: 0, outputs: 12,
};
const TEXT = "Day 1 starts at the Velin market. By evening the group reaches the ridge camp. It is one of the rarest views.";
const ITEM = {
  run: RUN,
  item: { item_id: ITEM_ID, sample_id: "t9b-901", writer: "t9_blog", channel: "blog", scope: "platform",
    tenant_id: null, split: "holdout", kind: "real", tiers: [], country: "Norvale", days: 5, label_verdict: "fail",
    label_source: "agent_panel", label: {}, input_sha256: "0".repeat(64), pick_reason: null },
  source_text: "FACTS:\nThe walk is 12 km. Use ``` carefully.",
  input: { context: {} },
  results: [{
    result_id: "r-b", arm: "candidate", mode: "regenerate", phase: "holdout", verdict: "fail", score: null,
    failure_codes: ["claims"], hard_stop: false, judge_model: "judge-x", judge_version: "j1", cost_usd: 0.05,
    created_at: null, scored_result_id: null, output_source: "own", output_sha256: null,
    output_fields: [{ field: "seo_title", text: "Lantern Hills" }, { field: "content_text", text: TEXT }],
    claims: [{ claim: "It is one of the rarest views.", why: "not in the source", judge_class: "unsupported",
      policy_class: "rarity", field: "body", pos: 80 }],
    checks: {}, judge: null, gate_replay: { passed: false, first_failure: "f17", blocking: ["f17"] },
  }],
};

test("tabs and links", () => {
  assert.deepEqual([...EVAL_TABS], ["runs", "coverage", "lookup"]);
  assert.equal(parseEvalTab("lookup"), "lookup");
  assert.equal(parseEvalTab("nope"), "runs");
  assert.equal(parseEvalTab(null), "runs");
  assert.equal(runsUrl({ writer: "t9_blog", prereg: "  proof-x " }, 25, 50),
    "/api/admin/writing-memory/eval-runs?writer=t9_blog&prereg=proof-x&limit=25&offset=50");
  assert.equal(runsUrl({ prereg: "  " }, 10, 0), "/api/admin/writing-memory/eval-runs?limit=10&offset=0");
  assert.equal(runUrl(RUN_ID), `/api/admin/writing-memory/eval-runs/${RUN_ID}`);
  assert.equal(itemUrl(RUN_ID, ITEM_ID), `/api/admin/writing-memory/eval-runs/${RUN_ID}/items/${ITEM_ID}`);
  assert.equal(lineageUrl("eval", ` ${RUN_ID} `), `/api/admin/quality/lineage/eval/${RUN_ID}`);
  assert.equal(coverageUrl(30), "/api/admin/quality/lineage/coverage?days=30");
  assert.equal(fanoutUrl(RUN_ID), `/api/admin/quality/lineage/fanout/${RUN_ID}`);
  assert.equal(evalRunHref(), EVAL_RUNS_PATH);
  assert.equal(evalRunHref(RUN_ID), `${EVAL_RUNS_PATH}?run=${RUN_ID}`);
  assert.equal(evalRunHref(RUN_ID, ITEM_ID), `${EVAL_RUNS_PATH}?run=${RUN_ID}&item=${ITEM_ID}`);
  assert.equal(evalRunHref(null, ITEM_ID), EVAL_RUNS_PATH);
  assert.ok(isUuid(RUN_ID) && isUuid(` ${RUN_ID.toUpperCase()} `));
  assert.ok(!isUuid("t9b-901") && !isUuid(null) && !isUuid(""));
});

test("tones, numbers and arm rows", () => {
  assert.equal(resultTone("pass"), "success");
  assert.equal(resultTone("fail"), "danger");
  assert.equal(resultTone("error"), "warning");
  assert.equal(resultTone("skipped"), "neutral");
  assert.equal(runTone("invalid"), "danger");
  assert.equal(runTone("informative"), "info");
  assert.equal(usd(null), "—");
  assert.equal(usd(0.07375), "$0.0737");
  assert.equal(usd(1.42), "$1.42");
  assert.equal(pct(0.416667), "41.7%");
  assert.equal(pct(null), "—");
  assert.equal(classLine({ rarity: 1, route_timing: 2 }), "rarity 1 · route_timing 2");
  const row = (arm, mode) => ({ result_id: `${arm}-${mode}`, arm, mode });
  const item = { rows: [row("control", "rejudge"), row("candidate", "regenerate"), row("control", "regenerate")] };
  assert.equal(armRow(item, "candidate").result_id, "candidate-regenerate");
  assert.equal(armRow(item, "control").result_id, "control-regenerate");
  assert.equal(armRow({ rows: [row("control", "rejudge")] }, "control").result_id, "control-rejudge");
  assert.equal(armRow({ rows: [] }, "candidate"), null);
});

test("flagged claims are marked in the text, case-insensitive, never overlapping", () => {
  const segs = highlightSegments(TEXT, [{ claim: "it is one of the rarest views." }, { claim: "By evening the group" },
    { claim: "the group reaches" }, { claim: "Not in this text at all" }, { claim: "ab" }]);
  assert.equal(segs.map((s) => s.text).join(""), TEXT);                     // nothing lost, nothing added
  const marked = segs.filter((s) => s.claim != null);
  assert.deepEqual(marked.map((s) => [s.claim, s.text]),
    [[1, "By evening the group"], [0, "It is one of the rarest views"]]);   // the overlap (2) keeps the earlier one
  assert.deepEqual(highlightSegments("plain", []), [{ text: "plain", claim: null }]);
  assert.deepEqual(highlightSegments("", [{ claim: "x y z" }]), []);
});

test("one sample as Markdown: source, verdicts, gates, claims, output and a review checklist", () => {
  const md = itemMarkdown(ITEM);
  assert.match(md, /^# t9b-901 \(t9_blog · blog\)/);
  assert.match(md, /- Pre-registration: proof-x \(arm B\)/);
  assert.match(md, /## Source the judge read/);
  assert.match(md, /````text\nFACTS:\nThe walk is 12 km\. Use ``` carefully\.\n````/);   // fence longer than the text's
  assert.match(md, /## candidate · regenerate — fail/);
  assert.match(md, /- Gate replay: failed · first failure f17 · blocking f17/);
  assert.match(md, /1\. \[rarity · body\] It is one of the rarest views\. — not in the source/);
  assert.match(md, /\*\*content_text\*\*/);
  assert.match(md, /- \[ \] Contract violations: \n- \[ \] Unsupported trip promises: /);
  const broken = itemMarkdown({ ...ITEM, source_text: null,
    results: [{ ...ITEM.results[0], claims: [], output_fields: [], gate_replay: { error: "WriterFailed" } }] });
  assert.match(broken, /Not rebuilt from the stored input/);
  assert.match(broken, /could not be replayed \(WriterFailed\)/);
  assert.match(broken, /None\.\n/);
  assert.match(broken, /No stored output\./);
});

test("a whole run as one Markdown file, items one level down", () => {
  const md = runMarkdown(RUN, [ITEM, ITEM]);
  assert.match(md, /^# Eval run proof-x\n/);
  assert.match(md, /- Items: 2/);
  assert.equal((md.match(/^## t9b-901 /gm) ?? []).length, 2);
  assert.equal((md.match(/^- Run: /gm) ?? []).length, 1);              // the run header once, not per item
  assert.equal(markdownFilename(RUN), "proof-x.md");
  assert.equal(markdownFilename({ ...RUN, prereg_id: null }), "00000000.md");
  assert.equal(markdownFilename(RUN, "t9b/901"), "proof-x-t9b-901.md");
});

test("the page is in the Writing Lab nav and opens for the Lab roles", () => {
  const lab = ADMIN_IA.find((g) => g.label === "Writing Lab");
  const item = lab.items.find((i) => i.href === EVAL_RUNS_PATH);
  assert.ok(item && item.icon === "evalRuns");
  assert.ok(existsSync(fileURLToPath(new URL("../app/admin/lab/eval-runs/page.tsx", import.meta.url))));
  assert.ok(canSeePage("admin", EVAL_RUNS_PATH) && canSeePage("ai_ops", EVAL_RUNS_PATH));
  assert.ok(!canSeePage("viewer", EVAL_RUNS_PATH));
});

test("every message has a Vietnamese twin and no issue id shows in the UI", () => {
  const src = readFileSync(fileURLToPath(new URL("../app/admin/lab/eval-runs/messages.ts", import.meta.url)), "utf8");
  assert.doesNotMatch(src.replace(/^\/\/.*$/gm, ""), /AA-\d+/);
  const page = readFileSync(fileURLToPath(new URL("../app/admin/lab/eval-runs/EvalRuns.tsx", import.meta.url)), "utf8");
  for (const m of page.matchAll(/\bt\("([A-Za-z]+)"/g)) assert.match(src, new RegExp(`\\b${m[1]}:`), m[1]);
});
