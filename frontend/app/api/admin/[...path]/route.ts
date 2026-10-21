// frontend/app/api/admin/[...path]/route.ts
// AA-232: forward verified admin identity (JWT sub claim) to the backend
// as x-admin-user-id. AA-816 (C9): the upstream credential is the Cognito Bearer for a Cognito
// session and X-Admin-Secret only for a legacy session (lib/auth-server.ts staffCredentialHeaders).
//
// x-reviewer-id (AA-241 shim) is kept as-is for backward compatibility
// with the current FE (frontend/app/admin/review/page.tsx still sends it
// from localStorage) — NOT removed in this slice. Once the backend
// (admin_pipeline.py) is updated to read x-admin-user-id and write a real
// UUID into generated_content.reviewed_by, x-reviewer-id becomes fully
// redundant and can be retired in a follow-up.
//
// AA-253: this route is NOT covered by proxy.ts's matcher, so a
// request hitting /api/admin/* directly never passed through page-level
// gating. requireAdmin() below performs real, independent verification
// (POST /auth/verify-admin, signature-checked) before X-Admin-Secret is
// ever attached to the outbound request — no assumption about how the
// request got here.
//
// AA-577 follow-up: lib/rbac.ts API_READ_RULES paths (openapi.json) need the admin role for
// every method, reads included. The backend path is built by lib/bffPath.ts proxyPath(), which
// refuses segments that could climb out of /admin/ (e.g. an encoded "a%2F..%2F..%2Fopenapi.json")
// so the rbac rules below always see the path the backend will route.
//
// AA-769 (S234): non-GET requests on lib/rbac.ts API_WRITE_RULES paths (Content
// Quality sentence cut / rewrite) need the admin role, and the verified role
// is forwarded as x-admin-role for the backend's own check.
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import { API_RULES, canReadAdminPath, canWriteAdminPath } from "@/lib/rbac";
import { proxyPath } from "@/lib/bffPath";
import { streamUpstream } from "@/lib/proxyResponse";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  // AA-911 — refuse a cross-site write before any cookie or body is read (lib/sameOrigin.ts).
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const auth = await requireAdmin(req, API_RULES.admin);
  if (!auth.ok) return auth.response;

  // AA-816 (C9): a Cognito session sends its Bearer and no X-Admin-Secret; a legacy session the secret.
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, "admin");
  if (!credential) return secretNotConfigured();

  const { path } = await params;
  const pathStr = path.join("/");
  const upstreamPath = proxyPath(path);
  if (upstreamPath === null) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }
  // AA-577 follow-up — a few paths (the OpenAPI schema) are admin-only even for reads.
  if (!canReadAdminPath(auth.role, pathStr)) {
    return NextResponse.json({ detail: "Only an admin can do this" }, { status: 403 });
  }
  // AA-769 — every staff role may read /admin/*, but edits to tenant content are admin-only.
  if (!canWriteAdminPath(auth.role, req.method, pathStr)) {
    return NextResponse.json({ detail: "Only an admin can do this" }, { status: 403 });
  }
  const search = req.nextUrl.search;
  const url = `${API_URL}/admin/${upstreamPath}${search}`;

  const contentType = req.headers.get("content-type") ?? "";
  const isMultipart = contentType.includes("multipart/form-data");

  const outHeaders: Record<string, string> = {
    "x-admin-user-id": auth.adminId,
    // AA-769 — the verified JWT role, so the backend can re-check admin-only writes.
    "x-admin-role": auth.role,
    // AA-816 (C5/C9): Cognito session → its access token only (the API's staff_bearer middleware takes
    // role + actor from it, no secret needed); legacy session → X-Admin-Secret.
    ...credential,
  };

  // AA-241 shim — kept for backward compat until backend reads
  // x-admin-user-id instead. See file header.
  const reviewerId = req.headers.get("x-reviewer-id");
  if (reviewerId) {
    outHeaders["x-reviewer-id"] = reviewerId;
  }

  let body: ArrayBuffer | string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    if (isMultipart) {
      outHeaders["Content-Type"] = contentType;
      body = await req.arrayBuffer();
    } else {
      outHeaders["Content-Type"] = "application/json";
      try { body = await req.text(); } catch { /* empty body */ }
    }
  }

  try {
    const res = await fetch(url, { method: req.method, headers: outHeaders, body });
    // S241 — stream the body through (lib/proxyResponse.ts: same status, Content-Type only).
    return streamUpstream(res);
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
