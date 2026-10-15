// frontend/app/api/admin/[...path]/route.ts
// AA-232: forward verified admin identity (JWT sub claim) to the backend
// as x-admin-user-id, alongside the existing X-Admin-Secret (unchanged —
// still the service-to-service auth between Vercel and ECS).
//
// x-reviewer-id (AA-241 shim) is kept as-is for backward compatibility
// with the current FE (frontend/app/admin/review/page.tsx still sends it
// from localStorage) — NOT removed in this slice. Once the backend
// (admin_pipeline.py) is updated to read x-admin-user-id and write a real
// UUID into generated_content.reviewed_by, x-reviewer-id becomes fully
// redundant and can be retired in a follow-up.
//
// AA-253: this route is NOT covered by middleware.ts's matcher, so a
// request hitting /api/admin/* directly never passed through page-level
// gating. requireAdmin() below performs real, independent verification
// (POST /auth/verify-admin, signature-checked) before X-Admin-Secret is
// ever attached to the outbound request — no assumption about how the
// request got here.
//
// AA-577 follow-up: lib/rbac.ts API_READ_RULES paths (openapi.json) need the admin role for
// every method, reads included.
//
// AA-769 (S234): non-GET requests on lib/rbac.ts API_WRITE_RULES paths (Content
// Quality sentence cut / rewrite) need the admin role, and the verified role
// is forwarded as x-admin-role for the backend's own check.
import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/auth-server";
import { API_RULES, canReadAdminPath, canWriteAdminPath } from "@/lib/rbac";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";

async function handler(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const auth = await requireAdmin(req, API_RULES.admin);
  if (!auth.ok) return auth.response;

  if (!ADMIN_SECRET) {
    return NextResponse.json({ detail: "Admin secret not configured" }, { status: 503 });
  }

  const { path } = await params;
  const pathStr = path.join("/");
  // AA-577 follow-up — a few paths (the OpenAPI schema) are admin-only even for reads.
  if (!canReadAdminPath(auth.role, pathStr)) {
    return NextResponse.json({ detail: "Only an admin can do this" }, { status: 403 });
  }
  // AA-769 — every staff role may read /admin/*, but edits to tenant content are admin-only.
  if (!canWriteAdminPath(auth.role, req.method, pathStr)) {
    return NextResponse.json({ detail: "Only an admin can do this" }, { status: 403 });
  }
  const search = req.nextUrl.search;
  const url = `${API_URL}/admin/${pathStr}${search}`;

  const contentType = req.headers.get("content-type") ?? "";
  const isMultipart = contentType.includes("multipart/form-data");

  const outHeaders: Record<string, string> = {
    "X-Admin-Secret": ADMIN_SECRET,
    "x-admin-user-id": auth.adminId,
    // AA-769 — the verified JWT role, so the backend can re-check admin-only writes.
    "x-admin-role": auth.role,
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
    const resContentType = res.headers.get("content-type") ?? "application/json";
    const data = await res.arrayBuffer();
    return new NextResponse(data, {
      status: res.status,
      headers: { "Content-Type": resContentType },
    });
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export const GET    = handler;
export const POST   = handler;
export const PUT    = handler;
export const PATCH  = handler;
export const DELETE = handler;
