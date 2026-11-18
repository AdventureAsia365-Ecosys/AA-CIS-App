// AA-1127 (C1): the Master Content audit badge reads one helper; "fixed" is a real backend value.
import { test } from "node:test";
import assert from "node:assert/strict";
import { brandAuditKind } from "./brandAudit.ts";

test("every backend audit status has a badge", () => {
  assert.equal(brandAuditKind("pass", false), "pass");
  assert.equal(brandAuditKind("fixed", true), "fixed");
  assert.equal(brandAuditKind("fixed", false), "fixed");
  assert.equal(brandAuditKind("flagged", true), "fixed");
  assert.equal(brandAuditKind("flagged", false), "flagged");
  assert.equal(brandAuditKind("manual_check", false), "manual");
});

test("no status, or an unknown one, shows no badge", () => {
  assert.equal(brandAuditKind(null, false), null);
  assert.equal(brandAuditKind("", true), null);
  assert.equal(brandAuditKind("something_new", false), null);
});
