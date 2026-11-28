// frontend/lib/loginErrors.test.mjs — AA-1111 (S285): the Cognito callback sends an administrator refused for a
// missing authenticator code to /login?error=mfa_required, whose message exists in English and Vietnamese.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { loginErrorFor } from "./loginErrors.ts";

test("a 403 mfa_required answer maps to the mfa_required sign-in error", () => {
  assert.equal(loginErrorFor({ ok: false, status: 403, detail: "mfa_required" }), "mfa_required");
});

test("other refusals stay no_access and transient answers stay backend", () => {
  assert.equal(loginErrorFor({ ok: false, status: 403, detail: "No CIS staff group" }), "no_access");
  assert.equal(loginErrorFor({ ok: false, status: 403 }), "no_access");
  assert.equal(loginErrorFor({ ok: false, status: 401, detail: "mfa_required" }), "no_access");
  assert.equal(loginErrorFor({ ok: false, status: 0 }), "backend");
  assert.equal(loginErrorFor({ ok: false, status: 503, detail: "mfa_required" }), "backend");
  assert.equal(loginErrorFor({ ok: true }), "no_access");
});

test("the sign-in page knows the mfa_required code in both languages", () => {
  const src = readFileSync(new URL("../app/login/messages.ts", import.meta.url), "utf8");
  assert.match(src, /mfa_required: "errMfaRequired"/);
  assert.equal(src.match(/errMfaRequired:/g)?.length, 2);
});

test("a 403 e2e_pilot_fence answer maps to the pilot_fence sign-in error (AA-1054)", () => {
  assert.equal(loginErrorFor({ ok: false, status: 403, detail: "e2e_pilot_fence" }), "pilot_fence");
  assert.equal(loginErrorFor({ ok: false, status: 401, detail: "e2e_pilot_fence" }), "no_access");
  assert.equal(loginErrorFor({ ok: false, status: 503, detail: "e2e_pilot_fence" }), "backend");
});

test("the sign-in page knows the pilot_fence code in both languages", () => {
  const src = readFileSync(new URL("../app/login/messages.ts", import.meta.url), "utf8");
  assert.match(src, /pilot_fence: "errPilotFence"/);
  assert.equal(src.match(/errPilotFence:/g)?.length, 2);
});
