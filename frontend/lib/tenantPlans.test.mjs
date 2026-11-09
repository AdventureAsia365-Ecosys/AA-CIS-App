// frontend/lib/tenantPlans.test.mjs
// AA-1028 R1 slice 3: unit tests for the plan catalogue and Tenant 360 › Subscription helpers (lib/tenantPlans.ts).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  INT4_MAX, RATE_LIMIT_RPM_MAX, defaultOverrideMode, featuresFor, limitText, lockedLimitKeys, overrideBody,
  overrideDraftOf, overrideFeatures, overrideModes, planBody, planDraftOf, refusalOf, runSaga, tierChoices,
  addOnDecisionPath, addOnRequestPath, approveBody, requestShown, requestTone, sortAddOnRequests,
} from "./tenantPlans.ts";

const CATALOGUE = [
  { feature_key: "content.rate_limit_rpm", product: "content", value_kind: "limit", unit: "rpm", description: "RPM" },
  { feature_key: "content.social", product: "content", value_kind: "flag", unit: null, description: "Social" },
  { feature_key: "crm.core", product: "crm", value_kind: "flag", unit: null, description: "CRM" },
  { feature_key: "crm.seats", product: "crm", value_kind: "seats", unit: "seats", description: "Seats" },
  { feature_key: "ai.budget_usd_monthly", product: "platform", value_kind: "limit", unit: "usd_month",
    description: "AI" },
  { feature_key: "content.rewrite", product: "content", value_kind: "limit", unit: "count_month",
    description: "Rewrites" },
];

const plan = (over) => ({
  plan_key: "content.growth", product: "content", name: "Growth", tier: "growth", price_usd_monthly: "799.00",
  price_unit: "tenant", seats_included: null, price_extra_seat_usd: null, is_public: true, version: 3, notes: null,
  updated_at: null, updated_by: "migration:355", retired_at: null, live_subscriptions: 4,
  features: [{ feature_key: "ai.budget_usd_monthly", limit_value: "40.00" },
             { feature_key: "content.rate_limit_rpm", limit_value: "300.00" },
             { feature_key: "content.rewrite", limit_value: "200.00" },
             { feature_key: "content.social", limit_value: null }],
  ...over,
});

test("tier choices come from the live public content plans, cheapest first, plus the current tier", () => {
  const plans = [
    plan({ plan_key: "content.business", tier: "business", price_usd_monthly: "1499.00" }),
    plan({}),
    plan({ plan_key: "content.starter", tier: "starter", price_usd_monthly: "299.00" }),
    plan({ plan_key: "content.enterprise", tier: "enterprise", price_usd_monthly: null, is_public: false }),
    plan({ plan_key: "content.old", tier: "starter", retired_at: "2026-11-01T00:00:00Z" }),
    plan({ plan_key: "crm.core", product: "crm", tier: null }),
  ];
  assert.deepEqual(tierChoices(plans), ["starter", "growth", "business"]);
  assert.deepEqual(tierChoices(plans, "internal"), ["internal", "starter", "growth", "business"]);
  assert.deepEqual(tierChoices(undefined, "growth"), ["growth"]);
});

test("no hard-coded tier list is left in the tenant pages", () => {
  for (const f of ["../app/admin/tenants/page.tsx", "../app/admin/tenants/_components/TenantDetail.tsx",
    "../app/admin/tenants/_components/Tenant360Settings.tsx"]) {
    const src = readFileSync(fileURLToPath(new URL(f, import.meta.url)), "utf8");
    assert.ok(!/PLAN_OPTIONS|PLAN_CHOICES/.test(src), `${f} still lists the plans`);
    assert.ok(!/\["starter", "growth", "business"\]/.test(src), `${f} still lists the tiers`);
  }
});

