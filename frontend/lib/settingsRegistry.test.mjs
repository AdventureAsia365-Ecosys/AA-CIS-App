// frontend/lib/settingsRegistry.test.mjs
// S279 thresholds L1 — Platform › Settings: the pure helpers of lib/settingsRegistry.ts (value text, bounds, the parse
// of a typed value against its definition, the form checks, the proposal body, badges, grouping, paths), the BFF rules
// (admin only) and the nav entry. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  actorLabel, actorText, badgesOf, boundsText, decisionBody, decisionPath, directionWords, draftProblem, draftText,
  formatValue, groupByArea, guideFor, historyPath, nowExample, parseDraft, parseNumber, proposalBody, proposalPath,
  registryPath, valuesInForce, withValue, PLATFORM_SCOPE, historyTenant, scopeOptions, scopeReady, scopeTarget,
  scopeTenant, searchSettings, tenantScoped, approvedVersion, scheduledOf, openProposal, tenantProposals,
  canApplyAtOnce, lastChangeState, isScheduled, loadProblem, effectiveInPast, localMinute, PROBLEM_IDS, agencyScoped,
  agencyNames, effectiveShown,
} from "./settingsRegistry.ts";
import { canReadAdminPath, canWriteAdminPath } from "./rbac.ts";
import { ADMIN_IA } from "./adminIa.ts";

const INT = { key: "cis.publish.daily_limit_default", app: "cis", area: "publish", label: "Daily publish limit",
  description: "Posts per day.", value_type: "int", unit: "posts/day", min_value: 1, max_value: 1000,
  allowed_values: null, default_value: 20, scope_kind: "platform", impact: "high", approval: "maker_checker",
  evidence_required: false, env_override: null, cache_s: 30, enforced: true };
const RATIO = { ...INT, key: "cis.t7.blog_chars_per_seed_word", area: "content", label: "Ratio", value_type: "numeric",
  unit: "chars/word", min_value: 0, max_value: 20, default_value: 5.8, evidence_required: true };
const MODE = { ...INT, key: "cis.billing.rewrite_over_quota_mode", value_type: "enum", unit: null, min_value: null,
  max_value: null, allowed_values: ["soft", "hard"], default_value: "soft" };
const TABLE = { ...INT, key: "cis.spend.dfs_call_estimate_usd", value_type: "json", unit: "USD/call", min_value: 0,
  max_value: 1, default_value: { serp_advanced: 0.002 } };
const FLAG = { ...INT, key: "cis.settings.two_person_approval", value_type: "bool", unit: null, min_value: null,
  max_value: null, default_value: false, approval: "maker_checker" };

function item(d, over = {}) {
  return { ...d, value: d.default_value, source: "default", version: null, overridden_by_env: false, env_ignored: null,
    refused: null, last_change: null, proposals: [], ...over };
}

test("value and bounds text", () => {
  assert.equal(formatValue(20, INT), "20 posts/day");
  assert.equal(formatValue(0.25, { value_type: "usd", unit: "USD" }), "$0.25");
  assert.equal(formatValue(true, FLAG), "on");
  assert.equal(formatValue({ a: 1 }, TABLE), '{"a":1} USD/call');      // S280: a table carries its unit
  assert.equal(formatValue(null, INT), "—");
  assert.equal(boundsText(INT), "1 – 1000 posts/day");
  assert.equal(boundsText(MODE), "one of soft, hard");
  assert.equal(boundsText({ ...INT, min_value: null, max_value: null }), "");
});

test("parseDraft follows the definition (the reader's rules)", () => {
  assert.deepEqual(parseDraft(INT, " 25 "), { value: 25 });
  assert.deepEqual(parseDraft(INT, "0").error, { id: "belowMin", arg: "1" });
  assert.deepEqual(parseDraft(INT, "1001").error, { id: "aboveMax", arg: "1000" });
  assert.equal(parseDraft(INT, "2.5").error.id, "wholeNoSep");
  assert.equal(parseDraft(INT, "abc").error.id, "whole");
  assert.deepEqual(parseDraft(RATIO, "3.364"), { value: 3.364 });
  assert.deepEqual(parseDraft(MODE, "hard"), { value: "hard" });
  assert.deepEqual(parseDraft(MODE, "block").error, { id: "chooseOne", arg: "soft, hard" });
  assert.deepEqual(parseDraft(FLAG, "on"), { value: true });
  assert.deepEqual(parseDraft(TABLE, '{"serp_advanced": 0.003}'), { value: { serp_advanced: 0.003 } });
  assert.deepEqual(parseDraft(TABLE, '{"serp_advanced": 2}').error, { id: "entryAboveMax", arg: "1" });
  assert.equal(parseDraft(TABLE, '{"serp_advanced": "x"}').error.id, "entryNumber");
  assert.equal(parseDraft(TABLE, "{bad").error.id, "json");
  assert.equal(draftText(FLAG, false), "off");
  assert.equal(draftText(INT, 20), "20");
});

