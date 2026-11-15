// frontend/lib/tenantCustomDomain.test.mjs
// AA-1090 TripPlanner R8 — Tenant 360 › Subscription › TripPlanner own domains: paths, the host check the form makes
// alone, status tones, polling and the add rule. Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canAdd, domainPath, domainsKey, domainsPath, hostProblem, normaliseHost, removalStuck, shouldPoll, statusTone,
} from "./tenantCustomDomain.ts";

const T = "11111111-2222-3333-4444-555555555555";
const row = (status) => ({ domain_id: "d1", domain: "trip.quillmere.example.invalid", status, dns_records: [],
  failure_code: null, failure_message: null, requested_at: null, validated_at: null, active_at: null,
  updated_at: null, updated_by: null });
const list = (statuses, extra = {}) => ({ tenant_id: T, has_tripplanner: true, available: true, max_domains: 1,
  domains: statuses.map(row), ...extra });

test("paths and query key", () => {
  assert.equal(domainsPath(T), `/api/admin/tenants/${T}/tripplanner-site/domains`);
  assert.equal(domainPath(T, "a/b"), `/api/admin/tenants/${T}/tripplanner-site/domains/a%2Fb`);
  assert.deepEqual(domainsKey(T), ["tenant360", T, "tp-domains"]);
});

test("host normalisation and the form's own checks", () => {
  assert.equal(normaliseHost("  https://Trip.Quillmere.Example.Invalid./plan "), "trip.quillmere.example.invalid");
  assert.equal(hostProblem("trip.quillmere.example.invalid"), null);
  assert.equal(hostProblem("quillmere.example"), "domain_apex");
  for (const bad of ["", "trip_x.q.example.invalid", "-a.q.example.invalid", "10.0.0.1", "a b.example.invalid"]) {
    assert.equal(hostProblem(bad), "domain_invalid", bad);
  }
});

test("status tones", () => {
  assert.equal(statusTone("active"), "success");
  assert.equal(statusTone("failed"), "danger");
  assert.equal(statusTone("validating"), "warning");
  assert.equal(statusTone("pending"), "info");
  assert.equal(statusTone("removing"), "neutral");
});

test("polls only while the job works on a host", () => {
  assert.equal(shouldPoll(null), false);
  assert.equal(shouldPoll(list(["active", "failed"])), false);
  for (const s of ["pending", "validating", "attaching", "removing"]) assert.equal(shouldPoll(list([s])), true, s);
});

test("add rule: switched on, TripPlanner, under the limit (a removing host does not count)", () => {
  assert.equal(canAdd(list([])), true);
  assert.equal(canAdd(list(["active"])), false);
  assert.equal(canAdd(list(["removing"])), true);
  assert.equal(canAdd(list([], { available: false })), false);
  assert.equal(canAdd(list([], { has_tripplanner: false })), false);
});

test("a removal the job could not finish stops the polling and offers the removal again", () => {
  const stuck = { ...row("removing"), failure_code: "edge_not_configured", failure_message: "switched off" };
  assert.equal(removalStuck(stuck), true);
  assert.equal(removalStuck(row("removing")), false);
  assert.equal(removalStuck({ ...row("failed"), failure_code: "job_failed" }), false);
  assert.equal(shouldPoll({ ...list([]), domains: [stuck] }), false);
  assert.equal(shouldPoll({ ...list([]), domains: [stuck, row("removing")] }), true);
});
