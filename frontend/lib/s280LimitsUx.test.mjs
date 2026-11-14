// frontend/lib/s280LimitsUx.test.mjs
// S280 (AA-1096) — the helpers behind the cap / threshold / override editors outside Platform › Settings:
//   - kit apiErrorMessage: a pydantic 422 (a list of {loc, msg}) or a {code, message} detail reads as text, never
//     "[object Object]";
//   - loosenedFields (the client copy of settings_registry.loosened) and the new-cap defaults (a new provider cap stops
//     at the cap; a tighten-only role's new central cap too);
//   - qualifyingEvalRuns (the eval runs that can back a gated route change);
//   - Jev floors (a comma decimal, NaN refused, reject below accept, what enforce needs);
//   - unitText / overrideUnit (an RPM, seats or USD override in its own unit);
//   - tenantSearchText (a table row found by the tenant's slug or id);
//   - the daily publish default from the API, never a constant;
//   - the route alert policy ranges.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import { apiErrorMessage } from "../app/_kit/api.ts";
import { budgetBody, centralBody, centralDraftOf } from "./aiGateway.ts";
import {
  NEW_BUDGET_DRAFT, SPEND_CAP_KINDS, TENANT_BUDGET_KINDS, TIGHTEN_ONLY_REFUSAL, isTightenOnlyRefusal, loosenedFields,
  newCentralDraft, qualifyingEvalRuns,
} from "./capTighten.ts";
import { parseFloor, questionBody } from "./decisionFloors.ts";
import { overrideUnit, unitText } from "./tenantPlans.ts";
import { tenantSearchText } from "./tenants.ts";
import { POLICY_LIMITS, policyBody } from "./routeTrace.ts";
import { isTightenOnly } from "./rbac.ts";
import {
  dailyPublishDefault, dailyPublishDefaultText, fmtDailyPublishLimit,
} from "../app/admin/tenants/_components/tenant360.ts";

// ── kit apiErrorMessage ─────────────────────────────────────────────────────────────────────────────────────────

test("a pydantic 422 reads as field: message, never [object Object]", () => {
  const body = { detail: [
    { loc: ["body", "shadow_sample_pct"], msg: "Input should be a valid integer", type: "int_parsing" },
    { loc: ["body", "accept_floor"], msg: "Input should be less than or equal to 1" },
  ] };
  assert.equal(apiErrorMessage(body, 422),
    "shadow_sample_pct: Input should be a valid integer; accept_floor: Input should be less than or equal to 1");
});

test("an object detail, a string detail and an empty body", () => {
  assert.equal(apiErrorMessage({ detail: { detail: "plan_retired", message: "The plan is retired" } }, 409),
    "The plan is retired");
  assert.equal(apiErrorMessage({ detail: "no such tenant" }, 404), "no such tenant");
  assert.equal(apiErrorMessage({ message: "upstream down" }, 502), "upstream down");
  assert.equal(apiErrorMessage(null, 500), "Request failed (500)");
  assert.equal(apiErrorMessage({ detail: [{}] }, 422), "Request failed (422)");
  assert.notEqual(apiErrorMessage({ detail: [{ loc: ["query", "limit"], msg: "too big" }] }, 422), "[object Object]");
});

// ── tighten-only and new caps ───────────────────────────────────────────────────────────────────────────────────

test("a new provider cap starts with the hard stop on, which a tighten-only role may add", () => {
  assert.equal(NEW_BUDGET_DRAFT.hard_stop, true);
  const { body } = budgetBody({ ...NEW_BUDGET_DRAFT, per_day: "5" });
  assert.deepEqual(loosenedFields(null, body, SPEND_CAP_KINDS), []);
  assert.deepEqual(loosenedFields(null, { ...body, hard_stop: false }, SPEND_CAP_KINDS), ["hard_stop"]);
});

test("a new central cap is alert only for the administrator, stops for a tighten-only role", () => {
  assert.equal(centralDraftOf(null).hard_stop, false);
  const tight = newCentralDraft(true);
  assert.equal(tight.hard_stop, true);
  const { body } = centralBody({ ...tight, target: "t9_write", per_day: "2" });
  assert.deepEqual(loosenedFields(null, body, SPEND_CAP_KINDS), []);
});

test("loosenedFields mirrors the backend: raise, remove, soften, later alert", () => {
  const before = { per_run_usd: 1, per_day_usd: 10, per_month_usd: null, hard_stop: true, alert_pct: 80 };
  assert.deepEqual(loosenedFields(before, { ...before, per_day_usd: 5, alert_pct: 70 }, SPEND_CAP_KINDS), []);
  assert.deepEqual(loosenedFields(before, { ...before, per_day_usd: 12 }, SPEND_CAP_KINDS), ["per_day_usd"]);
  assert.deepEqual(loosenedFields(before, { ...before, per_run_usd: null }, SPEND_CAP_KINDS), ["per_run_usd"]);
  assert.deepEqual(loosenedFields(before, { ...before, per_month_usd: 50 }, SPEND_CAP_KINDS), []);
  assert.deepEqual(loosenedFields(before, { ...before, hard_stop: false, alert_pct: 90 }, SPEND_CAP_KINDS),
    ["hard_stop", "alert_pct"]);
  const tb = { monthly_usd: 15, alert_pct: 80, approval_pct: 100, hard_stop: true };
  assert.deepEqual(loosenedFields(tb, { ...tb, monthly_usd: null }, TENANT_BUDGET_KINDS), ["monthly_usd"]);
  assert.deepEqual(loosenedFields(tb, { ...tb, approval_pct: 120 }, TENANT_BUDGET_KINDS), ["approval_pct"]);
});