test("the form needs a 20-character reason and, where required, evidence", () => {
  const draft = { text: "25", reason: "short", evidence: "", effectiveFrom: "", apply: true };
  assert.deepEqual(draftProblem(INT, draft), { id: "reasonShort", arg: "20" });
  const ok = { ...draft, reason: "lower the default for the trial period" };
  assert.equal(draftProblem(INT, ok), null);
  assert.deepEqual(draftProblem(RATIO, { ...ok, text: "3.364" }), { id: "evidence" });
  assert.equal(draftProblem(RATIO, { ...ok, text: "3.364", evidence: "replay:zz-1" }), null);
  const body = proposalBody(INT, { version: 2 }, { ...ok, evidence: " ", effectiveFrom: "" });
  assert.deepEqual(body, { value: 25, reason: "lower the default for the trial period", evidence_ref: null,
    effective_from: null, base_version: 2, apply: true });
  assert.equal(proposalBody(INT, { version: null }, ok).base_version, 0);
});

test("badges and grouping", () => {
  assert.deepEqual(badgesOf(item(INT)), []);
  assert.deepEqual(badgesOf(item(RATIO, { overridden_by_env: true, source: "env" })), ["env", "evidence"]);
  assert.deepEqual(badgesOf(item({ ...INT, enforced: false }, { proposals: [{}], source: "platform", version: 1 })),
    ["notEnforced", "pending", "changed"]);
  const groups = groupByArea([item(INT), item(RATIO), item(MODE)]);
  assert.deepEqual(groups.map((g) => g.area), ["content", "publish"]);
  assert.deepEqual(groups[1].items.map((i) => i.label), ["Daily publish limit", "Daily publish limit"]);
  assert.equal(actorText("admin:abc"), "abc");
  assert.equal(actorText(null), "—");
});

test("paths", () => {
  assert.equal(registryPath("cis"), "/api/admin/settings/registry?app=cis");
  assert.equal(historyPath("cis.a.b"), "/api/admin/settings/registry/cis.a.b/history");
  assert.equal(proposalPath("cis.a.b"), "/api/admin/settings/registry/cis.a.b/proposals");
  assert.equal(decisionPath("v1", false), "/api/admin/settings/registry/proposals/v1/reject");
});

test("the BFF lets only the administrator read or change the registry", () => {
  assert.equal(canReadAdminPath("admin", "settings/registry"), true);
  for (const role of ["ai_ops", "content_lead", "customer_success", "finance", "viewer"]) {
    assert.equal(canReadAdminPath(role, "settings/registry"), false, role);
    assert.equal(canWriteAdminPath(role, "POST", "settings/registry/cis.a.b/proposals"), false, role);
  }
  assert.equal(canWriteAdminPath("admin", "POST", "settings/registry/proposals/v1/approve"), true);
});

test("the nav lists Platform › Settings and the page shows no issue id", () => {
  const platform = ADMIN_IA.find((g) => g.label === "Platform");
  assert.ok(platform.items.some((i) => i.href === "/admin/platform/settings"));
  for (const f of ["../app/admin/platform/settings/messages.ts", "../app/admin/platform/settings/SettingsRegistry.tsx"]) {
    const text = readFileSync(new URL(f, import.meta.url), "utf8");
    const strings = text.match(/"[^"\n]*"|`[^`\n]*`/g) ?? [];
    assert.ok(!strings.some((s) => /AA-\d+/.test(s)), f);
  }
});

