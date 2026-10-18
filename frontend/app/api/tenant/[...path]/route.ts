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
// Tenant branch is unchanged — still Bearer cis_tenant_token, still a real
// per-tenant JWT, never was the problem.
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
// alone. This mirrors the same known-limitation carve-out middleware.ts
// already calls out for page-level gating (content is unhardened by
// design, tracked under AA-253) — flagged here, not silently patched
// around, since it needs a product decision (does content need write
// access through this proxy, or was the old pass-through itself the bug).
import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";
import { requireAdmin, requireTenant, staffBearerHeader } from "@/lib/auth-server";
import { API_RULES, isApiDocsPath } from "@/lib/rbac";
import { proxyPath } from "@/lib/bffPath";
import { streamUpstream } from "@/lib/proxyResponse";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";

// AA-816 (C5): the backend paths where the API's staff_bearer middleware reads a staff Bearer
// (api/core/staff_authz.py STAFF_PATH_PREFIXES). This proxy reaches ANY backend path, and several
// tenant routes read `Authorization` as a TENANT token first (e.g. v1_tours get_tenant), so a staff
// Bearer is forwarded only under these prefixes; elsewhere the staff branch stays secret-only.
const STAFF_BEARER_PREFIXES = ["admin", "v1/pipeline", "v1/rules", "v1/social"];

function isStaffBearerPath(pathStr: string): boolean {
  return STAFF_BEARER_PREFIXES.some((p) => pathStr === p || pathStr.startsWith(p + "/"));
}

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const cookieStore = await cookies();
  // AA-656: staff vs tenant is decided by which session token is present, not by the
  // client-writable cis_role cookie; requireAdmin() then verifies the token and its role.
  const isStaff     = Boolean(cookieStore.get("cis_admin_token")?.value);

  const headers: Record<string, string> = {};
  let staffRole: string | null = null;
  let staffBearer: Record<string, string> = {};

  if (isStaff) {
    const auth = await requireAdmin(req, API_RULES.tenantProxy);
    if (!auth.ok) return auth.response;
    staffRole = auth.role;
    staffBearer = staffBearerHeader(auth);

    if (!ADMIN_SECRET) {
      return NextResponse.json({ detail: "Admin secret not configured" }, { status: 503 });
    }
    headers["x-admin-secret"] = ADMIN_SECRET;
    headers["x-admin-user-id"] = auth.adminId;
    // AA-882 — the verified JWT role, as /api/admin/[...path] sends it. Without it the backend
    // treats the call as the machine principal (api/core/staff_authz.py), which may call the
    // scheduler POSTs (jev-canary/check, dfs-balance/check, jev/402-sweep, retention/schedule)
    // whatever the staff role; with it, reviewer/content get 403 on every POLICY route.
    headers["x-admin-role"] = auth.role;
  } else {
    // AA-656: verify the tenant JWT before forwarding it (was: presence only).
    const auth = await requireTenant(req);
    if (!auth.ok) return auth.response;
    const tenantToken = cookieStore.get("cis_tenant_token")?.value ?? "";
    headers["Authorization"] = `Bearer ${tenantToken}`;
  }

  const { path } = await params;
  const pathStr = path.join("/");
  // AA-577 follow-up — no encoded slash, backslash or dot segment may rewrite the path after the checks
  // below (lib/bffPath.ts), and the staff branch (which carries the admin secret to ANY backend path)
  // must not hand the AA-577 docs (/openapi.json, /docs, /redoc) to a non-admin role.
  const upstreamPath = proxyPath(path);
  if (upstreamPath === null) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }
  if (staffRole !== null && staffRole !== "admin" && isApiDocsPath(pathStr)) {
    return NextResponse.json({ detail: "Only an admin can do this" }, { status: 403 });
  }
  if (staffRole !== null && isStaffBearerPath(pathStr)) {
    Object.assign(headers, staffBearer);
  }
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