test("the tighten-only refusal and roles", () => {
  const e = Object.assign(new Error(`${TIGHTEN_ONLY_REFUSAL} (lower it or turn the hard stop on); ask: per_day_usd`),
    { status: 403 });
  assert.equal(isTightenOnlyRefusal(e), true);
  assert.equal(isTightenOnlyRefusal(new Error("nope")), false);
  assert.equal(isTightenOnly("admin"), false);
  assert.equal(isTightenOnly("ai_ops"), true);
  assert.equal(isTightenOnly("customer_success"), true);
  assert.equal(isTightenOnly(null), false);
});

// ── evidence ────────────────────────────────────────────────────────────────────────────────────────────────────

test("only passed, completed, regression-free runs back a gated change, newest first", () => {
  const run = (id, o) => ({ id, pipeline: "writing_memory", verdict: "pass", stop_reason: "completed",
    finished_at: "2026-11-10T00:00:00Z", created_at: "2026-11-10T00:00:00Z", regression_detected: false, ...o });
  const rows = [
    run("a"), run("b", { verdict: "fail" }), run("c", { stop_reason: "cost_cap" }), run("d", { finished_at: null }),
    run("e", { regression_detected: true }), run("f", { pipeline: "aa_content", verdict: null }),
    run("g", { pipeline: "aa_content", verdict: "invalid" }), run("h", { finished_at: "2026-11-12T00:00:00Z" }),
  ];
  assert.deepEqual(qualifyingEvalRuns(rows).map((r) => r.id), ["h", "a", "f"]);
});

// ── Jev floors ──────────────────────────────────────────────────────────────────────────────────────────────────

test("a floor accepts a comma decimal and refuses what Number() turned into NaN", () => {
  assert.equal(parseFloor("0,95"), 0.95);
  assert.equal(parseFloor(" 0.3 "), 0.3);
  assert.equal(parseFloor(""), null);
  assert.equal(parseFloor("0.9x"), "bad");
  assert.equal(parseFloor("95"), "bad");
  assert.equal(parseFloor("-0.1"), "bad");
});

test("the question body follows the API's rules", () => {
  const base = { mode: "shadow", accept: "0.95", reject: "0.3", calibration: "" };
  assert.deepEqual(questionBody(base).body,
    { mode: "shadow", accept_floor: 0.95, reject_ceiling: 0.3, calibration_ref: null });
  assert.deepEqual(questionBody({ ...base, accept: "0,9x" }), { error: "accept" });
  assert.deepEqual(questionBody({ ...base, reject: "0.96" }), { error: "order" });
  assert.deepEqual(questionBody({ ...base, mode: "enforce" }), { error: "enforceCalibration" });
  assert.deepEqual(questionBody({ ...base, mode: "enforce", accept: "", reject: "", calibration: "docs/c.md" }),
    { error: "enforceFloor" });
});

// ── units, search, defaults, ranges ─────────────────────────────────────────────────────────────────────────────

test("an override reads in its own unit", () => {
  const w = { usdMonth: "USD a month", perMinute: "requests a minute", seats: "seats", days: "days", perMonth: "a month" };
  assert.equal(unitText("rpm", w), "requests a minute");
  assert.equal(unitText("usd_month", w), "USD a month");
  assert.equal(unitText(null, w), "a month");
  const catalogue = [{ feature_key: "content.rate_limit_rpm", unit: "rpm" }];
  assert.equal(overrideUnit({ feature_key: "content.rate_limit_rpm" }, catalogue), "rpm");
  assert.equal(overrideUnit({ feature_key: "crm.seats", unit: "seats" }, catalogue), "seats");
});

test("a tenant row is found by name, slug, id and extra values", () => {
  const byId = new Map([["t-1", { name: "Lotus Trails", slug: "lotus-trails" }]]);
  const text = tenantSearchText("t-1", byId, "req-42", null);
  for (const q of ["lotus trails", "lotus-trails", "t-1", "req-42"]) assert.ok(text.toLowerCase().includes(q), q);
  assert.equal(tenantSearchText(null, byId), "");
});

test("the daily publish default comes from the API, not a constant", () => {
  const w = { perDayDefault: (n) => `${n}/day (default)`, perDay: (n) => `${n}/day`, platformDefault: "the platform default" };
  assert.equal(dailyPublishDefault({ daily_publish_limit: 5, daily_publish_limit_default: 12 }), 12);
  assert.equal(dailyPublishDefault({ daily_publish_limit: null, daily_publish_limit_effective: 7 }), 7);
  assert.equal(dailyPublishDefault({ daily_publish_limit: 5, daily_publish_limit_effective: 5 }), null);
  assert.equal(fmtDailyPublishLimit(null, w, 12), "12/day (default)");
  assert.equal(fmtDailyPublishLimit(null, w, null), "the platform default");
  assert.equal(fmtDailyPublishLimit(4, w, 12), "4/day");
  assert.equal(dailyPublishDefaultText(null, w), "the platform default");
});

test("the route alert policy ranges are the ones the editor quotes", () => {
  const d = { fallback_alert_pct: "", min_requests: "", cost_shift_usd: "", alert_last_rung: "inherit",
    hold_fallback_writes: "inherit", notes: "" };
  assert.equal(policyBody({ ...d, fallback_alert_pct: String(POLICY_LIMITS.pct[1] + 1) }), "fallback_alert_pct");
  assert.equal(policyBody({ ...d, min_requests: String(POLICY_LIMITS.minRequests[1]) }).min_requests, 10000);
  assert.equal(policyBody({ ...d, cost_shift_usd: "0.001" }), "cost_shift_usd");
});
