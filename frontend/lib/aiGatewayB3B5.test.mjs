// frontend/lib/aiGatewayB3B5.test.mjs
// AA-1025 slices B3 / B5 (S263 gateway plan): the provider circuit line of the Providers tab, the Credit view (the
// credit ledger of the vendor API connections) and its form helpers. Pure helpers of aiGateway.ts plus text guards.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  PROVIDER_VIEWS, aiGatewayHref, circuitLine, creditEntryBody, parseView, thresholdValue,
} from "./aiGateway.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const CLOSED = { state: "closed", error_class: null, last_error: null, consecutive_failures: 0, open_streak: 0,
  opened_at: null, open_until: null, last_ok_at: "2026-11-02T01:00:00Z", last_error_at: null };

test("circuitLine: nothing for no row or a healthy closed circuit", () => {
  assert.equal(circuitLine(null), null);
  assert.equal(circuitLine(undefined), null);
  assert.equal(circuitLine(CLOSED), null);
});

test("circuitLine: failures in a row while still closed", () => {
  assert.deepEqual(circuitLine({ ...CLOSED, consecutive_failures: 3, error_class: "other" }),
    { kind: "failing", until: null, cls: "other", failures: 3 });
});

test("circuitLine: open with its end, half-open without", () => {
  const open = { ...CLOSED, state: "open", error_class: "other", consecutive_failures: 5,
    open_until: "2026-11-02T02:00:00Z" };
  assert.deepEqual(circuitLine(open), { kind: "open", until: "2026-11-02T02:00:00Z", cls: "other", failures: 5 });
  assert.equal(circuitLine({ ...open, state: "half_open" }).kind, "half_open");
});

test("the Credit view is a Providers view with its own link", () => {
  assert.ok(PROVIDER_VIEWS.includes("credit"));
  assert.equal(parseView("credit"), "credit");
  assert.equal(aiGatewayHref("providers", "credit"), "/admin/ai-gateway?tab=providers&view=credit");
});

test("creditEntryBody: a top-up must be above 0, a balance read may be 0", () => {
  assert.equal(creditEntryBody("topup", "0", "", ""), "amount");
  assert.equal(creditEntryBody("topup", "", "", ""), "amount");
  assert.equal(creditEntryBody("topup", "-1", "", ""), "amount");
  assert.equal(creditEntryBody("topup", "abc", "", ""), "amount");
  assert.equal(creditEntryBody("opening_balance", "100001", "", ""), "amount");
  assert.deepEqual(creditEntryBody("opening_balance", "0", "", ""), { kind: "opening_balance", amount_usd: 0 });
  assert.deepEqual(creditEntryBody("topup", "12.345", "", "  "), { kind: "topup", amount_usd: 12.35 });
});

test("creditEntryBody: the date is a UTC day, the note is trimmed", () => {
  assert.equal(creditEntryBody("topup", "5", "02/11/2026", ""), "date");
  assert.deepEqual(creditEntryBody("topup", "5", "2026-11-02", " read on the console "),
    { kind: "topup", amount_usd: 5, effective_at: "2026-11-02T00:00:00Z", note: "read on the console" });
});

test("thresholdValue: empty = no alert, a bad value = undefined", () => {
  assert.equal(thresholdValue(""), null);
  assert.equal(thresholdValue("  "), null);
  assert.equal(thresholdValue("5"), 5);
  assert.equal(thresholdValue("5.555"), 5.56);
  assert.equal(thresholdValue("-1"), undefined);
  assert.equal(thresholdValue("x"), undefined);
});

test("the Credit panel uses the kit and its own messages, both languages have the same keys", () => {
  const panel = read("../app/admin/ai-gateway/ProviderCreditPanel.tsx");
  assert.match(panel, /from "\.\.\/\.\.\/_kit"/);
  assert.doesNotMatch(panel, /"[^"\n]*AA-\d+[^"\n]*"/);    // no issue id in a UI string
  const msgs = read("../app/admin/ai-gateway/credit.messages.ts");
  const [en, vi] = msgs.split("\n  {\n").slice(1);
  const keys = (block) => [...block.matchAll(/^ {4}([a-zA-Z]+):/gm)].map((m) => m[1]).sort();
  assert.deepEqual(keys(en), keys(vi));
  assert.doesNotMatch(msgs, /"[^"\n]*AA-\d+[^"\n]*"/);
});

test("the Providers tab shows the circuit line and the Credit view", () => {
  const tab = read("../app/admin/ai-gateway/ProvidersTab.tsx");
  assert.match(tab, /<CircuitLine row=\{row\} \/>/);
  assert.match(tab, /view === "credit" && <ProviderCreditPanel \/>/);
  const m = read("../app/admin/ai-gateway/messages.ts");
  for (const key of ["viewCredit", "circuitOpen", "circuitHalfOpen", "circuitFailing", "noThreshold"]) {
    assert.equal(m.split(`${key}:`).length - 1, 2, key);      // English and Vietnamese
  }
});

test("the Credit view hides its write controls from a session the API would refuse", () => {
  const panel = read("../app/admin/ai-gateway/ProviderCreditPanel.tsx");
  assert.match(panel, /canEdit && !notDeployed && connections\.length > 0 && <CheckNowButton \/>/);
  assert.match(panel, /\{canEdit && \(\s*<>\s*<span[^\n]*addEntry/);
  assert.match(panel, /if \(canEdit\) \{\s*cols\.push\(\{ id: "actions"/);
  assert.match(panel, /canThreshold\s*\? <ThresholdForm c=\{c\} \/>/);
  assert.match(panel, /data-testid="ai-gateway-credit-read-only"/);
});
