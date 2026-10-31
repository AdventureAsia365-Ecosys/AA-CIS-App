// frontend/lib/aiGatewayC4.test.mjs
// S262 slice C4 (AA-804 / AA-1016 / AA-1017 / AA-1018 / AA-900 UI follow-ups and the live findings F1–F10): the pure
// helpers added to aiGateway.ts, plus text guards over the AI providers page's message files.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  accessTone, actorLabel, blockedReasonText, domainParts, groupRows, heldByReason, invariantByStage, isAccessState,
  priceSourceText, registryFacts, sameTenantDraft, tenantBudgetBody, tenantBudgetDraftOf, tenantBudgetPath,
  tenantBudgetTone, tenantCostHref, verdictTone, warmPathOf,
} from "./aiGateway.ts";

const STAFF = "7a1b2c3d-4e5f-4a6b-9c8d-1e2f3a4b5c6d";
const TENANT = "3c9e1a2b-4d5f-4a6b-8c7d-0e1f2a3b4c5d";

test("actorLabel never prints a raw staff id (F5)", () => {
  assert.equal(actorLabel({ updated_by: STAFF, updated_by_kind: "staff", updated_by_name: "Mai Tran" }, "x"), "Mai Tran");
  assert.equal(actorLabel({ updated_by: STAFF, updated_by_kind: "staff", updated_by_name: null }, "a staff member"),
    "a staff member");
  // an older API without the kind: the id is still recognised
  assert.equal(actorLabel({ updated_by: `admin:${STAFF}` }, "a staff member"), "a staff member");
  assert.equal(actorLabel({ updated_by: "migration-226", updated_by_name: "Migration 226" }, "x"), "Migration 226");
  assert.equal(actorLabel({ updated_by: "ops-tester" }, "x"), "ops-tester");
  // an API that sends only the raw value (Routing's llm-config rows)
  assert.equal(actorLabel({ updated_by: "migration-280-wm-seed" }, "x"), "Migration 280");
  assert.equal(actorLabel({ updated_by: null }, "x"), null);
});

test("tenant budget draft -> body, and its problems", () => {
  const empty = tenantBudgetDraftOf(null);
  assert.deepEqual(empty, { monthly: "", alert_pct: "100", approval_pct: "150", hard_stop: false, notes: "" });
  assert.deepEqual(tenantBudgetBody(empty).body,
    { monthly_usd: null, alert_pct: 100, approval_pct: 150, hard_stop: false, notes: null });
  assert.deepEqual(tenantBudgetBody({ ...empty, monthly: " 12.345 ", notes: " pilot " }).body,
    { monthly_usd: 12.35, alert_pct: 100, approval_pct: 150, hard_stop: false, notes: "pilot" });
  assert.equal(tenantBudgetBody({ ...empty, monthly: "-1" }).error, "monthly");
  assert.equal(tenantBudgetBody({ ...empty, monthly: "abc" }).error, "monthly");
  assert.equal(tenantBudgetBody({ ...empty, alert_pct: "0" }).error, "alertPct");
  assert.equal(tenantBudgetBody({ ...empty, alert_pct: "1.5" }).error, "alertPct");
  assert.equal(tenantBudgetBody({ ...empty, approval_pct: "1001" }).error, "approvalPct");
  assert.equal(tenantBudgetBody({ ...empty, alert_pct: "120", approval_pct: "110" }).error, "order");
  assert.equal(tenantBudgetBody({ ...empty, notes: "x".repeat(2001) }).error, "notes");
  const row = { monthly_usd: 20, alert_pct: 80, approval_pct: 120, hard_stop: true, notes: null };
  assert.ok(sameTenantDraft(tenantBudgetDraftOf(row), { ...tenantBudgetDraftOf(row), monthly: " 20 " }));
  assert.ok(!sameTenantDraft(tenantBudgetDraftOf(row), { ...tenantBudgetDraftOf(row), hard_stop: false }));
});

test("tenant budget paths, links and tones", () => {
  assert.equal(tenantBudgetPath(TENANT), `/api/admin/ai-gateway/tenant-budgets/${TENANT}`);
  assert.equal(tenantCostHref(TENANT), `/admin/tenants?tenant=${TENANT}&tab=cost`);
  assert.equal(tenantBudgetTone("approval"), "danger");
  assert.equal(tenantBudgetTone("alert"), "warning");
  assert.equal(tenantBudgetTone("unlimited"), "neutral");
  assert.equal(tenantBudgetTone("new-state"), "neutral");
});

test("warm paths are the two direct vendor APIs only", () => {
  assert.equal(warmPathOf({ group: "llm", provider: "anthropic", data_scope: "vendor_direct" }), "anthropic");
  assert.equal(warmPathOf({ group: "llm", provider: "openai", data_scope: "vendor_direct" }), "openai");
  assert.equal(warmPathOf({ group: "llm", provider: "bedrock", data_scope: "aws_only" }), null);
  assert.equal(warmPathOf({ group: "llm", provider: "openai", data_scope: "aws_only" }), null);
  assert.equal(warmPathOf({ group: "decide", provider: "openai" }), null);
});

test("registryFacts: only what the API sends; absent stays absent (hidden)", () => {
  const conns = [{ connection_key: "c1", access_state: "ok", last_probe_at: null, failure_domain: "bedrock:111122223333:aws_only" },
    { connection_key: "c2" }];
  const avail = [{ connection_key: "c2", failure_domain: "openai:openai-direct:vendor_direct" }];
  assert.deepEqual(registryFacts("c1", conns, avail),
    { failure_domain: "bedrock:111122223333:aws_only", access_state: "ok", last_probe_at: null });
  // before migration 322: no access state / probe keys, the domain from the availability read
  assert.deepEqual(registryFacts("c2", conns, avail), { failure_domain: "openai:openai-direct:vendor_direct" });
  assert.deepEqual(registryFacts("c3", conns, avail), {});
  assert.deepEqual(registryFacts(null, conns, avail), {});
  assert.deepEqual(registryFacts("c1", undefined, undefined), {});
});

