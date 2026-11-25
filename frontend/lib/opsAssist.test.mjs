// AA-1154 (A0, A1): the pure helpers of the assistant panel (lib/opsAssist.ts) and the panel's wiring on the Ecosystem
// status page (no button runs an action: the only POSTs ask the AI to rank / explain / phrase; kit components only).
// Fixtures are invented. Run: npm run test:unit.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  SIGNALS_PATH, analysePath, autoPath, callText, cardsPath, changesPath, citedEvidence, confidenceTone,
  costText, expired, explanationEvidence, extraSignals, llmNotice, ranked, riskTone, windowWords,
} from "./opsAssist.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");

const card = (over = {}) => ({
  id: "x:observe", rank: 1, action: "observe", title: "t", reason: "r", evidence: ["a", "zz", "b"],
  risk: { level: "R0", axes: { direction: "read", scope: "all", money: "none", reversible: true }, why: [] },
  predictions: [], if_nothing: "", way_back: { text: "", call: null }, cost: { usd: 0, basis: "" },
  approval: { who: "", capability: [], roles: [], needs_reason: false, reason_min_chars: null, second_person: false },
  call: null, dry_run: null, copy_only: false, runbook: null, copy_text: null, precondition: null,
  precondition_met: true, check_after: [], expires_at: "2026-11-20T03:30:00+00:00", ...over,
});

test("paths: the cards of an item follow the page's preset range; a custom range reads 24 h", () => {
  assert.equal(cardsPath("check-llm_stage_coverage.t2_judge", "6h"),
    "/api/admin/ops-assist/items/check-llm_stage_coverage.t2_judge/cards?range=6h");
  assert.equal(cardsPath("a b", "custom"), "/api/admin/ops-assist/items/a%20b/cards?range=24h");
  assert.equal(SIGNALS_PATH, "/api/admin/ops-assist/signals");
});

test("tones: risk and confidence", () => {
  assert.deepEqual(["R0", "R1", "R2", "R3", "R4"].map(riskTone), ["neutral", "info", "warning", "danger", "danger"]);
  assert.deepEqual(["code", "measured", "history", "not_enough_data"].map(confidenceTone),
    ["info", "success", "success", "neutral"]);
});

test("order, cited evidence, expiry, call text", () => {
  assert.deepEqual(ranked([card({ rank: 3, id: "c" }), card({ rank: 1, id: "a" })]).map((c) => c.id), ["a", "c"]);
  const ev = [{ id: "b", label: "B" }, { id: "a", label: "A" }];
  assert.deepEqual(citedEvidence(card(), ev).map((e) => e.id), ["a", "b"]);
  assert.equal(expired(card(), new Date("2026-11-20T03:29:00Z")), false);
  assert.equal(expired(card(), new Date("2026-11-20T03:30:00Z")), true);
  assert.equal(callText({ method: "POST", path: "/admin/waves/w/pause" }), "POST /admin/waves/w/pause");
  assert.equal(callText(null), "");
});

test("signals already on the Needs-attention list are not repeated", () => {
  const s = [{ id: "llm-conn-zz1" }, { id: "llm-spend-openai-direct" }];
  assert.deepEqual(extraSignals(s, ["llm-conn-zz1"]).map((x) => x.id), ["llm-spend-openai-direct"]);
});

test("A1 paths: analyse, auto, changes", () => {
  assert.equal(analysePath("llm-conn-zz1", "6h", "quick", false),
    "/api/admin/ops-assist/items/llm-conn-zz1/analyse?range=6h&depth=quick");
  assert.equal(analysePath("a b", "custom", "deep", true),
    "/api/admin/ops-assist/items/a%20b/analyse?range=24h&depth=deep&fresh=true");
  assert.equal(autoPath("1h"), "/api/admin/ops-assist/auto?range=1h");
  assert.equal(autoPath("zz"), "/api/admin/ops-assist/auto?range=24h");
  // the windows are a setting (minutes); null = the API's default, a bad value is never sent
  assert.equal(changesPath(60), "/api/admin/ops-assist/changes?window=60");
  assert.equal(changesPath(15, true), "/api/admin/ops-assist/changes/phrase?window=15");
  assert.equal(changesPath(null), "/api/admin/ops-assist/changes");
  assert.equal(changesPath(1.5), "/api/admin/ops-assist/changes");
  assert.deepEqual([15, 60, 180, 1440].map(windowWords), [{ unit: "minutes", n: 15 }, { unit: "hour", n: 1 },
    { unit: "hours", n: 3 }, { unit: "hours", n: 24 }]);
});