test("an edit body keeps the version and the feature set", () => {
  const p = plan({});
  const d = planDraftOf(p);
  assert.equal(d.features["content.rate_limit_rpm"], "300.00");
  d.name = " Growth plus ";
  d.features["ai.budget_usd_monthly"] = "40";
  const { body, error } = planBody(d, CATALOGUE, p);
  assert.equal(error, null);
  assert.equal(body.version, 3);
  assert.equal(body.name, "Growth plus");
  assert.ok(!("plan_key" in body) && !("tier" in body));
  assert.deepEqual(body.features, [
    { feature_key: "ai.budget_usd_monthly", limit_value: "40" },
    { feature_key: "content.rate_limit_rpm", limit_value: "300.00" },
    { feature_key: "content.rewrite", limit_value: "200.00" },
    { feature_key: "content.social", limit_value: null },
  ]);
});

test("a create body names product, key and tier; the rules the API applies", () => {
  const d = planDraftOf(null, "crm");
  assert.equal(d.plan_key, "crm.");
  assert.equal(planBody(d, CATALOGUE, null).error, "key");
  Object.assign(d, { plan_key: "crm.team", name: "Team", features: { "crm.core": "", "crm.seats": "5" } });
  const ok = planBody(d, CATALOGUE, null);
  assert.equal(ok.error, null);
  assert.deepEqual([ok.body.plan_key, ok.body.product, ok.body.tier, ok.body.price_unit], ["crm.team", "crm", null, "seat"]);
  assert.equal(planBody({ ...d, plan_key: "content.team" }, CATALOGUE, null).error, "key");
  assert.equal(planBody({ ...d, price: "1.234" }, CATALOGUE, null).error, "price");
  assert.equal(planBody({ ...d, seats_included: "-1" }, CATALOGUE, null).error, "seats");
  const bad = planBody({ ...d, features: { "crm.seats": "lots" } }, CATALOGUE, null);
  assert.deepEqual([bad.error, bad.field], ["limit", "crm.seats"]);
  const tiered = { ...planDraftOf(null, "content"), plan_key: "content.zz", name: "Z", tier: "growth" };
  assert.equal(planBody(tiered, CATALOGUE, null).error, "rpmRequired");
  tiered.features = { "content.rate_limit_rpm": "100", "ai.budget_usd_monthly": "40", "content.rewrite": "" };
  assert.equal(planBody(tiered, CATALOGUE, null).body.tier, "growth");
});

test("a tiered content plan keeps its enforced limits; the AI budget is a number except on internal", () => {
  const tiered = { ...planDraftOf(null, "content"), plan_key: "content.zz", name: "Z", tier: "growth",
                   features: { "content.rate_limit_rpm": "100", "content.rewrite": "50" } };
  assert.deepEqual(pick(planBody(tiered, CATALOGUE, null)), ["enforcedRequired", "ai.budget_usd_monthly"]);
  tiered.features["ai.budget_usd_monthly"] = "";                  // "no limit" is not a budget on a priced tier
  assert.deepEqual(pick(planBody(tiered, CATALOGUE, null)), ["enforcedRequired", "ai.budget_usd_monthly"]);
  assert.equal(planBody({ ...tiered, tier: "internal" }, CATALOGUE, null).error, null);
  tiered.features["ai.budget_usd_monthly"] = "0";                 // 0 = no AI included, explicit
  assert.equal(planBody(tiered, CATALOGUE, null).error, null);
  delete tiered.features["content.rewrite"];
  assert.deepEqual(pick(planBody(tiered, CATALOGUE, null)), ["enforcedRequired", "content.rewrite"]);
});

test("an edit cannot untick a limit the plan has", () => {
  const p = plan({});
  const d = planDraftOf(p);
  delete d.features["ai.budget_usd_monthly"];
  assert.deepEqual(pick(planBody(d, CATALOGUE, p)), ["limitRemoved", "ai.budget_usd_monthly"]);
  const crm = plan({ plan_key: "crm.core", product: "crm", tier: null,
                     features: [{ feature_key: "crm.core", limit_value: null }, { feature_key: "crm.seats", limit_value: "10" }] });
  const c = planDraftOf(crm);
  delete c.features["crm.seats"];
  assert.deepEqual(pick(planBody(c, CATALOGUE, crm)), ["limitRemoved", "crm.seats"]);
  delete c.features["crm.core"];                                  // a flag can still be taken off ...
  c.features["crm.seats"] = "";                                   // ... and a limit set to "no limit" explicitly
  assert.equal(planBody(c, CATALOGUE, crm).error, null);
  assert.deepEqual([...lockedLimitKeys(crm, CATALOGUE)], ["crm.seats"]);
  assert.equal(lockedLimitKeys(null, CATALOGUE).size, 0);
});

