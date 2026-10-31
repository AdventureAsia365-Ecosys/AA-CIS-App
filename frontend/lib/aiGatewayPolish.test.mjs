// frontend/lib/aiGatewayPolish.test.mjs
// S262 C4 polish (AA-804 live findings N1–N4): the wording helpers of lib/aiGateway.ts (actor of a login value, error
// classes in words, the read-only refusal, the failure-domain account) and text guards over the AI providers page,
// the Cost Operations tree and the Tenants detail tab strip.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ERROR_CLASSES, READ_ONLY_REFUSAL, actorLabel, domainAccountText, errorClassKey, isReadOnlyRefusal,
} from "./aiGateway.ts";

const STAFF = "7a1b2c3d-4e5f-4a6b-9c8d-1e2f3a4b5c6d";
const login = (ref) => `staff (${ref})`;
const PAGE_DIR = fileURLToPath(new URL("../app/admin/ai-gateway/", import.meta.url));
const APP_DIR = fileURLToPath(new URL("../app/admin/", import.meta.url));
const read = (dir, f) => readFileSync(join(dir, f), "utf8");

test("actorLabel: a '<username>:<reference>' value is never printed raw (N4)", () => {
  // the backend named the staff member through the directory
  assert.equal(actorLabel({ updated_by: "mai:ops12", updated_by_kind: "login", updated_by_name: "Mai Tran" },
    "a staff member", login), "Mai Tran");
  // not a staff username: "staff (<reference>)"
  assert.equal(actorLabel({ updated_by: "bao:ops12", updated_by_kind: "login", updated_by_name: null },
    "a staff member", login), "staff (ops12)");
  // an older API that sent it as free text ("other", the name = the raw value)
  assert.equal(actorLabel({ updated_by: "bao:ops12", updated_by_kind: "other", updated_by_name: "bao:ops12" },
    "a staff member", login), "staff (ops12)");
  // only the raw value (Routing's llm-config rows)
  assert.equal(actorLabel({ updated_by: "Bao:OPS12" }, "a staff member", login), "staff (OPS12)");
  // no login words given: the staff fallback, still not the raw value
  assert.equal(actorLabel({ updated_by: "bao:ops12" }, "a staff member"), "a staff member");
  // unchanged kinds
  assert.equal(actorLabel({ updated_by: `admin:${STAFF}` }, "a staff member", login), "a staff member");
  assert.equal(actorLabel({ updated_by: "migration-226" }, "x", login), "Migration 226");
  assert.equal(actorLabel({ updated_by: "Break-glass", updated_by_kind: "other", updated_by_name: "Break-glass" },
    "x", login), "Break-glass");
  assert.equal(actorLabel({ updated_by: "note: two words" }, "x", login), "note: two words");
  assert.equal(actorLabel({ updated_by: "" }, "x", login), null);
});

test("error classes: the list matches the ledger's, every class has words in EN and VI (N4)", () => {
  const py = readFileSync(fileURLToPath(new URL("../../shared/llm_client/call_log.py", import.meta.url)), "utf8");
  const m = /ERROR_CLASSES = \(([^)]*)\)/s.exec(py);
  assert.ok(m, "call_log.py ERROR_CLASSES not found");
  const backend = [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  assert.deepEqual([...ERROR_CLASSES].sort(), backend.sort());

  const src = read(PAGE_DIR, "messages.ts");
  const block = /export const ERROR_CLASS = defineMessages\(([\s\S]*?)\n\);/.exec(src);
  assert.ok(block, "messages.ts ERROR_CLASS not found");
  for (const cls of ERROR_CLASSES) {
    const n = [...block[1].matchAll(new RegExp(`\\b${cls}: "([^"]+)"`, "g"))];
    assert.equal(n.length, 2, `${cls}: one English and one Vietnamese text`);
    for (const [, words] of n) assert.doesNotMatch(words, /_/, `${cls} reads as words, not a code`);
  }
});

test("errorClassKey: a known class, an unknown one reads 'other', none is null", () => {
  assert.equal(errorClassKey("access_denied"), "access_denied");
  assert.equal(errorClassKey("deadline"), "deadline");
  assert.equal(errorClassKey("a_future_class"), "other");
  assert.equal(errorClassKey(null), null);
  assert.equal(errorClassKey(""), null);
});

