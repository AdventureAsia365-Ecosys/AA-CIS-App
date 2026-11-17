// frontend/lib/spendRules.test.mjs
// AA-1126 (S283 review fix): the admin pages that show an app-wide AI cap say what the code does when it is reached,
// from the API's rule (api/services/spend_rules_view.py): "stops_ai" under the dev spend rules, "alert_only" under
// prod. Pure helper capRuleOf plus text guards: the App profiles card and the TripPlanner caps block render the badge
// from the API value, and their messages carry both sentences in English and Vietnamese.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { budgetRuleOf, capRuleOf, capRuleTone } from "./spendRules.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

test("the rule follows the API value and nothing else", () => {
  assert.equal(capRuleOf("alert_only"), "alert_only");
  assert.equal(capRuleOf("stops_ai"), "stops_ai");
  for (const v of [null, undefined, "", "prod", "dev", 1]) assert.equal(capRuleOf(v), null);
  assert.equal(capRuleTone("stops_ai"), "warning");
  assert.equal(capRuleTone("alert_only"), "info");
  assert.equal(budgetRuleOf({ app: "crm", budget_rule: "alert_only" }), "alert_only");
  assert.equal(budgetRuleOf({ app: "cis" }), null);
});

test("the App profiles card shows the budget's rule beside the budget", () => {
  const card = read("../app/admin/ai-gateway/AppProfilesCard.tsx");
  assert.match(card, /budgetRuleOf\(p\)/);
  assert.match(card, /rule === "alert_only" \? "ruleAlertOnly" : "ruleStops"/);
  const msgs = read("../app/admin/ai-gateway/appProfiles.messages.ts");
  assert.equal((msgs.match(/ruleAlertOnly:/g) || []).length, 2);   // English and Vietnamese
  assert.equal((msgs.match(/ruleStops:/g) || []).length, 2);
  assert.match(msgs, /under the prod spend rules/);
});

test("the TripPlanner caps block shows the rule and no longer claims a mirrored, always-hard cap", () => {
  const caps = read("../app/admin/platform/apps/TpStageCaps.tsx");
  assert.match(caps, /capRuleOf\(spend\.cap_rule\)/);
  assert.match(caps, /unreadableProd/);
  assert.doesNotMatch(caps, /TP_STAGE_CAPS_USD/);
  const msgs = read("../app/admin/platform/apps/tpCaps.messages.ts");
  for (const key of ["ruleAlertOnly:", "ruleStops:", "unreadableProd:"]) {
    assert.equal(msgs.split(key).length - 1, 2, key);
  }
  assert.doesNotMatch(msgs, /as CIS knows them/);
});
