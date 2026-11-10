// frontend/lib/claimLabels.test.mjs — AA-1063: the pure helpers of Writing memory › Claim labels (lib/claimLabels.ts)
// agree with the backend (api/routers/admin_claim_labels.py, services/ground_truth/review.py), and the tab's messages
// have both languages, no issue ids and no window.confirm. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  ANSWERS, ANSWER_OF_LABEL, BULK_MAX, CLAIM_API, LABEL_OF_ANSWER, acceptable, acceptableCount, chunks, claimsUrl,
  deciderLabel, facetCounts, labelTone, note, parseSetKey, proposalReason, reviewPageOffset, reviewUrl, setKey,
  splitMark,
} from "./claimLabels.ts";

const read = (p) => readFileSync(new URL(`../${p}`, import.meta.url), "utf8");

test("answers map to the labels the API writes (correct = ok, wrong = violation)", () => {
  assert.deepEqual([...ANSWERS], ["correct", "wrong", "unsure"]);
  for (const a of ANSWERS) assert.equal(ANSWER_OF_LABEL[LABEL_OF_ANSWER[a]], a);
  assert.equal(LABEL_OF_ANSWER.wrong, "violation");
  assert.equal(BULK_MAX, 500);    // review.MAX_BULK
});

test("set keys round-trip; a set id may hold an @ (t2@2)", () => {
  assert.equal(setKey("CL-1", "1.1.0"), "CL-1@1.1.0");
  assert.deepEqual(parseSetKey("CL-1@1.1.0"), { set_id: "CL-1", set_version: "1.1.0" });
  assert.deepEqual(parseSetKey("t2@2@1.0.0"), { set_id: "t2@2", set_version: "1.0.0" });
  assert.equal(parseSetKey(""), null);
  assert.equal(parseSetKey("CL-1@"), null);
});

test("review and claims URLs carry only the filters that are set", () => {
  const f = { set: "CL-1@1.1.0", writer: "", proposal: "none", state: "open" };
  const u = new URL(reviewUrl(f, 25), "http://x");
  assert.equal(u.pathname, `${CLAIM_API}/review`);
  assert.deepEqual(Object.fromEntries(u.searchParams), { set_id: "CL-1", set_version: "1.1.0", proposal: "none",
    state: "open", limit: "25", offset: "25" });
  const ids = new URL(reviewUrl({ ...f, set: "" }, 0, 100, false), "http://x");
  assert.equal(ids.searchParams.get("context"), "false");
  assert.equal(ids.searchParams.get("set_id"), null);
  const c = new URL(claimsUrl("t2@2", "1.0.0", { source: "tech_lead", label: "", writer: "t2", split: "",
    relabelled: "yes", q: "  Ostrin  " }, 50), "http://x");
  assert.equal(c.pathname, `${CLAIM_API}/t2%402/1.0.0/claims`);
  assert.deepEqual(Object.fromEntries(c.searchParams), { source: "tech_lead", writer: "t2", relabelled: "true",
    q: "Ostrin", limit: "50", offset: "50" });
});

test("the unsupported part is marked once, case-insensitively, else the sentence stays whole", () => {
  assert.deepEqual(splitMark("Walk 40 km to Ostrin Valley.", "40 KM"),
    [{ text: "Walk ", mark: false }, { text: "40 km", mark: true }, { text: " to Ostrin Valley.", mark: false }]);
  assert.deepEqual(splitMark("Walk 40 km.", "12 km"), [{ text: "Walk 40 km.", mark: false }]);
  assert.deepEqual(splitMark("Walk 40 km.", null), [{ text: "Walk 40 km.", mark: false }]);
  assert.deepEqual(splitMark("Velmora", "Velmora"), [{ text: "Velmora", mark: true }]);
});

const FACETS = [
  { set_id: "CL-1", set_version: "1.0.0", status: "resolved", writer: "t9_blog", proposal: "ok", n: 80 },
  { set_id: "CL-1", set_version: "1.1.0", status: "open", writer: "t9_blog", proposal: "ok", n: 150 },
  { set_id: "CL-1", set_version: "1.1.0", status: "open", writer: "t9_social", proposal: "violation", n: 20 },
  { set_id: "CL-1", set_version: "1.1.0", status: "open", writer: "t9_blog", proposal: "none", n: 30 },
  { set_id: "CL-1", set_version: "1.1.0", status: "resolved", writer: "t9_blog", proposal: "ok", n: 5 },
];

