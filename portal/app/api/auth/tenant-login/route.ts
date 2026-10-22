// portal/app/api/auth/tenant-login/route.ts
// AA-427 — tenant login BFF route (AA-885: moved from the admin app into the portal app; the cookies
// are now host-only on portal.lumiguides.it.com — no Domain attribute, on purpose).
//
// Before this route existed, tenant-login/page.tsx called the FastAPI backend
// (api-cis.lumiguides.it.com) directly from the browser, then set 5 cookies
// itself via document.cookie — plain, JS-readable, no httpOnly. Any XSS on
// the page could read cis_tenant_token straight out of document.cookie and
// impersonate the tenant for the full 24h JWT TTL.
//
// Fix mirrors the admin login pattern (AA-232, /api/auth/login/route.ts):
// route the login through a same-origin Next.js route so a real Set-Cookie
// response header — not client JS — decides the cookie flags. This is
// deliberately NOT "add Set-Cookie in api/main.py" — see
// docs/implementation-notes/AA-427-tenant-jwt-httponly-cookie.md for why the
// cookie has to be minted here: api-cis.lumiguides.it.com and
// aa-cis.lumiguides.it.com are different origins, so a cookie set by FastAPI
// would live on the wrong domain for the page gate (now portal/proxy.ts) to ever see it.
//
// The JSON response deliberately does NOT include the raw JWT — only the
// httpOnly Set-Cookie header carries it. Returning it in the body would
// defeat httpOnly entirely (page JS / XSS can read a fetch response body
// just as easily as a plain cookie).
import { NextRequest, NextResponse } from "next/server";
import { API_URL } from "@/lib/auth-server";

const COOKIE_MAX_AGE = 60 * 60 * 24; // 24h — matches backend JWT_EXPIRY_H (api/routers/auth.py)

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const apiKey = typeof body?.api_key === "string" ? body.api_key : "";

  if (!apiKey || apiKey.length < 10) {
    return NextResponse.json({ detail: "Invalid API key format" }, { status: 400 });
  }

  try {
    const res = await fetch(`${API_URL}/auth/tenant-login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // AA-760 — client "portal": the token is a portal session, rate-limited but not metered as an API call
      // (the Billing page's "API calls" count the tenant's own systems only; api/routers/auth.py PORTAL_CLIENT).
      body: JSON.stringify({ api_key: apiKey, client: "portal" }),
      signal: AbortSignal.timeout(5000),
    });

    if (res.status === 401) {
      return NextResponse.json({ detail: "Invalid API key" }, { status: 401 });
    }
    // AA-876 — the API's login throttle (or the API Gateway login throttle) answers 429. Pass it on as a 429
    // with a readable message instead of the 502 below; Retry-After is forwarded when the API sent one.
    if (res.status === 429) {
      const retryAfter = res.headers.get("retry-after");
      return NextResponse.json(
        { detail: "Too many sign-in attempts. Try again in a few minutes." },
        { status: 429, headers: retryAfter ? { "Retry-After": retryAfter } : undefined },
      );
    }
    if (!res.ok) {
      return NextResponse.json({ detail: "Login failed — backend error" }, { status: 502 });
    }

    const data = await res.json();
    // { token, tenant_id, tenant_name, plan_tier } — see TenantLoginResponse in api/main.py

    const response = NextResponse.json({ ok: true });
    const cookieOpts = {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax" as const,
      path: "/",
      maxAge: COOKIE_MAX_AGE,
    };
    response.cookies.set("cis_role", "tenant", cookieOpts);
    response.cookies.set("cis_tenant_token", data.token, cookieOpts);
    response.cookies.set("cis_tenant_id", data.tenant_id, cookieOpts);
    // Next.js's cookies.set() already URI-encodes the value on serialize —
    // encodeURIComponent()-ing it here too would double-encode (verified live:
    // produced "%2520" instead of "%20"). Pass the raw display name through.
    response.cookies.set("cis_tenant_name", data.tenant_name ?? "", cookieOpts);
    response.cookies.set("cis_tenant_plan", data.plan_tier ?? "", cookieOpts);
    return response;
  } catch {
    // Network error / timeout / route unreachable — a real infra failure,
    // not a reason to grant access.
    return NextResponse.json({ detail: "Connection error — please try again" }, { status: 502 });
  }
}
