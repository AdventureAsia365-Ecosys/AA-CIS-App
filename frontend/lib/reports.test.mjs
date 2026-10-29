// frontend/lib/reports.test.mjs
// AA-1003: unit tests for the Reports page helpers in reports.ts (paths, the client-side filter over the whole
// registry, the run opened first, the period text). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EMPTY_FILTERS, defaultRun, families, filterReports, generatePath, periodText, runPath, runsPath,
} from "./reports.ts";

const ROW = {
  id: "EX-04", family: "executive", name_en: "Executive KPI scorecard", name_vi: "Bảng chỉ số", audience: "a",
  owner_role: "tech lead", cadence: "daily", schedule: "daily", channel: "c", automation: "auto", status: "partial",
  priority: "P0", template: null, generator: "cis", runnable: true, kpis: [], last_run: null, covered_to: null,
  failed_7d: 0, due: { state: "due", expected_period_end: "2026-10-28" },
};
const OTHER = { ...ROW, id: "FI-01", family: "finops", name_en: "Monthly cost review", priority: "P1", runnable: false,
  generator: null };

test("paths are encoded BFF paths", () => {
  assert.equal(runsPath("EX-04", 2), "/api/admin/reports/EX-04/runs?page=2&page_size=10");
  assert.equal(runPath("a/b"), "/api/admin/reports/runs/a%2Fb");
  assert.equal(generatePath("EX-06"), "/api/admin/reports/EX-06/run");
});

test("filters apply to the whole registry", () => {
  const rows = [ROW, OTHER];
  assert.deepEqual(filterReports(rows, EMPTY_FILTERS).map((r) => r.id), ["EX-04", "FI-01"]);
  assert.deepEqual(filterReports(rows, { ...EMPTY_FILTERS, generatedOnly: true }).map((r) => r.id), ["EX-04"]);
  assert.deepEqual(filterReports(rows, { ...EMPTY_FILTERS, family: "finops" }).map((r) => r.id), ["FI-01"]);
  assert.deepEqual(filterReports(rows, { ...EMPTY_FILTERS, priority: "P0" }).map((r) => r.id), ["EX-04"]);
  assert.deepEqual(filterReports(rows, { ...EMPTY_FILTERS, q: " cost " }).map((r) => r.id), ["FI-01"]);
  assert.deepEqual(families(rows), ["executive", "finops"]);
});

test("the drawer opens the newest succeeded run first", () => {
  const runs = [{ id: "3", status: "failed" }, { id: "2", status: "succeeded" }, { id: "1", status: "succeeded" }];
  assert.equal(defaultRun(runs).id, "2");
  assert.equal(defaultRun([{ id: "9", status: "queued" }]).id, "9");
  assert.equal(defaultRun([]), null);
});

test("period text", () => {
  assert.equal(periodText("2026-10-28", "2026-10-28"), "2026-10-28");
  assert.equal(periodText("2026-10-01", "2026-10-28"), "2026-10-01 – 2026-10-28");
  assert.equal(periodText(null, null), "");
});
