// frontend/lib/aiGateway.test.mjs
// AA-804: unit tests for the AI providers page helpers in aiGateway.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  groupRows, needsLook, parseTrace, pct, severity, statusTone, traceHref, tracePath, usd,
} from "./aiGateway.ts";

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
