// frontend/lib/usageExplorer.test.mjs
// AA-1045: unit tests for the usage explorer helpers in usageExplorer.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  NONE, QUERY_KEYS, csvColumns, csvName, drillHref, filterValues, fmtMetric, fmtMs, fmtPct, fmtUsd, groupOf,
  optionsPath, queryPath, readQuery, toggleValue, viewHref,
} from "./usageExplorer.ts";

const params = (url) => new URL(url, "https://x.test").searchParams;

test("1. the address query: only set keys, group at most two, filters toggle", () => {
  const addr = new URLSearchParams("range=custom&from=2026-10-01&to=2026-10-31&group=provider,stage,model&f_app=cis"
    + "&tab=zz&f_stage=");
  const q = readQuery((k) => addr.get(k) ?? "");
  assert.deepEqual(q, { range: "custom", from: "2026-10-01", to: "2026-10-31", group: "provider,stage,model",
    f_app: "cis" });
  assert.deepEqual(groupOf(q), ["provider", "stage"]);
  assert.deepEqual(groupOf({}), ["provider"]);
  assert.deepEqual(filterValues(q, "app"), ["cis"]);
  assert.equal(toggleValue(q, "app", "tripplanner"), "cis,tripplanner");
  assert.equal(toggleValue(q, "app", "cis"), null);
  assert.ok(QUERY_KEYS.includes("f_error_class") && QUERY_KEYS.includes("per_unit"));
});

test("2. API paths and the saved view link", () => {
  const q = { range: "7d", group: "provider", f_tenant: "6f9619ff-8b86-d011-b42d-00c04fc964ff" };
  assert.equal(params(queryPath(q)).get("f_tenant"), q.f_tenant);
  assert.ok(queryPath(q).startsWith("/api/admin/usage-explorer/query?"));
  assert.equal(optionsPath(q), "/api/admin/usage-explorer/options?range=7d");
  assert.equal(viewHref({ range: "30d" }), "/admin/usage?range=30d");
  assert.equal(viewHref({}), "/admin/usage");
});

test("3. drill-down: the cell's range and dimensions, single filters carried, the rest listed as lost", () => {
  const range = { from: "2026-10-28T00:00:00+07:00", to: "2026-11-04T00:00:00+07:00" };
  const link = drillHref(range, { provider: "bedrock", stage: "t9_write", tenant: "" },
    { app: ["cis"], model: ["m-one", "m-two"], family: ["claude"] });
  const p = params(link.href);
  assert.ok(link.href.startsWith("/admin/ai-gateway?"));
  assert.equal(p.get("tab"), "calls");
  assert.equal(p.get("c_since"), range.from);
  assert.equal(p.get("c_until"), range.to);
  assert.equal(p.get("c_provider"), "bedrock");
  assert.equal(p.get("c_stage"), "t9_write");
  assert.equal(p.get("c_tenant"), NONE);
  assert.equal(p.get("c_app"), "cis");
  assert.equal(p.get("c_model"), null);
  assert.deepEqual(link.lost.sort(), ["family", "model"]);
  // a time bucket narrows the range; a provider the Calls list does not know and an empty app are lost
  const b = drillHref(range, { time: "2026-11-02", provider: "unknown", app: "" }, {},
    { start: "2026-11-02T00:00:00+07:00", end: "2026-11-03T00:00:00+07:00", label: "2026-11-02" });
  assert.equal(params(b.href).get("c_since"), "2026-11-02T00:00:00+07:00");
  assert.deepEqual(b.lost.sort(), ["app", "provider"]);
  assert.equal(params(drillHref(range, { source: "crm" }, {}).href).get("c_source"), "crm");
});

test("3b. drill-down: a 7d range at day granularity clips its first and last day buckets to the range", () => {
  // the range starts and ends at 15:00 ICT; the day buckets start at midnight
  const range = { from: "2026-10-28T15:00:00+07:00", to: "2026-11-04T15:00:00+07:00" };
  const first = drillHref(range, { time: "2026-10-28" }, {},
    { start: "2026-10-28T00:00:00+07:00", end: "2026-10-29T00:00:00+07:00", label: "2026-10-28" });
  assert.equal(params(first.href).get("c_since"), range.from);
  assert.equal(params(first.href).get("c_until"), "2026-10-29T00:00:00+07:00");
  const last = drillHref(range, { time: "2026-11-04" }, {},
    { start: "2026-11-04T00:00:00+07:00", end: "2026-11-05T00:00:00+07:00", label: "2026-11-04" });
  assert.equal(params(last.href).get("c_since"), "2026-11-04T00:00:00+07:00");
  assert.equal(params(last.href).get("c_until"), range.to);
  // instants compare as times, not text: a UTC bucket end before the ICT range end is kept
  const utc = drillHref(range, {}, {},
    { start: "2026-11-01T17:00:00+00:00", end: "2026-11-02T17:00:00+00:00", label: "2026-11-02" });
  assert.equal(params(utc.href).get("c_since"), "2026-11-01T17:00:00+00:00");
  assert.equal(params(utc.href).get("c_until"), "2026-11-02T17:00:00+00:00");
});

test("4. CSV columns and name, number formats", () => {
  const row = { key: "k", dims: { tenant: "t-1", provider: "openai" }, requests: 3, cost_usd: 0.5,
    p95_latency_ms: null };
  const cols = csvColumns(["tenant", "provider"], { "t-1": "Ember Coast" });
  assert.deepEqual(cols.slice(0, 3).map((c) => c.header), ["tenant", "tenant_name", "provider"]);
  assert.equal(cols[1].value(row), "Ember Coast");
  assert.equal(cols.find((c) => c.header === "cost_usd").value(row), 0.5);
  assert.equal(cols.find((c) => c.header === "p95_latency_ms").value(row), "");
  assert.equal(csvName({ range: "custom", from: "2026-10-01", to: "2026-10-31", group: "stage" }),
    "usage-2026-10-01_2026-10-31-stage.csv");
  assert.equal(csvName({}), "usage-7d-provider.csv");
  assert.equal(fmtUsd(1234.5), "$1,234.50");
  assert.equal(fmtUsd(0.01234), "$0.0123");
  assert.equal(fmtPct(0.1234), "12.3%");
  assert.equal(fmtMs(850), "850 ms");
  assert.equal(fmtMs(12500), "12.5 s");
  assert.equal(fmtMetric("error_rate", null), "—");
  assert.equal(fmtMetric("requests", 12345), "12,345");
});

test("5. ICT times and the invoice months", async () => {
  const { fmtIct, lastMonths } = await import("./usageExplorer.ts");
  assert.equal(fmtIct("2026-11-04T02:00:00Z"), "4 Nov 2026, 09:00 ICT");
  assert.equal(fmtIct("2026-11-04T20:00:00+00:00", false), "5 Nov 2026");
  assert.equal(fmtIct(null), "—");
  assert.deepEqual(lastMonths(new Date("2026-02-10T00:00:00Z"), 3), ["2026-02", "2026-01", "2025-12"]);
});
