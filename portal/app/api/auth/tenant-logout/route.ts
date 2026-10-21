// portal/app/api/auth/tenant-logout/route.ts (AA-885: moved from the admin app)
// AA-427 — clears the 5 httpOnly tenant cookies server-side.
//
// Sidebar.tsx / PlaceholderTabs.tsx used to clear the session by looping
// `document.cookie = "<name>=; path=/; max-age=0"` over the 5 cookie names.
// That stops working once the cookies are httpOnly — client JS can no
// longer see or overwrite them at all (expiry included), so the browser
// would keep sending a "logged out" user's real tenant JWT on every request
// after clicking Logout. This route does the clearing server-side instead.
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { TENANT_COOKIES, TENANT_TOKEN_COOKIE, tenantVerifyCache } from "@/lib/auth-server";

export async function POST(req: NextRequest) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  // AA-885: drop the token's verify-cache entry on this instance too (same idea as the staff logout).
  const token = req.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  if (token) tenantVerifyCache.delete(token);
  const response = NextResponse.json({ ok: true });
  for (const name of TENANT_COOKIES) {
    response.cookies.set(name, "", { path: "/", maxAge: 0 });
  }
  return response;
}
