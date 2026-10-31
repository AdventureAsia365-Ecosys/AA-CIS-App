// frontend/lib/aiGateway.test.mjs
// AA-804: unit tests for the AI providers page helpers in aiGateway.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CALL_PARAM, RETIRED_COST_OPS_TABS, RETIRED_SETTINGS_TABS, accountRouteMatters, aiGatewayHref, budgetBody,
  budgetDraftOf, budgetProviderLabel, budgetPutPath, budgetTone, callFiltersFrom, callTone, callsHref, callsPath,
  costOpsSpendHref, groupRows, needsLook, parseSpendView, parseTab, parseTrace, parseTraceTarget, parseView, pct,
  retiredTabTarget, sameDraft, severity, statusTone, tourTraceHref, traceHref, traceParam, tracePath, usd, validScope,
} from "./aiGateway.ts";
import { LLM_ACCOUNT, MAIN_ACCOUNT } from "./awsAccounts.ts";

const ID = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b";

test("statusTone maps every status and falls back to neutral", () => {
  assert.equal(statusTone("ok"), "success");
  assert.equal(statusTone("open"), "danger");
  assert.equal(statusTone("cap_reached"), "danger");
  assert.equal(statusTone("low"), "warning");
  assert.equal(statusTone("degraded"), "warning");
  assert.equal(statusTone("disabled"), "neutral");
  assert.equal(statusTone("something-new"), "neutral");
  assert.equal(statusTone(null), "neutral");
});

test("severity puts a blocked source before a healthy one, switched off last", () => {
  const order = ["ok", "disabled", "low", "open", "unknown", "cap_reached", "degraded"]
    .sort((a, b) => severity(a) - severity(b));
  assert.deepEqual(order, ["open", "cap_reached", "degraded", "low", "unknown", "ok", "disabled"]);
  assert.equal(needsLook([{ status: "ok" }, { status: "disabled" }, { status: "low" }, { status: "unknown" }]), 2);
});

test("usd shows more decimals below one dollar; pct rounds", () => {
  assert.equal(usd(12.345), "$12.35");
  assert.equal(usd(0.0123), "$0.0123");
  assert.equal(usd(0), "$0.00");
  assert.equal(usd(null), "—");
  assert.equal(usd(0.5, 2), "$0.50");
  assert.equal(pct(4.83), "4.8 %");
  assert.equal(pct(82.4), "82 %");
  assert.equal(pct(null), "—");
});

test("trace links round-trip and refuse anything but a job uuid", () => {
  const href = traceHref(ID);
  assert.equal(href, `/admin/ai-gateway?trace=job%3A${ID}`);
  const value = new URL(href, "https://example.test").searchParams.get("trace");
  assert.equal(parseTrace(value), ID);
  assert.equal(parseTrace(`job:${ID.toUpperCase()}`), ID);
  assert.equal(parseTrace("job:not-a-uuid"), null);
  assert.equal(parseTrace(`tour:${ID}`), null);
  assert.equal(parseTrace(""), null);
  assert.equal(parseTrace(null), null);
  assert.equal(tracePath(ID), `/api/admin/ai-gateway/trace?job_id=${ID}`);
});

test("groupRows keeps the API order inside each group", () => {
  const rows = [
    { key: "llm:b", group: "llm" }, { key: "jev", group: "decide" }, { key: "llm:a", group: "llm" },
    { key: "dataforseo", group: "data" }, { key: "geoapify", group: "data" },
  ];
  const g = groupRows(rows);
  assert.deepEqual(g.llm.map((r) => r.key), ["llm:b", "llm:a"]);
  assert.deepEqual(g.decide.map((r) => r.key), ["jev"]);
  assert.deepEqual(g.data.map((r) => r.key), ["dataforseo", "geoapify"]);
});

// ── UI part 2: tabs, retired links, account select, budgets ───────────────────────────────────────────────────────

/** The `tab` values of a link (a retired link must carry exactly one). */
const tabsOf = (href) => new URL(href, "https://admin.example").searchParams.getAll("tab");

test("tabs and views parse strictly and default to the providers overview", () => {
  assert.equal(parseTab("routing"), "routing");
  assert.equal(parseTab("budgets"), "budgets");
  assert.equal(parseTab("calls"), "calls");
  assert.equal(parseTab("ledger"), "providers");
  assert.equal(parseTab(null), "providers");
  assert.equal(parseView("jev-credit"), "jev-credit");
  assert.equal(parseView("nope"), "overview");
});