test("a CRM row is decided in the CRM; an agency's own row names its agency", () => {
  const crm = { ...INT, key: "crm.rate.intake_form_per_hour", app: "crm" };
  assert.deepEqual(decisionBody(INT, { tenant_id: null }, "  "), { note: null });
  assert.deepEqual(decisionBody(crm, { tenant_id: null }, "ok"), { note: "ok", app: "crm" });
  const agency = "00000000-0000-4000-8000-0000000000a1";
  assert.deepEqual(decisionBody(crm, { tenant_id: agency }, ""), { note: null, app: "crm", tenant_id: agency });
  assert.equal(historyPath("crm.rate.intake_form_per_hour", agency),
    `/api/admin/settings/registry/crm.rate.intake_form_per_hour/history?tenant_id=${agency}`);
});

// S280 fix round: the CRM tab lists each agency's own value and open change (the relay maps agency_overrides /
// agency_pending), named by the CRM; Review opens the agency's rows, where Approve / Reject send its id.
test("a CRM platform+tenant key lists its agencies by the CRM's names, and a change stays a platform change", () => {
  const crm = { ...INT, key: "crm.rate.intake_form_per_hour", app: "crm", scope_kind: "platform+tenant" };
  const a = "00000000-0000-4000-8000-0000000000a1";
  const b = "00000000-0000-4000-8000-0000000000b2";
  assert.equal(agencyScoped(crm), true);
  assert.equal(agencyScoped({ ...crm, scope_kind: "platform" }), false);
  assert.equal(agencyScoped({ ...crm, app: "cis" }), false);
  assert.deepEqual(scopeOptions(crm), ["platform"]);
  const item = { ...crm, proposals: [{ tenant_id: b, agency_name: "Beta Travel", status: "proposed" }],
    tenant_overrides: [{ tenant_id: a, agency_name: "Alpha Tours", value: 900, version: 2, effective_from: null },
      { tenant_id: b, agency_name: null, value: 300, version: 1, effective_from: null }] };
  assert.deepEqual([...agencyNames(item)], [[a, "Alpha Tours"], [b, "Beta Travel"]]);
  assert.equal(historyTenant(item), b);
  assert.deepEqual(decisionBody(crm, { tenant_id: b }, "ok"), { note: "ok", app: "crm", tenant_id: b });
  assert.deepEqual(agencyNames({ proposals: [] }), new Map());
});

// AA-1096 l23: the tech lead typed "3,364" and got "Enter a number"; both decimal marks are one number now. A
// whole-number key refuses a separator, so "2,000" never becomes 2.
test("a decimal key takes a decimal comma or point; a whole-number key takes digits only", () => {
  assert.deepEqual(parseDraft(RATIO, "3,364"), { value: 3.364 });
  assert.deepEqual(parseDraft(RATIO, " 3.364 "), { value: 3.364 });
  assert.deepEqual(parseDraft(RATIO, "0,5"), { value: 0.5 });
  assert.equal(parseDraft(RATIO, "3,364,1").error.id, "number");
  assert.equal(parseDraft(RATIO, "1.000,5").error.id, "number");
  assert.equal(parseDraft(RATIO, "21,5").error.id, "aboveMax");
  assert.deepEqual(parseDraft({ ...RATIO, value_type: "usd", min_value: 0 }, "0,25"), { value: 0.25 });
  assert.equal(parseDraft(INT, "2,000").error.id, "wholeNoSep");
  assert.equal(parseDraft(INT, "2.5").error.id, "wholeNoSep");
  assert.deepEqual(parseDraft(INT, "-0"), { error: { id: "belowMin", arg: "1" } });
  assert.deepEqual(parseNumber("3,364", false), { value: 3.364 });
  assert.deepEqual(parseNumber("12", true), { value: 12 });
  assert.deepEqual(parseNumber("", false), { error: { id: "number" } });
  const ok = { text: "3,364", reason: "the replay of stored blogs measured it", evidence: "replay:zz-1",
    effectiveFrom: "", apply: true };
  assert.equal(draftProblem(RATIO, ok), null);
  assert.equal(proposalBody(RATIO, { version: 0 }, ok).value, 3.364);
});

test("the last change names the staff member, the id stays in the tooltip", () => {
  const id = "00000000-0000-4000-8000-0000000000aa";
  assert.deepEqual(actorLabel(`admin:${id}`, "Zz Admin"), { text: "Zz Admin", title: id });
  assert.deepEqual(actorLabel(`admin:${id}`, null), { text: id, title: id });
  assert.deepEqual(actorLabel(`admin:${id}`, "  "), { text: id, title: id });
  assert.deepEqual(actorLabel(null, undefined), { text: "—", title: "—" });
});