test("platform keys are offered and sent on content plans only", () => {
  assert.ok(featuresFor("content", CATALOGUE).some((f) => f.feature_key === "ai.budget_usd_monthly"));
  assert.ok(!featuresFor("crm", CATALOGUE).some((f) => f.product === "platform"));
  const d = { ...planDraftOf(null, "crm"), plan_key: "crm.team", name: "Team",
              features: { "crm.core": "", "ai.budget_usd_monthly": "5" } };
  assert.deepEqual(planBody(d, CATALOGUE, null).body.features, [{ feature_key: "crm.core", limit_value: null }]);
});

test("count, seat and RPM limits stay within what their readers hold", () => {
  const p = plan({});
  const d = planDraftOf(p);
  d.features["content.rewrite"] = "10000000000";
  assert.deepEqual(pick(planBody(d, CATALOGUE, p)), ["limit", "content.rewrite"]);
  d.features["content.rewrite"] = "12.5";
  assert.deepEqual(pick(planBody(d, CATALOGUE, p)), ["limit", "content.rewrite"]);
  d.features["content.rewrite"] = String(INT4_MAX);
  d.features["content.rate_limit_rpm"] = String(RATE_LIMIT_RPM_MAX + 1);
  assert.deepEqual(pick(planBody(d, CATALOGUE, p)), ["limit", "content.rate_limit_rpm"]);
  d.features["content.rate_limit_rpm"] = String(RATE_LIMIT_RPM_MAX);
  assert.equal(planBody(d, CATALOGUE, p).error, null);
  d.features["ai.budget_usd_monthly"] = "12.34";                  // USD keeps its 2 decimals
  assert.equal(planBody(d, CATALOGUE, p).error, null);
  const o = { feature_key: "content.rewrite", mode: "limit", limit: "3000000000", reason: "r", expires: "" };
  assert.equal(overrideBody(o, CATALOGUE, "2026-11-03").error, "limit");
  assert.equal(overrideBody({ ...o, limit: "300" }, CATALOGUE, "2026-11-03").error, null);
});

function pick(r) {
  return [r.error, r.field];
}

test("override drafts", () => {
  const today = "2026-11-03";
  assert.deepEqual(overrideBody({ feature_key: "crm.core", mode: "off", limit: "", reason: "texts", expires: "" },
                                CATALOGUE, today).body,
                   { enabled: false, limit_value: null, reason: "texts", expires_at: null });
  assert.deepEqual(overrideBody({ feature_key: "ai.budget_usd_monthly", mode: "limit", limit: "0.01",
                                  reason: " proof ", expires: "2026-11-04" }, CATALOGUE, today).body,
                   { enabled: true, limit_value: "0.01", reason: "proof", expires_at: "2026-11-04T23:59:59Z" });
  const err = (d) => overrideBody({ feature_key: "crm.seats", mode: "limit", limit: "3", reason: "r", expires: "",
                                    ...d }, CATALOGUE, today).error;
  assert.equal(err({ feature_key: "" }), "feature");
  assert.equal(err({ feature_key: "crm.core" }), "flagLimit");
  assert.equal(err({ limit: "x" }), "limit");
  assert.equal(err({ reason: " " }), "reason");
  assert.equal(err({ expires: "2026-11-02" }), "expires");
  assert.equal(err({}), null);
  const row = { feature_key: "crm.seats", product: "crm", value_kind: "seats", limit_value: "3.00", enabled: true,
                reason: "r", granted_by: "admin:x", expires_at: "2026-12-01T23:59:59+00:00", updated_at: null, active: true };
  assert.deepEqual(overrideDraftOf(row), { feature_key: "crm.seats", mode: "limit", limit: "3.00", reason: "r",
                                           expires: "2026-12-01" });
});

