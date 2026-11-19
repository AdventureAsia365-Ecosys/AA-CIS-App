// frontend/lib/aa1127CeFreshness.test.mjs
// AA-1127 (C2 "spend truth", tech lead decision S285 E2): the Spend page's "AWS figures fresh / stale" badge. The view
// helper (lib/ceFreshness.ts) and the places that show it (the forecast and the By app tab). Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { ceFreshnessView, hoursText } from "./ceFreshness.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

test("no block (an older API) or a failed read shows nothing", () => {
  assert.equal(ceFreshnessView(undefined), null);
  assert.equal(ceFreshnessView(null), null);
  assert.equal(ceFreshnessView({ stale: null, error: "freshness unavailable" }), null);
});

test("fresh figures: success tone, their age, the stale window and the daily hour", () => {
  const v = ceFreshnessView({ stale: false, age_hours: 7.5, stale_after_hours: 36,
                              daily_refresh: { enabled: true, hour_utc: 1 } });
  assert.deepEqual(v, { stale: false, tone: "success", parts: [
    { key: "age", arg: "7.5" }, { key: "staleAfter", arg: "36" }, { key: "dailyAt", arg: "01" }] });
});

test("stale figures: warning tone; nothing stored says so; a daily check that is off says so", () => {
  const v = ceFreshnessView({ stale: true, age_hours: null, stale_after_hours: 36,
                              daily_refresh: { enabled: false, hour_utc: 1 } });
  assert.equal(v.tone, "warning");
  assert.deepEqual(v.parts, [{ key: "noData" }, { key: "staleAfter", arg: "36" }, { key: "dailyOff" }]);
  assert.equal(hoursText(48), "48");
});

test("the forecast and the By app tab both show the badge from the API block; every word has a Vietnamese twin", () => {
  assert.match(read("../app/admin/spend/ForecastPanel.tsx"), /<CeFreshnessBadge f=\{data\.ce_freshness\} \/>/);
  assert.match(read("../app/admin/spend/CostByApp.tsx"), /<CeFreshnessBadge f=\{data\.ce_freshness\} \/>/);
  const msgs = read("../app/admin/spend/ceFreshness.messages.ts");
  for (const key of ["fresh", "stale", "age", "noData", "staleAfter", "dailyAt", "dailyOff", "title"]) {
    assert.equal(msgs.split(new RegExp(`\\n    ${key}:`)).length - 1, 2, `${key} in English and Vietnamese`);
  }
});
