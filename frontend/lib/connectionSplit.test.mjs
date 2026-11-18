// frontend/lib/connectionSplit.test.mjs
// AA-1113 (S284) — AI providers › Budgets, "Connection budget split": the badge tone of each state (over-allocated is
// the only red one) and the order the table shows rows in; the section's copy in EN + VI names no issue id.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { missingShareApps, splitOrder, splitTone } from "./connectionSplit.ts";

const row = (connection_key, state) => ({ connection_key, state });

test("over-allocated is red, a missing or unknown share amber, ok green, nothing to check neutral", () => {
  assert.equal(splitTone("over_allocated"), "danger");
  assert.equal(splitTone("unbounded"), "warning");
  assert.equal(splitTone("crm_unknown"), "warning");
  assert.equal(splitTone("ok"), "success");
  assert.equal(splitTone("no_provider_budget"), "neutral");
});

test("rows needing a look come first, then by connection key", () => {
  const out = splitOrder([row("zz-d", "no_provider_budget"), row("zz-c", "ok"), row("zz-b", "unbounded"),
    row("zz-a", "over_allocated"), row("zz-0", "ok")]);
  assert.deepEqual(out.map((r) => r.connection_key), ["zz-a", "zz-b", "zz-0", "zz-c", "zz-d"]);
});

test("an alert-only share is named apart from a missing one", () => {
  assert.deepEqual(missingShareApps({ uncapped_apps: ["cis", "crm"], alert_only_apps: ["cis"] }), ["crm"]);
  assert.deepEqual(missingShareApps({ uncapped_apps: ["cis"], alert_only_apps: [] }), ["cis"]);
  assert.deepEqual(missingShareApps({ uncapped_apps: ["cis"], alert_only_apps: ["cis"] }), []);
});

test("the section's copy has English and Vietnamese and names no issue id", () => {
  const src = readFileSync(fileURLToPath(new URL("../app/admin/ai-gateway/ConnectionSplitSection.tsx", import.meta.url)),
    "utf8");
  assert.ok(src.includes("defineMessages("));
  assert.ok(!/AA-\d+/.test(src.split("defineMessages(")[1] ?? ""), "UI copy must not show an issue id");
});
