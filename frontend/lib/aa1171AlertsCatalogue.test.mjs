// AA-1171 (S287): the pure helpers of Platform › Alerts › Catalogue (lib/alertsCatalogue.ts).
import { test } from "node:test";
import assert from "node:assert/strict";
import { appOptions, isFiring, matchesRow, staleSources } from "./alertsCatalogue.ts";

const row = (over) => ({
  id: "x", name: "aa-test-api-down", app: "cis", app_label: "CIS", component: "api", type: "metric",
  source: "cloudwatch", severity: "ticket", mutable: true, condition: "HealthyHostCount < 1", threshold: "1",
  threshold_source: "Terraform", editable_via: "terraform", edit_href: null, channel: [], runbook: null,
  owner_role: "tech lead", state: "OK", state_since: null, last_alert_at: null, account_id: null, freshness: null,
  coverage_gap: null, description: null, ...over,
});
const none = { q: "", app: "", type: "", severity: "", state: "", edit: "", gaps: "" };

test("filters: app, type, severity (untagged too), firing, editable via, gaps and search", () => {
  assert.equal(matchesRow(row({}), none), true);
  assert.equal(matchesRow(row({}), { ...none, app: "crm" }), false);
  assert.equal(matchesRow(row({}), { ...none, type: "in_app_check" }), false);
  assert.equal(matchesRow(row({ severity: null }), { ...none, severity: "untagged" }), true);
  assert.equal(matchesRow(row({}), { ...none, severity: "untagged" }), false);
  assert.equal(matchesRow(row({ state: "ALARM" }), { ...none, state: "firing" }), true);
  assert.equal(matchesRow(row({}), { ...none, state: "firing" }), false);
  assert.equal(matchesRow(row({ state: "ALARM" }), { ...none, state: "quiet" }), false);
  assert.equal(matchesRow(row({}), { ...none, edit: "settings" }), false);
  assert.equal(matchesRow(row({ coverage_gap: "no tag" }), { ...none, gaps: "1" }), true);
  assert.equal(matchesRow(row({}), { ...none, gaps: "1" }), false);
  assert.equal(matchesRow(row({}), { ...none, q: "HEALTHY" }), true);
  assert.equal(matchesRow(row({}), { ...none, q: "budget" }), false);
});

test("app options are unique and sorted by label; firing and stale sources", () => {
  const opts = appOptions([row({ app: "crm", app_label: "Partner CRM" }), row({}), row({}),
    row({ app: null, app_label: "Unknown" })]);
  assert.deepEqual(opts, [{ value: "cis", label: "CIS" }, { value: "crm", label: "Partner CRM" }]);
  assert.equal(isFiring({ state: "ALARM" }), true);
  assert.equal(isFiring({ state: "not_live" }), false);
  assert.deepEqual(staleSources({ aws: { status: "ok" }, crm: { status: "unavailable" }, cis: { status: null } }),
    ["crm"]);
});
