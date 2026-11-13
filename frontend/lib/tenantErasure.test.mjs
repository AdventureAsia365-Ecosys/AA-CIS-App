// frontend/lib/tenantErasure.test.mjs
// AA-1093 lane L2: the pure helpers of the Tenant offboarding tab (lib/tenantErasure.ts). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  APPS, ERASURE_REQUESTS_API, STOPPED_KIND, approvalBlockers, decisionActions, isStopped, notBefore, phaseTone,
  prefixMatches, shortHash, stepOf, stepStatus,
} from "./tenantErasure.ts";

const FP = "abcdef0123456789".repeat(4);
const NOW = Date.parse("2026-03-10T12:00:00Z");
const WORDS = { notAwaiting: "not awaiting", switchOff: "switch off", tooOld: (h) => `older than ${h} h` };
const SETTINGS = {
  apply_enabled: true, apply_switch: "cis.erasure.saga_apply_enabled", approval_valid_hours: 24,
  window_days: { cis: 30, booking: 30, tripplanner: 30, crm: 90 }, environment: "dev",
};

function req(over = {}) {
  return {
    request_id: "11111111-1111-4111-8111-111111111111", tenant_id: "22222222-2222-4222-8222-222222222222",
    mode: "drill", phase: "awaiting_approval", round: 2, opened_at: "2026-03-01T00:00:00Z", is_pilot: false,
    is_synthetic: true, round_apps: ["cis", "booking", "tripplanner"], done_apps: [],
    not_before_cis: "2026-03-31T00:00:00Z", not_before_booking: "2026-03-31T00:00:00Z",
    not_before_tripplanner: "2026-03-31T00:00:00Z", not_before_crm: "2026-05-30T00:00:00Z",
    combined_fingerprint: FP, fingerprint_at: "2026-03-10T10:00:00Z", approved_fingerprint: null,
    approved_by: null, approved_at: null, approval_expires_at: null, snapshot_id: null, blockers: [], receipt: null,
    created_by: "staff:someone", created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-10T10:00:00Z",
    cancelled_at: null, done_at: null, steps: [], ...over,
  };
}

test("the BFF path and the four apps of the saga", () => {
  assert.equal(ERASURE_REQUESTS_API, "/api/admin/job-runner/erasure/requests");
  assert.deepEqual([...APPS], ["cis", "booking", "tripplanner", "crm"]);
});

test("approval form open only while awaiting approval, the switch on and the fingerprint fresh", () => {
  assert.deepEqual(approvalBlockers(req(), SETTINGS, NOW, WORDS), []);
  assert.deepEqual(approvalBlockers(req({ phase: "dry_run" }), SETTINGS, NOW, WORDS), ["not awaiting"]);
  assert.deepEqual(approvalBlockers(req({ combined_fingerprint: null }), SETTINGS, NOW, WORDS), ["not awaiting"]);
  assert.deepEqual(approvalBlockers(req(), { ...SETTINGS, apply_enabled: false }, NOW, WORDS), ["switch off"]);
  const stale = req({ fingerprint_at: "2026-03-09T11:00:00Z" });
  assert.deepEqual(approvalBlockers(stale, SETTINGS, NOW, WORDS), ["older than 24 h"]);
  // the validity is the setting's value, never a local default
  assert.deepEqual(approvalBlockers(stale, { ...SETTINGS, approval_valid_hours: 48 }, NOW, WORDS), []);
  assert.deepEqual(approvalBlockers(stale, undefined, NOW, WORDS), ["switch off"]);
});

test("the typed prefix must equal the first 8 characters of the combined fingerprint", () => {
  assert.equal(prefixMatches(FP, "ABCDEF01 "), true);
  assert.equal(prefixMatches(FP, "abcdef0"), false);
  assert.equal(prefixMatches(null, "abcdef01"), false);
  assert.equal(shortHash(FP), "abcdef01");
  assert.equal(shortHash(null), "—");
});

test("step status per app: done, waiting outside the round, failed on a stopped report, else active", () => {
  const steps = [
    { round: 2, app: "booking", mode: "dry_run", status: "blocked" },
    { round: 1, app: "tripplanner", mode: "dry_run", status: "failed" },
    { round: 2, app: "cis", mode: "dry_run", status: "ok" },
  ];
  const r = req({ steps, done_apps: [] });
  assert.equal(stepStatus(r, "cis"), "active");
  assert.equal(stepStatus(r, "booking"), "failed");
  assert.equal(stepStatus(r, "tripplanner"), "active");
  assert.equal(stepStatus(r, "crm"), "waiting");
  assert.equal(stepStatus(req({ done_apps: ["crm"] }), "crm"), "done");
  assert.equal(stepOf(r, "cis", "dry_run")?.status, "ok");
  assert.equal(stepOf(r, "tripplanner", "dry_run"), undefined);
  assert.equal(notBefore(r, "crm"), "2026-05-30T00:00:00Z");
});

test("phase tones", () => {
  assert.equal(phaseTone("done"), "success");
  assert.equal(phaseTone("failed"), "danger");
  assert.equal(phaseTone("awaiting_approval"), "warning");
  assert.equal(phaseTone("cancelled"), "neutral");
  assert.equal(phaseTone("apply"), "info");
});

test("a failed request offers a new round or a stop; a stopped one neither; apply / verify never a stop", () => {
  assert.deepEqual(decisionActions(req({ phase: "failed" })), { newRound: true, stop: true });
  const stopped = req({ phase: "failed", blockers: [{ app: "cis", kind: STOPPED_KIND, detail: "stopped" }] });
  assert.equal(isStopped(stopped), true);
  assert.deepEqual(decisionActions(stopped), { newRound: false, stop: false });
  assert.deepEqual(decisionActions(req({ phase: "apply" })), { newRound: true, stop: false });
  assert.deepEqual(decisionActions(req({ phase: "verify" })), { newRound: false, stop: false });
  assert.deepEqual(decisionActions(req({ phase: "done" })), { newRound: false, stop: false });
  assert.deepEqual(decisionActions(req({ phase: "failed", is_pilot: true })), { newRound: false, stop: true });
});
