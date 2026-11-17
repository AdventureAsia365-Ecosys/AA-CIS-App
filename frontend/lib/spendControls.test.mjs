// frontend/lib/spendControls.test.mjs
// AA-1126 (S283): the Spend controls section of AI gateway › Budgets (backend GET /admin/ai-gateway/spend-controls).
// Pure helpers (value text, breaker state, what a connection at its budget does, who may reset) plus text guards: the
// section is on the Budgets tab, it links every value to Platform › Settings (area Spend) and never edits a value
// itself, the reset follows usePageAccess, the kit only, and its messages have the same keys in English and
// Vietnamese. Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { ACTION_CAPABILITY, PAGE_ACTIONS } from "./aiGateway.ts";
import {
  SPEND_SETTINGS_HREF, anomalyResetPath, breakerState, breakerTone, breakerUsedPct, changedFromDefault,
  connectionAtCap, mayReset, runCapSettingOf, settingValueText,
} from "./spendControls.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const SECTION = read("../app/admin/ai-gateway/SpendControlsSection.tsx");
const BUDGETS_TAB = read("../app/admin/ai-gateway/BudgetsTab.tsx");
const MESSAGES = read("../app/admin/ai-gateway/spendControls.messages.ts");

test("settingValueText: enum, money with its unit, a multiple, days", () => {
  assert.equal(settingValueText("failover_continue", { value_type: "enum", unit: null }), "failover_continue");
  assert.equal(settingValueText(2, { value_type: "usd", unit: "USD/run" }), "$2.00/run");
  assert.equal(settingValueText(25, { value_type: "usd", unit: "USD/day" }), "$25.00/day");
  assert.equal(settingValueText(4, { value_type: "numeric", unit: "x" }), "4 ×");
  assert.equal(settingValueText(14, { value_type: "int", unit: "days" }), "14 days");
  assert.equal(settingValueText(null, { value_type: "usd", unit: "USD/run" }), "—");
});

test("changedFromDefault compares the values, not their text", () => {
  assert.equal(changedFromDefault({ value: 4, default_value: 4 }), false);
  assert.equal(changedFromDefault({ value: "enforce", default_value: "alert" }), true);
});

test("breakerState / breakerTone: normal, alert only, refusing", () => {
  assert.equal(breakerState({ tripped: false, refuses: false }), "ok");
  assert.equal(breakerState({ tripped: true, refuses: false }), "tripped_alert");
  assert.equal(breakerState({ tripped: true, refuses: true }), "refusing");
  assert.deepEqual(["ok", "tripped_alert", "refusing"].map(breakerTone), ["success", "warning", "danger"]);
  // review S283: an app whose gateway does not read the breaker is never shown as refusing
  assert.equal(breakerState({ tripped: true, refuses: false, mode: "enforce", enforced: false }), "not_enforced");
  assert.equal(breakerState({ tripped: true, refuses: false, mode: "alert", enforced: false }), "tripped_alert");
  assert.equal(breakerTone("not_enforced"), "warning");
  assert.equal(breakerUsedPct({ spent_usd: 50, threshold_usd: 25 }), 200);
  assert.equal(breakerUsedPct({ spent_usd: 5, threshold_usd: 0 }), null);
});

test("runCapSettingOf: the setting that is a central scope's per-run cap (one source of truth)", () => {
  const s1 = { key: "cis.spend.run_cap_s1_rewrite_usd", value: 2, href: "/admin/platform/settings?key=x" };
  const data = { settings: [s1], run_cap_scopes: { "job:s1_rewrite": s1.key, eval: "cis.spend.run_cap_eval_usd" } };
  assert.equal(runCapSettingOf(data, "job:s1_rewrite"), s1);
  assert.equal(runCapSettingOf(data, "eval"), null);                     // key not among the settings sent
  assert.equal(runCapSettingOf(data, "job:a3_atomize"), null);
  assert.equal(runCapSettingOf(undefined, "job:s1_rewrite"), null);       // before the read
  assert.equal(runCapSettingOf({ settings: [s1] }, "job:s1_rewrite"), null);   // an older API
});

