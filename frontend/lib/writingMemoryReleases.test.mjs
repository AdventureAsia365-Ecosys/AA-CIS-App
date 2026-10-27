// frontend/lib/writingMemoryReleases.test.mjs
// AA-919 WM-22: the pure helpers of the Releases tab (lib/writingMemory.ts) agree with the backend rules
// (services/writing_memory/releases.py: TRANSITIONS, the route guards, evidence_problem). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EVIDENCE_NEEDED, actorLabel, evidenceOk, evidenceWord, isUuid, monthStart, releaseActions, releaseTone,
} from "./writingMemory.ts";

const run = (run_kind, verdict, extra = {}) => ({ run_kind, verdict, finished: true, ...extra });

test("evidence needed per content class matches attach_evidence", () => {
  assert.deepEqual(EVIDENCE_NEEDED.empty, ["r0", "eval"]);
  assert.deepEqual(EVIDENCE_NEEDED.lexicon_only, ["r0"]);
  assert.deepEqual(EVIDENCE_NEEDED.repair_hint_only, ["eval", "shadow"]);
  assert.deepEqual(EVIDENCE_NEEDED.rules, ["eval", "shadow"]);
});

test("an evidence run passes only with the kind and verdict evidence_problem accepts", () => {
  assert.equal(evidenceOk("r0", run("r0_replay", "pass"), "empty"), true);
  assert.equal(evidenceOk("r0", run("r0_replay", "fail"), "empty"), false);
  assert.equal(evidenceOk("eval", run("baseline_import", "baseline"), "empty"), true);
  assert.equal(evidenceOk("eval", run("baseline_import", "baseline"), "rules"), false, "baseline only for empty");
  assert.equal(evidenceOk("eval", run("r2", "pass"), "rules"), true);
  assert.equal(evidenceOk("shadow", run("shadow", "pass"), "rules"), true);
  assert.equal(evidenceOk("r0", run("r0_replay", "pass", { finished: false }), "empty"), false, "unfinished");
  assert.equal(evidenceOk("r0", null, "empty"), false);
});

test("evidence words for the list", () => {
  assert.equal(evidenceWord("r0", null, "empty"), "—");
  assert.equal(evidenceWord("r0", run(null, null, { missing: true, finished: false }), "empty"), "missing");
  assert.equal(evidenceWord("r0", run("r0_replay", "pass", { finished: false }), "empty"), "running");
  assert.equal(evidenceWord("r0", run("r0_replay", "pass"), "empty"), "passed");
  assert.equal(evidenceWord("eval", run("baseline_import", "baseline"), "empty"), "baseline");
  assert.equal(evidenceWord("eval", run("r2", "fail"), "rules"), "failed");
});

test("actions per status follow the backend transitions", () => {
  const a = (status, previous_release_id = null) => releaseActions({ status, previous_release_id });
  assert.deepEqual(a("draft"), { evidence: true, approve: false, activate: false, rollback: false, reject: true });
  assert.deepEqual(a("evaluated"), { evidence: true, approve: true, activate: false, rollback: false, reject: true });
  assert.deepEqual(a("approved"), { evidence: false, approve: false, activate: true, rollback: false, reject: true });
  assert.equal(a("active").rollback, false, "release 0 has no previous: the switch is the answer");
  assert.equal(a("active", "x").rollback, true);
  for (const s of ["retired", "rejected"])
    assert.deepEqual(a(s, "x"), { evidence: false, approve: false, activate: false, rollback: false, reject: false });
});

test("small formatters", () => {
  assert.equal(actorLabel("admin:11111111-2222-3333-4444-555555555555"), "admin 11111111");
  assert.equal(actorLabel("11111111-2222-3333-4444-555555555555"), "admin 11111111");
  assert.equal(actorLabel("agent:s252-aa919"), "agent:s252-aa919");
  assert.equal(actorLabel(null), "—");
  assert.equal(monthStart("2026-10"), "2026-10-01");
  assert.equal(monthStart("2026-13"), null);
  assert.equal(monthStart(""), null);
  assert.equal(isUuid(" 11111111-2222-3333-4444-555555555555 "), true);
  assert.equal(isUuid("nope"), false);
  assert.equal(releaseTone("active"), "success");
  assert.equal(releaseTone("rejected"), "danger");
});
