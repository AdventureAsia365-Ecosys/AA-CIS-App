// frontend/lib/tenantMembers.test.mjs
// AA-1031 (S264): unit tests for the Tenant 360 Members tab helpers (lib/tenantMembers.ts).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  STUDIO_ROLES,
  deliveryOk,
  deliveryProblem,
  invitationsPath,
  isInviteEmail,
  isLastActiveOwner,
  isStudioRole,
  liveCount,
  memberActions,
  memberPath,
  removePath,
  sendAgainPath,
  statusTone,
} from "./tenantMembers.ts";

const T = "11111111-2222-3333-4444-555555555555";
const row = (id, status, role, invitation = null) => ({
  membership_id: id, identity_fingerprint: status === "invited" ? null : "abcdef012345", content_studio_role: role,
  status, invitation_id: invitation, created_at: "2026-11-01T00:00:00Z", created_by: "staff",
  updated_at: "2026-11-01T00:00:00Z", removed_at: null, removed_by: null,
});

test("paths are the existing admin member routes under the BFF", () => {
  assert.equal(invitationsPath(T), `/api/admin/tenants/${T}/members/invitations`);
  assert.equal(memberPath(T, "m1"), `/api/admin/tenants/${T}/members/m1`);
  assert.equal(sendAgainPath(T, "m1"), `/api/admin/tenants/${T}/members/m1/invitation/send`);
  assert.equal(removePath(T, "m1"), `/api/admin/tenants/${T}/members/m1/remove`);
});

test("e-mail check mirrors the API pattern and limits", () => {
  assert.ok(isInviteEmail("owner@lotus-trips.example"));
  assert.ok(isInviteEmail("  owner@lotus-trips.example  "));
  for (const bad of ["", "a@b.c", "no-at-sign.example", "two@@at.example", "space in@x.example", "x@nodot"]) {
    assert.equal(isInviteEmail(bad), false, bad);
  }
  assert.equal(isInviteEmail(`${"a".repeat(65)}@x.example`), false);
});

test("only the three Content studio roles exist (no CRM role)", () => {
  assert.deepEqual([...STUDIO_ROLES], ["owner", "editor", "viewer"]);
  assert.ok(isStudioRole("editor"));
  assert.equal(isStudioRole("platform_admin"), false);
});

test("row actions: removed is final, send again only for an invited row with an invitation", () => {
  assert.deepEqual(memberActions(row("a", "active", "owner")), { changeRole: true, sendAgain: false, remove: true });
  assert.deepEqual(memberActions(row("b", "invited", "viewer", "inv-1")), { changeRole: true, sendAgain: true, remove: true });
  assert.deepEqual(memberActions(row("c", "invited", "viewer", null)), { changeRole: true, sendAgain: false, remove: true });
  assert.deepEqual(memberActions(row("d", "removed", "owner")), { changeRole: false, sendAgain: false, remove: false });
});

test("last active owner warning counts active owners only", () => {
  const ms = [row("a", "active", "owner"), row("b", "invited", "owner", "i"), row("c", "active", "editor")];
  assert.ok(isLastActiveOwner(ms, "a"));
  assert.equal(isLastActiveOwner(ms, "b"), false);
  assert.equal(isLastActiveOwner([...ms, row("e", "active", "owner")], "a"), false);
  assert.equal(liveCount([...ms, row("z", "removed", "viewer")]), 3);
});

test("status tones and delivery outcome", () => {
  assert.equal(statusTone("active"), "success");
  assert.equal(statusTone("invited"), "warning");
  assert.equal(statusTone("removed"), "neutral");
  assert.ok(deliveryOk({ status: "sent", crm_status: 202 }));
  assert.equal(deliveryOk({ status: "failed", crm_status: 502 }), false);
  assert.equal(deliveryProblem({ status: "sent", crm_status: 202 }), "");
  assert.equal(deliveryProblem({ status: "failed", crm_status: 409, detail: "x_code", message: "Nope" }), "Nope (x_code)");
  assert.equal(deliveryProblem({ status: "failed", crm_status: 409, detail: "x_code" }), "x_code");
  assert.equal(deliveryProblem({ status: "failed", crm_status: 503 }), "HTTP 503");
});
