// frontend/lib/deliveryRange.test.mjs
// AA-1135: unit tests for the range, sorting, trend, flow and chart helpers of Platform › Delivery (invented data).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SERIES_VARS, appTrendRows, customProblem, doraNumber, dayCount, edgeLabel, flowSteps, histRows, isDay, parseRange,
  pickSort, rangeParams, rangeQuery, rangeWhen, sortCards, squarify, statusRank, trendRows, trendValue, weeklyRows,
  windowNote, withRange,
} from "./deliveryRange.ts";
import { gitWindow, kindRows } from "./deliveryActivity.ts";

const getter = (q) => (k) => new URLSearchParams(q).get(k) ?? "";
const META = { key: "28d", store_from: "2026-08-12", last_day: "2026-11-18", presets: ["7d", "28d", "90d"],
  custom: true, max_days: 90 };

test("parseRange reads presets and custom ranges from the URL, else the default", () => {
  assert.deepEqual(parseRange(getter("")), { range: "28d", from: "", to: "" });
  assert.deepEqual(parseRange(getter("range=7d")), { range: "7d", from: "", to: "" });
  assert.deepEqual(parseRange(getter("range=45d")), { range: "28d", from: "", to: "" });
  assert.deepEqual(parseRange(getter("range=custom&from=2026-10-01&to=2026-10-31")),
    { range: "custom", from: "2026-10-01", to: "2026-10-31" });
  assert.deepEqual(parseRange(getter("range=custom&from=01/10/2026&to=2026-10-31")),
    { range: "28d", from: "", to: "" });
  assert.equal(isDay("2026-02-30"), true); // Date.parse rolls over; the API refuses it
  assert.equal(isDay("2026-13-40"), false);
});

test("rangeQuery, withRange and rangeParams keep the default URL unchanged", () => {
  assert.equal(rangeQuery({ range: "28d", from: "", to: "" }), "");
  assert.equal(rangeQuery({ range: "90d", from: "", to: "" }), "range=90d");
  assert.equal(withRange("/api/x", { range: "7d", from: "", to: "" }), "/api/x?range=7d");
  assert.equal(withRange("/api/x?section=git", { range: "custom", from: "2026-10-01", to: "2026-10-02" }),
    "/api/x?section=git&range=custom&from=2026-10-01&to=2026-10-02");
  assert.equal(withRange("/api/x", { range: "28d", from: "", to: "" }), "/api/x");
  assert.deepEqual(rangeParams({ range: "28d", from: "", to: "" }), { range: null, from: null, to: null });
  assert.deepEqual(rangeParams({ range: "custom", from: "2026-10-01", to: "2026-10-02" }),
    { range: "custom", from: "2026-10-01", to: "2026-10-02" });
});

test("customProblem mirrors the API checks", () => {
  assert.equal(dayCount("2026-10-01", "2026-10-01"), 1);
  assert.equal(dayCount("2026-10-01", "2026-12-29"), 90);
  assert.equal(customProblem("", "2026-10-02", META), "missing");
  assert.equal(customProblem("2026-10-05", "2026-10-02", META), "reversed");
  assert.equal(customProblem("2026-08-12", "2026-11-18", META), "tooLong");
  assert.equal(customProblem("2026-08-01", "2026-08-20", META), "beforeStore");
  assert.equal(customProblem("2026-11-10", "2026-11-19", META), "afterLast");
  assert.equal(customProblem("2026-10-01", "2026-10-31", { ...META, custom: false }), "unavailable");
  assert.equal(customProblem("2026-10-01", "2026-10-31", META), null);
  assert.equal(customProblem("2026-10-01", "2026-10-31", null), null);
});

test("sortCards: worst first, by name, by a metric worst first with no value last", () => {
  const apps = [
    { id: "b", name: "Beta", s: "green", v: 3 }, { id: "a", name: "Alpha", s: "red", v: null },
    { id: "c", name: "Gamma", s: "amber", v: 9 }, { id: "d", name: "Delta", s: "red", v: 1 },
  ];
  const worst = (x) => statusRank(x.s);
  const value = (x) => x.v;
  assert.deepEqual(sortCards(apps, "worst", worst, value, () => false).map((x) => x.id), ["a", "d", "c", "b"]);
  assert.deepEqual(sortCards(apps, "name", worst, value, () => false).map((x) => x.id), ["a", "b", "d", "c"]);
  // lower is better (lead time): the slowest first
  assert.deepEqual(sortCards(apps, "lead_time", worst, value, () => false).map((x) => x.id), ["c", "b", "d", "a"]);
  // higher is better (deploys a week): the fewest first
  assert.deepEqual(sortCards(apps, "deployment_frequency", worst, value, () => true).map((x) => x.id),
    ["d", "b", "c", "a"]);
  assert.equal(pickSort("lead_time", ["lead_time"]), "lead_time");
  assert.equal(pickSort("bogus", ["lead_time"]), "worst");
  assert.equal(pickSort("name", []), "name");
  assert.equal(statusRank(undefined), 4);
});

