// Gateway slice S4 — the Connections tab's pure helpers.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  budgetTone, circuitTone, connectionName, formOf, healthTone, isProvisional, patchBody, reconcileTone, takesBaseUrl,
  takesSecret, testChoices, usd,
} from "./llmConnections.ts";
import { LLM_ACCOUNT } from "./awsAccounts.ts";

const DIR = { [LLM_ACCOUNT]: { account_id: "444455556666", name: "444455556666", label: "x", role: "LLM" } };

function conn(over = {}) {
  return {
    connection_key: "anthropic-direct", kind: "anthropic", auth_mode: "api_key", label: "Anthropic API",
    enabled: false, data_scope: "vendor_direct", account_id: null, region: null, role_name: null,
    credential_source: "secret", secret_name: "aa-llm/dev/anthropic-direct", credential_env: null,
    credential_env_set: null, base_url: "https://api.anthropic.com", timeout_s: null, max_retries: null,
    max_concurrency: null, rpm_limit: null, monthly_budget_usd: null, policy_problems: [], notes: null,
    updated_at: null, updated_by: null, deployments: [], ...over,
  };
}

test("a Bedrock connection is named by its account ID, never its key", () => {
  assert.equal(connectionName({ connection_key: LLM_ACCOUNT, kind: "bedrock", account_id: null }, DIR), "444455556666");
  assert.equal(connectionName({ connection_key: "org", kind: "bedrock", account_id: "111122223333" }, DIR),
    "111122223333");
  assert.equal(connectionName(conn(), DIR), "anthropic-direct");
});

test("tones fall back to neutral", () => {
  assert.equal(healthTone("open"), "danger");
  assert.equal(healthTone("weird"), "neutral");
  assert.equal(circuitTone("half_open"), "warning");
  assert.equal(circuitTone(null), "success");
});

test("the patch carries only changed fields, emptied ones as null", () => {
  const c = conn({ rpm_limit: 60, notes: "keep" });
  const form = { ...formOf(c), rpm_limit: "120", timeout_s: "60", notes: "" };
  const { body, errors } = patchBody(c, form);
  assert.deepEqual(body, { rpm_limit: 120, timeout_s: 60, notes: null });
  assert.deepEqual(errors, {});
});

test("client-side checks mirror the API rules", () => {
  const c = conn();
  const { errors } = patchBody(c, {
    ...formOf(c), secret_name: "aa-cis/dev/rds", base_url: "http://api.anthropic.com", max_retries: "9",
    max_concurrency: "1.5",
  });
  assert.equal(errors.secret_name.code, "secretPrefix");
  assert.equal(errors.base_url.code, "httpsOnly");
  assert.deepEqual(errors.max_retries, { code: "range", min: 0, max: 5 });
  assert.equal(errors.max_concurrency.code, "wholeNumber");
});

test("a Bedrock task-role connection never sends a secret or a base URL", () => {
  const c = conn({ connection_key: "main", kind: "bedrock", auth_mode: "task_role", secret_name: null, base_url: null });
  assert.equal(takesSecret(c), false);
  assert.equal(takesBaseUrl(c), false);
  const { body } = patchBody(c, { ...formOf(c), secret_name: "aa-llm/dev/x", base_url: "https://x", rpm_limit: "20" });
  assert.deepEqual(body, { rpm_limit: 20 });
});

test("test choices skip embeddings, enabled first", () => {
  const d = (model_key, enabled, priority) => ({ model_key, enabled, priority, wire_id: "w", origin: "seed",
    batch_capable: false, price_in_override: null, price_out_override: null, notes: null });
  const c = conn({ deployments: [d("b", false, 1), d("embed", true, 1), d("a", true, 20), d("c", true, 10)] });
  const style = (k) => (k === "embed" ? "embed" : "converse");
  assert.deepEqual(testChoices(c, style).map((x) => x.model_key), ["c", "a", "b"]);
});

// ── gateway slice S10: budget policy fields, budget / reconcile tones, money text ─────────────────────────────────

const POLICY = { alert_pct: 80, hard_stop: true, usage_secret_name: null, usage_scope_id: null, stored: false,
  updated_at: null, updated_by: null };

test("S10: policy fields join the patch only when they change; usage fields only where a usage API exists", () => {
  const c = conn({ cost_policy: POLICY, usage_key_supported: true });
  const form = { ...formOf(c), budget_alert_pct: "90", budget_hard_stop: "false",
    usage_secret_name: "aa-llm/dev/anthropic-usage", usage_scope_id: "wrkspc_01ABC" };
  const { body, errors } = patchBody(c, form);
  assert.deepEqual(body, { budget_alert_pct: 90, budget_hard_stop: false,
    usage_secret_name: "aa-llm/dev/anthropic-usage", usage_scope_id: "wrkspc_01ABC" });
  assert.deepEqual(errors, {});
  const bedrock = conn({ kind: "bedrock", auth_mode: "assume_role", cost_policy: POLICY, usage_key_supported: false });
  const b = patchBody(bedrock, { ...formOf(bedrock), usage_secret_name: "aa-llm/dev/x" });
  assert.deepEqual(b.body, {});
});

test("S10: an emptied alert percent means the default, which is no change from the default", () => {
  const c = conn({ cost_policy: POLICY });
  assert.deepEqual(patchBody(c, { ...formOf(c), budget_alert_pct: "" }).body, {});
  const c90 = conn({ cost_policy: { ...POLICY, alert_pct: 90 } });
  assert.deepEqual(patchBody(c90, { ...formOf(c90), budget_alert_pct: "" }).body, { budget_alert_pct: 80 });
});

test("S10: bad policy values are flagged before the round trip", () => {
  const c = conn({ cost_policy: POLICY, usage_key_supported: true });
  const { errors } = patchBody(c, { ...formOf(c), budget_alert_pct: "0", usage_secret_name: "aa-cis/dev/rds",
    usage_scope_id: "bad id" });
  assert.equal(errors.budget_alert_pct.code, "range");
  assert.equal(errors.usage_secret_name.code, "secretPrefix");
  assert.equal(errors.usage_scope_id.code, "scopeId");
});

test("S10: an older API without the policy sends no policy field", () => {
  const c = conn();
  assert.deepEqual(patchBody(c, { ...formOf(c), budget_alert_pct: "50", budget_hard_stop: "false" }).body, {});
});

test("S10: tones and money text", () => {
  assert.equal(budgetTone("stopped"), "danger");
  assert.equal(budgetTone("alert"), "warning");
  assert.equal(budgetTone(undefined), "neutral");
  assert.equal(reconcileTone("mismatch"), "warning");
  assert.equal(reconcileTone("not_configured"), "neutral");
  assert.equal(usd(12.345), "$12.35");
  assert.equal(usd(0.0042), "$0.0042");
  assert.equal(usd(-1.5), "-$1.50");
  assert.equal(usd(null), "—");
  assert.equal(isProvisional({ details: { provisional: true } }), true);
  assert.equal(isProvisional({ details: {} }), false);
});