test("the guide in the page's language, else English; the directions are named by value type", () => {
  const en = { what: "w", raise: "r", lower: "l", when: "n", example: "e" };
  const vi = { what: "vw", raise: "vr", lower: "vl", when: "vn", example: "ve" };
  assert.equal(guideFor({ guide: { en, vi } }, "vi"), vi);
  assert.equal(guideFor({ guide: { en, vi } }, "en"), en);
  assert.equal(guideFor({ guide: { en } }, "vi"), en);
  assert.equal(guideFor({ guide: null }, "en"), null);
  assert.equal(guideFor({}, "en"), null);
  assert.equal(directionWords("numeric"), "number");
  assert.equal(directionWords("usd"), "number");
  assert.equal(directionWords("bool"), "switch");
  assert.equal(directionWords("enum"), "choice");
  assert.equal(directionWords("json"), "choice");
});

// AA-1096 l23 review: the example names the default; the page computes the value in force for the derived keys.
const IN_FORCE = valuesInForce([
  { key: "cis.t9.blog_chars_min", value: 4700 }, { key: "cis.t9.blog_chars_max", value: 13000 },
  { key: "cis.t7.blog_chars_per_seed_word", value: 3.364 }, { key: "cis.t7.blog_min_seed_words", value: 600 },
  { key: "cis.t9.repeat_drop_near_exact", value: 0.9 }, { key: "cis.quality.t9_blog_length_target_margin", value: 0.17 },
]);

test("the in-force line computes the blog source words from the values in force, not the default", () => {
  assert.deepEqual(nowExample("cis.t7.blog_chars_per_seed_word", IN_FORCE, "en"),
    { id: "nowSeedRatio", ratio: "3.364", words: "1,398", floor: "4,700", off: false });
  assert.deepEqual(nowExample("cis.t7.blog_chars_per_seed_word", IN_FORCE, "vi"),
    { id: "nowSeedRatio", ratio: "3,364", words: "1.398", floor: "4.700", off: false });
  assert.deepEqual(nowExample("cis.t7.blog_min_seed_words", IN_FORCE, "en"),
    { id: "nowSeedMin", flat: "600", sized: "1,398", need: "1,398" });
  const off = withValue(IN_FORCE, "cis.t7.blog_min_seed_words", 0);
  assert.deepEqual(nowExample("cis.t7.blog_min_seed_words", off, "en"), { id: "nowSeedMinOff" });
  assert.equal(nowExample("cis.t7.blog_chars_per_seed_word", off, "en").off, true);
  const noSizing = withValue(IN_FORCE, "cis.t7.blog_chars_per_seed_word", 0);
  assert.deepEqual(nowExample("cis.t7.blog_chars_per_seed_word", noSizing, "en"), { id: "nowSeedRatioZero" });
  assert.deepEqual(nowExample("cis.t7.blog_min_seed_words", noSizing, "en"),
    { id: "nowSeedMin", flat: "600", sized: "0", need: "600" });
});

test("the in-force line of the repeat cleanup and the blog length aim; other keys get none", () => {
  assert.deepEqual(nowExample("cis.t9.repeat_drop_near_exact", IN_FORCE, "en"),
    { id: "nowRepeat", share: "0.9", pct: "90" });
  assert.deepEqual(nowExample("cis.quality.t9_blog_length_target_margin", IN_FORCE, "en"),
    { id: "nowLengthAim", margin: "0.17", aim: "5,499", floor: "4,700" });
  const capped = withValue(IN_FORCE, "cis.quality.t9_blog_length_target_margin", 5);
  assert.equal(nowExample("cis.quality.t9_blog_length_target_margin", capped, "en").aim, "13,000");
  assert.equal(nowExample("cis.publish.daily_limit_default", IN_FORCE, "en"), null);
  assert.equal(nowExample("cis.t9.repeat_drop_near_exact", valuesInForce([]), "en"), null);
  assert.equal(nowExample("cis.t7.blog_chars_per_seed_word", withValue(IN_FORCE, "cis.t9.blog_chars_min", "x"), "en"),
    null);
});

