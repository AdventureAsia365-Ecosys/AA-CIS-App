// frontend/lib/deliveryActivity.test.mjs
// AA-1125: unit tests for the Merge queue and Git map helpers in deliveryActivity.ts (invented repos and paths).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  attemptLabel, calendarWeeks, fmtLines, heatLevel, kindRows, kindShares, outcomeTone, pickDir, queueN, queueValue,
  weeklyCommits, windowKeys, worstQueueStatus,
} from "./deliveryActivity.ts";

const queue = {
  wait: { p50_h: 0.2, p95_h: 1.5, n: 9 },
  ejection_rate: { value: 0.125, n: 8, ejected: 1 },
  flaky: { value: null, n: 0, flaky: 0, rerun_green: 0, reentry_green: 0 },
};

test("queue metric values and sample sizes", () => {
  assert.equal(queueValue(queue, "queue_wait"), 1.5);
  assert.equal(queueValue(queue, "ejection_rate"), 0.125);
  assert.equal(queueValue(queue, "flaky_rate"), null);
  assert.deepEqual(["queue_wait", "ejection_rate", "flaky_rate"].map((m) => queueN(queue, m)), [9, 8, 0]);
  assert.equal(queueValue({}, "queue_wait"), null);
  assert.equal(queueN({}, "flaky_rate"), 0);
});

test("worst queue status and outcome tones", () => {
  assert.equal(worstQueueStatus({ queue_wait: "green", ejection_rate: "amber", flaky_rate: "few" }), "amber");
  assert.equal(worstQueueStatus({ queue_wait: "red" }), "red");
  assert.equal(worstQueueStatus(undefined), "none");
  assert.equal(outcomeTone("merged"), "success");
  assert.equal(outcomeTone("ejected"), "danger");
  assert.equal(outcomeTone("queued"), "info");
  assert.equal(outcomeTone("removed"), "neutral");
});

test("heat levels scale to the busiest day", () => {
  assert.equal(heatLevel(0, 10), 0);
  assert.equal(heatLevel(1, 10), 1);
  assert.equal(heatLevel(5, 10), 2);
  assert.equal(heatLevel(10, 10), 4);
  assert.equal(heatLevel(3, 0), 0);
});

test("calendar weeks start on Monday and keep every day once", () => {
  // 2026-03-04 is a Wednesday; 10 days to Friday 2026-03-13
  const daily = Array.from({ length: 10 }, (_, i) => ({ date: `2026-03-${String(4 + i).padStart(2, "0")}`, n: i }));
  const weeks = calendarWeeks(daily);
  assert.equal(weeks.length, 2);
  assert.ok(weeks.every((w) => w.length === 7));
  assert.equal(weeks[0][0], null);                       // Monday 2 March is before the series
  assert.equal(weeks[0][2].date, "2026-03-04");
  assert.equal(weeks[1][0].date, "2026-03-09");
  assert.equal(weeks[1][6], null);                       // Sunday 15 March is after it
  assert.equal(weeks.flat().filter(Boolean).length, 10);
  assert.equal(weeks[1][4].level, 4);                     // the busiest day
  assert.deepEqual(calendarWeeks([]), []);
});

test("weekly commits group by the Monday of each week", () => {
  const daily = [{ date: "2026-03-08", n: 2 }, { date: "2026-03-09", n: 1 }, { date: "2026-03-15", n: 4 },
    { date: "2026-03-16", n: 3 }];
  assert.deepEqual(weeklyCommits(daily), [{ week: "2026-03-02", n: 2 }, { week: "2026-03-09", n: 5 },
    { week: "2026-03-16", n: 3 }]);
});

test("author kind shares and chart rows", () => {
  assert.deepEqual(kindShares({ human: 1, agent: 3, bot: 0 }), [
    { kind: "agent", n: 3, share: 0.75 }, { kind: "human", n: 1, share: 0.25 }]);
  assert.deepEqual(kindShares(undefined), []);
  const apps = [{ name: "Lantern", git: { windows: { d30: { by_kind: { agent: 2, dependabot: 1 } } } } },
    { name: "Harbour", git: { windows: {} } }];
  const rows = kindRows(apps, "d30");
  assert.equal(rows[0].agent, 2);
  assert.equal(rows[0].dependabot, 1);
  assert.equal(rows[0].human, 0);
  assert.equal(rows[1].agent, 0);
});

test("directory drill-down, window keys, entry and line labels", () => {
  const win = { dirs: [{ path: "app", children: [{ path: "app/lanterns" }] }, { path: "docs" }] };
  assert.equal(pickDir(win, "app").children[0].path, "app/lanterns");
  assert.equal(pickDir(win, "nowhere"), null);
  assert.equal(pickDir(undefined, "app"), null);
  assert.deepEqual(windowKeys({ windows: { d90: {}, d30: {} } }), ["d30", "d90"]);
  assert.deepEqual(windowKeys(undefined), []);
  assert.equal(attemptLabel({ attempt: 2, attempts: 3 }), "2/3");
  assert.equal(attemptLabel({ attempt: 1, attempts: 1 }), "1");
  assert.equal(fmtLines(12, 3), "+12 / −3");
  assert.equal(fmtLines(null, 3), "—");
});
