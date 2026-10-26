// frontend/lib/dataCoverage.test.mjs
// AA-992: unit tests for the Data coverage page helpers in dataCoverage.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CSV_COLUMNS,
  csvRows,
  formatAge,
  formatCount,
  formatExpected,
  formatOf,
  formatPct,
  formatUtcTime,
  groupGaps,
  groupUnavailableReason,
  statusBadge,
} from "./dataCoverage.ts";

const row = (over = {}) => ({
  id: "atoms.tours", label: "Live tours with atoms", unit: "tours", kind: "must", polarity: "coverage",
  value: 883, denominator: 892, denominator_label: "live tours", pct: 99.0, missing: 9, expected_gap: 9,
  expected_note: "tours with markers only", limit: null, status: "ok", reason: null, issue: null,
  drill: "/admin/atom-curation?section=atomize", source: "acp_contract.v_active_tour_atoms", timing_ms: 12,
  as_of: "2026-10-26T04:00:00+00:00", ...over,
});

test("status badge follows status + kind only", () => {
  assert.deepEqual(statusBadge({ status: "ok", kind: "must" }), { tone: "success", label: "OK" });
  assert.deepEqual(statusBadge({ status: "gap", kind: "must" }), { tone: "danger", label: "Gap" });
  assert.deepEqual(statusBadge({ status: "gap", kind: "backlog" }), { tone: "warning", label: "Backlog" });
  assert.deepEqual(statusBadge({ status: "info", kind: "info" }), { tone: "neutral", label: "Info" });
  assert.deepEqual(statusBadge({ status: "na", kind: "must" }), { tone: "neutral", label: "n/a" });
});

test("numbers, percentages and the Of column", () => {
  assert.equal(formatCount(25878), "25,878");
  assert.equal(formatCount(null), "—");
  assert.equal(formatPct(99), "99.0%");
  assert.equal(formatPct(null), "—");
  assert.equal(formatOf(row()), "892 live tours");
  assert.equal(formatOf(row({ polarity: "threshold", limit: 30, denominator: null })), "≤ 30");
  assert.equal(formatOf(row({ denominator: null })), "—");
  assert.equal(formatExpected(row()), "9 · tours with markers only");
  assert.equal(formatExpected(row({ expected_gap: null })), "tours with markers only");
  assert.equal(formatExpected(row({ expected_gap: null, expected_note: null })), "—");
});

test("as-of time and cache age", () => {
  assert.equal(formatUtcTime("2026-10-26T10:42:13+00:00"), "10:42:13 UTC");
  assert.equal(formatUtcTime(null), "—");
  const now = Date.parse("2026-10-26T10:42:36Z");
  assert.equal(formatAge("2026-10-26T10:42:13Z", now), "23 s");
  assert.equal(formatAge("2026-10-26T10:30:00Z", now), "13 min");
  assert.equal(formatAge(null, now), "");
});

test("group gaps and a fully unavailable group", () => {
  const g = { id: "crm", label: "Partner CRM", note: null, rows: [row({ status: "gap" }), row(), row({ status: "gap", kind: "backlog" })] };
  assert.equal(groupGaps(g), 2);
  assert.equal(groupUnavailableReason(g), null);
  const na = { ...g, rows: [row({ status: "na", reason: "unavailable: cognito_session_required" }),
                            row({ status: "na", reason: "unavailable: cognito_session_required" })] };
  assert.equal(groupUnavailableReason(na), "unavailable: cognito_session_required");
});

test("CSV holds every row of every stage, in page order", () => {
  const data = {
    as_of: "x", generated_ms: 1, cached: false, stale: false, live_tours: 892,
    summary: { rows: 3, gaps: 0, backlog: 0, na: 0 }, sources: {},
    groups: [{ id: "a", label: "Stage A", note: null, rows: [row(), row({ id: "b.c" })] },
             { id: "b", label: "Stage B", note: null, rows: [row({ id: "d.e", polarity: "threshold", limit: 30 })] }],
  };
  const rows = csvRows(data);
  assert.deepEqual(rows.map((r) => [r.stage, r.row.id]), [["Stage A", "atoms.tours"], ["Stage A", "b.c"], ["Stage B", "d.e"]]);
  const of = CSV_COLUMNS.find((c) => c.header === "Of");
  assert.equal(of.value(rows[2]), 30);
  assert.equal(of.value(rows[0]), 892);
  assert.ok(CSV_COLUMNS.some((c) => c.header === "Expected gap") && CSV_COLUMNS.some((c) => c.header === "Source"));
});
