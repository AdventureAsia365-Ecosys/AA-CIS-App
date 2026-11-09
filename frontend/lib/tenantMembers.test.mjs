// frontend/lib/tenantMembers.test.mjs
// AA-1031 (S264): unit tests for the Tenant 360 Members tab helpers (lib/tenantMembers.ts).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  MEMBER_WRITE_ACTIONS,
  STUDIO_ROLES,
  anyMemberWrite,
  deliveryOk,
  deliveryProblem,
  invitationsPath,
  isInviteEmail,
  isLastActiveOwner,
  isStudioRole,
  liveCount,
  mayInviteAs,
  memberActions,
  memberPath,
  memberPermissions,
  sendAgainBody,
  memberRowActions,
  membersAccessPath,
  removePath,
  roleChoices,
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

// ── AA-1031 (S264 live check F3): the members access read decides which write controls show ─────────────────────

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const all = (v) => Object.fromEntries(MEMBER_WRITE_ACTIONS.map((a) => [a, v]));
const ADMIN = { read_only: false, actions: all(true), invite_roles: ["owner", "editor", "viewer"] };
const READ_ONLY = { read_only: true, actions: all(false), invite_roles: [] };
const E2E_SYNTHETIC = {
  read_only: false, actions: { invite: true, send_again: true, change_role: false, remove: false },
  invite_roles: ["editor", "viewer"],
};

test("members access: the API's answer decides, the role is ignored once it answered", () => {
  const admin = memberPermissions(ADMIN, false, false);
  assert.deepEqual(admin, {
    settled: true, invite: true, sendAgain: true, changeRole: true, remove: true, inviteRoles: [...STUDIO_ROLES],
    ownerWrites: true, sendAgainSame: false, sendAgainNew: true,   // an answer without send_again_to: typed only
  });
  const ro = memberPermissions(READ_ONLY, false, true);
  assert.deepEqual(ro, {
    settled: true, invite: false, sendAgain: false, changeRole: false, remove: false, inviteRoles: [],
    ownerWrites: false, sendAgainSame: false, sendAgainNew: false,
  });
  assert.equal(anyMemberWrite(ro), false);
  assert.equal(anyMemberWrite(admin), true);
});

test("members access: nothing offered while loading; a failed read falls back to the role", () => {
  const loading = memberPermissions(undefined, false, true);
  assert.equal(loading.settled, false);
  assert.equal(anyMemberWrite(loading), false);
  const failedWriter = memberPermissions(undefined, true, true);
  assert.equal(failedWriter.settled, true);
  assert.ok(failedWriter.invite && failedWriter.sendAgain && failedWriter.changeRole && failedWriter.remove);
  assert.deepEqual(failedWriter.inviteRoles, [...STUDIO_ROLES]);
  const failedReader = memberPermissions(undefined, true, false);
  assert.equal(failedReader.settled, true);
  assert.equal(anyMemberWrite(failedReader), false);
});

test("members access: the E2E session on a synthetic tenant invites editor / viewer only", () => {
  const p = memberPermissions(E2E_SYNTHETIC, false, true);
  assert.deepEqual(p.inviteRoles, ["editor", "viewer"]);
  assert.equal(mayInviteAs(p, "owner"), false);
  assert.equal(mayInviteAs(p, "viewer"), true);
  // a send again re-seals the stored role: an owner invitation is not offered, an editor one is
  assert.deepEqual(memberRowActions(row("a", "invited", "owner", "inv-1"), p),
    { changeRole: false, sendAgain: false, remove: false });
  assert.deepEqual(memberRowActions(row("b", "invited", "editor", "inv-2"), p),
    { changeRole: false, sendAgain: true, remove: false });
});

test("members access: row actions are the row's AND the session's", () => {
  const admin = memberPermissions(ADMIN, false, false);
  for (const m of [row("a", "active", "owner"), row("b", "invited", "viewer", "inv"), row("c", "removed", "editor")]) {
    assert.deepEqual(memberRowActions(m, admin), memberActions(m), m.membership_id);
    assert.deepEqual(memberRowActions(m, memberPermissions(READ_ONLY, false, true)),
      { changeRole: false, sendAgain: false, remove: false }, m.membership_id);
  }
});

test("members access: unknown roles are dropped, a missing invite_roles means every role, none without invite", () => {
  const odd = memberPermissions({ ...ADMIN, invite_roles: ["owner", "superuser"] }, false, false);
  assert.deepEqual(odd.inviteRoles, ["owner"]);
  const older = memberPermissions({ read_only: false, actions: all(true) }, false, false);
  assert.deepEqual(older.inviteRoles, [...STUDIO_ROLES]);
  const noInvite = memberPermissions({
    read_only: false, actions: { change_role: true }, invite_roles: ["owner"],
  }, false, false);
  assert.deepEqual(noInvite.inviteRoles, []);
  assert.equal(noInvite.changeRole, true);
  assert.equal(mayInviteAs(noInvite, "owner"), false);
});