test("limit text", () => {
  const w = { on: "On", noLimit: "No limit", perMonth: (v) => `${v}/mo`, usdMonth: (v) => `USD ${v}/mo`,
              perMinute: (v) => `${v}/min`, seats: (v) => `${v} seats`, days: (v) => `${v} d` };
  assert.equal(limitText("flag", null, null, w), "On");
  assert.equal(limitText("limit", "count_month", null, w), "No limit");
  assert.equal(limitText("limit", "count_month", "20000.00", w), "20,000/mo");
  assert.equal(limitText("limit", "usd_month", "15.00", w), "USD 15.00/mo");
  assert.equal(limitText("limit", "rpm", "300.00", w), "300/min");
  assert.equal(limitText("seats", "seats", "4.00", w), "4 seats");
  assert.equal(limitText("policy", "days", "7.00", w), "7 d");
});

test("refusal bodies keep the API code", () => {
  assert.deepEqual(refusalOf({ detail: { detail: "version_conflict", message: "stale" } }, 409),
                   { code: "version_conflict", message: "stale" });
  assert.deepEqual(refusalOf({ detail: "Tenant not found" }, 404), { code: "http_404", message: "Tenant not found" });
  assert.equal(refusalOf({ detail: [{ loc: ["body", "version"], msg: "Field required" }] }, 422).message,
               "version: Field required");
  assert.equal(refusalOf(null, 500).code, "http_500");
});

test("the saga call waits for its job", async () => {
  const answers = [{ job_id: "j", status: "queued", steps: [], error: null, result: null },
                   { job_id: "j", status: "running", steps: [], error: null, result: null },
                   { job_id: "j", status: "succeeded", steps: ["crm_canceled"], error: null, result: {} }];
  const calls = [];
  globalThis.fetch = async (path, init) => {
    calls.push([path, init.method]);
    return { ok: true, status: 200, json: async () => answers.shift() };
  };
  const job = await runSaga("/api/admin/tenants/t/provision/cancel", { request_key: "k" }, { sleep: async () => {} });
  assert.equal(job.status, "succeeded");
  assert.deepEqual(calls.map((c) => c[1]), ["POST", "GET", "GET"]);
  assert.equal(calls[1][0], "/api/admin/tenants/provision/j");
});

test("override rules: no 'off' for a limit, no override of the request rate, a number for the rewrite quota", () => {
  const today = "2026-11-03";
  const body = (d) => overrideBody({ limit: "", reason: "r", expires: "", ...d }, CATALOGUE, today);
  // "off" on a limit would remove it (the AI budget would fail open): refused, 0 is the way
  assert.equal(body({ feature_key: "ai.budget_usd_monthly", mode: "off" }).error, "offLimit");
  assert.equal(body({ feature_key: "crm.seats", mode: "off" }).error, "offLimit");
  assert.deepEqual(body({ feature_key: "ai.budget_usd_monthly", mode: "limit", limit: "0" }).body,
                   { enabled: true, limit_value: "0", reason: "r", expires_at: null });
  assert.equal(body({ feature_key: "content.rate_limit_rpm", mode: "limit", limit: "450" }).error, "notOverridable");
  assert.equal(body({ feature_key: "content.rewrite", mode: "on" }).error, "limitRequired");
  assert.equal(body({ feature_key: "content.rewrite", mode: "limit", limit: "" }).error, "limitRequired");
  assert.equal(body({ feature_key: "content.rewrite", mode: "limit", limit: "0" }).error, null);
  assert.equal(body({ feature_key: "crm.core", mode: "off" }).error, null);
  // the drawer's choices
  assert.ok(!overrideFeatures(CATALOGUE).some((f) => f.feature_key === "content.rate_limit_rpm"));
  const f = (k) => CATALOGUE.find((x) => x.feature_key === k);
  assert.deepEqual(overrideModes(f("crm.core")), ["off", "on"]);
  assert.deepEqual(overrideModes(f("ai.budget_usd_monthly")), ["limit", "on"]);
  assert.deepEqual(overrideModes(f("content.rewrite")), ["limit"]);
  assert.equal(defaultOverrideMode(f("ai.budget_usd_monthly")), "limit");
  assert.equal(defaultOverrideMode(f("crm.core")), "off");
  // the API's rules win over the defaults
  const rules = { not_overridable: [], limit_required: [] };
  assert.equal(overrideBody({ feature_key: "content.rewrite", mode: "on", limit: "", reason: "r", expires: "" },
                            CATALOGUE, today, rules).error, null);
});