test("domainParts splits kind, account and scope", () => {
  assert.deepEqual(domainParts("bedrock:111122223333:aws_only"),
    { kind: "bedrock", account: "111122223333", scope: "aws_only" });
  assert.deepEqual(domainParts("x:a:b:c"), { kind: "x", account: "a:b", scope: "c" });
  assert.equal(domainParts("broken"), null);
  assert.equal(domainParts(null), null);
});

test("access state tone and guard", () => {
  assert.equal(accessTone("ok"), "success");
  assert.equal(accessTone("access_denied"), "danger");
  assert.equal(accessTone("quota_exhausted"), "danger");
  assert.equal(accessTone("unconfigured"), "warning");
  assert.equal(accessTone(null), "neutral");
  assert.ok(isAccessState("unknown") && !isAccessState("other"));
});

test("invariantByStage: empty without a report", () => {
  const s = { stage: "s1_judge", role: "judge", ok: false, primary: null, primary_domain: null, other_domains: [],
    reason: "x" };
  assert.deepEqual(Object.keys(invariantByStage({ ready: true, stages: [s] })), ["s1_judge"]);
  assert.deepEqual(invariantByStage({ ready: false, stages: [s] }), {});
  assert.deepEqual(invariantByStage({ ready: true }), {});
  assert.deepEqual(invariantByStage(undefined), {});
});

test("heldByReason groups the dry run's held jobs, biggest first", () => {
  const held = [{ id: "1", kind: "a", error: "", finished_at: null, held: "connection still refused" },
    { id: "2", kind: "a", error: "", finished_at: null, held: "kind refuses retries" },
    { id: "3", kind: "b", error: "", finished_at: null, held: "connection still refused" }];
  assert.deepEqual(heldByReason(held), [{ reason: "connection still refused", jobs: 2 },
    { reason: "kind refuses retries", jobs: 1 }]);
  assert.deepEqual(heldByReason([]), []);
});

test("groupRows keeps extra fields of the rows", () => {
  const out = groupRows([{ group: "llm", key: "a", facts: {} }, { group: "data", key: "b", facts: {} }]);
  assert.equal(out.llm[0].key, "a");
  assert.deepEqual(out.llm[0].facts, {});
  assert.equal(out.data.length, 1);
});

test("verdict tone", () => {
  assert.equal(verdictTone("pass"), "success");
  assert.equal(verdictTone("pass", true), "danger");
  assert.equal(verdictTone("fail"), "danger");
  assert.equal(verdictTone("informative"), "info");
  assert.equal(verdictTone(null), "neutral");
});

test("blocked reason without session tags or capitals (F4)", () => {
  assert.equal(blockedReasonText("S235: RETIRED; KEPT ONLY AS LAST-RESORT FALLBACK"),
    "Retired; kept only as last-resort fallback");
  assert.equal(blockedReasonText("AA-998 retired account"), "Retired account");
  assert.equal(blockedReasonText(""), null);
  assert.equal(blockedReasonText(null), null);
});

test("price source without code files or migrations (F4)", () => {
  const fb = "Built-in price table";
  assert.equal(priceSourceText("pricing.py", fb), fb);
  assert.equal(priceSourceText("Vendor list price (28/09/2026); provider price not published. Migration 169 had "
    + "$0.10/$0.50.", fb), "Vendor list price (28/09/2026); provider price not published");
  assert.equal(priceSourceText("Vendor list price, S246 21/10/2026: partner-priced", fb),
    "Vendor list price, 21/10/2026: partner-priced");
  assert.equal(priceSourceText(null, fb), null);
});

// ── text guards over the page's message files (F9, F10) ─────────────────────────────────────────────────────────

const PAGE_DIR = fileURLToPath(new URL("../app/admin/ai-gateway/", import.meta.url));
const MESSAGE_FILES = readdirSync(PAGE_DIR).filter((n) => /messages\.ts$/.test(n));
// Only the string literals (comments may name tables and files): line comments dropped, then "…" and `…`.
const literals = (src) => {
  const code = src.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  return [...code.matchAll(/"((?:\\.|[^"\\])*)"|`((?:\\.|[^`\\])*)`/g)].map((m) => m[1] ?? m[2]);
};

test("the AI providers messages name no internal table, file or migration (F10)", () => {
  assert.ok(MESSAGE_FILES.length >= 7);
  for (const f of MESSAGE_FILES) {
    for (const s of literals(readFileSync(join(PAGE_DIR, f), "utf8"))) {
      assert.doesNotMatch(s, /\b(?:shared|acp_shared|acp_contract|geo|crm|catalog)\.[a-z_]{3,}\b/, `${f}: "${s}"`);
      assert.doesNotMatch(s, /\b[a-z_]+\.(?:py|sql)\b/, `${f}: "${s}"`);
      assert.doesNotMatch(s, /\bmigration\s+\d{3}\b/i, `${f}: "${s}"`);
    }
  }
});

test("no message still says refused calls are not logged (F9)", () => {
  for (const f of MESSAGE_FILES) {
    const src = readFileSync(join(PAGE_DIR, f), "utf8");
    assert.doesNotMatch(src, /refused[^"]*not logged yet/i, f);
  }
  const jev = readFileSync(join(PAGE_DIR, "JevCreditPanel.tsx"), "utf8");
  assert.doesNotMatch(jev, /hint="shared\./, "the Jev credit tile hint names no table");
});
