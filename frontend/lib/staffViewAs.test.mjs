// AA-1157: the owner's read-only "view as a lower role" — the pure helpers (lib/staffViewAs.ts) and the wiring the BFF
// relies on (requireAdmin refuses writes in a view, the token rides along on staff paths only, the generic proxy refuses
// the start, the token never reaches a page).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  STAFF_VIEW_AS_HEADER, STAFF_VIEW_CLOSE_GRACE_S, displayCookieValue, effectiveSession, expiredOwnStaffView,
  isStaffViewAsStartPath, isViewEnded, ownStaffView, parseDisplayCookie, refusedWriteLog, staffViewClaims, staffViewStep,
} from "./staffViewAs.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");

const SID = "33333333-3333-4333-8333-333333333333";
const ME = "11111111-1111-4111-8111-111111111111";

function token(payload) {
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

const future = Math.floor(Date.now() / 1000) + 600;
const good = token({ typ: "staff_view_as", aud: "cis-staff-view-as", svs: SID, stf: ME, vrl: "content",
  vcp: ["content.read", "review.act", 7], exp: future });

test("claims: only a view-as-a-role token", () => {
  assert.deepEqual(staffViewClaims(good), { sessionId: SID, staffId: ME, role: "content", exp: future,
    capabilities: ["content.read", "review.act"] });
  assert.equal(staffViewClaims(token({ vas: SID, stf: ME, srl: "admin", exp: future })), null);   // a tenant view
  assert.equal(staffViewClaims(token({ typ: "staff_view_as", svs: "x", stf: ME, vrl: "content", vcp: [], exp: future })),
    null);
  assert.equal(staffViewClaims("garbage"), null);
  assert.equal(staffViewClaims(undefined), null);
});

test("own view: the signed-in member's and unexpired only", () => {
  assert.ok(ownStaffView(good, ME));
  assert.equal(ownStaffView(good, "22222222-2222-4222-8222-222222222222"), null);
  assert.equal(ownStaffView(good, ME, (future + 1) * 1000), null);
});

test("effective session: never wider than the verified one, only from a Cognito admin", () => {
  const view = staffViewClaims(token({ typ: "staff_view_as", svs: SID, stf: ME, vrl: "custom_x",
    vcp: ["content.read", "staff.read", "made.up"], exp: future }));
  const admin = { role: "admin", capabilities: ["content.read", "staff.read"], cognito: true };
  assert.deepEqual(effectiveSession(admin, view), { role: "custom_x", capabilities: ["content.read", "staff.read"],
    viewing: true });
  // a forged cookie on a non-admin or a legacy session is ignored
  assert.deepEqual(effectiveSession({ role: "content", capabilities: ["content.read"], cognito: true }, view),
    { role: "content", capabilities: ["content.read"], viewing: false });
  assert.equal(effectiveSession({ ...admin, cognito: false }, view).viewing, false);
  assert.equal(effectiveSession(admin, null).viewing, false);
  // the claimed list is intersected with the verified one
  assert.deepEqual(effectiveSession({ ...admin, capabilities: ["content.read"] }, view).capabilities, ["content.read"]);
});

test("an owner on a custom role holding staff.read is never honoured, and its writes are still refused", () => {
  // The API refuses such a start (api/core/staff_view_as.py may_start needs the built-in admin role); a cookie that
  // is present anyway (stale or forged) must never let a write run with the owner's own rights.
  const view = staffViewClaims(good);
  const customOwner = { role: "custom_ops", capabilities: ["content.read", "staff.read"], cognito: true };
  assert.equal(effectiveSession(customOwner, view).viewing, false);
  for (const m of ["POST", "PATCH", "PUT", "DELETE"]) {
    assert.deepEqual(staffViewStep(false, customOwner, view, false), { kind: "refuse" }, m);
  }
  assert.deepEqual(staffViewStep(true, customOwner, view, false),
    { kind: "read", role: "custom_ops", capabilities: ["content.read", "staff.read"], viewing: false });
});

test("view step: writes refused in any own view, reads swapped only from a Cognito admin, owner routes untouched", () => {
  const view = staffViewClaims(good);
  const admin = { role: "admin", capabilities: ["content.read", "staff.read"], cognito: true };
  assert.deepEqual(staffViewStep(false, admin, null, false), { kind: "none" });
  assert.deepEqual(staffViewStep(true, admin, null, false), { kind: "none" });
  assert.deepEqual(staffViewStep(false, admin, view, false), { kind: "refuse" });
  assert.deepEqual(staffViewStep(false, { ...admin, cognito: false }, view, false), { kind: "refuse" });
  assert.deepEqual(staffViewStep(false, admin, view, true), { kind: "owner" });
  assert.deepEqual(staffViewStep(true, admin, view, false),
    { kind: "read", role: "content", capabilities: ["content.read"], viewing: true });
});

test("the start path is refused by the generic proxy in any spelling", () => {
  for (const p of ["view-as/roles", "View-As/Roles", "/view-as/roles/", "view-as/roles/x"]) {
    assert.equal(isStaffViewAsStartPath(p), true, p);
  }
  for (const p of ["view-as/sessions", "view-as/sessions/x/end", "platform/roles", "tenants/x/view-as"]) {
    assert.equal(isStaffViewAsStartPath(p), false, p);
  }
});

test("display cookie round trip; an expired one is gone", () => {
  const until = new Date(Date.now() + 60_000).toISOString();
  const d = { role: "content", label: "Content Team", caps: ["content.read"], until };
  assert.deepEqual(parseDisplayCookie(displayCookieValue(d)), d);
  assert.equal(parseDisplayCookie(displayCookieValue({ ...d, until: new Date(Date.now() - 1).toISOString() })), null);
  assert.equal(parseDisplayCookie(displayCookieValue({ ...d, role: "Bad Role" })), null);
  assert.equal(parseDisplayCookie("%7Bbroken"), null);
});

test("expired own view: only the signed-in member's and only past its expiry (never honoured)", () => {
  const past = Math.floor(Date.now() / 1000) - 30;
  const expired = token({ typ: "staff_view_as", aud: "cis-staff-view-as", svs: SID, stf: ME, vrl: "content", vcp: [],
    exp: past });
  assert.equal(expiredOwnStaffView(expired, ME)?.sessionId, SID);
  assert.equal(ownStaffView(expired, ME), null);
  assert.equal(expiredOwnStaffView(good, ME), null);                    // still running: not "expired"
  assert.equal(expiredOwnStaffView(expired, "22222222-2222-4222-8222-222222222222"), null);
  assert.equal(expiredOwnStaffView(expired, null), null);
  assert.ok(STAFF_VIEW_CLOSE_GRACE_S > 0 && STAFF_VIEW_CLOSE_GRACE_S <= 600);
});

test("a refused write leaves one structured log line: session, role, method, path; no query string", () => {
  const line = refusedWriteLog("cis", { sessionId: SID, role: "content", staffId: ME }, "post",
    "/api/admin/platform/roles?x=secret value");
  assert.equal(line, `[view-as] staff_view_as_write_refused app=cis session=${SID} staff=${ME} view_role=content `
    + "method=POST path=/api/admin/platform/roles");
  assert.doesNotMatch(refusedWriteLog("cis", { sessionId: SID, role: "content", staffId: ME }, "DELETE", "/a b\nc"),
    /\s(b|c)\b|\n/);
});

test("the view read closes an expired own view through the API before answering inactive", () => {
  const route = read("app/api/view-as/roles/route.ts");
  assert.match(route, /if \(!claims\) return closeExpired\(token, auth\.adminId, headers\);/);
  assert.match(route, /const expired = expiredOwnStaffView\(token, adminId\);\s*if \(expired\) await readSession\(expired\.sessionId, headers\)/);
  const banner = read("app/_kit/StaffViewAsBanner.tsx");
  assert.match(banner, /if \(over\) void checkView\(\)/);
});

test("an ended view is the 409 staff_view_as_ended only", () => {
  assert.equal(isViewEnded(409, { detail: "staff_view_as_ended" }), true);
  assert.equal(isViewEnded(409, { detail: "other" }), false);
  assert.equal(isViewEnded(403, { detail: "staff_view_as_ended" }), false);
});

test("requireAdmin refuses writes inside a view and swaps the role for reads; the token rides on staff paths only", () => {
  const src = read("lib/auth-server.ts");
  assert.match(src, /staffViewStep\(isReadMethod\(request\.method\)/);
  assert.match(src, /if \(step\.kind === "refuse"\) \{[^}]*console\.warn\(refusedWriteLog\("cis"[\s\S]*?return \{ ok: false, response: NextResponse\.json\(\{ detail: VIEW_AS_READ_ONLY \}/);
  assert.match(src, /allowDuringViewAs/);
  assert.match(src, /if \(auth\.bearer && isStaffApiPath\(backendPath\)\) \{\s*return \{ Authorization: `Bearer \$\{auth\.bearer\}`, \.\.\.staffViewAsHeader\(auth\) \};/);
  assert.match(src, /auth\.viewAs\?\.forward \? \{ \[STAFF_VIEW_AS_HEADER\]: auth\.viewAs\.token \} : \{\}/);
  assert.equal(STAFF_VIEW_AS_HEADER, "x-staff-view-as");
});

test("the generic proxy refuses the start; the start route keeps the token httpOnly and never answers it", () => {
  const proxy = read("app/api/admin/[...path]/route.ts");
  assert.match(proxy, /isStaffViewAsStartPath\(pathStr\)/);
  const start = read("app/api/view-as/roles/route.ts");
  assert.match(start, /const \{ token, \.\.\.session \} = data;/);
  assert.doesNotMatch(start, /NextResponse\.json\(\{[^}]*\btoken\b/);
  const cookies = read("lib/staffViewCookies.ts");
  assert.match(cookies, /STAFF_VIEW_AS_COOKIE, token,\s*\{ httpOnly: true/);
  // the token cookie outlives the view by the grace (the banner's last check closes it); the hint ends on time
  assert.match(cookies, /maxAge: maxAge \+ STAFF_VIEW_CLOSE_GRACE_S \}/);
  // the end and the session read act as the owner: no view header
  assert.match(start, /allowDuringViewAs: true/);
});

test("the shell renders the banner and the identity follows the view hint", () => {
  assert.match(read("app/_kit/AdminShell.tsx"), /banner=\{\(\) => <StaffViewAsBanner \/>\}/);
  assert.match(read("app/_kit/useAdminIdentity.ts"), /parseDisplayCookie\(readCookie\(STAFF_VIEW_DISPLAY_COOKIE\)\)/);
});

test("the page gate uses the view session", () => {
  const proxy = read("proxy.ts");
  assert.match(proxy, /effectiveSession\(/);
  assert.match(proxy, /canSeePage\(subject, request\.nextUrl\.pathname\)/);
});