test("facets give the set list and the counts within the chosen set", () => {
  const all = facetCounts(FACETS, "");
  assert.deepEqual(all.sets, [{ key: "CL-1@1.0.0", n: 80 }, { key: "CL-1@1.1.0", n: 205 }]);
  assert.deepEqual(all.state, { resolved: 85, open: 200 });
  const one = facetCounts(FACETS, "CL-1@1.1.0");
  assert.deepEqual(one.state, { open: 200, resolved: 5 });
  assert.deepEqual(one.proposal, { ok: 150, violation: 20, none: 30 });
  assert.deepEqual(one.writer, { t9_blog: 180, t9_social: 20 });
});

test("accept every remaining proposal: open items with a proposal under the filters", () => {
  assert.equal(acceptableCount(FACETS, { set: "", writer: "", proposal: "" }), 170);
  assert.equal(acceptableCount(FACETS, { set: "CL-1@1.1.0", writer: "t9_social", proposal: "" }), 20);
  assert.equal(acceptableCount(FACETS, { set: "CL-1@1.1.0", writer: "", proposal: "violation" }), 20);
  assert.equal(acceptableCount(FACETS, { set: "CL-1@1.0.0", writer: "", proposal: "" }), 0);
  assert.deepEqual(acceptable([{ item_id: "a", status: "open", proposal: { label: "ok" } },
    { item_id: "b", status: "open", proposal: null }, { item_id: "c", status: "resolved", proposal: { label: "ok" } }]),
  ["a"]);
  assert.deepEqual(chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
  assert.deepEqual(chunks([]), []);
});

test("reasons and notes fall back to the other language", () => {
  assert.equal(proposalReason({ reason_en: "The source says 12 km.", reason_vi: null }, "vi"), "The source says 12 km.");
  assert.equal(proposalReason({ reason_en: "EN", reason_vi: "VI" }, "vi"), "VI");
  assert.equal(proposalReason({ reason_en: null, reason_vi: null }, "en"), "");
  assert.equal(note({ source_says_vi: "Nguồn ghi 12 km." }, "source_says", "en"), "Nguồn ghi 12 km.");
  assert.equal(note(undefined, "doubt", "en"), "");
});

test("tones and who decided", () => {
  assert.equal(labelTone("ok"), "success");
  assert.equal(labelTone("violation"), "danger");
  assert.equal(labelTone("unsure"), "warning");
  assert.equal(labelTone(null), "neutral");
  assert.equal(deciderLabel("admin:11111111-2222-3333-4444-555555555555"), "admin 11111111");
  assert.equal(deciderLabel("panel"), "panel");
  assert.equal(deciderLabel(null), "—");
});

test("the tab's messages: both languages, no issue id, no window.confirm in the page", () => {
  const msgs = read("app/admin/lab/writing-memory/claimLabels.messages.ts");
  const page = read("app/admin/lab/writing-memory/ClaimLabelsTab.tsx");
  assert.doesNotMatch(msgs.replace(/^\s*\/\/.*$/gm, ""), /\bAA-\d+/, "no issue id in UI text");
  assert.doesNotMatch(page.replace(/^\s*\/\/.*$/gm, ""), /window\.confirm|confirm\(/, "the second click is in the page");
  assert.match(msgs, /Đồng ý mọi đề xuất còn lại/);
  assert.match(msgs, /Accept every remaining proposal/);
  const wm = read("app/admin/lab/writing-memory/WritingMemory.tsx");
  assert.match(wm, /key: "claims"/);
  assert.match(read("app/admin/lab/writing-memory/MetricsTab.tsx"), /exclude_source=golden_claim/);
});

test("review paging: answered open items leave the list, so the next page starts that many earlier", () => {
  // page 1 of the open list fully answered: the next unseen items are now at offset 0
  assert.equal(reviewPageOffset("open", 0, 25, 1), 0);
  // 10 of page 2 answered: items 51-75 (original order) now start at offset 40
  assert.equal(reviewPageOffset("open", 25, 10, 1), 40);
  assert.equal(reviewPageOffset("open", 25, 0, 1), 50);
  // resolved / all keep every item: plain offsets
  assert.equal(reviewPageOffset("all", 25, 10, 1), 50);
  assert.equal(reviewPageOffset("resolved", 0, 0, 1), 25);
  assert.equal(reviewPageOffset("open", 25, 3, -1), 0);
  assert.equal(reviewPageOffset("all", 10, 0, -1), 0);
});
