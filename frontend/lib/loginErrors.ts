// frontend/lib/loginErrors.ts — AA-1111 (S285): the sign-in page's `?error=` code for a refused POST /auth/verify-admin
// answer at the end of the Cognito callback (lib/cognitoRoutes.ts). Pure, so lib/loginErrors.test.mjs runs it.

/** The parts of a verify-admin answer this needs (lib/auth-server.ts VerifyStaffTokenResult). */
export type VerifyAnswer = { ok: true } | { ok: false; status: number; detail?: string };

/**
 * A verify-admin answer that says nothing about the user: no answer at all (timeout / network, status 0) or a backend
 * error (5xx). Only a real refusal (401/403, or any other 4xx) ends a session.
 */
export function isTransientStatus(status: number): boolean {
  return status === 0 || status >= 500;
}

/**
 * "backend" for a transient answer; "mfa_required" when the API refused an administrator who signed in without an
 * authenticator code while the switch cis.platform.admin_mfa_required is on (403 detail "mfa_required");
 * "no_access" for any other refusal (and for an answer whose role is not a staff role).
 */
export function loginErrorFor(verified: VerifyAnswer): "backend" | "mfa_required" | "no_access" {
  if (verified.ok) return "no_access";
  if (isTransientStatus(verified.status)) return "backend";
  if (verified.status === 403 && verified.detail === "mfa_required") return "mfa_required";
  return "no_access";
}
