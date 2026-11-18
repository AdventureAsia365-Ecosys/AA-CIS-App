// frontend/lib/docsFeedback.test.mjs
// AA-1133 follow-up (S284 live check): who is offered "Was this page helpful?" and what a refused answer says
// (lib/docsFeedback.ts). The live check found the read-only Dev E2E session (role cookie "admin") offered the vote and
// shown the raw code "e2e_read_only".
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import { feedbackAllowed, feedbackRefusal } from "./docsFeedback.ts";

class FakeApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

test("feedbackAllowed: the manifest's answer wins over the role cookie", () => {
  assert.equal(feedbackAllowed({ can_feedback: false }, "admin"), false);   // the read-only E2E session
  assert.equal(feedbackAllowed({ can_feedback: true }, "support"), true);
  assert.equal(feedbackAllowed({ can_feedback: false }, "viewer"), false);
});

test("feedbackAllowed: an older API without the field falls back to the role cookie", () => {
  assert.equal(feedbackAllowed({}, "viewer"), false);
  assert.equal(feedbackAllowed({}, "content"), true);
  assert.equal(feedbackAllowed(undefined, "admin"), true);
  assert.equal(feedbackAllowed(undefined, ""), true);
});

test("feedbackRefusal: read-only refusals (E2E session, viewer role) say so", () => {
  assert.equal(feedbackRefusal(new FakeApiError("e2e_read_only", 403)), "feedbackErrReadOnly");
  assert.equal(feedbackRefusal(new FakeApiError(" e2e_read_only ", 403)), "feedbackErrReadOnly");
  assert.equal(feedbackRefusal(new FakeApiError("Role 'viewer' is read-only", 403)), "feedbackErrReadOnly");
});

test("feedbackRefusal: other refusals map to a sentence, never the code", () => {
  assert.equal(feedbackRefusal(new FakeApiError("Forbidden", 403)), "feedbackErrNotAllowed");
  assert.equal(feedbackRefusal(new FakeApiError("not listed", 404)), "feedbackErrPageGone");
  assert.equal(feedbackRefusal(new FakeApiError("bad body", 422)), "feedbackErrInvalid");
  assert.equal(feedbackRefusal(new FakeApiError("unavailable", 503)), "feedbackErrUnavailable");
  assert.equal(feedbackRefusal(new FakeApiError("boom", 500)), "feedbackErrFailed");
  assert.equal(feedbackRefusal(new Error("Failed to fetch")), "feedbackErrFailed");
  assert.equal(feedbackRefusal(null), "feedbackErrFailed");
  assert.equal(feedbackRefusal("e2e_read_only"), "feedbackErrFailed");
});