// AA-1036 R3: the add-on requests section (pending first, oldest first; the approve body; the decision paths)
test("add-on requests: order, approve body, tone, paths", () => {
  const r = (id, status, requested_at, decided_at = null) => ({ request_id: id, product: "crm", status, requested_at,
    decided_at, note: null, requested_by: "membership:x", decided_by: null, decision_note: null, plan_key: null,
    provision_job_id: null });
  const rows = [r("a", "declined", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"),
                r("b", "pending", "2026-11-04T00:00:00Z"), r("c", "pending", "2026-11-03T00:00:00Z"),
                r("d", "approved", "2026-11-01T00:00:00Z", "2026-11-04T00:00:00Z")];
  assert.deepEqual(sortAddOnRequests(rows).map((x) => x.request_id), ["c", "b", "d", "a"]);
  assert.deepEqual(approveBody("crm.core", "", "  ").body, { plan_key: "crm.core", seats: null, note: null });
  assert.deepEqual(approveBody("crm.core", "3", " ok ").body, { plan_key: "crm.core", seats: 3, note: "ok" });
  assert.equal(approveBody("", "", "").error, "plan");
  for (const bad of ["0", "501", "2.5", "x"]) assert.equal(approveBody("crm.core", bad, "").error, "seats");
  assert.equal(requestTone("pending"), "warning");
  assert.equal(requestTone("approved"), "success");
  assert.equal(requestTone("declined"), "neutral");
  assert.equal(addOnDecisionPath("t1", "r1", "decline"), "/api/admin/tenants/t1/addon-requests/r1/decline");
  assert.equal(addOnRequestPath("t1", "r1"), "/api/admin/tenants/t1/addon-requests/r1");
});

// AA-1036 R3 review: an approval whose attach job failed is not shown as a bare "Approved"
test("add-on requests: the setup of an approval decides the badge", () => {
  assert.equal(requestShown({ status: "approved", setup: "failed" }), "setup_failed");
  assert.equal(requestShown({ status: "approved", setup: "in_progress" }), "setting_up");
  assert.equal(requestShown({ status: "approved", setup: "done" }), "approved");
  assert.equal(requestShown({ status: "approved", setup: null }), "approved");
  assert.equal(requestShown({ status: "declined", setup: null }), "declined");
  assert.equal(requestShown({ status: "pending" }), "pending");
  assert.equal(requestTone("setup_failed"), "danger");
  assert.equal(requestTone("setting_up"), "warning");
});

test("runSaga polls the given path (an add-on approval polls its request route)", async () => {
  const seen = [];
  const replies = [{ job_id: "j1", status: "queued" }, { job_id: "j1", status: "failed", error: "boom" }];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push(`${init?.method ?? "GET"} ${url}`);
    return new Response(JSON.stringify(replies.shift()), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    const job = await runSaga("/api/admin/tenants/t1/addon-requests/r1/approve", { plan_key: "crm.core" },
      { sleep: async () => {}, pollPath: () => "/api/admin/tenants/t1/addon-requests/r1" });
    assert.equal(job.status, "failed");
    assert.deepEqual(seen, ["POST /api/admin/tenants/t1/addon-requests/r1/approve",
                            "GET /api/admin/tenants/t1/addon-requests/r1"]);
  } finally {
    globalThis.fetch = realFetch;
  }
});