// ── AA-1096 (S280): the tenant scope of a change and the search over every area ─────────────────────────────────
const EXTEND = { ...MODE, key: "cis.t9.blog_length_extend", area: "content", label: "Blog length: lengthen a short blog",
  description: "A tenant value turns it on for one agency only.", allowed_values: ["off", "on"], default_value: "off",
  scope_kind: "platform+tenant", evidence_required: true };
const T1 = "11111111-1111-4111-8111-111111111111";
const T2 = "22222222-2222-4222-8222-222222222222";

test("scope options: Platform only, unless the definition allows a tenant value (never a CRM key)", () => {
  assert.deepEqual(scopeOptions(INT), ["platform"]);
  assert.deepEqual(scopeOptions(EXTEND), ["platform", "tenant"]);
  assert.deepEqual(scopeOptions({ ...EXTEND, app: "crm" }), ["platform"]);
  assert.deepEqual(scopeOptions({ ...EXTEND, app: "tripplanner" }), ["platform", "tenant"]);
  assert.equal(tenantScoped(INT), false);
  assert.equal(tenantScoped(EXTEND), true);
  assert.equal(scopeTenant(PLATFORM_SCOPE), null);
  assert.equal(scopeTenant({ kind: "tenant", tenantId: "" }), null);
  assert.equal(scopeTenant({ kind: "tenant", tenantId: T1 }), T1);
  assert.equal(scopeTenant({ kind: "platform", tenantId: T1 }), null);
  assert.equal(scopeReady(PLATFORM_SCOPE), true);
  assert.equal(scopeReady({ kind: "tenant", tenantId: "" }), false);
  assert.equal(scopeReady({ kind: "tenant", tenantId: T1 }), true);
});

test("scope target: the platform value, a tenant's own value, or what the tenant inherits (version 0)", () => {
  const it = item(EXTEND, { value: "off", version: 3, source: "platform",
    tenant_overrides: [{ tenant_id: T1, value: "on", version: 2, effective_from: null }] });
  assert.deepEqual(scopeTarget(it, null), { value: "off", version: 3, inherited: false });
  assert.deepEqual(scopeTarget(it, T1), { value: "on", version: 2, inherited: false });
  assert.deepEqual(scopeTarget(it, T2), { value: "off", version: 0, inherited: true });
  assert.deepEqual(scopeTarget(item(INT), null), { value: 20, version: 0, inherited: false });
  // an older API (or the CRM relay) sends no tenant_overrides
  assert.deepEqual(scopeTarget(item(EXTEND), T1), { value: "off", version: 0, inherited: true });
});

test("a tenant proposal names its tenant and the tenant's version; a platform one sends no tenant_id", () => {
  const draft = { text: "on", reason: "trial the lengthening on the audit tenant only", evidence: "replay:zz-1",
    effectiveFrom: "", apply: false };
  const body = proposalBody(EXTEND, { version: 2 }, draft, T1);
  assert.equal(body.tenant_id, T1);
  assert.equal(body.base_version, 2);
  assert.equal("tenant_id" in proposalBody(EXTEND, { version: 3 }, draft, null), false);
  assert.equal("tenant_id" in proposalBody(EXTEND, { version: 3 }, draft), false);
  assert.equal(historyPath(EXTEND.key, T1), `/api/admin/settings/registry/cis.t9.blog_length_extend/history?tenant_id=${T1}`);
});

test("the history dialog opens on the tenant of the open proposal only when no platform proposal is open", () => {
  assert.equal(historyTenant(item(EXTEND)), null);
  assert.equal(historyTenant(item(EXTEND, { proposals: [{ tenant_id: T1 }] })), T1);
  assert.equal(historyTenant(item(EXTEND, { proposals: [{ tenant_id: T1 }, { tenant_id: null }] })), null);
});