const week = (w, over = {}) => ({ week: w, start: "2026-10-05", end: "2026-10-11", partial: false, complete: true,
  deploys: 4, deploy_attempts: 5, lead_time_p50_h: 6, lead_time_n: 3, cfr: 0.25, failed: 1, recovery_p50_h: 2,
  recovery_n: 1, rework_rate: 0, rework: 0, main_failure_rate: 0, main_n: 4, ci_p95_s: 600, minutes: 40, ...over });

test("trend rows per week, one column per app, and one app's four keys", () => {
  assert.equal(trendValue(week("2026-W41"), "cfr"), 25);
  assert.equal(trendValue(week("2026-W41", { complete: false }), "deploys"), null);
  assert.equal(trendValue(week("2026-W41", { cfr: null }), "cfr"), null);
  const apps = [
    { id: "x", trend: { weeks: [week("2026-W41"), week("2026-W42", { deploys: 7 })] } },
    { id: "y", trend: { weeks: [week("2026-W42", { deploys: 2 })] } },
  ];
  assert.deepEqual(trendRows(apps, "deploys"), [
    { week: "2026-W41", label: "W41", x: 4, y: null }, { week: "2026-W42", label: "W42", x: 7, y: 2 }]);
  assert.deepEqual(appTrendRows(apps[0])[0], { week: "2026-W41", label: "W41", deploys: 4, lead_time: 6, cfr: 25,
    recovery: 2 });
  assert.deepEqual(trendRows([], "deploys"), []);
});

test("flowSteps marks the longest median and shares", () => {
  const steps = flowSteps([
    { segment: "to_first_green", p50_h: 1, p90_h: 2, p95_h: 3, n: 4 },
    { segment: "to_queue", p50_h: 5, p90_h: 6, n: 4 },
    { segment: "to_merge", p50_h: 0.5, p90_h: 1, p95_h: 1.5, n: 4 },
    { segment: "to_deploy", p50_h: 9, p90_h: 9, p95_h: 9, n: 0 },
  ]);
  assert.deepEqual(steps.map((s) => [s.from, s.to]), [["opened", "first_green"], ["first_green", "queued"],
    ["queued", "merged"], ["merged", "deployed"], ["deployed", "verified"]]);
  assert.deepEqual(steps.map((s) => s.slowest), [false, true, false, false, false]);
  assert.equal(steps[1].p95_h, 6);          // an older summary without p95 falls back to p90
  assert.equal(steps[0].p95_h, 3);
  assert.equal(Math.round(steps[1].share * 1000), Math.round((5 / 6.5) * 1000));
  assert.equal(steps[3].share, 0);          // n 0: not part of the flow
  assert.equal(steps[4].p50_h, null);
  assert.ok(flowSteps(undefined).every((s) => !s.slowest && s.n === 0));
});

test("histogram and weekly ejection rows", () => {
  assert.equal(edgeLabel(0.25), "15 min");
  assert.equal(edgeLabel(2), "2 h");
  assert.equal(edgeLabel(72), "3 d");
  assert.deepEqual(histRows([{ lo_h: 0, hi_h: 0.25, n: 2 }, { lo_h: 0.25, hi_h: 1, n: 1 }, { lo_h: 24, hi_h: null, n: 0 }]),
    [{ label: "< 15 min", n: 2 }, { label: "15 min–1 h", n: 1 }, { label: "≥ 24 h", n: 0 }]);
  assert.deepEqual(histRows(undefined), []);
  assert.deepEqual(weeklyRows([{ week: "2026-W46", start: "2026-11-09", merged: 3, ejected: 1, removed: 0,
    ejection_rate: 0.25 }, { week: "2026-W47", start: "2026-11-16", merged: 0, ejected: 0, removed: 1,
    ejection_rate: null }]), [{ label: "W46", rate: 25, judged: 4 }, { label: "W47", rate: null, judged: 0 }]);
});

test("squarify fills the box with areas proportional to the values", () => {
  const rects = squarify([{ value: 6, item: "a" }, { value: 6, item: "b" }, { value: 4, item: "c" },
    { value: 3, item: "d" }, { value: 2, item: "e" }, { value: 2, item: "f" }, { value: 1, item: "g" },
    { value: 0, item: "zero" }], 600, 400);
  assert.equal(rects.length, 7);
  const area = rects.reduce((t, r) => t + r.w * r.h, 0);
  assert.ok(Math.abs(area - 600 * 400) < 1e-6);
  for (const r of rects) {
    assert.ok(Math.abs(r.w * r.h - (r.value / 24) * 600 * 400) < 1e-6, r.item);
    assert.ok(r.x >= -1e-9 && r.y >= -1e-9 && r.x + r.w <= 600 + 1e-6 && r.y + r.h <= 400 + 1e-6, r.item);
  }
  assert.deepEqual(squarify([], 10, 10), []);
  assert.deepEqual(squarify([{ value: 1, item: "a" }], 0, 10), []);
  const one = squarify([{ value: 5, item: "a" }], 100, 50);
  assert.deepEqual([one[0].x, one[0].y, one[0].w, one[0].h], [0, 0, 100, 50]);
});

