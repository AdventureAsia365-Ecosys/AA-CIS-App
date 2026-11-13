// frontend/lib/settingsRegistry.test.mjs
// S279 thresholds L1 — Platform › Settings: the pure helpers of lib/settingsRegistry.ts (value text, bounds, the parse
// of a typed value against its definition, the form checks, the proposal body, badges, grouping, paths), the BFF rules
// (admin only) and the nav entry. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  actorText, badgesOf, boundsText, decisionPath, draftProblem, draftText, formatValue, groupByArea, historyPath,
  parseDraft, proposalBody, proposalPath, registryPath,
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
  assert.equal(formatValue({ a: 1 }, TABLE), '{"a":1}');
  assert.equal(formatValue(null, INT), "—");
  assert.equal(boundsText(INT), "1 – 1000 posts/day");
  assert.equal(boundsText(MODE), "one of soft, hard");
  assert.equal(boundsText({ ...INT, min_value: null, max_value: null }), "");
});

test("parseDraft follows the definition (the reader's rules)", () => {
  assert.deepEqual(parseDraft(INT, " 25 "), { value: 25 });
  assert.match(parseDraft(INT, "0").error, /minimum/);
  assert.match(parseDraft(INT, "1001").error, /maximum/);
  assert.match(parseDraft(INT, "2.5").error, /whole/);
  assert.match(parseDraft(INT, "abc").error, /number/);
  assert.deepEqual(parseDraft(RATIO, "3.364"), { value: 3.364 });
  assert.deepEqual(parseDraft(MODE, "hard"), { value: "hard" });
  assert.ok(parseDraft(MODE, "block").error);
  assert.deepEqual(parseDraft(FLAG, "on"), { value: true });
  assert.deepEqual(parseDraft(TABLE, '{"serp_advanced": 0.003}'), { value: { serp_advanced: 0.003 } });
  assert.match(parseDraft(TABLE, '{"serp_advanced": 2}').error, /maximum/);
  assert.match(parseDraft(TABLE, '{"serp_advanced": "x"}').error, /number/);
  assert.match(parseDraft(TABLE, "{bad").error, /JSON/);
  assert.equal(draftText(FLAG, false), "off");
  assert.equal(draftText(INT, 20), "20");
});

test("the form needs a 20-character reason and, where required, evidence", () => {
  const draft = { text: "25", reason: "short", evidence: "", effectiveFrom: "", apply: true };
  assert.match(draftProblem(INT, draft), /20 characters/);
  const ok = { ...draft, reason: "lower the default for the trial period" };
  assert.equal(draftProblem(INT, ok), null);
  assert.match(draftProblem(RATIO, { ...ok, text: "3.364" }), /evidence/);
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