test("search: key, label and description across every area, each word must match, sorted by area then label", () => {
  const all = [item(INT), item(RATIO), item(MODE), item(EXTEND),
    item({ ...INT, key: "cis.erasure.saga_apply_enabled", area: "tenants", label: "Erasure: apply the saga",
      description: "Run the data erasure saga for real." })];
  assert.deepEqual(searchSettings(all, ""), []);
  assert.deepEqual(searchSettings(all, "   "), []);
  // a full key, in any case, found outside the area tab
  assert.deepEqual(searchSettings(all, "CIS.erasure.saga_apply_enabled").map((s) => s.key),
    ["cis.erasure.saga_apply_enabled"]);
  // label words
  assert.deepEqual(searchSettings(all, "daily publish").map((s) => s.key).sort(),
    ["cis.billing.rewrite_over_quota_mode", "cis.publish.daily_limit_default"]);
  // description words
  assert.deepEqual(searchSettings(all, "one agency").map((s) => s.key), ["cis.t9.blog_length_extend"]);
  // every word must match
  assert.deepEqual(searchSettings(all, "erasure ratio"), []);
  // across areas, sorted by area then label
  assert.deepEqual(searchSettings(all, "cis.").map((s) => s.area), ["content", "content", "publish", "publish",
    "tenants"]);
  assert.deepEqual(searchSettings(all, "cis.").slice(0, 2).map((s) => s.label),
    ["Blog length: lengthen a short blog", "Ratio"]);
});


// ── AA-1096 (S280 UX fix round) ───────────────────────────────────────────────────────────────────────────────────
const NOW = new Date("2026-11-14T10:00:00Z");
const row = (over = {}) => ({ id: "v", key: INT.key, tenant_id: null, value: 30, version: 1, status: "approved",
  effective_from: "2026-11-13T10:00:00Z", proposed_by: "admin:a", proposed_at: null, reason: "r", evidence_ref: null,
  self_approved: false, decided_by: "admin:a", decided_at: "2026-11-13T10:00:00Z", decision_note: null, ...over });

test("the page's words: on / off, 'one of', units, a share with its percentage, a table with its unit", () => {
  const vi = { on: "bật", off: "tắt", oneOf: (l) => `một trong ${l}`, unit: (u) => (u === "posts/day" ? "bài/ngày" : u) };
  assert.equal(formatValue(true, FLAG, vi), "bật");
  assert.equal(formatValue(20, INT, vi), "20 bài/ngày");
  assert.equal(boundsText(MODE, vi), "một trong soft, hard");
  assert.equal(boundsText(INT, vi), "1 – 1000 bài/ngày");
  const SHARE = { ...RATIO, unit: "share", min_value: 0, max_value: 1 };
  assert.equal(formatValue(0.9, SHARE), "0.9 (90 %)");
  assert.equal(formatValue(0.125, SHARE), "0.125 (12.5 %)");
  assert.equal(formatValue({ ads: 2 }, { value_type: "json", unit: "per Subject" }), '{"ads":2} per Subject');
});

test("every parse problem is a message id, never English text", () => {
  for (const [d, text] of [[INT, "x"], [INT, "0"], [MODE, "x"], [FLAG, "x"], [TABLE, "[1"], [TABLE, "3"]]) {
    const e = parseDraft(d, text).error;
    assert.ok(e && PROBLEM_IDS.includes(e.id), `${d.key} ${text}`);
  }
});

test("a change may not be dated in the past; the input's min is now, to the minute", () => {
  const ok = { text: "25", reason: "lower the default for the trial period", evidence: "", effectiveFrom: "", apply: true };
  assert.equal(effectiveInPast("", NOW), false);
  assert.equal(effectiveInPast("2026-11-14T09:00:00Z", NOW), true);
  assert.equal(effectiveInPast("2026-11-14T09:58:00Z", NOW), false);           // inside the grace: counts as now
  assert.equal(effectiveInPast("2026-11-15T09:00:00Z", NOW), false);
  assert.deepEqual(draftProblem(INT, { ...ok, effectiveFrom: "2026-11-13T09:00:00Z" }, NOW), { id: "effectivePast" });
  assert.equal(draftProblem(INT, { ...ok, effectiveFrom: "2026-11-20T09:00:00Z" }, NOW), null);
  assert.match(localMinute(new Date(2026, 10, 4, 9, 5, 30)), /^2026-11-04T09:05$/);
});

