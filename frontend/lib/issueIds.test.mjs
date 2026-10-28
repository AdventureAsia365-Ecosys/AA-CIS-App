// AA-994 — stripIssueIds(): the API reference shows route docstrings without their issue references.
import { test } from "node:test";
import assert from "node:assert/strict";
import { stripIssueIds, stripIssueIdsDeep } from "./issueIds.ts";

test("drops a leading reference", () => {
  assert.equal(stripIssueIds("AA-941 — lock an agency"), "lock an agency");
  assert.equal(stripIssueIds("AA-665 C3 — forecast"), "forecast");
  assert.equal(stripIssueIds("AA-646/650: the active run"), "the active run");
  assert.equal(stripIssueIds("AA-916 WM-06 — optional body"), "optional body");
  assert.equal(stripIssueIds("line one\nAA-12 — line two"), "line one\nline two");
});

test("drops parentheses that hold only references, and trailing ones", () => {
  assert.equal(stripIssueIds("served after a retire (AA-977)."), "served after a retire.");
  assert.equal(stripIssueIds("writer (AA-620, AA-763) stage"), "writer stage");
  assert.equal(stripIssueIds("Lambda (daily, AA-627)"), "Lambda (daily)");
});

test("drops a bare reference and keeps everything else", () => {
  assert.doesNotMatch(stripIssueIds("through the AA-720 path"), /AA-[0-9]/);
  assert.equal(stripIssueIds("AA-authored content"), "AA-authored content");
  assert.equal(stripIssueIds("no reference here"), "no reference here");
});

test("deep strip touches only text keys", () => {
  const spec = { paths: { "/x": { get: { summary: "AA-1 — one", operationId: "AA-2" } } },
                 components: { schemas: { S: { description: "a (AA-3)", properties: { description: { type: "string" } } } } } };
  const out = stripIssueIdsDeep(spec);
  assert.equal(out.paths["/x"].get.summary, "one");
  assert.equal(out.paths["/x"].get.operationId, "AA-2");
  assert.equal(out.components.schemas.S.description, "a");
  assert.deepEqual(out.components.schemas.S.properties.description, { type: "string" });
});

test("a long run of references does not hang (no exponential backtracking)", () => {
  const evil = "(AA-0" + "\tAA-0".repeat(5000) + "!";
  const t0 = Date.now();
  stripIssueIds(evil);
  assert.ok(Date.now() - t0 < 500, "stripIssueIds took too long");
  assert.equal(stripIssueIds("x (AA-1 C2 / AA-2) y"), "x y");
  assert.equal(stripIssueIds("x (AA-1 and AA-2) y"), "x y");
});