test("the central caps show the per-run cap of such a scope from Platform › Settings, never a second value", () => {
  assert.match(BUDGETS_TAB, /<RunCapCell scope=\{c\.row\.original\.scope\}/);
  assert.match(BUDGETS_TAB, /<RunCapEditorNote setting=\{run\.setting\} \/>/);
  assert.match(BUDGETS_TAB, /\{run\.perRunField && \(/);                    // no per-run field for those scopes
  assert.match(BUDGETS_TAB, /run\.error/);
  assert.match(SECTION, /perRunField: kind === "job" && !setting/);
  assert.match(SECTION, /t\("errRunSetting"\)/);
  assert.match(SECTION, /t\("runCapNote"\)/);                               // and the section says so
});

test("connectionAtCap follows the connection's policy and the environment's mode", () => {
  assert.equal(connectionAtCap(true, "hard_stop"), "stops");
  assert.equal(connectionAtCap(true, undefined), "stops");              // an older API: today's rule
  assert.equal(connectionAtCap(true, "failover_continue"), "fails_over");
  assert.equal(connectionAtCap(false, "failover_continue"), "alert_only");
  assert.equal(connectionAtCap(false, "hard_stop"), "alert_only");
});

test("mayReset: only a tripped app, only with the reset control, never a tighten-only role", () => {
  assert.equal(mayReset({ tripped: true }, true, false), true);
  assert.equal(mayReset({ tripped: false }, true, false), false);
  assert.equal(mayReset({ tripped: true }, false, false), false);
  assert.equal(mayReset({ tripped: true }, true, true), false);
});

test("the reset control is a page action of ai.admin and its path is per app", () => {
  assert.ok(PAGE_ACTIONS.includes("anomaly_reset"));
  assert.equal(ACTION_CAPABILITY.anomaly_reset, "ai.admin");
  assert.equal(anomalyResetPath("tripplanner"), "/api/admin/ai-gateway/anomaly-breaker/tripplanner/reset");
});

test("the section is on the Budgets tab and links to Platform › Settings, area Spend", () => {
  assert.equal(SPEND_SETTINGS_HREF, "/admin/platform/settings?app=cis&area=spend");
  assert.match(BUDGETS_TAB, /<SpendControlsSection \/>/);
  assert.match(BUDGETS_TAB, /useAtCapText\(\)/);                          // the connection tier follows the mode
  assert.match(SECTION, /export function useAtCapText/);
  assert.match(SECTION, /x\.row\.original\.href/);                       // each row: its own settings link
  assert.match(SECTION, /SPEND_SETTINGS_HREF/);
});

test("the section never writes a setting: its only write is the breaker reset", () => {
  const writes = [...SECTION.matchAll(/apiSend\(([^,]+),/g)].map((m) => m[1].trim());
  assert.deepEqual(writes, ["anomalyResetPath(app)"]);
  assert.doesNotMatch(SECTION, /\/admin\/settings\/registry/);
  assert.match(SECTION, /access\.can\("anomaly_reset"\)/);
});

test("the section uses the kit only", () => {
  assert.doesNotMatch(SECTION, /from "\.\.\/\.\.\/_kit\/core/);
  assert.doesNotMatch(SECTION, /<(table|button|select|input)\b/);
});

test("the messages have the same keys in English and Vietnamese", async () => {
  // The kit's defineMessages is replaced by a shim (the messages module alone, no React import graph).
  const dir = mkdtempSync(join(tmpdir(), "spend-controls-"));
  const file = join(dir, "messages.ts");
  writeFileSync(file, MESSAGES.replace(/^import \{ defineMessages \} from .*$/m,
    "const defineMessages = (en: object, vi: object) => ({ en, vi });"));
  const mod = await import(pathToFileURL(file).href);
  for (const name of ["SPEND_CONTROLS", "BREAKER_STATE", "VALUE_SOURCE"]) {
    assert.deepEqual(Object.keys(mod[name].vi).sort(), Object.keys(mod[name].en).sort(), name);
  }
  const text = MESSAGES.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  assert.doesNotMatch(text, /AA-\d+/);                                   // no issue ids in UI text
});