test("gitWindow prefers the served range over the fixed windows", () => {
  const win = (n) => ({ commits: n, by_kind: { agent: n }, dirs: [], files: [] });
  const g = { windows: { d30: win(1), d90: win(2) } };
  assert.equal(gitWindow(g, "d90").commits, 2);
  assert.equal(gitWindow({ ...g, range: { ...win(5), daily: [] } }, "d90").commits, 5);
  assert.equal(gitWindow(undefined, "d30"), undefined);
  assert.equal(kindRows([{ name: "R", git: { ...g, range: win(7) } }], "d30")[0].agent, 7);
});

test("doraNumber reads each card metric", () => {
  const app = { dora: { deployment_frequency: { value: 2.5 }, lead_time: { p50_h: 30 },
    change_failure_rate: { value: null }, recovery_time: { p50_h: 4 }, rework_rate: { value: 0.1 } },
  ci: { slowest: { p95_s: 600 } } };
  assert.equal(doraNumber(app, "deployment_frequency"), 2.5);
  assert.equal(doraNumber(app, "lead_time"), 30);
  assert.equal(doraNumber(app, "change_failure_rate"), null);
  assert.equal(doraNumber(app, "ci_p95"), 600);
  assert.equal(doraNumber({ dora: {}, ci: {} }, "rework_rate"), null);
  assert.equal(doraNumber(app, "unknown"), null);
});

test("windowNote labels a preset by days and a custom range by its dates", () => {
  const preset = { start: "2026-11-11T09:00:00Z", end: "2026-11-18T09:00:00Z", days: 7, covered_days: 7,
    requested_start: "2026-11-11T09:00:00Z" };
  assert.deepEqual(windowNote(preset, false), { custom: false, days: 7, from: "2026-11-11", to: "2026-11-18",
    clipped: false, coveredDays: 7, startsOn: "2026-11-11" });
  // a past custom range (1-30 Sep): the end is midnight after the last day, so `to` is that last day
  const past = { start: "2026-09-01T00:00:00Z", end: "2026-10-01T00:00:00Z", days: 30, covered_days: 30,
    requested_start: "2026-09-01T00:00:00Z" };
  const n = windowNote(past, true);
  assert.equal(n.from, "2026-09-01");
  assert.equal(n.to, "2026-09-30");
  assert.equal(n.clipped, false);
  // a custom range ending today ends at the collection time: never a fraction of days in the label
  const today = { start: "2026-11-12T00:00:00Z", end: "2026-11-18T12:00:00Z", days: 6.5, covered_days: 6.5,
    requested_start: "2026-11-12T00:00:00Z" };
  assert.deepEqual([windowNote(today, true).from, windowNote(today, true).to], ["2026-11-12", "2026-11-18"]);
  assert.equal(windowNote(today, false).days, 7);
  assert.equal(windowNote(null, false), null);
  assert.equal(windowNote({ start: "x", end: "2026-11-18T00:00:00Z" }, false), null);
});

test("windowNote says when the range starts before the complete history", () => {
  const clipped = { start: "2026-10-20T00:00:00Z", end: "2026-11-18T09:00:00Z", days: 90, covered_days: 29.4,
    requested_start: "2026-08-20T09:00:00Z" };
  const n = windowNote(clipped, false);
  assert.equal(n.clipped, true);
  assert.equal(n.coveredDays, 29.4);
  assert.equal(n.startsOn, "2026-10-20");
  assert.equal(n.from, "2026-08-20");
  // the git activity is served unclipped: its own complete day clips it
  const git = { start: "2026-09-01T00:00:00Z", end: "2026-10-01T00:00:00Z" };
  const g = windowNote(git, true, "2026-09-21");
  assert.deepEqual([g.clipped, g.coveredDays, g.startsOn, g.days], [true, 10, "2026-09-21", 30]);
  assert.equal(windowNote(git, true, "2026-08-01").clipped, false);
  assert.equal(windowNote(git, true, "not a day").clipped, false);
});

test("rangeWhen says the preset's days or the custom days (AA-1135 follow-up: never a fixed 4 weeks)", () => {
  assert.deepEqual(rangeWhen(parseRange(getter(""))), { custom: false, days: 28 });
  assert.deepEqual(rangeWhen(parseRange(getter("range=7d"))), { custom: false, days: 7 });
  assert.deepEqual(rangeWhen(parseRange(getter("range=90d"))), { custom: false, days: 90 });
  assert.deepEqual(rangeWhen(parseRange(getter("range=custom&from=2026-11-05&to=2026-11-12"))),
    { custom: true, from: "2026-11-05", to: "2026-11-12" });
});

test("the series colours are distinct kit tokens with one orange only (CIS and TripPlanner looked alike)", () => {
  assert.equal(new Set(SERIES_VARS).size, SERIES_VARS.length);
  assert.ok(SERIES_VARS.length >= 6, "one colour per app of the summary");
  const oranges = SERIES_VARS.filter((v) => /accent|gold|amber/.test(v));
  assert.deepEqual(oranges, ["--aa-accent"]);
  for (const v of SERIES_VARS) assert.match(v, /^--aa-[a-z-]+$/);
});