test("isReadOnlyRefusal: only the E2E session's 403 code", () => {
  const err = (msg, status) => Object.assign(new Error(msg), status === undefined ? {} : { status });
  assert.equal(READ_ONLY_REFUSAL, "e2e_read_only");
  assert.ok(isReadOnlyRefusal(err("e2e_read_only", 403)));
  assert.ok(isReadOnlyRefusal(err("e2e_read_only")));
  assert.ok(!isReadOnlyRefusal(err("e2e_read_only", 500)));
  assert.ok(!isReadOnlyRefusal(err("Forbidden", 403)));
  assert.ok(!isReadOnlyRefusal("e2e_read_only"));
  assert.ok(!isReadOnlyRefusal(null));
});

test("domainAccountText: never a connection key (N4)", () => {
  const words = { own: "this platform's AWS account", vendor: "its own vendor account" };
  const labels = { "bedrock-north": "Bedrock, north account (assume role)" };
  assert.equal(domainAccountText("bedrock", "444455556666", words, { labels }), "444455556666");
  assert.equal(domainAccountText("openai", "openai-direct", words), "OpenAI API");
  assert.equal(domainAccountText("anthropic", "anthropic-direct", words), "Anthropic API");
  // a connection key in the account part: its registry label
  assert.equal(domainAccountText("bedrock", "bedrock-north", words, { labels }), "Bedrock, north account (assume role)");
  // no label: the row's own name, unless that is the key itself
  assert.equal(domainAccountText("bedrock", "bedrock-north", words, { ownName: "Bedrock, north" }), "Bedrock, north");
  assert.equal(domainAccountText("bedrock", "bedrock-north", words, { ownName: "bedrock-north" }), words.own);
  assert.equal(domainAccountText("bedrock", "BEDROCK_ACCT_X", words), words.own);
  assert.equal(domainAccountText("typesafe", "jev", words), words.vendor);
});

test("the page prints no raw error class, and the re-queue card says 'read-only' in words (N4)", () => {
  for (const f of ["ProvidersTab.tsx", "CallsTab.tsx", "TraceDrawer.tsx", "ConnectionsPanel.tsx"]) {
    const src = read(PAGE_DIR, f);
    assert.doesNotMatch(src, /\{\s*[\w.]*\berror_class\s*\}/, `${f}: an error class rendered as is`);
    assert.doesNotMatch(src, /\[who, e\.error_class/, `${f}: an error class joined as is`);
    assert.doesNotMatch(src, /t\("lastError", h\.last_error_class\)/, `${f}: an error class passed as is`);
  }
  const providers = read(PAGE_DIR, "ProvidersTab.tsx");
  assert.ok(providers.includes("isReadOnlyRefusal(e)"), "the re-queue card recognises the read-only refusal");
  const msgs = read(PAGE_DIR, "messages.ts");
  assert.equal([...msgs.matchAll(/requeueReadOnly:/g)].length, 2, "requeueReadOnly in EN and VI");
});

test("the Calls table has no inner height cap (N2)", () => {
  const src = read(PAGE_DIR, "CallsTab.tsx");
  assert.doesNotMatch(src, /maxBodyHeight/);
});

test("the Cost Operations tree keeps the cost on the right of a wrapping row (N1)", () => {
  const src = read(APP_DIR, "cost-ops/page.tsx");
  assert.match(src, /const TREE_MID = \{[^}]*minWidth: 0[^}]*flexWrap: "wrap"/s);
  assert.match(src, /const TREE_COST = \{[^}]*flexShrink: 0[^}]*whiteSpace: "nowrap"/s);
  assert.equal([...src.matchAll(/style=\{\{ \.\.\.TREE_COST/g)].length, 2, "group and node cost both use TREE_COST");
});

test("the Tenants detail tab strip is the kit Tabs (N3)", () => {
  const src = read(APP_DIR, "tenants/page.tsx");
  assert.match(src, /<Tabs variant="underline"[\s\S]{0,400}?active=\{tab\}/);
  assert.doesNotMatch(src, /TABS\.map\(t => \(\s*<button/);
});
