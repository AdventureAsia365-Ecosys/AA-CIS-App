// lib/routeTrace.test.mjs — AA-1042 (S268): the pure helpers of the AI gateway Trace tab (lib/routeTrace.ts), plus
// text guards: the tab is wired into the page and the Calls row, the page actions match the backend PAGE_WRITES, and
// the UI copy names no issue id.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  DEFAULT_STAGE, ROUTE_TEST_API, SKIPPED_STATUS, TEST_MAX_REQUESTS, attemptTone, outcomeTone, parseRouteTarget,
  parseRouteWindow, pct, policyBody, policyDraft, policyPath, requestsPath, rungLabel, rungsPath, tracePath,
} from "./routeTrace.ts";
import { ACTION_CAPABILITY, PAGE_ACTIONS, TAB_KEYS, parseTab } from "./aiGateway.ts";

const ID = "6f1c5f8e-1b9e-4c55-9f5e-1a2b3c4d5e6f";

test("paths carry their filters", () => {
  assert.equal(requestsPath("24h", null, false), "/api/admin/ai-gateway/route-trace/requests?window=24h");
  assert.equal(requestsPath("1h", "stage_a", true),
    "/api/admin/ai-gateway/route-trace/requests?window=1h&stage=stage_a&fallback_only=true");
  assert.equal(tracePath(ID), `/api/admin/ai-gateway/route-trace/requests/${ID}`);
  assert.ok(tracePath(ID, "2026-11-04T09:00:00+00:00").endsWith("?at=2026-11-04T09%3A00%3A00%2B00%3A00"));
  assert.equal(rungsPath("7d"), "/api/admin/ai-gateway/route-trace/rungs?window=7d");
  assert.equal(policyPath(DEFAULT_STAGE), "/api/admin/ai-gateway/route-policy/*");
  assert.equal(ROUTE_TEST_API, "/api/admin/ai-gateway/route-trace/test");
});

test("the address opens a request only for a UUID", () => {
  assert.deepEqual(parseRouteTarget(ID.toUpperCase(), "2026-11-04T09:00:00Z"), { id: ID, at: "2026-11-04T09:00:00Z" });
  assert.deepEqual(parseRouteTarget(ID, "not a date"), { id: ID, at: null });
  assert.equal(parseRouteTarget("x", null), null);
  assert.equal(parseRouteTarget(null, null), null);
  assert.equal(parseRouteWindow("7d"), "7d");
  assert.equal(parseRouteWindow("30d"), "24h");
});

test("tones and labels", () => {
  assert.equal(outcomeTone("primary"), "success");
  assert.equal(outcomeTone("fallback"), "warning");
  assert.equal(outcomeTone("no_answer"), "danger");
  assert.equal(attemptTone("refused"), "warning");
  assert.equal(attemptTone("billed_unusable"), "danger");
  assert.equal(attemptTone("other"), "neutral");
  assert.equal(attemptTone(SKIPPED_STATUS), "neutral");      // AA-1042 (S270): a deferred rung, never called
  assert.equal(SKIPPED_STATUS, "skipped");
  assert.equal(rungLabel("model-two", "api"), "model-two @ api");
  assert.equal(rungLabel(null, "api"), "—");
  assert.equal(pct(33.333), "33.3 %");
  assert.equal(pct(null), "—");
});

test("a policy draft round-trips and empty means inherit", () => {
  const row = { stage: "stage_a", fallback_alert_pct: 30, min_requests: null, alert_last_rung: false,
    cost_shift_usd: 2.5, hold_fallback_writes: true, notes: "n", updated_at: null, updated_by: null };
  const d = policyDraft(row);
  assert.deepEqual(d, { fallback_alert_pct: "30", min_requests: "", cost_shift_usd: "2.5", alert_last_rung: "off",
    hold_fallback_writes: "on", notes: "n" });
  assert.deepEqual(policyBody(d), { fallback_alert_pct: 30, min_requests: null, cost_shift_usd: 2.5,
    alert_last_rung: false, hold_fallback_writes: true, notes: "n" });
  assert.deepEqual(policyBody(policyDraft(null)), { fallback_alert_pct: null, min_requests: null,
    cost_shift_usd: null, alert_last_rung: null, hold_fallback_writes: null, notes: null });
});

test("a bad policy value names its field", () => {
  const base = policyDraft(null);
  assert.equal(policyBody({ ...base, fallback_alert_pct: "0" }), "fallback_alert_pct");
  assert.equal(policyBody({ ...base, fallback_alert_pct: "12.5" }), "fallback_alert_pct");
  assert.equal(policyBody({ ...base, min_requests: "10001" }), "min_requests");
  assert.equal(policyBody({ ...base, cost_shift_usd: "0" }), "cost_shift_usd");
  assert.equal(policyBody({ ...base, cost_shift_usd: "1.234" }), "cost_shift_usd");
  assert.equal(policyBody({ ...base, notes: "x".repeat(501) }), "notes");
});

test("the Trace tab and its write actions are registered", () => {
  assert.ok(TAB_KEYS.includes("trace"));
  assert.equal(parseTab("trace"), "trace");
  assert.ok(PAGE_ACTIONS.includes("route_policy") && PAGE_ACTIONS.includes("route_test"));
  assert.equal(ACTION_CAPABILITY.route_policy, "ai.admin");
  assert.equal(TEST_MAX_REQUESTS, 6);
  const page = readFileSync(new URL("../app/admin/ai-gateway/AiGateway.tsx", import.meta.url), "utf8");
  assert.match(page, /tab === "trace" && <TraceTab/);
  const calls = readFileSync(new URL("../app/admin/ai-gateway/CallsTab.tsx", import.meta.url), "utf8");
  assert.match(calls, /route_trace/);
  const py = readFileSync(new URL("../../api/services/llm_route_trace.py", import.meta.url), "utf8");
  assert.match(py, new RegExp(`TEST_MAX_REQUESTS = ${TEST_MAX_REQUESTS}\\b`));
});

test("the Trace tab copy names no issue id", () => {
  for (const f of ["routeTrace.messages.ts"]) {
    const text = readFileSync(new URL(`../app/admin/ai-gateway/${f}`, import.meta.url), "utf8");
    const body = text.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
    assert.doesNotMatch(body, /AA-\d+/);
  }
});
