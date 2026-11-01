// frontend/lib/contentTrace.test.mjs
// AA-1006 follow-up (S263 F3): Content Trace paging, header counts and column sort. Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  pageFor, traceFilterQuery, traceQuery, traceSortValue, traceStats, TRACE_DEFAULT_PAGE_SIZE, TRACE_PAGE_SIZES,
} from "./contentTrace.ts";

const row = (over = {}) => ({
  topic: "Morning light over the rope bridge",
  tenant_name: "Lantern Trails",
  tour: { name: "Mistvale Ridge Walk" },
  channel: "blog",
  angles: [{ name: "Quiet Hours", chosen: false }, { name: "The Long Way Round", chosen: true }],
  status: "approved",
  publish_status: "n/a",
  gate_pass_count: 4,
  retry_count: 0,
  created_at: "2026-10-30T08:00:00Z",
  ...over,
});

test("the filter query keeps only the filters set, in a stable order", () => {
  assert.equal(traceFilterQuery({}), "");
  assert.equal(
    traceFilterQuery({ tenantId: "t1", channel: "blog", status: "held", published: "no", dateFrom: "2026-10-01", dateTo: "2026-10-31" }),
    "tenant_id=t1&channel=blog&status=held&published=no&date_from=2026-10-01&date_to=2026-10-31",
  );
});

test("a page query adds limit and offset (offset = page index × page size)", () => {
  assert.equal(traceQuery("", 0, 50), "limit=50&offset=0");
  assert.equal(traceQuery("status=held", 2, 50), "status=held&limit=50&offset=100");
  assert.equal(traceQuery("", -1, 0), "limit=1&offset=0");
});

test("the default page is 50 rows, and 200 (the old fixed load) is still on offer", () => {
  assert.equal(TRACE_DEFAULT_PAGE_SIZE, 50);
  assert.ok(TRACE_PAGE_SIZES.includes(TRACE_DEFAULT_PAGE_SIZE));
  assert.ok(TRACE_PAGE_SIZES.includes(200));
});

test("a page index belongs to its filters: another filter set starts at the first page", () => {
  const page = { filterQuery: "status=held", index: 3 };
  assert.equal(pageFor(page, "status=held"), 3);
  assert.equal(pageFor(page, "status=approved"), 0);
  assert.equal(pageFor({ filterQuery: "", index: 2 }, ""), 2);
});

test("header tiles read the API total and channel counts, not the rows of the page", () => {
  const answer = { data: [row(), row({ channel: "linkedin" })], total: 230, by_channel: { blog: 118, linkedin: 89, facebook: 23 } };
  assert.deepEqual(traceStats(answer), {
    total: 230,
    byChannel: [["blog", 118], ["linkedin", 89], ["facebook", 23]],
  });
});

test("an answer without counts falls back to the loaded rows", () => {
  const answer = { data: [row(), row(), row({ channel: "facebook" })] };
  assert.deepEqual(traceStats(answer), { total: 3, byChannel: [["blog", 2], ["facebook", 1]] });
  assert.deepEqual(traceStats(null), { total: 0, byChannel: [] });
});

test("equal channel counts sort by name; zero counts are left out", () => {
  const answer = { data: [], total: 4, by_channel: { linkedin: 2, blog: 2, email: 0 } };
  assert.deepEqual(traceStats(answer).byChannel, [["blog", 2], ["linkedin", 2]]);
});

test("Tenant / tour sorts by tenant, then by tour", () => {
  const values = [
    row({ tenant_name: "Lantern Trails", tour: { name: "Zephyr Coast" } }),
    row({ tenant_name: "Copper Bay", tour: { name: "Amber Steps" } }),
    row({ tenant_name: "Lantern Trails", tour: { name: "Amber Steps" } }),
    row({ tenant_name: "Lantern", tour: { name: "Zephyr Coast" } }),
  ].map((r) => traceSortValue(r, "tenant")).sort();
  assert.deepEqual(values, [
    "Copper Bay\u0001Amber Steps",
    "Lantern\u0001Zephyr Coast",
    "Lantern Trails\u0001Amber Steps",
    "Lantern Trails\u0001Zephyr Coast",
  ]);
});

test("Status sorts by status, then by publish progress (not ready, waiting, live)", () => {
  const order = ["published", "n/a", "pending_publish"].map((p) => traceSortValue(row({ publish_status: p }), "status")).sort();
  assert.deepEqual(order, ["approved\u00010", "approved\u00011", "approved\u00012"]);
});

test("Gates sorts by gates passed, then by retries", () => {
  const a = traceSortValue(row({ gate_pass_count: 3, retry_count: 5 }), "gate");
  const b = traceSortValue(row({ gate_pass_count: 4, retry_count: 0 }), "gate");
  const c = traceSortValue(row({ gate_pass_count: 4, retry_count: 2 }), "gate");
  assert.ok(a < b && b < c);
});

test("the other columns sort by their own value", () => {
  assert.equal(traceSortValue(row(), "angle"), "The Long Way Round");
  assert.equal(traceSortValue(row({ angles: [] }), "angle"), "");
  assert.equal(traceSortValue(row(), "channel", (c) => c.toUpperCase()), "BLOG");
  assert.equal(traceSortValue(row(), "created_at"), Date.parse("2026-10-30T08:00:00Z"));
  assert.equal(traceSortValue(row({ topic: null }), "topic"), "");
});
