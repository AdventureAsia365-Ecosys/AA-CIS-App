// frontend/app/api/tenant/[...path]/route.ts
//
// AA-232 hotfix: the staff (admin/content) branch used to forward the
// client-readable cis_api_token cookie as x-admin-secret. Pre-AA-232 that
// cookie held the raw ADMIN_SECRET (the admin typed it in), so it happened
// to work — and, as a side effect, exposed ADMIN_SECRET in a non-httpOnly
// client cookie. Post-AA-232, login now sets cis_api_token to a JWT, so the
// forwarded value stopped matching ADMIN_SECRET → every staff call through
// this proxy 403'd ("Invalid admin secret").
//
// Fix: inject process.env.ADMIN_SECRET server-side for the staff branch —
// exactly like /api/admin/[...path]/route.ts already does — instead of
// forwarding any client cookie as the secret. This closes the accidental
// cookie exposure permanently (good) and restores staff access (the bug).
//
// AA-885: the tenant branch is gone. The tenant portal is its own app (portal/), with its own
// tenant-only copy of this proxy (portal/app/api/tenant/[...path]/route.ts, no admin secret). Here
// the route is STAFF-ONLY: no verified cis_admin_token → requireAdmin()'s 401, whatever other cookie
// (cis_tenant_token included) the request carries.
//
// AA-253: the staff branch used to trust the plain, client-writable
// cis_role cookie ("admin" or "content") at face value — no JWT check at
// all — before forwarding ADMIN_SECRET. requireAdmin() now performs real
// verification (POST /auth/verify-admin, signature-checked) instead.
//
// Known behavior change: backend /auth/verify-admin only whitelists
// role in ("admin","reviewer") and content-role sessions carry no JWT
// (separate login path, see login/route.ts) — so content-role callers now
// get 401 through this proxy where they previously passed on the cookie
// alone. This mirrors the same known-limitation carve-out proxy.ts
// already calls out for page-level gating (content is unhardened by
// design, tracked under AA-253) — flagged here, not silently patched
// around, since it needs a product decision (does content need write
// access through this proxy, or was the old pass-through itself the bug).
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import { API_RULES, isApiDocsPath } from "@/lib/rbac";
import { proxyPath } from "@/lib/bffPath";
import { streamUpstream } from "@/lib/proxyResponse";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";

// AA-816 (C5/C9): this proxy reaches ANY backend path, and several tenant routes read `Authorization` as
// a TENANT token first (e.g. v1_tours get_tenant), so a Cognito session's Bearer is forwarded only under
// the staff API prefixes (lib/auth-server.ts isStaffApiPath), and there WITHOUT X-Admin-Secret; elsewhere,
// and for a legacy session, the staff branch stays secret-only.
async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  // AA-885: staff only. requireAdmin() answers 401 without a verified staff token.
  const auth = await requireAdmin(req, API_RULES.tenantProxy);
  if (!auth.ok) return auth.response;

  const headers: Record<string, string> = {
    "x-admin-user-id": auth.adminId,
  };
  // AA-882 — the verified JWT role, as /api/admin/[...path] sends it. Without it the backend
  // treats the call as the machine principal (api/core/staff_authz.py), which may call the
  // scheduler POSTs (jev-canary/check, dfs-balance/check, jev/402-sweep, retention/schedule)
  // whatever the staff role; with it, reviewer/content get 403 on every POLICY route.
  headers["x-admin-role"] = auth.role;

  const { path } = await params;
  const pathStr = path.join("/");
  // AA-577 follow-up — no encoded slash, backslash or dot segment may rewrite the path after the checks
  // below (lib/bffPath.ts), and the staff branch (which carries the admin secret to ANY backend path)
  // must not hand the AA-577 docs (/openapi.json, /docs, /redoc) to a non-admin role.
  const upstreamPath = proxyPath(path);
  if (upstreamPath === null) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }
  if (auth.role !== "admin" && isApiDocsPath(pathStr)) {
    return NextResponse.json({ detail: "Only an admin can do this" }, { status: 403 });
  }
  // AA-816 (C9) — decided on the path the backend will route (proxyPath above): Cognito Bearer only on a
  // staff API path, else X-Admin-Secret (503 when a secret is needed and none is configured).
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, upstreamPath);
  if (!credential) return secretNotConfigured();
  Object.assign(headers, credential);
  const search = req.nextUrl.search;
  const url = `${API_URL}/${upstreamPath}${search}`;

  // AA-441: was unconditionally forcing Content-Type: application/json + reading the body as
  // .text() — silently mangled any multipart/form-data request (e.g. BrandTab.tsx's brand-guide
  // file upload, which always sent real file bytes this way). Mirrors the admin proxy's
  // (/api/admin/[...path]/route.ts) existing multipart pass-through instead of re-encoding.
  const contentType = req.headers.get("content-type") ?? "";
  const isMultipart = contentType.includes("multipart/form-data");

  let body: ArrayBuffer | string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    if (isMultipart) {
      headers["Content-Type"] = contentType;
      body = await req.arrayBuffer();
    } else {
      headers["Content-Type"] = "application/json";
      try { body = await req.text(); } catch { /* empty body */ }
    }
  }

  try {
    const res = await fetch(url, { method: req.method, headers, body });
    // S241 — stream the body through (lib/proxyResponse.ts: same status, Content-Type only).
    return streamUpstream(res);
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export const GET    = handler;
export const POST   = handler;
export const PATCH  = handler;
export const DELETE = handler;
