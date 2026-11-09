// frontend/lib/aiGatewayAccess.test.mjs
// AA-1025 C1 follow-up: the AI providers page shows a write control only when GET /api/admin/ai-gateway/access says
// the session may use it (backend api/routers/admin_ai_gateway.py PAGE_WRITES). Pure helper mayUse plus text guards:
// every write control of the page reads usePageAccess, the action list matches the backend's, and the provider
// tables fit 1440 px.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { ACCESS_API, ACTION_CAPABILITY, PAGE_ACTIONS, mayUse } from "./aiGateway.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const ADMIN = { read_only: false, actions: Object.fromEntries(PAGE_ACTIONS.map((a) => [a, true])) };
const READ_ONLY = { read_only: true, actions: Object.fromEntries(PAGE_ACTIONS.map((a) => [a, false])) };

test("mayUse: the API's answer decides", () => {
  for (const a of PAGE_ACTIONS) {
    assert.equal(mayUse(ADMIN, a), true, a);
    assert.equal(mayUse(READ_ONLY, a), false, a);
    // the role fallback is ignored once the API answered
    assert.equal(mayUse(READ_ONLY, a, false, () => true), false, a);
  }
});

test("mayUse: hidden while loading, and for an action an older API does not send", () => {
  assert.equal(mayUse(undefined, "requeue"), false);
  assert.equal(mayUse(undefined, "requeue", false, () => true), false);
  assert.equal(mayUse({ read_only: false, actions: {} }, "provider_switch"), false);
});

test("mayUse: when the read failed the role check decides", () => {
  assert.equal(mayUse(undefined, "budgets", true, (a) => a === "budgets"), true);
  assert.equal(mayUse(undefined, "routing", true, (a) => a === "budgets"), false);
  assert.equal(mayUse(undefined, "routing", true), false);
});

test("the action list and the route match the backend", () => {
  const py = read("../../api/routers/admin_ai_gateway.py");
  const block = py.slice(py.indexOf("PAGE_WRITES"), py.indexOf("def page_access"));
  const keys = [...block.matchAll(/^\s+"([a-z_]+)": \(/gm)].map((m) => m[1]);
  assert.deepEqual([...keys].sort(), [...PAGE_ACTIONS].sort());
  assert.ok(py.includes('@router.get("/access"'));
  assert.equal(ACCESS_API, "/api/admin/ai-gateway/access");
  assert.deepEqual(Object.keys(ACTION_CAPABILITY).sort(), [...PAGE_ACTIONS].sort());
});

test("every write control of the page reads usePageAccess", () => {
  const files = {
    "ProvidersTab.tsx": ['can("provider_switch")', 'can("requeue")'],
    "ConnectionsPanel.tsx": ['can("connections")', 'can("connection_test")'],
    "JevCreditPanel.tsx": ['can("jev_credit")'],
    "ProviderCreditPanel.tsx": ['can("credit")', 'can("connections")'],
    "BudgetsTab.tsx": ['can("budgets")'],
    "RoutingTab.tsx": ['can("routing")'],
    "CatalogTable.tsx": ['can("catalog")'],
  };
  for (const [file, needles] of Object.entries(files)) {
    const src = read(`../app/admin/ai-gateway/${file}`);
    assert.ok(src.includes("usePageAccess"), file);
    for (const n of needles) assert.ok(src.includes(n), `${file}: ${n}`);
  }
  const tab = read("../app/admin/ai-gateway/ProvidersTab.tsx");
  // the switch handler is passed only when the session may switch; the re-queue button only when it may re-queue
  assert.match(tab, /onSwitch=\{canSwitch \? askSwitch : undefined\}/);
  assert.match(tab, /actions=\{canRun \?/);
});

test("the provider tables fit the 1440 px column with room to spare", () => {
  const tab = read("../app/admin/ai-gateway/ProvidersTab.tsx");
  const m = tab.match(/PROVIDER_TABLE_MIN_WIDTH = (\d+);/);
  assert.ok(m);
  // 1440 - 236 sidebar - 72 page padding - 6 scrollbar - 2 card border = 1124; keep >= 20 px (lessons: Frontend)
  assert.ok(Number(m[1]) <= 1124 - 20, `min width ${m[1]}`);
  assert.ok(!/tableMinWidth=\{1160\}/.test(tab));
});