test("A1 windows come from the API, not a page constant", () => {
  const src = read("app/admin/overview/_status/AssistLookup.tsx");
  assert.ok(src.includes(".windows") && !/\b(15|360|1440)\b/.test(src), "AssistLookup builds its choices from the API");
});

test("a read-only session sends no assistant AI request (the E2E identity, a viewer, view-as)", () => {
  const assist = read("app/admin/overview/_status/Assist.tsx");
  const hook = read("app/admin/overview/_status/useMayAsk.ts");
  assert.ok(hook.includes("useSessionAccess()") && /settled && !readOnly/.test(hook), "waits for the session answer");
  assert.ok(assist.includes("useMayAsk()"), "Assist reads it");
  assert.ok(/enabled:\s*mayAsk\b/.test(assist), "the automatic analysis waits for a writable session");
  assert.ok(/enabled:\s*open && mayAsk\b/.test(assist), "the drawer asks the AI only for a writable session");
  assert.ok(assist.includes("<WriteOnly>"), "Deep / Again are hidden for a read-only session");
  const lookup = read("app/admin/overview/_status/AssistLookup.tsx");
  assert.ok(lookup.includes("<WriteOnly>") && /enabled:[^\n]*mayAsk/.test(lookup), "Phrase with AI is hidden too");
});

test("A1 notices: what the model did, or why the cards keep the rules' order", () => {
  assert.equal(llmNotice(undefined), "llmNone");
  assert.equal(llmNotice({ used: false, calls: 0 }), "llmNone");
  assert.equal(llmNotice({ used: true, calls: 1 }), "llmUsed");
  assert.equal(llmNotice({ used: true, calls: 1, cached: true }), "llmCached");
  assert.equal(llmNotice({ used: false, calls: 0, error: "switched_off" }), "llmOff");
  assert.equal(llmNotice({ used: false, calls: 0, error: "budget" }), "llmBudget");
  assert.equal(llmNotice({ used: false, calls: 0, error: "no_cap" }), "llmBudget");
  assert.equal(llmNotice({ used: false, calls: 0, error: "not_configured" }), "llmNotSet");
  assert.equal(llmNotice({ used: true, calls: 1, error: "unreadable_reply" }), "llmFailed");
  assert.equal(costText(0.00314), "0.0031 USD");
  assert.equal(costText(undefined), "0.0000 USD");
});

test("A1 explanation evidence keeps the cited order and drops unknown ids", () => {
  const ev = [{ id: "b", label: "B" }, { id: "a", label: "A" }];
  assert.deepEqual(explanationEvidence(card({ explanation: { text: "t", evidence: ["a", "zz", "b"] } }), ev)
    .map((e) => e.id), ["a", "b"]);
  assert.deepEqual(explanationEvidence(card(), ev), []);
});

test("the panel runs no action, uses the kit and is wired on the attention rows", () => {
  for (const f of ["app/admin/overview/_status/Assist.tsx", "app/admin/overview/_status/AssistLookup.tsx"]) {
    const src = read(f);
    assert.ok(src.includes('from "../../../_kit"'), `${f}: kit components`);
    assert.ok(!/apiPost|apiPut|apiPatch|apiDelete|method:\s*"(PUT|PATCH|DELETE)"/.test(src), `${f}: no write verb`);
    // The only POSTs ask the AI (analyse, auto, phrase); a card's own API call is shown, never sent.
    for (const m of src.matchAll(/apiSend<[^>]+>\(([^,]+),/g)) {
      assert.ok(/^(analysePath|autoPath|changesPath)\(/.test(m[1].trim()), `${f}: unexpected POST ${m[1]}`);
    }
    assert.ok(!/apiSend[^;]*card\.(call|dry_run|way_back)/.test(src), `${f}: a card's call is never sent`);
  }
  const src = read("app/admin/overview/_status/Assist.tsx");
  assert.ok(src.includes("cardsPath(") && src.includes("SIGNALS_PATH") && src.includes("analysePath("));
  const verdict = read("app/admin/overview/_status/Verdict.tsx");
  assert.ok(verdict.includes("<AssistButton") && verdict.includes("<AssistSignals"));
  const msgs = read("app/admin/overview/assist.messages.ts").split("\n").filter((l) => !l.trim().startsWith("//"))
    .join("\n");
  assert.ok(!/AA-\d+/.test(msgs), "no tracker ids in UI copy");
  const rbac = read("lib/rbac.ts");
  assert.ok(rbac.includes('{ prefix: "ops-assist", caps: ["ops.read"], roles: ["admin"] }'));
});
