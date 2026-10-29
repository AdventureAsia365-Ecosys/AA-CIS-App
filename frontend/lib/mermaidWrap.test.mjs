// AA-994 — the docs reader's Mermaid label wrapping width (lib/mermaidWrap.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { BASE_WRAP_WIDTH, MAX_WRAP_WIDTH, estimateWidth, sourceWords, wrappingWidthFor } from "./mermaidWrap.ts";

// 10px per character: easy to reason about.
const tenPx = (w) => w.length * 10;

test("words are split at spaces, line breaks and label delimiters", () => {
  const words = sourceWords('flowchart TB\n  R2[AA-TripPlanner-Web] -->|OIDC| OIDC\n  ACC3[acc3:<br/>AA3-Bedrock-Invoker]\n  RDS[(RDS "x")]');
  for (const w of ["AA-TripPlanner-Web", "OIDC", "acc3:", "AA3-Bedrock-Invoker", "RDS"]) assert.ok(words.includes(w), w);
  for (const w of words) assert.doesNotMatch(w, /[\s[\](){}|"<>]/, w);
});

test("short words keep the base width, so multi-word labels still wrap", () => {
  assert.equal(wrappingWidthFor("flowchart TB\n  A[one two three four five six seven] --> B[eight]", tenPx), BASE_WRAP_WIDTH);
});

test("the width grows to the widest single word, so no word is cut mid-way", () => {
  const src = "flowchart TB\n  L12[Lambdas: aa-tripplanner-dev-browse + assembly]\n  ACC3[acc3: AA3-Bedrock-Invoker]";
  const width = wrappingWidthFor(src, (w) => w.length * 12);
  const widest = "aa-tripplanner-dev-browse".length * 12;
  assert.ok(width > widest, `${width} > ${widest}`);
  assert.ok(width < widest * 1.2, `${width} close to ${widest}`);
});

test("one pathological token is capped", () => {
  assert.equal(wrappingWidthFor(`flowchart TB\n  A[${"x".repeat(500)}]`, tenPx), MAX_WRAP_WIDTH);
});

test("the estimate is used for rough sizing without a canvas", () => {
  assert.ok(estimateWidth("aa-booking-dev-api") > 150);
  assert.equal(estimateWidth(""), 0);
});
