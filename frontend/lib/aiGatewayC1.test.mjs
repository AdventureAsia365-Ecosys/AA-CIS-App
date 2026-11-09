// frontend/lib/aiGatewayC1.test.mjs
// AA-1025 slice C1 (S263 gateway plan): the Providers tab's errors / latency column, the provider switch of Jev /
// DataForSEO / Geoapify, the CRM, Guardrails and Mapbox rows. Pure helpers of aiGateway.ts plus text guards that keep
// the page's words in step with the API (every status reason the backend sends has its sentence).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  STATS_WINDOWS, errorRateTone, groupRows, latencyText, needsLook, providerSwitch, severity, statusTone,
  statusWordKey,
} from "./aiGateway.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

test("latencyText: milliseconds below a second, seconds above", () => {
  assert.equal(latencyText(812.4), "812 ms");
  assert.equal(latencyText(0), "0 ms");
  assert.equal(latencyText(2401), "2.4 s");
  assert.equal(latencyText(null), "—");
  assert.equal(latencyText(undefined), "—");
});

test("errorRateTone: a few failures stay quiet, most failing is danger", () => {
  assert.equal(errorRateTone(null), "neutral");
  assert.equal(errorRateTone(0), "neutral");
  assert.equal(errorRateTone(4.9), "neutral");
  assert.equal(errorRateTone(12.5), "warning");
  assert.equal(errorRateTone(50), "danger");
});

test("providerSwitch: only a switchable row with a key, and it flips the switch", () => {
  assert.deepEqual(providerSwitch({ switchable: true, connection_key: "typesafe", enabled: true }),
    { path: "/api/admin/llm-connections/typesafe", body: { enabled: false } });
  assert.deepEqual(providerSwitch({ switchable: true, connection_key: "geoapify", enabled: false }).body,
    { enabled: true });
  assert.equal(providerSwitch({ switchable: false, connection_key: "bedrock-guardrails", enabled: true }), null);
  assert.equal(providerSwitch({ connection_key: "typesafe", enabled: true }), null);      // an older API
  assert.equal(providerSwitch({ switchable: true, connection_key: null, enabled: true }), null);
  assert.equal(providerSwitch({ switchable: true, connection_key: "a b", enabled: true }).path,
    "/api/admin/llm-connections/a%20b");
});

test("Mapbox's untracked status is never a problem and sorts after OK", () => {
  assert.equal(needsLook([{ status: "untracked" }, { status: "ok" }, { status: "degraded" }]), 1);
  assert.ok(severity("untracked") > severity("ok") && severity("untracked") < severity("disabled"));
  assert.equal(statusTone("untracked"), "info");
});

test("the CRM rows say 'No recent calls' like an LLM connection; data rows keep Unknown", () => {
  assert.equal(statusWordKey({ group: "guard", status: "unknown", status_reason: null }), "no_recent_calls");
  assert.equal(statusWordKey({ group: "guard", status: "unknown", status_reason: "crm_sign_in_needed" }), "unknown");
  assert.equal(statusWordKey({ group: "data", status: "unknown", status_reason: null }), "unknown");
});

test("groupRows puts the guardrail row in its own group", () => {
  const g = groupRows([{ key: "crm:llm", group: "llm" }, { key: "guardrails", group: "guard" },
    { key: "mapbox", group: "data" }]);
  assert.deepEqual(g.guard.map((r) => r.key), ["guardrails"]);
  assert.deepEqual(g.llm.map((r) => r.key), ["crm:llm"]);
  assert.deepEqual(g.data.map((r) => r.key), ["mapbox"]);
});

test("the stats windows are the API's", () => {
  assert.deepEqual([...STATS_WINDOWS], ["24h", "7d"]);
  const api = read("../../api/services/ai_gateway.py");
  assert.ok(api.includes('STATS_WINDOWS = (("24h", "$1"), ("7d", "$2"))'));
});

test("every status reason the providers API sends has a sentence, in English and Vietnamese", () => {
  const api = read("../../api/services/ai_gateway.py");
  for (const r of ["crm_sign_in_needed", "crm_refused", "crm_unreachable", "free_tier_not_logged",
    "switched_off_in_registry"]) {
    assert.ok(api.includes(`"${r}"`), `the API sends ${r}`);
  }
  const messages = read("../app/admin/ai-gateway/messages.ts");
  const reasonBlock = messages.slice(messages.indexOf("export const REASON"), messages.indexOf("export const SOURCE"));
  for (const r of ["crm_sign_in_needed", "crm_refused", "crm_unreachable", "free_tier_not_logged",
    "switched_off_in_registry"]) {
    assert.equal(reasonBlock.split(`${r}:`).length - 1, 2, `${r}: one English and one Vietnamese sentence`);
  }
  assert.match(messages, /untracked: "Not logged"/);
  assert.match(messages, /untracked: "Không ghi sổ"/);
});

test("the Providers tab offers the switch only through providerSwitch and confirms first", () => {
  const src = read("../app/admin/ai-gateway/ProvidersTab.tsx");
  assert.ok(src.includes("providerSwitch(row)"), "the button shows only for a switchable row");
  assert.ok(src.includes('testId="ai-gateway-switch-confirm"'), "a confirm dialog before the PATCH");
  assert.ok(src.includes("isReadOnlyRefusal(e)"), "a read-only session gets words, not the code");
  assert.ok(src.includes('id="ai-gateway-guard"'), "the Guards section");
  // never an issue id in the page's words
  for (const f of ["ProvidersTab.tsx", "messages.ts", "guide.ts", "guide.vi.ts"]) {
    const text = read(`../app/admin/ai-gateway/${f}`).split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
    assert.doesNotMatch(text, /"[^"\n]*\bAA-\d{2,5}\b[^"\n]*"/, f);
  }
});
