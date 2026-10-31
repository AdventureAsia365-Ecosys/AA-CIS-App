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
  REQUEUE_BATCH, accessTone, actorLabel, blockedReasonText, domainParts, groupRows, heldGroups, invariantByStage,
  isAccessState, nextRequeueSince, priceSourceText, registryFacts, requeueBody, sameTenantDraft, statusWordKey,
  tenantBudgetBody, tenantBudgetDraftOf, tenantBudgetPath, tenantBudgetTone, tenantCostHref, verdictTone, warmPathOf,
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

test("heldGroups groups the dry run's held jobs by why, never keeping the raw text", () => {
  const blocked = { "acc7": "access_denied", "acc70": "quota_exhausted", "vendor-x": "throttled" };
  const job = (id, kind, held, finished_at = null) => ({ id, kind, error: "", finished_at, held });
  const held = [
    job("1", "writer_run", "connection still refused: acc7 (access_denied)"),
    job("2", "writer_run", "connection still refused: acc7 (access_denied)"),
    job("3", "judge_run", "a connection is still refused with the same class: acc70 (quota_exhausted)"),
    job("4", "cleanup_run", "kind is not registered"),
    job("5", "erase_run", "an apply is never retried: start a new check"),
    job("6", "erase_run", "an apply is never retried: start a new check"),
    job("7", "writer_run", "connection still refused: acc7 (access_denied), vendor-x (throttled)"),
  ];
  const out = heldGroups(held, blocked);
  assert.deepEqual(out, [
    { type: "connection", keys: ["acc7"], classes: ["access_denied"], kind: null, jobs: 2 },
    { type: "kind_refuses", keys: [], classes: [], kind: "erase_run", jobs: 2 },
    { type: "connection", keys: ["acc7", "vendor-x"], classes: ["access_denied", "throttled"], kind: null, jobs: 1 },
    { type: "same_class", keys: ["acc70"], classes: ["quota_exhausted"], kind: null, jobs: 1 },
    { type: "kind_unknown", keys: [], classes: [], kind: "cleanup_run", jobs: 1 },
  ]);
  // whole words: "acc7" is not found inside "acc70"
  assert.deepEqual(heldGroups([job("8", "a", "connection still refused: acc70 (quota_exhausted)")], blocked)[0].keys,
    ["acc70"]);
  // no group carries the backend sentence
  assert.ok(out.every((g) => !("reason" in g)));
  assert.deepEqual(heldGroups([], blocked), []);
});

test("re-queue batches: the dry run asks for what one apply does", () => {
  assert.equal(REQUEUE_BATCH, 50);
  assert.deepEqual(requeueBody(true), { dry_run: true, limit: 50 });
  assert.deepEqual(requeueBody(false, "2026-03-01T08:00:00+00:00"),
    { dry_run: false, limit: 50, since: "2026-03-01T08:00:00+00:00" });
  assert.deepEqual(requeueBody(false, null), { dry_run: false, limit: 50 });
});

test("nextRequeueSince starts the next batch where this one ended", () => {
  const since = "2026-03-01T00:00:00+00:00";
  const j = (finished_at) => ({ id: finished_at ?? "x", kind: "k", error: "", finished_at });
  const r = { more: true, since, jobs: [j("2026-03-02T10:00:00.123456+00:00")],
    held: [j("2026-03-03T09:00:00+00:00"), j(null)] };
  assert.equal(nextRequeueSince(r), "2026-03-03T09:00:00+00:00");
  assert.equal(nextRequeueSince({ ...r, more: false }), null);
  // the cursor would not move: no next batch to offer
  assert.equal(nextRequeueSince({ more: true, since, jobs: [j(since)], held: [] }), null);
  assert.equal(nextRequeueSince({ more: true, since, jobs: [], held: [] }), null);
});

test("statusWordKey: 'No recent calls' only for an LLM connection with no reason (F8)", () => {
  assert.equal(statusWordKey({ group: "llm", status: "unknown", status_reason: null }), "no_recent_calls");
  assert.equal(statusWordKey({ group: "llm", status: "unknown", status_reason: "source_unreadable" }), "unknown");
  assert.equal(statusWordKey({ group: "decide", status: "unknown", status_reason: "no_topup_recorded" }), "unknown");
  assert.equal(statusWordKey({ group: "data", status: "unknown", status_reason: "no_balance_snapshot" }), "unknown");
  assert.equal(statusWordKey({ group: "data", status: "unknown", status_reason: null }), "unknown");
  assert.equal(statusWordKey({ group: "llm", status: "ok", status_reason: null }), "ok");
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

test("the re-queue dialog never prints the backend's held reason text (connection keys, class codes)", () => {
  const src = readFileSync(join(PAGE_DIR, "ProvidersTab.tsx"), "utf8");
  assert.ok(!/\.held\s*\}|\{\s*h\.reason|\.reason\s*\|\|/.test(src), "held reason rendered verbatim");
  assert.ok(src.includes("heldGroups("), "held jobs go through heldGroups");
});
