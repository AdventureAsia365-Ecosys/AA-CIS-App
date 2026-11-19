// frontend/lib/connectionBudgets.test.mjs
// AA-1143 (decision G3) — AI gateway › Budgets, "Connection budgets": the draft check builds only what changed, keeps
// shares under the total and needs a reason; the CIS remainder follows a share; the row state and order; the section's
// copy in EN + VI names no issue id. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { appLabel, budgetOrder, checkDraft, draftOf, rowState } from "./connectionBudgets.ts";

const APPS = ["booking", "crm", "tripplanner"];
const v = (usd) => ({ usd, version: 1, reason: "zz seed reason of twenty chars", actor: "migration:456",
  valid_from: "2026-11-19T00:00:00Z" });
const row = (over = {}) => ({
  connection_key: "zz-a", label: "Zz A", kind: "bedrock", enabled: true, total: v(100),
  shares: { booking: null, crm: v(5), tripplanner: null }, cis_remainder_usd: 95, cis_cap_usd: 95,
  cis_cap_in_sync: true, spend: {}, cis_pool_spent_usd: 0, alert_pct: 80, hard_stop: true, crm_expected_usd: 5,
  crm_copy_usd: 5, crm_setting_usd: 5, crm_state: "in_sync", ...over,
});
const REASON = "Give the CRM more room this month for drafts";

test("changing only the CRM share moves the CIS remainder and sends only that share", () => {
  const d = { ...draftOf(row(), APPS), reason: REASON };
  d.shares.crm = "8";
  const out = checkDraft(row(), APPS, d);
  assert.equal(out.remainder, 92);
  assert.deepEqual(out.body, { shares: { crm: 8 }, reason: REASON });
});

test("an unchanged draft has no body and needs no reason", () => {
  const out = checkDraft(row(), APPS, draftOf(row(), APPS));
  assert.equal(out.changed, false);
  assert.equal(out.body, null);
  assert.deepEqual(out.errors, {});
});

test("shares above the total are refused, as the API does", () => {
  const d = { ...draftOf(row(), APPS), reason: REASON };
  d.shares.tripplanner = "96";
  const out = checkDraft(row(), APPS, d);
  assert.equal(out.errors.total, "overAllocated");
  assert.equal(out.body, null);
});

test("a short reason, a bad amount and a missing total are named", () => {
  const d = { ...draftOf(row(), APPS), reason: "too short" };
  d.shares.booking = "1.234";
  const out = checkDraft(row(), APPS, d);
  assert.equal(out.errors.booking, "amount");
  assert.equal(out.errors.reason, "reason");
  const none = row({ total: null, shares: { booking: null, crm: null, tripplanner: null } });
  assert.equal(checkDraft(none, APPS, { total: "", shares: { tripplanner: "3" }, reason: REASON }).errors.total,
    "totalRequired");
});

test("a removed share is sent as null and a new total as total_usd", () => {
  const d = { ...draftOf(row(), APPS), total: "120", reason: REASON };
  d.shares.crm = "";
  const out = checkDraft(row(), APPS, d);
  assert.deepEqual(out.body, { total_usd: 120, shares: { crm: null }, reason: REASON });
  assert.equal(out.remainder, 120);
});

test("row state: no total, cap out of step, CRM not in step, CIS stopped, ok", () => {
  assert.equal(rowState(row({ total: null })).key, "noTotal");
  assert.equal(rowState(row({ cis_cap_in_sync: false })).key, "capOutOfSync");
  assert.equal(rowState(row({ crm_state: "not_sent" })).key, "crmNotInStep");
  assert.equal(rowState(row({ cis_pool_spent_usd: 95 })).key, "cisStopped");
  assert.equal(rowState(row({ cis_pool_spent_usd: 95, hard_stop: false })).key, "ok");
  assert.equal(rowState(row()).tone, "success");
});

test("connections with a total come first, then by key", () => {
  const out = budgetOrder([row({ connection_key: "zz-c", total: null }), row({ connection_key: "zz-b" }),
    row({ connection_key: "zz-a" })]);
  assert.deepEqual(out.map((r) => r.connection_key), ["zz-a", "zz-b", "zz-c"]);
  assert.equal(appLabel("tripplanner"), "TripPlanner");
});

test("the section's copy names no issue id in either language", () => {
  const path = fileURLToPath(new URL("../app/admin/ai-gateway/connectionBudgets.messages.ts", import.meta.url));
  assert.doesNotMatch(readFileSync(path, "utf8"), /AA-\d+/);
});
