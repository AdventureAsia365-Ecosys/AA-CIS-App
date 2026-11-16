// frontend/lib/deliveryMetrics.test.mjs
// AA-1108: unit tests for the Platform › Delivery helpers in deliveryMetrics.ts (an invented app summary).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  doraN,
  doraValue,
  fmtHours,
  fmtNum,
  fmtRatio,
  fmtSeconds,
  hoursSince,
  prUrl,
  safeGithubUrl,
  segmentRows,
  slowestSegment,
  sparkSeries,
  statusTone,
  worstStatus,
} from "./deliveryMetrics.ts";

const week = (i, over = {}) => ({
  week: `2026-W${String(i).padStart(2, "0")}`, start: "2026-03-02", end: "2026-03-08", partial: false, complete: true,
  deploys: i, deploy_attempts: i, lead_time_p50_h: 2, lead_time_n: 3, cfr: 0.1, failed: 0, recovery_p50_h: null,
  recovery_n: 0, rework_rate: 0, rework: 0, main_failure_rate: 0.05, main_n: 20, ci_p95_s: 600, minutes: 90, ...over,
});

const app = (over = {}) => ({
  id: "demo", name: "Demo", repo: "demo-app", environment: "dev",
  window: { start: "2026-02-20T00:00:00Z", end: "2026-03-20T00:00:00Z", days: 28 }, complete_since: "2025-12-12",
  dora: {
    deployment_frequency: { value: 1.5, n: 6, attempts: 8, unit: "per_week" },
    lead_time: { p50_h: 1.333, p90_h: 3.3, n: 6 },
    change_failure_rate: { value: 0.375, n: 8, failed: 3, by_reason: {} },
    recovery_time: { p50_h: 0.5, p90_h: 1.9, n: 3, open: 0 },
    rework_rate: { value: 0.125, n: 8, rework: 1 },
  },
  lead_time_segments: [
    { segment: "to_first_green", p50_h: 0.3, p90_h: 0.5, n: 2 },
    { segment: "to_queue", p50_h: 0.7, p90_h: 0.7, n: 2 },
    { segment: "to_merge", p50_h: 0.3, p90_h: 0.4, n: 6 },
    { segment: "to_deploy", p50_h: 2.25, p90_h: 3, n: 6 },
    { segment: "to_verify", p50_h: null, p90_h: null, n: 0 },
  ],
  ci: { workflows: [], slowest: { name: "CI", p95_s: 1680, n: 9 },
    main: { state: "red", red_since: "2026-03-20T09:00:00Z", red_hours: 3, url: null, failure_rate: 0.33, n: 12 },
    reruns: 1, minutes: { total: 120, coverage: 1, n: 10 } },
  merge_queue: { wait_p50_h: 0.3, wait_p95_h: 0.4, n: 2, ejections: 1 },
  flaky: [], verify: { pass_rate: 0.9, n: 10, red: 1, last_red_at: null, last_red_url: null },
  deploys: [], failed_deploys: [],
  changes: { merged: 8, by_kind: { agent: 6, human: 1, bot: 1 }, docs_only: 1, awaiting_deploy: 0, bot: [],
    slowest: [{ number: 11, title: "feat: harbour map", url: "https://github.com/o/r/pull/11", kind: "human",
      login: null, excluded: null, merged_at: null, lead_time_h: 3.3, segments: { to_first_green: 0.5, to_deploy: 2.25 } }] },
  trend: { weeks: [week(1), week(2, { complete: false }), week(3, { cfr: null })] },
  status: { deployment_frequency: "green", lead_time: "green", change_failure_rate: "red", recovery_time: "few",
    rework_rate: "amber", ci_p95: "amber", main: "red" },
  ...over,
});

test("formatters: hours, seconds, ratios and numbers", () => {
  assert.equal(fmtHours(null), "—");
  assert.equal(fmtHours(0.25), "15 min");
  assert.equal(fmtHours(0.001), "1 min");
  assert.equal(fmtHours(5.04), "5 h");
  assert.equal(fmtHours(72), "3 d");
  assert.equal(fmtSeconds(45), "45 s");
  assert.equal(fmtSeconds(600), "10 min");
  assert.equal(fmtSeconds(undefined), "—");
  assert.equal(fmtRatio(0.375), "38 %");
  assert.equal(fmtRatio(null), "—");
  assert.equal(fmtNum(1.55), "1.6");
});

test("status tone: colours to kit tones, few and none grey", () => {
  assert.equal(statusTone("green"), "success");
  assert.equal(statusTone("amber"), "warning");
  assert.equal(statusTone("red"), "danger");
  assert.equal(statusTone("few"), "neutral");
  assert.equal(statusTone(undefined), "neutral");
});

test("headline values and their n", () => {
  const a = app();
  assert.equal(doraValue(a, "deployment_frequency"), "1.5");
  assert.equal(doraValue(a, "lead_time"), "1.3 h");
  assert.equal(doraValue(a, "change_failure_rate"), "38 %");
  assert.equal(doraValue(a, "ci_p95"), "28 min");
  assert.equal(doraN(a, "deployment_frequency"), 8, "n of the frequency is the deploys that reached Dev");
  assert.equal(doraN(a, "ci_p95"), 9);
  assert.equal(doraN(a, "main"), 12);
});

test("sparkline: an incomplete week and a missing value are gaps", () => {
  const s = sparkSeries(app(), "change_failure_rate");
  assert.deepEqual(s.map((p) => p.v), [10, null, null]);
  assert.deepEqual(sparkSeries(app(), "deployment_frequency").map((p) => p.v), [1, null, 3]);
});

test("segment rows: the median first, then each slow change", () => {
  const rows = segmentRows(app(), "Median");
  assert.equal(rows[0].label, "Median");
  assert.equal(rows[0].to_deploy, 2.25);
  assert.equal("to_verify" in rows[0], false, "a segment without a value is not drawn");
  assert.equal(rows[1].label, "#11");
  assert.equal(slowestSegment(app()), "to_deploy");
  assert.equal(slowestSegment(app({ lead_time_segments: [] })), null);
});

test("worst status decides the card border", () => {
  assert.equal(worstStatus(app()), "red");
  const calm = app({ status: { deployment_frequency: "green", lead_time: "few", change_failure_rate: "green",
    recovery_time: "none", rework_rate: "green", ci_p95: "green", main: "green" } });
  assert.equal(worstStatus(calm), "green");
});

test("links: only github.com links are followed", () => {
  assert.equal(safeGithubUrl("https://github.com/o/r/actions/runs/1"), "https://github.com/o/r/actions/runs/1");
  assert.equal(safeGithubUrl("javascript:alert(1)"), null);
  assert.equal(safeGithubUrl(null), null);
  assert.equal(prUrl("demo-app", 7), "https://github.com/AdventureAsia365-Ecosys/demo-app/pull/7");
});

test("hours since", () => {
  const now = Date.parse("2026-03-20T12:00:00Z");
  assert.equal(hoursSince("2026-03-20T09:00:00Z", now), 3);
  assert.equal(hoursSince(null, now), null);
  assert.equal(hoursSince("not a date", now), null);
});