test("page links carry exactly one tab, and a view only on Providers", () => {
  assert.equal(aiGatewayHref("routing"), "/admin/ai-gateway?tab=routing");
  assert.equal(aiGatewayHref("providers", "jev-credit"), "/admin/ai-gateway?tab=providers&view=jev-credit");
  assert.equal(aiGatewayHref("providers", "overview"), "/admin/ai-gateway?tab=providers");
  assert.equal(aiGatewayHref("budgets", "connections"), "/admin/ai-gateway?tab=budgets");
  for (const href of [...Object.values(RETIRED_SETTINGS_TABS), ...Object.values(RETIRED_COST_OPS_TABS)]) {
    assert.deepEqual(tabsOf(href).length, 1, href);
  }
});

test("retired Settings and Cost Operations tabs point to their new tab", () => {
  assert.equal(retiredTabTarget(RETIRED_SETTINGS_TABS, "models"), "/admin/ai-gateway?tab=routing");
  assert.equal(retiredTabTarget(RETIRED_SETTINGS_TABS, "jev-credit"),
    "/admin/ai-gateway?tab=providers&view=jev-credit");
  assert.equal(retiredTabTarget(RETIRED_COST_OPS_TABS, "budgets"), "/admin/ai-gateway?tab=budgets");
  // UI part 3: the LLM, DataForSEO and Jev tabs are views of the merged "AI & data spend" tab
  assert.equal(retiredTabTarget(RETIRED_COST_OPS_TABS, "llm"), "/admin/cost-ops?tab=spend");
  assert.equal(retiredTabTarget(RETIRED_COST_OPS_TABS, "dfs"), "/admin/cost-ops?tab=spend&view=dfs");
  assert.equal(retiredTabTarget(RETIRED_COST_OPS_TABS, "jev"), "/admin/cost-ops?tab=spend&view=jev");
  // tabs that stay where they are, no tab, and object keys are never a target
  for (const v of ["brand", "pipeline", "", null, undefined, "toString", "__proto__"]) {
    assert.equal(retiredTabTarget(RETIRED_SETTINGS_TABS, v), null, String(v));
    assert.equal(retiredTabTarget(RETIRED_COST_OPS_TABS, v), null, String(v));
  }
  for (const v of ["overview", "spend", "aws"]) assert.equal(retiredTabTarget(RETIRED_COST_OPS_TABS, v), null, v);
});

test("the account select shows only while a stage routes to another account than the LLM one", () => {
  assert.equal(accountRouteMatters([]), false);
  assert.equal(accountRouteMatters([{ account_route: null }, { account_route: LLM_ACCOUNT }]), false);
  assert.equal(accountRouteMatters([{ account_route: "" }, { account_route: undefined }]), false);
  assert.equal(accountRouteMatters([{ account_route: null }, { account_route: MAIN_ACCOUNT }]), true);
  assert.equal(accountRouteMatters([{ account_route: "retired" }]), true);
});

test("budget drafts: no cap is empty, the body follows the backend rules", () => {
  const row = { per_run_usd: null, per_day_usd: 2.5, hard_stop: true, alert_pct: 80 };
  const d = budgetDraftOf(row);
  assert.deepEqual(d, { per_run: "", per_day: "2.5", hard_stop: true, alert_pct: "80" });
  assert.equal(sameDraft(d, { ...d, per_day: " 2.5 " }), true);
  assert.equal(sameDraft(d, { ...d, hard_stop: false }), false);
  assert.deepEqual(budgetBody(d).body, { per_run_usd: null, per_day_usd: 2.5, hard_stop: true, alert_pct: 80 });
  assert.deepEqual(budgetBody({ ...d, per_run: "0" }).body.per_run_usd, 0);
  assert.equal(budgetBody({ ...d, per_day: "-1" }).error, "cap");
  assert.equal(budgetBody({ ...d, per_run: "abc" }).error, "cap");
  assert.equal(budgetBody({ ...d, alert_pct: "0" }).error, "alertPct");
  assert.equal(budgetBody({ ...d, alert_pct: "80.5" }).error, "alertPct");
  assert.equal(budgetDraftOf({ ...row, alert_pct: null }).alert_pct, "80");
});