test("the base version is the approved version in force, also under an env override or a refused value", () => {
  assert.equal(approvedVersion({ version: null, approved_version: 3 }), 3);
  assert.equal(approvedVersion({ version: 2 }), 2);
  assert.equal(approvedVersion({ version: null, last_change: row({ version: 4 }) }, NOW), 4);
  assert.equal(approvedVersion({ version: null, last_change: row({ version: 4, effective_from: "2026-11-20T00:00:00Z" }) },
    NOW), 0);
  assert.equal(approvedVersion({ version: null, last_change: row({ status: "rejected" }) }, NOW), 0);
  const env = item(INT, { source: "env", version: null, overridden_by_env: true, approved_version: 5 });
  assert.equal(scopeTarget(env, null).version, 5);
  assert.equal(proposalBody(INT, scopeTarget(env, null), { text: "25", reason: "r", evidence: "", effectiveFrom: "",
    apply: false }).base_version, 5);
});

test("scheduled changes: the list's own, else (CRM) the approved last change dated ahead; badge 'scheduled'", () => {
  const sched = [{ tenant_id: null, value: 40, version: 2, effective_from: "2026-11-20T00:00:00Z" },
    { tenant_id: "t1", value: 9, version: 1, effective_from: "2026-11-21T00:00:00Z" }];
  const it = item(INT, { scheduled: sched });
  assert.deepEqual(scheduledOf(it, null, NOW).map((s) => s.value), [40]);
  assert.deepEqual(scheduledOf(it, "t1", NOW).map((s) => s.value), [9]);
  assert.ok(badgesOf(it, NOW).includes("scheduled"));
  const crm = item(INT, { last_change: row({ effective_from: "2026-11-20T00:00:00Z" }) });
  assert.equal(scheduledOf(crm, null, NOW).length, 1);
  assert.equal(scheduledOf(item(INT, { last_change: row() }), null, NOW).length, 0);
  assert.equal(lastChangeState(row({ status: "rejected" }), NOW), "rejected");
  assert.equal(lastChangeState(row({ effective_from: "2026-11-20T00:00:00Z" }), NOW), "scheduled");
  assert.equal(lastChangeState(row(), NOW), null);
  assert.equal(isScheduled(row({ effective_from: "2026-11-20T00:00:00Z" }), NOW), true);
});

test("open proposals by scope; tenant proposals listed for review", () => {
  const it = item(INT, { proposals: [row({ status: "proposed", tenant_id: "t1" })] });
  assert.equal(openProposal(it, null), undefined);
  assert.equal(openProposal(it, "t1").tenant_id, "t1");
  assert.deepEqual(tenantProposals(it).map((p) => p.tenant_id), ["t1"]);
});

test("save and approve: a single-approval key may apply at once even while two-person approval is on", () => {
  assert.equal(canApplyAtOnce({ approval: "single" }, true), true);
  assert.equal(canApplyAtOnce({ approval: "maker_checker" }, true), false);
  assert.equal(canApplyAtOnce({ approval: "maker_checker" }, false), true);
});

test("a 503 is 'not set up yet' only for the registry's own codes; the CRM or the database not answering is a retry", () => {
  assert.equal(loadProblem(503, "not_migrated"), "notMigrated");
  assert.equal(loadProblem(503, "settings_unavailable"), "notMigrated");
  assert.equal(loadProblem(503, "crm_unavailable"), "unavailable");
  assert.equal(loadProblem(503, undefined), "unavailable");
  assert.equal(loadProblem(500, "x"), "other");
  assert.equal(actorText("platform:abc-sub"), "abc-sub");
});

// S280 fix round: a change sent without a date is stored with its proposal time; History shows when it really applies.
test("History's Effective from: when approved for an undated open change, else the later of date and approval", () => {
  const proposed_at = "2026-11-14T05:00:00.000Z";
  const row = { status: "proposed", effective_from: "2026-11-14T05:00:00.400Z", proposed_at, decided_at: null };
  assert.equal(effectiveShown(row), null);
  assert.equal(effectiveShown({ ...row, effective_from: "2026-11-20T00:00:00.000Z" }), "2026-11-20T00:00:00.000Z");
  const approved = { ...row, status: "approved", decided_at: "2026-11-15T08:00:00.000Z" };
  assert.equal(effectiveShown(approved), "2026-11-15T08:00:00.000Z");
  assert.equal(effectiveShown({ ...approved, effective_from: "2026-11-20T00:00:00.000Z" }), "2026-11-20T00:00:00.000Z");
  assert.equal(effectiveShown({ ...approved, status: "superseded" }), "2026-11-15T08:00:00.000Z");
  assert.equal(effectiveShown({ ...approved, status: "rejected" }), null);
});
