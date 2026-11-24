// AA-1154 (A0): the pure helpers of the assistant panel (lib/opsAssist.ts) and the panel's wiring on the Ecosystem
// status page (read-only: no button runs an action; kit components only). Fixtures are invented. Run: npm run test:unit.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  SIGNALS_PATH, callText, cardsPath, citedEvidence, confidenceTone, expired, extraSignals, ranked, riskTone,
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
  const s = [{ id: "llm-conn-acc1" }, { id: "llm-spend-openai-direct" }];
  assert.deepEqual(extraSignals(s, ["llm-conn-acc1"]).map((x) => x.id), ["llm-spend-openai-direct"]);
});

test("the panel is read-only, uses the kit and is wired on the attention rows", () => {
  const src = read("app/admin/overview/_status/Assist.tsx");
  assert.ok(src.includes('from "../../../_kit"'), "kit components");
  assert.ok(!/apiPost|apiPut|apiPatch|apiDelete|method:\s*"(POST|PUT|PATCH|DELETE)"/.test(src), "no write in A0");
  assert.ok(src.includes("cardsPath(") && src.includes("SIGNALS_PATH"));
  const verdict = read("app/admin/overview/_status/Verdict.tsx");
  assert.ok(verdict.includes("<AssistButton") && verdict.includes("<AssistSignals"));
  const msgs = read("app/admin/overview/assist.messages.ts").split("\n").filter((l) => !l.trim().startsWith("//"))
    .join("\n");
  assert.ok(!/AA-\d+/.test(msgs), "no tracker ids in UI copy");
  const rbac = read("lib/rbac.ts");
  assert.ok(rbac.includes('{ prefix: "ops-assist", caps: ["ops.read"], roles: ["admin"] }'));
});
