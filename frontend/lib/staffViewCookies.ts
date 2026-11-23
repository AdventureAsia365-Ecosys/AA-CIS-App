// frontend/lib/staffViewCookies.ts — AA-1157: set and clear the two cookies of the owner's "view as a lower role"
// session on a BFF response (lib/staffViewAs.ts: the httpOnly token cookie and the display hint the page reads).
import { NextResponse } from "next/server";
import { streamUpstream } from "./proxyResponse";
import {
  STAFF_VIEW_AS_COOKIE, STAFF_VIEW_CLOSE_GRACE_S, STAFF_VIEW_DISPLAY_COOKIE, displayCookieValue, isViewEnded,
  type StaffViewDisplay,
} from "./staffViewAs";

// Same rule as the staff session cookie (app/api/auth/login/route.ts): Secure outside local development.
const SECURE = process.env.NODE_ENV === "production";

/** `maxAge`: the view's seconds left. The token cookie lives STAFF_VIEW_CLOSE_GRACE_S longer (an expired token is never
 *  honoured; it only lets the banner's last check close the session with its end row), the display hint ends on time. */
export function setStaffViewCookies(res: NextResponse, token: string, display: StaffViewDisplay, maxAge: number) {
  res.cookies.set(STAFF_VIEW_AS_COOKIE, token,
    { httpOnly: true, secure: SECURE, sameSite: "strict", path: "/", maxAge: maxAge + STAFF_VIEW_CLOSE_GRACE_S });
  res.cookies.set(STAFF_VIEW_DISPLAY_COOKIE, displayCookieValue(display),
    { httpOnly: false, secure: SECURE, sameSite: "strict", path: "/", maxAge });
  return res;
}

export function clearStaffViewCookies<T extends NextResponse>(res: T): T {
  res.cookies.set(STAFF_VIEW_AS_COOKIE, "", { httpOnly: true, secure: SECURE, sameSite: "strict", path: "/", maxAge: 0 });
  res.cookies.set(STAFF_VIEW_DISPLAY_COOKIE, "", { httpOnly: false, secure: SECURE, sameSite: "strict", path: "/",
    maxAge: 0 });
  return res;
}

/** The upstream answer of a staff proxy (/api/admin, /api/tenant, /api/pipeline) for the page. Inside a view
 *  (`viewing`), a 409 staff_view_as_ended (expired, rights changed) also clears both view cookies, so the page stops
 *  sending a dead view at once; everything else streams through unchanged (lib/proxyResponse.ts). */
export async function answerStaffUpstream(res: Response, viewing: boolean): Promise<NextResponse> {
  if (res.status !== 409 || !viewing) return streamUpstream(res);
  const text = await res.text();
  let parsed: unknown = null;
  try { parsed = JSON.parse(text); } catch { /* not JSON */ }
  const out = new NextResponse(text, { status: 409, headers: { "Content-Type": "application/json" } });
  return isViewEnded(409, parsed) ? clearStaffViewCookies(out) : out;
}