// AA-1035 (S267 fix pass): customer_success (tenant.member.write) never touches the owner role; support only sends a
// non-owner invitation again (tenant.member.resend).
const CUSTOMER_SUCCESS = {
  read_only: false, actions: all(true), invite_roles: ["editor", "viewer"], owner_writes: false,
};
const SUPPORT = {
  read_only: false, actions: { invite: false, send_again: true, change_role: false, remove: false },
  invite_roles: ["editor", "viewer"], owner_writes: false,
};

test("members access: without owner_writes an owner row has no role change or removal, no owner choice", () => {
  const p = memberPermissions(CUSTOMER_SUCCESS, false, false);
  assert.equal(p.ownerWrites, false);
  assert.equal(mayInviteAs(p, "owner"), false);
  assert.deepEqual(roleChoices(p), ["editor", "viewer"]);
  assert.deepEqual(memberRowActions(row("a", "active", "owner"), p),
    { changeRole: false, sendAgain: false, remove: false });
  assert.deepEqual(memberRowActions(row("b", "invited", "owner", "inv-1"), p),
    { changeRole: false, sendAgain: false, remove: false });
  assert.deepEqual(memberRowActions(row("c", "active", "editor"), p),
    { changeRole: true, sendAgain: false, remove: true });
  assert.deepEqual(roleChoices(memberPermissions(ADMIN, false, false)), [...STUDIO_ROLES]);
});

test("members access: support only sends a non-owner invitation again", () => {
  const p = memberPermissions(SUPPORT, false, false);
  assert.equal(p.invite, false);
  assert.deepEqual(memberRowActions(row("a", "invited", "editor", "inv-1"), p),
    { changeRole: false, sendAgain: true, remove: false });
  assert.deepEqual(memberRowActions(row("b", "invited", "owner", "inv-2"), p),
    { changeRole: false, sendAgain: false, remove: false });
  assert.deepEqual(memberRowActions(row("c", "active", "viewer"), p),
    { changeRole: false, sendAgain: false, remove: false });
});

test("members access: the action list and the route match the backend", () => {
  const py = read("../../api/routers/admin_tenant360.py");
  const block = py.slice(py.indexOf("MEMBER_WRITES: dict"), py.indexOf("def members_access"));
  const keys = [...block.matchAll(/^\s+"([a-z_]+)": \(/gm)].map((m) => m[1]);
  assert.deepEqual([...keys].sort(), [...MEMBER_WRITE_ACTIONS].sort());
  assert.ok(py.includes('@router.get("/{tenant_id}/members/access"'));
  assert.equal(membersAccessPath(T), `/api/admin/tenants/${T}/members/access`);
});

test("members access: the Members tab and the agency drawer read it, not the role cookie", () => {
  const tab = read("../app/admin/tenants/_components/Tenant360Members.tsx");
  assert.ok(tab.includes("useMembersAccess(tenantId)"));
  assert.ok(!/can\(\s*useAdminIdentity\(\)\.role/.test(tab), "Members tab must not decide on the role cookie");
  assert.ok(!tab.includes('"tenant.write"'), "Members tab must not check tenant.write itself");
  assert.match(tab, /\{perm\.invite && \(/);
  assert.match(tab, /memberRowActions\(m, perm\)/);
  const drawer = read("../app/admin/tenants/agencies/AgencyDrawer.tsx");
  assert.ok(drawer.includes("useMembersAccess("));
  assert.match(drawer, /studioInviteOffered\(cisTenant, mayInviteAs\(studioAccess, "owner"\)\)/);
  assert.ok(!drawer.includes('"tenant.write")'), "the drawer's studio box must not decide on the role cookie");
  const hook = read("../app/admin/_components/useMembersAccess.ts");
  assert.ok(hook.includes("membersAccessPath(id)") && hook.includes('can(role, "tenant.write")'));
});


test("send again (AA-1035 close-out): the kept address for support, a typed one only where the API allows it", () => {
  const support = memberPermissions({
    read_only: false, actions: { invite: false, send_again: true, change_role: false, remove: false },
    invite_roles: ["editor", "viewer"], owner_writes: false,
    send_again_to: { same_address: true, new_address: false },
  }, false, false);
  assert.equal(support.sendAgainSame, true);
  assert.equal(support.sendAgainNew, false);
  const cs = memberPermissions({
    read_only: false, actions: { invite: true, send_again: true, change_role: true, remove: true },
    invite_roles: ["editor", "viewer"], owner_writes: false,
    send_again_to: { same_address: true, new_address: true },
  }, false, false);
  assert.ok(cs.sendAgainSame && cs.sendAgainNew);
  // no send again at all: neither way
  const none = memberPermissions({ read_only: true, actions: {}, send_again_to: { same_address: true } }, false, false);
  assert.ok(!none.sendAgainSame && !none.sendAgainNew);
  // the body: no address = the kept one; a typed one only when not sending to the kept address
  assert.deepEqual(sendAgainBody(true, "someone@agency.example", "en"), { locale: "en" });
  assert.deepEqual(sendAgainBody(false, "  ", "vi"), { locale: "vi" });
  assert.deepEqual(sendAgainBody(false, " someone@agency.example ", "en"), { email: "someone@agency.example", locale: "en" });
});
