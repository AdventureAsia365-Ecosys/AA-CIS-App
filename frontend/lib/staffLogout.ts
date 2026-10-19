// frontend/lib/staffLogout.ts
// AA-816 — where the browser goes after POST /api/auth/admin-logout. The route answers { redirect }: "/login"
// for a legacy session, or the Cognito /logout URL (managed-login domain, client_id + logout_uri) for a Cognito
// session, which ends the managed-login session so the next sign-in asks for a new email code (ADR 0006 §7).
// The client accepts only those two shapes; anything else falls back to /login (same idea as AA-Booking
// ui/signOut.ts, which accepts only an https logoutUrl).
//
// No imports: used by the client Topbar and by the smoke unit test.

export const LOGIN_PATH = "/login";

/** The navigation target for a logout answer: "/login", or a well-formed https Cognito /logout URL. */
export function logoutNavigationTarget(redirect: unknown): string {
  if (typeof redirect !== "string" || redirect === LOGIN_PATH) return LOGIN_PATH;
  let url: URL;
  try {
    url = new URL(redirect);
  } catch {
    return LOGIN_PATH;
  }
  if (url.protocol !== "https:" || url.pathname !== "/logout" || url.username || url.password) return LOGIN_PATH;
  if (!url.searchParams.get("client_id") || !url.searchParams.get("logout_uri")) return LOGIN_PATH;
  return url.toString();
}
