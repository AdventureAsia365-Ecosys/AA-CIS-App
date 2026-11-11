// frontend/lib/aiGatewayBatches.test.mjs — AA-1076: the Batches tab of AI providers (the gateway batch lane).
// Pure helpers of lib/aiGateway.ts (the send body, the tour-id parser, the phase tone, paths) plus text guards: the
// tab is wired into the page, its routes match the backend router, and the UI copy names no issue id.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  BATCHES_API, BATCH_DRAFT, BATCH_MAX_TOURS, TAB_KEYS, batchCancelPath, batchPhaseTone, batchSendBody, parseTab,
  parseTourIds,
} from "./aiGateway.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const A = "6f1c5f8e-1b9e-4c55-9f5e-1a2b3c4d5e6f";
const B = "7a2d6e9f-2c0f-4d66-8a1b-2b3c4d5e6f70";

test("the tab exists and parses", () => {
  assert.ok(TAB_KEYS.includes("batches"));
  assert.equal(parseTab("batches"), "batches");
});

test("tour ids: one per line or comma separated, deduplicated, bad ones named", () => {
  assert.deepEqual(parseTourIds(`${A}\n${B.toUpperCase()}, ${A}`), { ids: [A, B], bad: [] });
  assert.deepEqual(parseTourIds("x, " + A), { ids: [A], bad: ["x"] });
  assert.deepEqual(parseTourIds("  \n "), { ids: [], bad: [] });
});

test("the send body, or why it cannot be sent", () => {
  assert.equal(batchSendBody({ ...BATCH_DRAFT, tours: "" }), "tours");
  assert.equal(batchSendBody({ ...BATCH_DRAFT, tours: "nope" }), "tour_ids_bad");
  assert.equal(batchSendBody({ ...BATCH_DRAFT, tours: A, cap: "0" }), "cap");
  assert.equal(batchSendBody({ ...BATCH_DRAFT, tours: A, cap: "101" }), "cap");
  assert.equal(batchSendBody({ ...BATCH_DRAFT, tours: A, runBatchId: "x" }), "run_batch");
  const many = Array.from({ length: BATCH_MAX_TOURS + 1 },
    (_, i) => `00000000-0000-0000-0000-${String(i).padStart(12, "0")}`).join("\n");
  assert.equal(batchSendBody({ ...BATCH_DRAFT, tours: many }), "too_many");
  assert.deepEqual(batchSendBody({ ...BATCH_DRAFT, tours: `${A}\n${B}`, cap: "0.20" }),
    { consumer: "s1_attempt1", tour_ids: [A, B], cap_usd: 0.2, dry_run: true });
  assert.deepEqual(batchSendBody({ ...BATCH_DRAFT, tours: A, dryRun: false, drill: true, runBatchId: B }),
    { consumer: "s1_attempt1", tour_ids: [A], cap_usd: 0.2, dry_run: false, batch_id: B, drill_withhold: 1 });
});

test("an estimate is the default: sending starts at $0", () => {
  assert.equal(BATCH_DRAFT.dryRun, true);
  assert.ok(Number(BATCH_DRAFT.cap) <= 0.2);
});

test("phase tones", () => {
  assert.equal(batchPhaseTone({ phase: "done", job_status: "succeeded" }), "success");
  assert.equal(batchPhaseTone({ phase: "refused", job_status: "failed" }), "danger");
  assert.equal(batchPhaseTone({ phase: "polling", job_status: "queued" }), "accent");
  assert.equal(batchPhaseTone({ phase: "estimated", job_status: "succeeded" }), "info");
});

test("the paths match the backend router", () => {
  const py = read("../../api/routers/admin_ai_gateway.py");
  assert.ok(py.includes('@router.get("/batches"') && py.includes('@router.post("/batches"'));
  assert.ok(py.includes('@router.post("/batches/{job_id}/cancel"'));
  assert.equal(BATCHES_API, "/api/admin/ai-gateway/batches");
  assert.equal(batchCancelPath(A), `/api/admin/ai-gateway/batches/${A}/cancel`);
});

test("the page shows the tab, and the copy names no issue id", () => {
  const page = read("../app/admin/ai-gateway/AiGateway.tsx");
  assert.ok(page.includes('{ key: "batches", label: t("tabBatches") }') && page.includes("<BatchesTab />"));
  const copy = read("../app/admin/ai-gateway/batches.messages.ts");
  assert.ok(!/AA-\d+/.test(copy.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n")));
});

test("the messages have the same keys in English and Vietnamese, and the tab uses kit controls only", () => {
  const src = read("../app/admin/ai-gateway/batches.messages.ts");
  const at = src.indexOf("\n  {\n", src.indexOf("defineMessages("));
  const split = src.indexOf("\n  {\n", at + 1);
  const keys = (part) => [...part.matchAll(/^ {4}([A-Za-z_]+):/gm)].map((m) => m[1]).sort();
  const en = keys(src.slice(at, split));
  assert.ok(en.length > 40);
  assert.deepEqual(keys(src.slice(split)), en);
  const tab = read("../app/admin/ai-gateway/BatchesTab.tsx");
  assert.doesNotMatch(tab, /<(button|select|input|textarea|table)\b/, "kit controls only");
  assert.match(tab, /usePageAccess\(\)\.can\("batches"\)/);
});
