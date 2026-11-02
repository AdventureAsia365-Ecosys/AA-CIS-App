// frontend/lib/aiGatewayH1a.test.mjs
// AA-1025 H1a: the App profiles card of AI providers › Providers (backend GET / PATCH /admin/ai-gateway/app-profiles,
// shared/llm_client/app_profile.py). Pure helper appProfileBody (only what changed; a reason to switch AI off; the
// table's limits) plus text guards: the card is on the Providers overview, its Edit follows usePageAccess, it uses the
// kit only, and its messages have the same keys in English and Vietnamese.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ACTION_CAPABILITY, APP_PROFILES_API, DEGRADE_MODES, PAGE_ACTIONS, PROFILE_APPS, appProfileBody, appProfileDraft,
  appProfilePath,
} from "./aiGateway.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const CIS = {
  app: "cis", ai_enabled: true, bulk_paused: false, default_daily_budget_usd: null, degrade_mode: "queue_retry",
  disabled_reason: null, updated_at: null, updated_by: null, central_cap: null, enforced_here: true,
};

test("appProfileBody: nothing changed is refused", () => {
  assert.equal(appProfileBody(CIS, appProfileDraft(CIS)), "unchanged");
});

test("appProfileBody: switching AI off needs a reason of 3 to 500 characters", () => {
  const d = appProfileDraft(CIS);
  assert.equal(appProfileBody(CIS, { ...d, ai_enabled: false }), "reason");
  assert.equal(appProfileBody(CIS, { ...d, ai_enabled: false, reason: " ab " }), "reason");
  assert.equal(appProfileBody(CIS, { ...d, ai_enabled: false, reason: "x".repeat(501) }), "reason");
  assert.deepEqual(appProfileBody(CIS, { ...d, ai_enabled: false, reason: "  provider drill " }),
    { ai_enabled: false, reason: "provider drill" });
});

test("appProfileBody: switching back on sends only the switch", () => {
  const off = { ...CIS, ai_enabled: false, disabled_reason: "provider drill" };
  assert.deepEqual(appProfileBody(off, { ...appProfileDraft(off), ai_enabled: true }), { ai_enabled: true });
  // a new reason for an app already off goes with the switch (the API takes no reason alone)
  assert.deepEqual(appProfileBody(off, { ...appProfileDraft(off), reason: "longer drill" }),
    { reason: "longer drill", ai_enabled: false });
});

test("appProfileBody: the budget is empty (cleared) or a number from 0 to 100000", () => {
  const d = appProfileDraft(CIS);
  for (const bad of ["-1", "abc", "100000.01", "1e9"]) assert.equal(appProfileBody(CIS, { ...d, budget: bad }), "budget");
  assert.deepEqual(appProfileBody(CIS, { ...d, budget: " 2.5 " }), { default_daily_budget_usd: 2.5 });
  const set = { ...CIS, default_daily_budget_usd: 2.5 };
  assert.deepEqual(appProfileBody(set, { ...appProfileDraft(set), budget: "" }), { default_daily_budget_usd: null });
  assert.equal(appProfileDraft(set).budget, "2.5");
});

test("appProfileBody: bulk pause and degrade mode", () => {
  const d = appProfileDraft(CIS);
  assert.deepEqual(appProfileBody(CIS, { ...d, bulk_paused: true, degrade_mode: "last_saved_text" }),
    { bulk_paused: true, degrade_mode: "last_saved_text" });
});

test("the apps and degrade modes match the backend (migration 345)", () => {
  const py = read("../../shared/llm_client/app_profile.py");
  assert.match(py, /APPS = \("cis", "tripplanner", "crm", "booking"\)/);
  assert.deepEqual([...PROFILE_APPS], ["cis", "tripplanner", "crm", "booking"]);
  for (const m of DEGRADE_MODES) assert.ok(py.includes(`"${m}"`), m);
  assert.equal(appProfilePath("cis"), `${APP_PROFILES_API}/cis`);
});

test("the Edit control is an ai.admin page action the API answers", () => {
  assert.ok(PAGE_ACTIONS.includes("app_profiles"));
  assert.equal(ACTION_CAPABILITY.app_profiles, "ai.admin");
  const router = read("../../api/routers/admin_ai_gateway.py");
  assert.match(router, /"app_profiles": \("PATCH", "\/admin\/ai-gateway\/app-profiles\/\{app\}"\)/);
});

test("the card is on the Providers overview, kit only, Edit behind usePageAccess", () => {
  const tab = read("../app/admin/ai-gateway/ProvidersTab.tsx");
  assert.match(tab, /<AppProfilesCard \/>/);
  const card = read("../app/admin/ai-gateway/AppProfilesCard.tsx");
  assert.match(card, /usePageAccess\(\)\.can\("app_profiles"\)/);
  assert.match(card, /if \(canEdit\)/);
  assert.doesNotMatch(card, /<(input|select|textarea|table|button)[\s>]/);
  assert.doesNotMatch(card.replace(/^\s*\/\/.*$/gm, ""), /AA-\d+/);       // comments may name the issue, UI text not
});

test("the card's messages have the same keys in English and Vietnamese and no issue id", () => {
  const src = read("../app/admin/ai-gateway/appProfiles.messages.ts");
  assert.doesNotMatch(src.replace(/^\/\/.*$/gm, ""), /AA-\d+/);
  for (const name of ["PROFILES", "DEGRADE", "PROFILE_APP"]) assert.ok(src.includes(`export const ${name}`), name);
});