test("budget scopes, labels, tones and the PUT path", () => {
  assert.equal(validScope("global"), true);
  assert.equal(validScope("job:segment_research"), true);
  assert.equal(validScope("job:Segment"), false);
  assert.equal(validScope("job:"), false);
  assert.equal(validScope("tenant:x"), false);
  assert.equal(budgetProviderLabel("dfs"), "DataForSEO");
  assert.equal(budgetProviderLabel("newone"), "newone");
  assert.equal(budgetTone("cap_reached"), "danger");
  assert.equal(budgetTone("stopped"), "danger");
  assert.equal(budgetTone("alert"), "warning");
  assert.equal(budgetTone("no_budget"), "neutral");
  assert.equal(budgetTone(undefined), "neutral");
  assert.equal(budgetPutPath("dfs", "job:segment_research"), "/api/admin/budgets/dfs/job%3Asegment_research");
});

// ── UI part 3: tour trace, Calls tab, merged spend tab ─────────────────────────────────────────────────────────────

test("trace targets: a job or a tour, parsed strictly, with their API paths", () => {
  const href = tourTraceHref(ID);
  assert.equal(href, `/admin/ai-gateway?trace=tour%3A${ID}`);
  const value = new URL(href, "https://example.test").searchParams.get("trace");
  assert.deepEqual(parseTraceTarget(value), { kind: "tour", id: ID });
  assert.deepEqual(parseTraceTarget(`job:${ID.toUpperCase()}`), { kind: "job", id: ID });
  for (const bad of ["tour:x", "job:", `trip:${ID}`, "", null, undefined]) assert.equal(parseTraceTarget(bad), null);
  assert.equal(parseTrace(`tour:${ID}`), null, "parseTrace stays job-only");
  assert.equal(tracePath({ kind: "tour", id: ID }), `/api/admin/ai-gateway/trace?tour_id=${ID}`);
  assert.equal(tracePath({ kind: "job", id: ID }), `/api/admin/ai-gateway/trace?job_id=${ID}`);
  assert.equal(traceParam({ kind: "tour", id: ID }), `tour:${ID}`);
});

test("calls: the API path carries only the filters that are set, then the cursor", () => {
  assert.equal(callsPath({}), "/api/admin/ai-gateway/calls");
  assert.equal(callsPath({ status: "refused", stage: " invented_stage ", app: "" }, "2026-03-14T10:00:00+00:00~llm:x", 100),
    "/api/admin/ai-gateway/calls?status=refused&stage=invented_stage&cursor=2026-03-14T10%3A00%3A00%2B00%3A00%7Ellm%3Ax&limit=100");
});

test("calls: filters read from the address under their c_ keys; a bad job or tour id is dropped", () => {
  const q = new URLSearchParams({ c_job: ID.toUpperCase(), c_tour: "nope", c_status: "error", c_window: "7d", tab: "calls" });
  assert.deepEqual(callFiltersFrom((k) => q.get(k) ?? ""), { job_id: ID, status: "error", window: "7d" });
  assert.equal(CALL_PARAM.job_id, "c_job");
  const href = callsHref({ job_id: ID, source: "llm" });
  assert.equal(href, `/admin/ai-gateway?tab=calls&c_job=${ID}&c_source=llm`);
  assert.deepEqual(new URL(href, "https://x.test").searchParams.getAll("tab"), ["calls"]);
});

test("callTone: refused and errors red, partial outcomes amber, answers green", () => {
  assert.equal(callTone("refused"), "danger");
  assert.equal(callTone("error"), "danger");
  assert.equal(callTone("truncated"), "warning");
  assert.equal(callTone("billed_unusable"), "warning");
  assert.equal(callTone("ok"), "success");
  assert.equal(callTone("cache"), "info");
  assert.equal(callTone(null), "neutral");
});

test("the merged spend tab: exactly one tab value, a view only when it is not the LLM view", () => {
  assert.equal(costOpsSpendHref(), "/admin/cost-ops?tab=spend");
  assert.equal(costOpsSpendHref("llm"), "/admin/cost-ops?tab=spend");
  assert.equal(costOpsSpendHref("jev"), "/admin/cost-ops?tab=spend&view=jev");
  assert.equal(parseSpendView("dfs"), "dfs");
  assert.equal(parseSpendView("aws"), "llm");
  assert.equal(parseSpendView(null), "llm");
});
