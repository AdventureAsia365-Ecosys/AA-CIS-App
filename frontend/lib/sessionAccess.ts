// frontend/lib/sessionAccess.ts
// AA-1127 C3 — the shape of GET /admin/session (api/routers/admin_session.py) and the one rule that turns it into the
// read-only hint the admin shell hands to the kit (app/_kit/useSessionAccess.ts). Pure, tested in
// lib/sessionAccess.test.mjs.

export const SESSION_API = "/api/admin/session";

export interface SessionHint {
  role: string | null;
  read_only: boolean;
  /** AA-1157: "view_as" = the owner's read-only "view as a lower role" session. */
  reason: "role" | "e2e" | "view_as" | null;
}

/** The API's answer when it has one; otherwise (pending, failed) the role cookie's own rule. */
export function sessionReadOnly(hint: SessionHint | undefined | null, roleIsReadOnly: boolean): boolean {
  if (hint && typeof hint.read_only === "boolean") return hint.read_only;
  return roleIsReadOnly;
}
